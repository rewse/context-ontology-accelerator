"""Tests for deterministic Northwind synthetic data generation."""

from datetime import date
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest


_GENERATOR_PATH = (
    Path(__file__).resolve().parents[2] / "lib" / "lambdas" / "northwind-seed" / "generator.py"
)


@pytest.fixture
def generator() -> ModuleType:
    """Load the generator under a unique name to isolate Lambda modules."""
    spec = importlib.util.spec_from_file_location("northwind_seed_generator", _GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load generator from {_GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_targets_include_base_and_generated_rows(generator: ModuleType) -> None:
    rows = generator.generate_top_up(20260827, generator.DEFAULT_TARGETS)

    assert rows.final_counts() == {
        "customers": 500,
        "order_details": 15000,
        "orders": 5000,
        "products": 100,
    }


def test_same_seed_is_reproducible(generator: ModuleType) -> None:
    first = generator.generate_top_up(20260827, generator.DEFAULT_TARGETS)
    second = generator.generate_top_up(20260827, generator.DEFAULT_TARGETS)

    assert first == second


def test_every_order_detail_references_generated_or_base_keys(generator: ModuleType) -> None:
    rows = generator.generate_top_up(20260827, generator.DEFAULT_TARGETS)
    order_ids = generator.BASE_ORDER_IDS | {row["order_id"] for row in rows.orders}
    product_ids = generator.BASE_PRODUCT_IDS | {row["product_id"] for row in rows.products}

    assert all(row["order_id"] in order_ids for row in rows.order_details)
    assert all(row["product_id"] in product_ids for row in rows.order_details)


def test_generated_orders_cover_the_three_year_period_and_keep_dates_valid(
    generator: ModuleType,
) -> None:
    rows = generator.generate_top_up(20260827, generator.DEFAULT_TARGETS)
    order_dates = [date.fromisoformat(row["order_date"]) for row in rows.orders]

    assert min(order_dates) >= date(2023, 8, 28)
    assert max(order_dates) <= date(2026, 8, 27)
    assert all(row["required_date"] > row["order_date"] for row in rows.orders)
    assert all(
        row["shipped_date"] is None or row["shipped_date"] >= row["order_date"]
        for row in rows.orders
    )


def test_generated_rows_match_northwind_foreign_keys(generator: ModuleType) -> None:
    rows = generator.generate_top_up(20260827, generator.DEFAULT_TARGETS)
    customer_ids = generator.BASE_CUSTOMER_IDS | {row["customer_id"] for row in rows.customers}
    order_ids = {row["order_id"] for row in rows.orders}
    detail_order_ids = {row["order_id"] for row in rows.order_details}

    assert all(row["customer_id"] in customer_ids for row in rows.orders)
    assert all(row["employee_id"] in generator.BASE_EMPLOYEE_IDS for row in rows.orders)
    assert all(row["ship_via"] in generator.BASE_SHIPPER_IDS for row in rows.orders)
    assert all(row["category_id"] in generator.BASE_CATEGORY_IDS for row in rows.products)
    assert all(row["supplier_id"] in generator.BASE_SUPPLIER_IDS for row in rows.products)
    assert detail_order_ids == order_ids
    assert len({(row["order_id"], row["product_id"]) for row in rows.order_details}) == len(
        rows.order_details
    )
