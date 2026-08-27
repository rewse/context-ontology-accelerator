"""Deterministically generate Northwind rows that top up the standard dataset."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

SEED = 20260827
BASE_COUNTS = {
    "customers": 91,
    "order_details": 2_155,
    "orders": 830,
    "products": 77,
}
BASE_ORDER_IDS = frozenset(range(10_248, 11_078))
BASE_PRODUCT_IDS = frozenset(range(1, 78))
BASE_CUSTOMER_IDS = frozenset(
    {
        "ALFKI",
        "ANATR",
        "ANTON",
        "AROUT",
        "BERGS",
        "BLAUS",
        "BLONP",
        "BOLID",
        "BONAP",
        "BOTTM",
        "BSBEV",
        "CACTU",
        "CENTC",
        "CHOPS",
        "COMMI",
        "CONSH",
        "DRACD",
        "DUMON",
        "EASTC",
        "ERNSH",
        "FAMIA",
        "FISSA",
        "FOLIG",
        "FOLKO",
        "FRANK",
        "FRANR",
        "FRANS",
        "FURIB",
        "GALED",
        "GODOS",
        "GOURL",
        "GREAL",
        "GROSR",
        "HANAR",
        "HILAA",
        "HUNGC",
        "HUNGO",
        "ISLAT",
        "KOENE",
        "LACOR",
        "LAMAI",
        "LAUGB",
        "LAZYK",
        "LEHMS",
        "LETSS",
        "LILAS",
        "LINOD",
        "LONEP",
        "MAGAA",
        "MAISD",
        "MEREP",
        "MORGK",
        "NORTS",
        "OCEAN",
        "OLDWO",
        "OTTIK",
        "PARIS",
        "PERIC",
        "PICCO",
        "PRINI",
        "QUEDE",
        "QUEEN",
        "QUICK",
        "RANCH",
        "RATTC",
        "REGGC",
        "RICAR",
        "RICSU",
        "ROMEY",
        "SANTG",
        "SAVEA",
        "SEVES",
        "SIMOB",
        "SPECD",
        "SPLIR",
        "SUPRD",
        "THEBI",
        "THECR",
        "TOMSP",
        "TORTU",
        "TRADH",
        "TRAIH",
        "VAFFE",
        "VICTE",
        "VINET",
        "WANDK",
        "WARTH",
        "WELLI",
        "WHITC",
        "WILMK",
        "WOLZA",
    }
)
BASE_EMPLOYEE_IDS = frozenset(range(1, 10))
BASE_SHIPPER_IDS = frozenset(range(1, 7))
BASE_CATEGORY_IDS = frozenset(range(1, 9))
BASE_SUPPLIER_IDS = frozenset(range(1, 30))
_END_DATE = date(2026, 8, 27)
_START_DATE = date(2023, 8, 28)
_COUNTRY_CITIES = (
    ("Australia", "Sydney"),
    ("Brazil", "Rio de Janeiro"),
    ("Canada", "Vancouver"),
    ("Germany", "Berlin"),
    ("Japan", "Tokyo"),
    ("UK", "London"),
    ("USA", "Seattle"),
)


@dataclass(frozen=True)
class TargetCounts:
    """Final row counts, including the upstream Northwind standard data."""

    customers: int = 500
    order_details: int = 15_000
    orders: int = 5_000
    products: int = 100


DEFAULT_TARGETS = TargetCounts()


@dataclass(frozen=True)
class GeneratedRows:
    """Synthetic rows in foreign-key insertion order."""

    customers: tuple[dict[str, object], ...]
    order_details: tuple[dict[str, object], ...]
    orders: tuple[dict[str, object], ...]
    products: tuple[dict[str, object], ...]

    def final_counts(self) -> dict[str, int]:
        """Return standard-data and generated-data counts together."""
        return {
            "customers": BASE_COUNTS["customers"] + len(self.customers),
            "order_details": BASE_COUNTS["order_details"] + len(self.order_details),
            "orders": BASE_COUNTS["orders"] + len(self.orders),
            "products": BASE_COUNTS["products"] + len(self.products),
        }


def generate_top_up(seed: int, targets: TargetCounts) -> GeneratedRows:
    """Return reproducible rows that top up Northwind to ``targets``."""
    _validate_targets(targets)
    rng = random.Random(seed)
    products = tuple(_product(product_id, rng) for product_id in range(78, targets.products + 1))
    customers = tuple(_customer(index, rng) for index in range(1, targets.customers - BASE_COUNTS["customers"] + 1))
    order_count = targets.orders - BASE_COUNTS["orders"]
    delivery_statuses = _delivery_statuses(order_count, rng)
    orders = tuple(
        _order(order_id, rng, customers, delivery_status)
        for order_id, delivery_status in zip(range(11_078, 11_078 + order_count), delivery_statuses, strict=True)
    )
    order_details = _order_details(
        rng,
        orders,
        products,
        targets.order_details - BASE_COUNTS["order_details"],
    )
    return GeneratedRows(customers, order_details, orders, products)


def _validate_targets(targets: TargetCounts) -> None:
    for name, target in (
        ("customers", targets.customers),
        ("order_details", targets.order_details),
        ("orders", targets.orders),
        ("products", targets.products),
    ):
        if target < BASE_COUNTS[name]:
            raise ValueError(f"{name} target cannot be lower than the standard data")
    if targets.orders > BASE_COUNTS["orders"] and (targets.order_details - BASE_COUNTS["order_details"]) < (
        targets.orders - BASE_COUNTS["orders"]
    ):
        raise ValueError("every generated order requires at least one order detail")


def _customer(index: int, rng: random.Random) -> dict[str, object]:
    country, city = rng.choice(_COUNTRY_CITIES)
    return {
        "customer_id": f"S{index:04d}",
        "company_name": f"Synthetic Market {index:04d}",
        "contact_name": f"Contact {index:04d}",
        "contact_title": rng.choice(("Buyer", "Owner", "Sales Manager")),
        "address": f"{100 + index} Market Street",
        "city": city,
        "region": None,
        "postal_code": f"{rng.randrange(10_000, 100_000):05d}",
        "country": country,
        "phone": f"+1-206-555-{index:04d}",
        "fax": None,
    }


def _product(product_id: int, rng: random.Random) -> dict[str, object]:
    return {
        "product_id": product_id,
        "product_name": f"Synthetic Product {product_id}",
        "supplier_id": rng.choice(tuple(BASE_SUPPLIER_IDS)),
        "category_id": rng.choice(tuple(BASE_CATEGORY_IDS)),
        "quantity_per_unit": rng.choice(("12 x 250 g", "24 x 330 ml", "6 x 1 kg")),
        "unit_price": round(rng.uniform(8, 120), 2),
        "units_in_stock": rng.randrange(0, 151),
        "units_on_order": rng.randrange(0, 51),
        "reorder_level": rng.randrange(5, 31),
        "discontinued": 0,
    }


def _delivery_statuses(order_count: int, rng: random.Random) -> list[Literal["on_time", "late", "unshipped"]]:
    on_time_count = order_count * 80 // 100
    late_count = order_count * 15 // 100
    statuses: list[Literal["on_time", "late", "unshipped"]] = (
        ["on_time"] * on_time_count + ["late"] * late_count + ["unshipped"] * (order_count - on_time_count - late_count)
    )
    rng.shuffle(statuses)
    return statuses


def _order(
    order_id: int,
    rng: random.Random,
    customers: Sequence[dict[str, object]],
    delivery_status: Literal["on_time", "late", "unshipped"],
) -> dict[str, object]:
    generated_customers = tuple(str(row["customer_id"]) for row in customers)
    customer_id = rng.choice(tuple(sorted(BASE_CUSTOMER_IDS)) + generated_customers)
    order_date = _START_DATE + timedelta(days=rng.randrange((_END_DATE - _START_DATE).days + 1))
    required_date = order_date + timedelta(days=rng.randrange(7, 22))
    shipped_date: date | None
    if delivery_status == "on_time":
        shipped_date = order_date + timedelta(days=rng.randrange(1, (required_date - order_date).days + 1))
    elif delivery_status == "late":
        shipped_date = required_date + timedelta(days=rng.randrange(1, 15))
    else:
        shipped_date = None
    return {
        "order_id": order_id,
        "customer_id": customer_id,
        "employee_id": rng.choice(tuple(BASE_EMPLOYEE_IDS)),
        "order_date": order_date.isoformat(),
        "required_date": required_date.isoformat(),
        "shipped_date": shipped_date.isoformat() if shipped_date else None,
        "ship_via": rng.choice(tuple(BASE_SHIPPER_IDS)),
        "freight": round(rng.uniform(10, 180), 2),
        "ship_name": f"Northwind delivery {order_id}",
        "ship_address": f"{order_id % 500 + 1} Commerce Avenue",
        "ship_city": rng.choice(tuple(city for _, city in _COUNTRY_CITIES)),
        "ship_region": None,
        "ship_postal_code": f"{rng.randrange(10_000, 100_000):05d}",
        "ship_country": rng.choice(tuple(country for country, _ in _COUNTRY_CITIES)),
    }


def _order_details(
    rng: random.Random,
    orders: Sequence[dict[str, object]],
    products: Sequence[dict[str, object]],
    target_count: int,
) -> tuple[dict[str, object], ...]:
    if not orders:
        if target_count:
            raise ValueError("order details require generated orders")
        return ()
    if target_count < len(orders):
        raise ValueError("every generated order requires at least one order detail")
    product_prices = {int(row["product_id"]): float(row["unit_price"]) for row in products}
    product_ids = tuple(sorted(BASE_PRODUCT_IDS | set(product_prices)))
    if target_count > len(orders) * len(product_ids):
        raise ValueError("target order details exceed distinct products per order")
    detail_counts = [target_count // len(orders)] * len(orders)
    for index in rng.sample(range(len(orders)), target_count % len(orders)):
        detail_counts[index] += 1
    details: list[dict[str, object]] = []
    for order, detail_count in zip(orders, detail_counts, strict=True):
        for product_id in rng.sample(product_ids, detail_count):
            details.append(
                {
                    "order_id": order["order_id"],
                    "product_id": product_id,
                    "unit_price": product_prices.get(product_id, _base_product_price(product_id)),
                    "quantity": rng.randrange(1, 13),
                    "discount": rng.choice((0.0, 0.0, 0.0, 0.05, 0.1, 0.15, 0.2)),
                }
            )
    return tuple(details)


def _base_product_price(product_id: int) -> float:
    return round(6 + (product_id * 1.73) % 90, 2)
