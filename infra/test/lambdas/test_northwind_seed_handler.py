"""Tests for the Northwind RDS Data API custom-resource handler."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

_HANDLER_PATH = Path(__file__).resolve().parents[2] / "lib" / "lambdas" / "northwind-seed" / "index.py"
_ASSETS_PATH = _HANDLER_PATH.parent / "assets"
_CREATE_TABLE_PATTERN = re.compile(r"^CREATE TABLE ([a-z_]+) ", re.MULTILINE)


def _event(request_type: str, seed_hash: str = "sha256-test") -> dict[str, object]:
    properties = {
        "ClusterArn": "arn:aws:rds:us-east-1:123456789012:cluster:northwind",
        "SecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:northwind",
        "DatabaseName": "northwind",
        "SeedHash": seed_hash,
    }
    return {
        "RequestType": request_type,
        "ResourceProperties": properties,
        "OldResourceProperties": {**properties, "SeedHash": "sha256-old"},
    }


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "ExecuteStatement")


@pytest.fixture
def handler(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load an isolated handler module with a mocked RDS client."""
    module_name = "northwind_seed_handler"
    sys.modules.pop(module_name, None)
    sys.path.insert(0, str(_HANDLER_PATH.parent))
    try:
        spec = importlib.util.spec_from_file_location(module_name, _HANDLER_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load handler from {_HANDLER_PATH}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)

    module._rds = MagicMock()
    module._rds.begin_transaction.return_value = {"transactionId": "transaction-1"}
    module._rds.execute_statement.return_value = {"records": []}
    module._sleep = MagicMock()
    return module


def _config(handler: ModuleType) -> object:
    return handler.SeedConfig(
        cluster_arn="cluster-arn",
        secret_arn="secret-arn",
        database_name="northwind",
        seed_hash="sha256-test",
    )


def test_create_runs_schema_base_and_generated_data(handler: ModuleType) -> None:
    result = handler.handler(_event("Create"))

    assert result["PhysicalResourceId"] == "northwind-seed-sha256-test"
    handler._rds.begin_transaction.assert_called_once()
    handler._rds.commit_transaction.assert_called_once()
    assert handler._rds.batch_execute_statement.call_count >= 4


def test_same_hash_update_skips_reseed(handler: ModuleType) -> None:
    handler._seed_is_current = MagicMock(return_value=True)

    result = handler.handler(_event("Update"))

    assert result["PhysicalResourceId"] == "northwind-seed-sha256-test"
    handler._rds.begin_transaction.assert_not_called()
    handler._rds.batch_execute_statement.assert_not_called()


def test_seed_metadata_lookup_casts_regclass_for_data_api(handler: ModuleType) -> None:
    result = handler._applied_seed_hash(_config(handler))

    assert result is None
    assert handler._rds.execute_statement.call_args.kwargs["sql"] == (
        "SELECT to_regclass('public.seed_metadata')::text"
    )


def test_changed_hash_reruns_schema_base_and_generated_phases(
    handler: ModuleType,
) -> None:
    config = _config(handler)
    handler._applied_seed_hash = MagicMock(return_value="sha256-old")
    handler._reset_seed_tables = MagicMock()
    handler._run_sql_script = MagicMock()
    handler._load_base_data = MagicMock()
    handler._load_generated_data = MagicMock()
    handler._record_seed_hash = MagicMock()

    handler._apply_seed(config)

    transaction_id = "transaction-1"
    handler._reset_seed_tables.assert_called_once_with(config, transaction_id)
    handler._run_sql_script.assert_called_once_with(config, "schema.sql", transaction_id)
    handler._load_base_data.assert_called_once_with(config, transaction_id)
    handler._load_generated_data.assert_called_once_with(config, transaction_id)
    handler._record_seed_hash.assert_called_once_with(config, transaction_id)
    handler._rds.commit_transaction.assert_called_once()


def test_changed_hash_reset_drops_all_tables_from_schema_asset(
    handler: ModuleType,
) -> None:
    expected_tables = _CREATE_TABLE_PATTERN.findall((_ASSETS_PATH / "schema.sql").read_text())

    handler._reset_seed_tables(_config(handler), "transaction-1")

    reset_sql = handler._rds.execute_statement.call_args.kwargs["sql"]
    dropped_tables = reset_sql.removeprefix("DROP TABLE IF EXISTS ")
    assert dropped_tables.split(", ") == [*expected_tables, "seed_metadata"]
    assert reset_sql == f"DROP TABLE IF EXISTS {', '.join([*expected_tables, 'seed_metadata'])}"
    assert "CASCADE" not in reset_sql


