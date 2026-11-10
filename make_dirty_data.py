#!/usr/bin/env python3
"""
make_dirty_data.py - copies data/clean to data/dirty and plants known errors.

The exact number of planted errors per check is written to data/dirty/PLANTED_ERRORS.json,
so the checker's results can be compared against ground truth (see tests/).
Each error type uses its own rows, so one planted error never triggers another check.
"""
import json
import random
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
SRC, DST = ROOT / "data" / "clean", ROOT / "data" / "dirty"
DST.mkdir(parents=True, exist_ok=True)
rng = random.Random(11)

read = lambda name: pd.read_csv(SRC / f"{name}.csv", dtype=str, keep_default_na=False)
orders, items, customers, products = read("orders"), read("order_items"), read("customers"), read("products")

# 1. Stale data: the last two weeks of December never arrived (orders and their lines)
CUTOFF = "2025-12-14"
orders = orders[orders.order_date <= CUTOFF].reset_index(drop=True)
items = items[items.order_id.isin(orders.order_id)].reset_index(drop=True)


def pool(df):
    idx = list(range(len(df)))
    rng.shuffle(idx)
    return idx


def take(p, n):
    chunk = p[:n]
    del p[:n]
    return chunk


def with_copies(df, rows):
    return pd.concat([df, df.iloc[rows]], ignore_index=True)


expected = {}

# ---- orders
p = pool(orders)
orders.loc[take(p, 12), "customer_id"] = ""                                   # missing customer
expected["required_fields_orders"] = 12
for i in take(p, 25):                                                          # customer does not exist
    orders.loc[i, "customer_id"] = f"C9{rng.randint(100, 999)}"
expected["orphan_orders_customer"] = 25
bad_dates = ["2025-13-40", "31/02/2025", "not recorded", "2035-06-15", "1999-03-02",
             "2025-00-10", "15-Mar-2025", "2025/07/04"]
for i, value in zip(take(p, len(bad_dates)), bad_dates):
    orders.loc[i, "order_date"] = value
expected["invalid_dates_orders"] = len(bad_dates)
orders = with_copies(orders, take(p, 5))                                       # 5 duplicated orders
expected["duplicate_ids_orders"] = 10                                          # both rows of each pair

# ---- order items
p = pool(items)
items.loc[take(p, 15), "quantity"] = "-1"
items.loc[take(p, 6), "list_price"] = "0"
expected["positive_values_order_items"] = 21
items.loc[take(p, 7), "discount_pct"] = "1.5"
expected["discount_range_order_items"] = 7
for i in take(p, 20):
    items.loc[i, "order_id"] = f"ORD-9{rng.randint(10000, 99999)}"
expected["orphan_items_order"] = 20
items.loc[take(p, 4), "product_id"] = "P999"
expected["orphan_items_product"] = 4
items.loc[take(p, 5), "quantity"] = ""
expected["required_fields_order_items"] = 5
items = with_copies(items, take(p, 3))
expected["duplicate_ids_order_items"] = 6

# ---- customers
p = pool(customers)
for i, value in zip(take(p, 4), ["2023-02-30", "unknown", "2023-13-01", "2090-01-01"]):
    customers.loc[i, "signup_date"] = value
expected["invalid_dates_customers"] = 4
customers.loc[take(p, 3), "district"] = ""
expected["required_fields_customers"] = 3
customers = with_copies(customers, take(p, 3))
expected["duplicate_ids_customers"] = 6

# ---- products
p = pool(products)
for i in take(p, 2):
    products.loc[i, "unit_cost"] = str(round(float(products.loc[i, "list_price"]) * 1.2, 2))
expected["cost_above_price_products"] = 2
products.loc[take(p, 1), "category"] = ""
expected["required_fields_products"] = 1
products = with_copies(products, take(p, 1))
expected["duplicate_ids_products"] = 2

for name, df in (("orders", orders), ("order_items", items), ("customers", customers), ("products", products)):
    df.to_csv(DST / f"{name}.csv", index=False)

expected["freshness_orders"] = "FAIL"
(DST / "PLANTED_ERRORS.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")
print(f"Wrote {DST}")
print(json.dumps(expected, indent=2))
