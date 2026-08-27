"""Tests for deterministic Northwind synthetic data generation."""

import importlib.util
import re
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

_GENERATOR_PATH = Path(__file__).resolve().parents[2] / "lib" / "lambdas" / "northwind-seed" / "generator.py"
_ASSETS_PATH = _GENERATOR_PATH.parent / "assets"


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
    assert all(row["shipped_date"] is None or row["shipped_date"] >= row["order_date"] for row in rows.orders)


def test_generated_order_dates_are_spread_across_each_year_of_the_period(
    generator: ModuleType,
) -> None:
    rows = generator.generate_top_up(20260827, generator.DEFAULT_TARGETS)
    order_dates = [date.fromisoformat(row["order_date"]) for row in rows.orders]
    periods = (
        (date(2023, 8, 28), date(2024, 8, 28)),
        (date(2024, 8, 28), date(2025, 8, 28)),
        (date(2025, 8, 28), date(2026, 8, 28)),
    )
    period_counts = [sum(start <= order_date < end for order_date in order_dates) for start, end in periods]

    assert sum(period_counts) == len(order_dates)
    assert all(0.30 <= count / len(order_dates) <= 0.37 for count in period_counts)


def test_generated_shipping_states_follow_the_intended_proportions(
    generator: ModuleType,
) -> None:
    rows = generator.generate_top_up(20260827, generator.DEFAULT_TARGETS)
    total = len(rows.orders)
    unshipped = sum(row["shipped_date"] is None for row in rows.orders)
    on_time = sum(
        row["shipped_date"] is not None and row["shipped_date"] <= row["required_date"] for row in rows.orders
    )
    late = total - on_time - unshipped

    assert on_time / total == pytest.approx(0.80, abs=0.01)
    assert late / total == pytest.approx(0.15, abs=0.01)
    assert unshipped / total == pytest.approx(0.05, abs=0.01)


@pytest.mark.parametrize(
    ("targets", "message"),
    (
        ("customers", "customers target cannot be lower"),
        ("order_details", "order_details target cannot be lower"),
        ("orders", "orders target cannot be lower"),
        ("products", "products target cannot be lower"),
    ),
)
def test_rejects_targets_below_standard_data(generator: ModuleType, targets: str, message: str) -> None:
    counts = {name: generator.BASE_COUNTS[name] for name in generator.BASE_COUNTS}
    counts[targets] -= 1

    with pytest.raises(ValueError, match=message):
        generator.generate_top_up(20260827, generator.TargetCounts(**counts))


def test_rejects_order_details_without_generated_orders(generator: ModuleType) -> None:
    targets = generator.TargetCounts(
        customers=generator.BASE_COUNTS["customers"],
        order_details=generator.BASE_COUNTS["order_details"] + 1,
        orders=generator.BASE_COUNTS["orders"],
        products=generator.BASE_COUNTS["products"],
    )

    with pytest.raises(ValueError, match="order details require generated orders"):
        generator.generate_top_up(20260827, targets)


def test_schema_defers_foreign_keys_needed_by_base_data_order() -> None:
    schema = (_ASSETS_PATH / "schema.sql").read_text()
    base_data = (_ASSETS_PATH / "base-data.sql").read_text()

    assert "INSERT INTO employees VALUES (1," in base_data
    assert ", 2," in base_data.split("INSERT INTO employees VALUES (1,", 1)[1].splitlines()[0]
    assert base_data.index("INSERT INTO order_details VALUES") < base_data.index("INSERT INTO orders VALUES")
    assert base_data.index("INSERT INTO order_details VALUES") < base_data.index("INSERT INTO products VALUES")

    foreign_keys = re.findall(r"ADD CONSTRAINT (fk_[a-z_]+) FOREIGN KEY .*?;", schema, flags=re.DOTALL)
    assert len(foreign_keys) == 13
    for constraint in foreign_keys:
        pattern = (
            rf"ADD CONSTRAINT {constraint} FOREIGN KEY .*? "
            r"DEFERRABLE INITIALLY DEFERRED;"
        )
        assert re.search(pattern, schema, flags=re.DOTALL), constraint


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
    assert len({(row["order_id"], row["product_id"]) for row in rows.order_details}) == len(rows.order_details)