def test_changed_hash_reset_does_not_selectively_delete_standard_customers(
    handler: ModuleType,
) -> None:
    base_data = (_ASSETS_PATH / "base-data.sql").read_text()

    handler._reset_seed_tables(_config(handler), "transaction-1")

    reset_sql = handler._rds.execute_statement.call_args.kwargs["sql"]
    assert "INSERT INTO customers VALUES ('SANTG'" in base_data
    assert "DELETE FROM customers" not in reset_sql
    assert "LIKE 'S%'" not in reset_sql
    assert "customers" in reset_sql


def test_changed_hash_failure_after_reset_rolls_back_complete_replacement(
    handler: ModuleType,
) -> None:
    config = _config(handler)
    handler._applied_seed_hash = MagicMock(return_value="sha256-old")
    handler._reset_seed_tables = MagicMock()
    handler._create_schema = MagicMock()
    handler._load_base_data = MagicMock(side_effect=RuntimeError("base load failed"))

    with pytest.raises(RuntimeError, match="base load failed"):
        handler._apply_seed(config)

    handler._reset_seed_tables.assert_called_once_with(config, "transaction-1")
    handler._create_schema.assert_called_once_with(config, "transaction-1")
    handler._rds.rollback_transaction.assert_called_once()
    handler._rds.commit_transaction.assert_not_called()


def test_external_dependency_during_reset_rolls_back_without_cascade(
    handler: ModuleType,
) -> None:
    config = _config(handler)
    handler._applied_seed_hash = MagicMock(return_value="sha256-old")
    handler._rds.execute_statement.side_effect = RuntimeError("external dependency")
    handler._create_schema = MagicMock()

    with pytest.raises(RuntimeError, match="external dependency"):
        handler._apply_seed(config)

    reset_sql = handler._rds.execute_statement.call_args.kwargs["sql"]
    assert reset_sql.startswith("DROP TABLE IF EXISTS ")
    assert "CASCADE" not in reset_sql
    handler._create_schema.assert_not_called()
    handler._rds.rollback_transaction.assert_called_once()
    handler._rds.commit_transaction.assert_not_called()


def test_delete_is_no_op(handler: ModuleType) -> None:
    result = handler.handler(_event("Delete"))

    assert result["PhysicalResourceId"] == "northwind-seed-sha256-test"
    handler._rds.begin_transaction.assert_not_called()
    handler._rds.execute_statement.assert_not_called()


def test_failure_rolls_back_and_propagates(handler: ModuleType) -> None:
    handler._rds.batch_execute_statement.side_effect = RuntimeError("insert failed")

    with pytest.raises(RuntimeError, match="insert failed"):
        handler.handler(_event("Create"))

    handler._rds.rollback_transaction.assert_called_once()
    handler._rds.commit_transaction.assert_not_called()


def test_commit_failure_rolls_back_and_propagates(handler: ModuleType) -> None:
    handler._rds.commit_transaction.side_effect = RuntimeError("commit failed")

    with pytest.raises(RuntimeError, match="commit failed"):
        handler.handler(_event("Create"))

    handler._rds.rollback_transaction.assert_called_once()


def test_transient_resume_error_is_retried(handler: ModuleType) -> None:
    handler._rds.execute_statement.side_effect = [
        _client_error("DatabaseResumingException"),
        {"numberOfRecordsUpdated": 0},
    ]

    handler._execute(_config(handler), "SELECT 1")

    assert handler._rds.execute_statement.call_count == 2
    handler._sleep.assert_called_once_with(2)


@pytest.mark.parametrize(
    "code",
    [
        "DatabaseResumingException",
        "DatabaseUnavailableException",
        "ServiceUnavailableError",
    ],
)
def test_transient_error_stops_after_five_attempts(handler: ModuleType, code: str) -> None:
    handler._rds.execute_statement.side_effect = _client_error(code)

    with pytest.raises(ClientError):
        handler._execute(_config(handler), "SELECT 1")

    assert handler._rds.execute_statement.call_count == 5
    assert handler._sleep.call_args_list == [
        ((2,),),
        ((4,),),
        ((8,),),
        ((16,),),
    ]


def test_non_transient_error_is_not_retried(handler: ModuleType) -> None:
    handler._rds.execute_statement.side_effect = _client_error("BadRequestException")

    with pytest.raises(ClientError):
        handler._execute(_config(handler), "SELECT 1")

    handler._rds.execute_statement.assert_called_once()
    handler._sleep.assert_not_called()


def test_split_sql_keeps_semicolons_inside_literals_and_comments(
    handler: ModuleType,
) -> None:
    sql = """
        INSERT INTO notes VALUES ('quoted; value');
        -- comment; still a comment
        INSERT INTO notes VALUES ($tag$dollar; value$tag$);
        /* block; comment */ INSERT INTO notes VALUES ("double; quote");
    """

    statements = handler._split_sql(sql)

    assert len(statements) == 3
    assert "'quoted; value'" in statements[0]
    assert "$tag$dollar; value$tag$" in statements[1]
    assert '"double; quote"' in statements[2]


def test_batching_stays_within_count_and_request_size_limits(handler: ModuleType) -> None:
    parameter_sets = [
        [
            {
                "name": "value",
                "value": {"stringValue": "x" * 20_000},
            }
        ]
        for _ in range(500)
    ]

    handler._batch(
        _config(handler),
        "INSERT INTO test_table (value) VALUES (:value)",
        parameter_sets,
        "transaction-1",
    )

    calls = handler._rds.batch_execute_statement.call_args_list
    assert len(calls) > 1
    for call in calls:
        request = call.kwargs
        assert len(request["parameterSets"]) <= handler.MAX_BATCH_PARAMETER_SETS
        assert handler._request_size_bytes(request) < handler.MAX_REQUEST_BYTES


def test_request_size_uses_base64_wire_representation_for_blobs(handler: ModuleType) -> None:
    request = {
        "parameterSets": [
            [
                {
                    "name": "picture",
                    "value": {"blobValue": b"\x00\xff\x10"},
                }
            ]
        ]
    }
    wire_request = {
        "parameterSets": [
            [
                {
                    "name": "picture",
                    "value": {"blobValue": base64.b64encode(b"\x00\xff\x10").decode("ascii")},
                }
            ]
        ]
    }

    assert handler._request_size_bytes(request) == len(json.dumps(wire_request, separators=(",", ":")).encode("utf-8"))


def test_actual_base_asset_parses_all_rows_in_table_order(handler: ModuleType) -> None:
    statements = handler._split_sql((_ASSETS_PATH / "base-data.sql").read_text())
    parsed = [handler._parse_insert_statement(statement) for statement in statements]

    assert all(row is not None for row in parsed)
    groups: list[tuple[str, int]] = []
    for row in parsed:
        assert row is not None
        table_name, _ = row
        if groups and groups[-1][0] == table_name:
            groups[-1] = (table_name, groups[-1][1] + 1)
        else:
            groups.append((table_name, 1))

    assert sum(count for _, count in groups) == 3_362
    assert groups == [
        ("categories", 8),
        ("customers", 91),
        ("employees", 9),
        ("employee_territories", 49),
        ("order_details", 2_155),
        ("orders", 830),
        ("products", 77),
        ("region", 4),
        ("shippers", 6),
        ("suppliers", 29),
        ("territories", 53),
        ("us_states", 51),
    ]


def test_actual_base_asset_preserves_representative_parameter_types(
    handler: ModuleType,
) -> None:
    statements = handler._split_sql((_ASSETS_PATH / "base-data.sql").read_text())
    parsed = [handler._parse_insert_statement(statement) for statement in statements]
    rows = [row for row in parsed if row is not None]

    categories = next(values for table, values in rows if table == "categories")
    bon_app = next(values for table, values in rows if table == "customers" and values[0] == "BONAP")
    order_detail = next(values for table, values in rows if table == "order_details")
    employee = next(values for table, values in rows if table == "employees")

    category_values = [parameter["value"] for parameter in handler._base_parameter_set("categories", categories)]
    customer_values = [parameter["value"] for parameter in handler._base_parameter_set("customers", bon_app)]
    order_detail_values = [
        parameter["value"] for parameter in handler._base_parameter_set("order_details", order_detail)
    ]
    employee_values = [parameter["value"] for parameter in handler._base_parameter_set("employees", employee)]

    assert category_values == [
        {"longValue": 1},
        {"stringValue": "Beverages"},
        {"stringValue": "Soft drinks, coffees, teas, beers, and ales"},
        {"blobValue": b""},
    ]
    assert customer_values[1] == {"stringValue": "Bon app'"}
    assert customer_values[6] == {"isNull": True}
    assert order_detail_values == [
        {"longValue": 10248},
        {"longValue": 11},
        {"doubleValue": 14.0},
        {"longValue": 12},
        {"doubleValue": 0.0},
    ]
    assert employee_values[7] == {"stringValue": r"507 - 20th Ave. E.\nApt. 2A"}
    assert employee_values[14] == {"blobValue": b""}
    assert employee_values[5] == {"stringValue": "1948-12-08", "typeHint": "DATE"}
    assert employee_values[6] == {"stringValue": "1992-05-01", "typeHint": "DATE"}


