# NEXORA synthetic test data

Owned by QA/Security. **Everything here is synthetic.** No real people, customers or companies. Emails use
reserved example domains (`example.com/.org/.net`, `*.example.test`). Load these files with
`classification='synthetic'` so AI features work under the default `synthetic_only` policy.

## Regenerate

```bash
python testdata/generate.py                        # anchor = today (sales ends in the current month)
python testdata/generate.py --anchor 2026-11-17    # byte-identical to the committed files
python testdata/generate.py --out /tmp/td --only sales churn
```

Generation is deterministic: seed `20261117` + the anchor date fully determine every byte. Each dataset has
its own random stream (`<seed>:<dataset>`), so adding a dataset never changes another one.
`generated/MANIFEST.json` records the seed, the anchor, summary statistics and a sha256 per file;
`backend/tests/integration/test_testdata.py` regenerates into a temp dir and checks every hash.

The committed files were generated with `--anchor 2026-11-17`. Regenerate with today's date when a test needs
"the current month" to be the real current month (the frontend demo, e2e).

## Files (`generated/`)

| File | Rows | Purpose |
|---|---|---|
| `sales.csv` | 5,020 (5,000 orders + 20 exact duplicates) | Orders over 24 months up to the anchor month (the anchor month is partial). Columns `order_id, order_date, customer_id, customer_name, region, product, category, quantity, unit_price, discount, revenue, channel`. INR prices, `revenue = quantity * unit_price * (1 - discount)`. Regions North/South/East/West/Central. Seasonality: festive peak Sep–Nov, wedding season Dec–Feb, monsoon dip Jul–Aug, ~1.2 %/month growth. Planted anomaly: one week (see `MANIFEST.json` `stats.sales.anomaly_week`) with a 4x Smart Watch / Online spike. 6 empty cells each in `customer_name`, `region`, `discount`, `channel`. |
| `customers_crm.csv` | 500 | CRM extract: `crm_id, first_name, last_name, email, phone, city, date_of_birth, lifetime_value_inr` |
| `customers_web.csv` | 450 | Web sign-ups: `web_id, full_name, email, mobile, city, signup_date, source`. 350 of them are CRM people with typos, UPPER CASE, "Last, First" names, upper-cased or dot-dropped emails, `+91`/`0`/dashed phone formats and old city names (Bangalore/Bombay/Calcutta/Madras). |
| `customers_ground_truth.csv` | 600 | `person_id, crm_id, web_id` — the true identity map (350 CRM↔web matches). |
| `products_supplier_a.csv` | 170 | `sku, product_name, category, unit, pack_size, mrp_inr, ean` |
| `products_supplier_b.csv` | 160 | `item_code, description, cat, qty_per_pack, uom, price, in_stock` — different SKU scheme, UPPER CASE names with brand in brackets, typos, unit conversions (kg↔g, l↔ml). |
| `products_ground_truth.csv` | 200 | `master_id, supplier_a_sku, supplier_b_item_code, canonical_name, category, unit, pack_size` (139 A↔B matches). |
| `churn.csv` | 3,000 | `customer_id, tenure_months, monthly_spend_inr, support_tickets, days_since_login, plan, nps, region, churned`. Labels are drawn from a known logistic model. |
| `churn_meta.json` | – | The true coefficients and the **oracle ROC-AUC** (true probabilities, ≈0.81). A trained model should approach but not exceed it. |

### `generated/adversarial/`

| File | Attack | Expected handling |
|---|---|---|
| `malformed.csv` | BOM, ragged rows, unclosed quote, NUL byte, invalid UTF-8, wrong delimiter, non-numeric value | parse errors reported per row or `upload_rejected`; never a 500 |
| `formula_injection.csv` | `=`, `+`, `-`, `@`, TAB, CR prefixed cells, `HYPERLINK`/`IMPORTXML` payloads (and a plain `-10`) | imported as text; **exports** prefix dangerous cells with `'` |
| `huge_column.csv` | one cell just over 1 MiB | bounded memory; cell truncated or rejected with a clear error |
| `wide_columns.csv` | 2,000 columns | column limit enforced with a clear error |
| `zip_bomb.xlsx` | valid xlsx, ~230 KB on disk, ~64 MB sheet (ratio ≈ 290) | `upload_rejected` (ratio > 100) without inflating it |
| `png_as.csv`, `mz_as.csv`, `text_as.xlsx` | content does not match the extension (PNG / Windows executable / plain text) | `upload_rejected` by magic-byte check |

Oversized uploads are produced at test time (no committed file); see `backend/tests/security/test_uploads.py`.
