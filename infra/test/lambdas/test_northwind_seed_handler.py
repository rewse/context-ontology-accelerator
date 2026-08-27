"""Tests for the Northwind RDS Data API custom-resource handler."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import MagicMock

from botocore.exceptions import ClientError
import pytest


_HANDLER_PATH = (
    Path(__file__).resolve().parents[2]
    / "lib"
    / "lambdas"
    / "northwind-seed"
    / "index.py"
)


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


def test_changed_hash_update_clears_seed_owned_rows_before_reloading(
    handler: ModuleType,
) -> None:
    handler._applied_seed_hash = MagicMock(return_value="sha256-old")

    handler.handler(_event("Update"))

    executed_sql = [call.kwargs["sql"] for call in handler._rds.execute_statement.call_args_list]
    clear_order = [
        sql
        for sql in executed_sql
        if sql.startswith("DELETE FROM")
    ]
    assert clear_order == [
        "DELETE FROM order_details WHERE order_id >= 11078",
        "DELETE FROM orders WHERE order_id >= 11078",
        "DELETE FROM products WHERE product_id >= 78",
        "DELETE FROM customers WHERE customer_id LIKE 'S%'",
    ]
    handler._rds.commit_transaction.assert_called_once()


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
    handler._sleep.assert_called_once_with(1)


@pytest.mark.parametrize(
    "code",
    [
        "DatabaseResumingException",
        "DatabaseUnavailableException",
        "ServiceUnavailableError",
    ],
)
def test_transient_error_stops_after_five_attempts(
    handler: ModuleType, code: str
) -> None:
    handler._rds.execute_statement.side_effect = _client_error(code)

    with pytest.raises(ClientError):
        handler._execute(_config(handler), "SELECT 1")

    assert handler._rds.execute_statement.call_count == 5
    assert handler._sleep.call_args_list == [
        ((1,),),
        ((2,),),
        ((4,),),
        ((8,),),
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
    rows = handler._generator.generate_top_up(
        handler._generator.SEED, handler._generator.DEFAULT_TARGETS
    )

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
    assert order_values["shipped_date"] == {"isNull": True} or "stringValue" in order_values[
        "shipped_date"
    ]