def test_insert_parser_handles_commas_quotes_and_newlines(handler: ModuleType) -> None:
    parsed = handler._parse_insert_statement("INSERT INTO notes VALUES (7, 2.5, NULL, 'first line\nsecond, O''Brien')")

    assert parsed == (
        "notes",
        (7, 2.5, None, "first line\nsecond, O'Brien"),
    )


def test_non_insert_statements_flush_batches_and_execute_in_order(handler: ModuleType) -> None:
    operations: list[tuple[str, str, int | None]] = []
    handler._batch = MagicMock(
        side_effect=lambda _config, sql, parameter_sets, _transaction_id: operations.append(
            ("batch", sql, len(parameter_sets))
        )
    )
    handler._execute = MagicMock(
        side_effect=lambda _config, sql, *, transaction_id: operations.append(("execute", sql, None))
    )

    handler._run_base_data_statements(
        _config(handler),
        [
            "INSERT INTO demo VALUES (1, 'first')",
            "SELECT setval('demo_id_seq', 1)",
            "INSERT INTO demo VALUES (2, 'second')",
        ],
        "transaction-1",
    )

    assert operations == [
        ("batch", "INSERT INTO demo VALUES (:value_0, :value_1)", 1),
        ("execute", "SELECT setval('demo_id_seq', 1)", None),
        ("batch", "INSERT INTO demo VALUES (:value_0, :value_1)", 1),
    ]


def test_actual_base_asset_uses_fewer_than_thirty_data_api_calls(handler: ModuleType) -> None:
    handler._load_base_data(_config(handler), "transaction-1")

    calls = handler._rds.batch_execute_statement.call_args_list
    assert 0 < len(calls) < 30
    assert sum(len(call.kwargs["parameterSets"]) for call in calls) == 3_362
    assert handler._rds.execute_statement.call_count == 0
    for call in calls:
        request = call.kwargs
        assert len(request["sql"].encode("utf-8")) < handler.MAX_SQL_BYTES
        assert len(request["parameterSets"]) <= handler.MAX_BATCH_PARAMETER_SETS
        assert handler._request_size_bytes(request) < handler.MAX_REQUEST_BYTES


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, {"isNull": True}),
        (True, {"booleanValue": True}),
        (17, {"longValue": 17}),
        (2.5, {"doubleValue": 2.5}),
        ("2026-08-27", {"stringValue": "2026-08-27"}),
    ],
)
def test_generated_row_parameter_conversion_preserves_types(
    handler: ModuleType, value: object, expected: dict[str, object]
) -> None:
    parameter_set = handler._parameter_set({"sample": value})

    assert parameter_set == [{"name": "sample", "value": expected}]


def test_generated_rows_use_column_specific_typed_parameter_sets(handler: ModuleType) -> None:
    rows = handler._generator.generate_top_up(handler._generator.SEED, handler._generator.DEFAULT_TARGETS)

    product_parameters = handler._generated_parameter_sets("products", rows.products[:1])
    order_parameters = handler._generated_parameter_sets("orders", rows.orders[:1])

    assert {parameter["name"] for parameter in product_parameters[0]} == {
        "category_id",
        "discontinued",
        "product_id",
        "unit_price",
    } | {
        "product_name",
        "quantity_per_unit",
        "reorder_level",
        "supplier_id",
        "units_in_stock",
        "units_on_order",
    }
    order_values = {parameter["name"]: parameter["value"] for parameter in order_parameters[0]}
    assert order_values["order_id"].get("longValue")
    assert order_values["freight"].get("doubleValue")
    assert order_values["order_date"]["typeHint"] == "DATE"
    assert order_values["required_date"]["typeHint"] == "DATE"
    assert order_values["shipped_date"] == {"isNull": True} or "stringValue" in order_values["shipped_date"]
    if "stringValue" in order_values["shipped_date"]:
        assert order_values["shipped_date"]["typeHint"] == "DATE"
