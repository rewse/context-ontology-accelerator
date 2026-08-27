"""Seed Northwind data through the Amazon RDS Data API."""

from __future__ import annotations

import base64
import contextlib
import json
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import generator as _generator
from botocore.exceptions import ClientError

ASSETS_PATH = Path(__file__).parent / "assets"
MAX_BATCH_PARAMETER_SETS = 500
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_SQL_BYTES = 64 * 1024
TRANSIENT_CODES = frozenset(
    {
        "DatabaseResumingException",
        "DatabaseUnavailableException",
        "ServiceUnavailableError",
    }
)

_rds = boto3.client("rds-data")
_sleep = time.sleep
_CREATE_TABLE_PATTERN = re.compile(r"^CREATE TABLE ([a-z_]+) ", re.MULTILINE)
_FLOAT_LITERAL_PATTERN = re.compile(r"[+-]?(?:(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)")
_INSERT_VALUES_PATTERN = re.compile(
    r"^INSERT\s+INTO\s+([a-z_][a-z0-9_]*)\s+VALUES\s*\((.*)\)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_INTEGER_LITERAL_PATTERN = re.compile(r"[+-]?\d+")

_BASE_BLOB_COLUMN_INDEXES = {
    "categories": frozenset({3}),
    "employees": frozenset({14}),
}
_BASE_FLOAT_COLUMN_INDEXES = {
    "order_details": frozenset({2, 4}),
    "orders": frozenset({7}),
    "products": frozenset({5}),
}
_BASE_DATE_COLUMN_INDEXES = {
    "employees": frozenset({5, 6}),
    "orders": frozenset({3, 4, 5}),
}
_TABLE_DATE_COLUMNS = {
    "orders": frozenset({"order_date", "required_date", "shipped_date"}),
}

_TABLE_COLUMNS = {
    "customers": (
        "customer_id",
        "company_name",
        "contact_name",
        "contact_title",
        "address",
        "city",
        "region",
        "postal_code",
        "country",
        "phone",
        "fax",
    ),
    "order_details": (
        "order_id",
        "product_id",
        "unit_price",
        "quantity",
        "discount",
    ),
    "orders": (
        "order_id",
        "customer_id",
        "employee_id",
        "order_date",
        "required_date",
        "shipped_date",
        "ship_via",
        "freight",
        "ship_name",
        "ship_address",
        "ship_city",
        "ship_region",
        "ship_postal_code",
        "ship_country",
    ),
    "products": (
        "product_id",
        "product_name",
        "supplier_id",
        "category_id",
        "quantity_per_unit",
        "unit_price",
        "units_in_stock",
        "units_on_order",
        "reorder_level",
        "discontinued",
    ),
}


@dataclass(frozen=True)
class SeedConfig:
    """Connection and version inputs passed from the custom resource."""

    cluster_arn: str
    secret_arn: str
    database_name: str
    seed_hash: str


def handler(event: dict[str, Any], context: object = None) -> dict[str, str]:
    """Handle CloudFormation lifecycle events for the Northwind seed."""
    del context
    config = _seed_config(event)
    physical_resource_id = f"northwind-seed-{config.seed_hash}"
    if event.get("RequestType") == "Delete":
        return {"PhysicalResourceId": physical_resource_id}
    if _seed_is_current(config):
        return {"PhysicalResourceId": physical_resource_id}
    _apply_seed(config)
    return {"PhysicalResourceId": physical_resource_id}


def _seed_config(event: dict[str, Any]) -> SeedConfig:
    properties = event.get("ResourceProperties", {})
    required = {
        "ClusterArn": "cluster_arn",
        "SecretArn": "secret_arn",
        "DatabaseName": "database_name",
        "SeedHash": "seed_hash",
    }
    values: dict[str, str] = {}
    for property_name, field_name in required.items():
        value = properties.get(property_name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"ResourceProperties.{property_name} must be a non-empty string")
        values[field_name] = value
    return SeedConfig(**values)


def _apply_seed(config: SeedConfig) -> None:
    current_hash = _applied_seed_hash(config)
    transaction_id = _begin_transaction(config)
    try:
        if current_hash is not None:
            _reset_seed_tables(config, transaction_id)
        _create_schema(config, transaction_id)
        _load_base_data(config, transaction_id)
        _load_generated_data(config, transaction_id)
        _record_seed_hash(config, transaction_id)
        _commit_transaction(config, transaction_id)
    except Exception:
        _rollback_transaction(config, transaction_id)
        raise


def _seed_is_current(config: SeedConfig) -> bool:
    return _applied_seed_hash(config) == config.seed_hash


def _applied_seed_hash(config: SeedConfig) -> str | None:
    table_result = _execute(
        config,
        "SELECT to_regclass('public.seed_metadata')::text",
    )
    if _first_field(table_result) is None:
        return None
    result = _execute(
        config,
        "SELECT seed_hash FROM seed_metadata ORDER BY applied_at DESC LIMIT 1",
    )
    value = _first_field(result)
    return value if isinstance(value, str) else None


def _first_field(response: dict[str, Any]) -> object | None:
    records = response.get("records", [])
    if not records or not records[0]:
        return None
    field = records[0][0]
    if field.get("isNull"):
        return None
    for name in ("stringValue", "longValue", "doubleValue", "booleanValue"):
        if name in field:
            return field[name]
    return None


def _begin_transaction(config: SeedConfig) -> str:
    response = _call_with_retry(
        lambda: _rds.begin_transaction(
            resourceArn=config.cluster_arn,
            secretArn=config.secret_arn,
            database=config.database_name,
        )
    )
    transaction_id = response.get("transactionId")
    if not isinstance(transaction_id, str) or not transaction_id:
        raise RuntimeError("RDS Data API did not return a transaction ID")
    return transaction_id


def _commit_transaction(config: SeedConfig, transaction_id: str) -> None:
    _call_with_retry(
        lambda: _rds.commit_transaction(
            resourceArn=config.cluster_arn,
            secretArn=config.secret_arn,
            transactionId=transaction_id,
        )
    )


def _rollback_transaction(config: SeedConfig, transaction_id: str) -> None:
    # The original seed failure remains the useful CloudFormation error.
    with contextlib.suppress(Exception):
        _call_with_retry(
            lambda: _rds.rollback_transaction(
                resourceArn=config.cluster_arn,
                secretArn=config.secret_arn,
                transactionId=transaction_id,
            )
        )


def _execute(config: SeedConfig, sql: str, *, transaction_id: str | None = None) -> dict[str, Any]:
    if len(sql.encode("utf-8")) >= MAX_SQL_BYTES:
        raise ValueError("SQL statement exceeds the RDS Data API SQL size limit")
    request: dict[str, Any] = {
        "resourceArn": config.cluster_arn,
        "secretArn": config.secret_arn,
        "database": config.database_name,
        "sql": sql,
    }
    if transaction_id:
        request["transactionId"] = transaction_id
    if _request_size_bytes(request) >= MAX_REQUEST_BYTES:
        raise ValueError("RDS Data API request exceeds the 4 MiB limit")
    return _call_with_retry(lambda: _rds.execute_statement(**request))


def _batch(
    config: SeedConfig,
    sql: str,
    parameter_sets: Sequence[list[dict[str, Any]]],
    transaction_id: str,
) -> None:
    if len(sql.encode("utf-8")) >= MAX_SQL_BYTES:
        raise ValueError("SQL statement exceeds the RDS Data API SQL size limit")
    for parameter_chunk in _bounded_batches(config, sql, parameter_sets, transaction_id):
        request = {
            "resourceArn": config.cluster_arn,
            "secretArn": config.secret_arn,
            "database": config.database_name,
            "sql": sql,
            "parameterSets": parameter_chunk,
            "transactionId": transaction_id,
        }
        _call_with_retry(lambda request=request: _rds.batch_execute_statement(**request))


def _bounded_batches(
    config: SeedConfig,
    sql: str,
    parameter_sets: Sequence[list[dict[str, Any]]],
    transaction_id: str,
) -> Iterator[list[list[dict[str, Any]]]]:
    chunk: list[list[dict[str, Any]]] = []
    for parameter_set in parameter_sets:
        candidate = [*chunk, parameter_set]
        request = {
            "resourceArn": config.cluster_arn,
            "secretArn": config.secret_arn,
            "database": config.database_name,
            "sql": sql,
            "parameterSets": candidate,
            "transactionId": transaction_id,
        }
        if len(candidate) > MAX_BATCH_PARAMETER_SETS or _request_size_bytes(request) >= MAX_REQUEST_BYTES:
            if not chunk:
                raise ValueError("A single parameter set exceeds the RDS Data API request limit")
            yield chunk
            chunk = [parameter_set]
            request["parameterSets"] = chunk
            if _request_size_bytes(request) >= MAX_REQUEST_BYTES:
                raise ValueError("A single parameter set exceeds the RDS Data API request limit")
        else:
            chunk = candidate
    if chunk:
        yield chunk


def _request_size_bytes(request: dict[str, Any]) -> int:
    return len(
        json.dumps(
            request,
            default=_json_wire_value,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _json_wire_value(value: object) -> str:
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(value).decode("ascii")
    raise TypeError(f"Unsupported JSON request value: {type(value).__name__}")


def _call_with_retry(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    for attempt in range(5):
        try:
            return operation()
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code not in TRANSIENT_CODES or attempt == 4:
                raise
            _sleep(2 ** (attempt + 1))
    raise AssertionError("retry loop must return or raise")


def _create_schema(config: SeedConfig, transaction_id: str) -> None:
    _run_sql_script(config, "schema.sql", transaction_id)


def _load_base_data(config: SeedConfig, transaction_id: str) -> None:
    statements = _split_sql((ASSETS_PATH / "base-data.sql").read_text())
    _run_base_data_statements(config, statements, transaction_id)


def _run_base_data_statements(
    config: SeedConfig,
    statements: Iterable[str],
    transaction_id: str,
) -> None:
    table_name: str | None = None
    value_count = 0
    parameter_sets: list[list[dict[str, Any]]] = []

    for statement in statements:
        parsed = _parse_insert_statement(statement)
        if parsed is None:
            _flush_base_insert_batch(
                config,
                table_name,
                value_count,
                parameter_sets,
                transaction_id,
            )
            table_name = None
            value_count = 0
            parameter_sets = []
            _execute(config, statement, transaction_id=transaction_id)
            continue

        next_table_name, values = parsed
        if table_name is not None and next_table_name == table_name and len(values) != value_count:
            raise ValueError(f"Inconsistent value count for base-data table {table_name}")
        if table_name is not None and next_table_name != table_name:
            _flush_base_insert_batch(
                config,
                table_name,
                value_count,
                parameter_sets,
                transaction_id,
            )
            parameter_sets = []

        table_name = next_table_name
        value_count = len(values)
        parameter_sets.append(_base_parameter_set(table_name, values))

    _flush_base_insert_batch(
        config,
        table_name,
        value_count,
        parameter_sets,
        transaction_id,
    )


def _flush_base_insert_batch(
    config: SeedConfig,
    table_name: str | None,
    value_count: int,
    parameter_sets: Sequence[list[dict[str, Any]]],
    transaction_id: str,
) -> None:
    if table_name is None:
        return
    placeholders = ", ".join(f":value_{index}" for index in range(value_count))
    _batch(
        config,
        f"INSERT INTO {table_name} VALUES ({placeholders})",
        parameter_sets,
        transaction_id,
    )


def _parse_insert_statement(statement: str) -> tuple[str, tuple[object, ...]] | None:
    match = _INSERT_VALUES_PATTERN.fullmatch(statement.strip())
    if match is None:
        return None
    table_name = match.group(1).lower()
    values = tuple(_parse_sql_literal(literal) for literal in _split_insert_values(match.group(2)))
    return table_name, values


def _split_insert_values(values_sql: str) -> list[str]:
    values: list[str] = []
    current: list[str] = []
    index = 0
    in_quote = False
    while index < len(values_sql):
        char = values_sql[index]
        if in_quote:
            current.append(char)
            if char == "'":
                if values_sql[index + 1 : index + 2] == "'":
                    current.append("'")
                    index += 2
                    continue
                in_quote = False
        elif char == "'":
            current.append(char)
            in_quote = True
        elif char == ",":
            values.append(_required_sql_literal(current))
            current = []
        else:
            current.append(char)
        index += 1
    if in_quote:
        raise ValueError("Unterminated string literal in base-data INSERT")
    values.append(_required_sql_literal(current))
    return values


def _required_sql_literal(characters: Sequence[str]) -> str:
    literal = "".join(characters).strip()
    if not literal:
        raise ValueError("Empty value in base-data INSERT")
    return literal


def _parse_sql_literal(literal: str) -> object:
    if literal.upper() == "NULL":
        return None
    if literal.startswith("'") and literal.endswith("'"):
        return literal[1:-1].replace("''", "'")
    if _INTEGER_LITERAL_PATTERN.fullmatch(literal):
        return int(literal)
    if _FLOAT_LITERAL_PATTERN.fullmatch(literal):
        return float(literal)
    raise ValueError("Unsupported value in base-data INSERT")


def _base_parameter_set(
    table_name: str,
    values: Sequence[object],
) -> list[dict[str, Any]]:
    return [
        {
            "name": f"value_{index}",
            "value": _parameter_value(
                _base_parameter_value(table_name, index, value),
                type_hint=(
                    "DATE" if value is not None and index in _BASE_DATE_COLUMN_INDEXES.get(table_name, ()) else None
                ),
            ),
        }
        for index, value in enumerate(values)
    ]


def _base_parameter_value(table_name: str, index: int, value: object) -> object:
    if index in _BASE_BLOB_COLUMN_INDEXES.get(table_name, ()):
        if value is None:
            return None
        if value != r"\x":
            raise ValueError(f"Unsupported bytea literal for base-data table {table_name}")
        return b""
    if index in _BASE_FLOAT_COLUMN_INDEXES.get(table_name, ()) and value is not None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Non-numeric value for base-data table {table_name}")
        return float(value)
    return value


def _run_sql_script(config: SeedConfig, asset_name: str, transaction_id: str) -> None:
    for statement in _split_sql((ASSETS_PATH / asset_name).read_text()):
        _execute(config, statement, transaction_id=transaction_id)


def _reset_seed_tables(config: SeedConfig, transaction_id: str) -> None:
    table_names = ", ".join(_seed_owned_table_names())
    _execute(
        config,
        f"DROP TABLE IF EXISTS {table_names}",
        transaction_id=transaction_id,
    )


def _seed_owned_table_names() -> tuple[str, ...]:
    schema = (ASSETS_PATH / "schema.sql").read_text()
    table_names = _CREATE_TABLE_PATTERN.findall(schema)
    if not table_names:
        raise RuntimeError("Northwind schema does not define any tables")
    return (*table_names, "seed_metadata")


def _load_generated_data(config: SeedConfig, transaction_id: str) -> None:
    rows = _generator.generate_top_up(_generator.SEED, _generator.DEFAULT_TARGETS)
    for table_name, table_rows in (
        ("customers", rows.customers),
        ("products", rows.products),
        ("orders", rows.orders),
        ("order_details", rows.order_details),
    ):
        columns = _TABLE_COLUMNS[table_name]
        sql = (
            f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({', '.join(f':{column}' for column in columns)})"
        )
        _batch(
            config,
            sql,
            _generated_parameter_sets(table_name, table_rows),
            transaction_id,
        )


def _generated_parameter_sets(table_name: str, rows: Iterable[dict[str, object]]) -> list[list[dict[str, Any]]]:
    columns = _TABLE_COLUMNS[table_name]
    date_columns = _TABLE_DATE_COLUMNS.get(table_name, ())
    return [
        _parameter_set(
            {column: row[column] for column in columns},
            type_hints={column: "DATE" for column in date_columns},
        )
        for row in rows
    ]


def _parameter_set(
    row: dict[str, object],
    *,
    type_hints: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "value": _parameter_value(
                value,
                type_hint=(type_hints or {}).get(name) if value is not None else None,
            ),
        }
        for name, value in row.items()
    ]


def _parameter_value(value: object, *, type_hint: str | None = None) -> dict[str, object]:
    if value is None:
        return {"isNull": True}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"longValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, Decimal):
        return {"stringValue": str(value), "typeHint": "DECIMAL"}
    if isinstance(value, str):
        result: dict[str, object] = {"stringValue": value}
        if type_hint:
            result["typeHint"] = type_hint
        return result
    if isinstance(value, (bytes, bytearray)):
        return {"blobValue": bytes(value)}
    raise TypeError(f"Unsupported RDS Data API parameter type: {type(value).__name__}")


def _record_seed_hash(config: SeedConfig, transaction_id: str) -> None:
    _execute(
        config,
        "CREATE TABLE IF NOT EXISTS seed_metadata ("
        "seed_hash character varying(128) PRIMARY KEY, "
        "applied_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP)",
        transaction_id=transaction_id,
    )
    _execute(config, "TRUNCATE TABLE seed_metadata", transaction_id=transaction_id)
    _batch(
        config,
        "INSERT INTO seed_metadata (seed_hash) VALUES (:seed_hash)",
        [_parameter_set({"seed_hash": config.seed_hash})],
        transaction_id,
    )


def _split_sql(sql: str) -> list[str]:
    """Split SQL on statement terminators outside literals and comments."""
    statements: list[str] = []
    current: list[str] = []
    index = 0
    state = "normal"
    block_depth = 0
    dollar_delimiter = ""
    while index < len(sql):
        char = sql[index]
        next_two = sql[index : index + 2]
        if state == "normal":
            if next_two == "--":
                current.append(next_two)
                index += 2
                state = "line_comment"
                continue
            if next_two == "/*":
                current.append(next_two)
                index += 2
                state = "block_comment"
                block_depth = 1
                continue
            if char == "'":
                current.append(char)
                index += 1
                state = "single_quote"
                continue
            if char == '"':
                current.append(char)
                index += 1
                state = "double_quote"
                continue
            if char == "$":
                match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[index:])
                if match:
                    dollar_delimiter = match.group(0)
                    current.append(dollar_delimiter)
                    index += len(dollar_delimiter)
                    state = "dollar_quote"
                    continue
            if char == ";":
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
                index += 1
                continue
        elif state == "line_comment":
            if char == "\n":
                state = "normal"
        elif state == "block_comment":
            if next_two == "/*":
                current.append(next_two)
                index += 2
                block_depth += 1
                continue
            if next_two == "*/":
                current.append(next_two)
                index += 2
                block_depth -= 1
                if block_depth == 0:
                    state = "normal"
                continue
        elif state == "single_quote":
            if char == "'" and sql[index + 1 : index + 2] == "'":
                current.append("''")
                index += 2
                continue
            if char == "'":
                state = "normal"
        elif state == "double_quote":
            if char == '"' and sql[index + 1 : index + 2] == '"':
                current.append('""')
                index += 2
                continue
            if char == '"':
                state = "normal"
        elif state == "dollar_quote" and sql.startswith(dollar_delimiter, index):
            current.append(dollar_delimiter)
            index += len(dollar_delimiter)
            state = "normal"
            continue
        current.append(char)
        index += 1
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements
