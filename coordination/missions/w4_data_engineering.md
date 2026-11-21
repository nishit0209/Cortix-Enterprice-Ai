You own `backend/apps/{connectors,datasets,pipelines,quality,lineage,warehouse,semantic,sqlguard,customers,products}/**`. This wave adds `customers` and `products`.

## A. `publish_frame`

Add `apps.datasets.services.publish_frame(...)` EXACTLY per CONTRACT "W4 additions". AI/ML (batch predictions) and your own C360/P360 code use it IN PARALLEL.
- It writes the frame through the normal upload/ingest path (CSV via storage → `create_upload` → `create_dataset` / `add_version`), so profiling, quality, warehouse and semantic all run.
- It records lineage from `source`.
- Test it.

## B. CUSTOMER 360 FOUNDATION (`apps/customers`)

`docs/API_SPECIFICATION.md` Phase 6, `docs/DATABASE_SCHEMA.md` (customers tables) and `ML_ARCHITECTURE` §10 are binding, except scoring-runs. Churn/CLV scoring via the ML registry arrives in W5 when the ML workspace is merged; build `POST customers/scoring-runs` ONLY as a clear 409 `ml_model_required` until a production model exists, using `apps.ml.services.predict_frame` lazily.

1. **Models** (TenantModels + RLS): CustomerSource (dataset_id, field mapping JSON: `email`/`phone`/`name`/`address`/`dob`/`external_id`/`created_at` → columns), CustomerSourceRecord (normalized fields, source key), CustomerProfile (golden record with survivorship rules), IdentityMatch (record_a, record_b, score, features JSON, status `auto_merged|pending_review|confirmed|rejected`, reviewer, decided_at), CustomerEvent (timeline: orders, tickets, …, from mapped event datasets), CustomerSegment and its members.
2. **Sources:** `POST /customers/sources` maps a dataset to fields. The policy check applies: the user must hold `query` on the dataset, and masked or denied columns cannot be mapped.
3. **Identity resolution** (202 job). Use rapidfuzz plus deterministic rules (no Splink/duckdb; not installed):
   - normalization: lower-case and trim emails, E.164-ish phones, unicode-normalized names, address tokens;
   - blocking: email domain + first letter, phone suffix, soundex/metaphone-like name key;
   - pairwise features: exact email, phone, Jaro-Winkler name, token-set address, dob equality;
   - weighted score: ≥ `AUTO` threshold → auto-merge; between `REVIEW` and `AUTO` → `pending_review`; else no match;
   - thresholds configurable per org, with defaults documented;
   - union-find clustering into profiles, with survivorship (most recent non-null, most frequent, source priority).
4. **Review queue:** `GET /customers/matches?status=pending_review`; `POST /customers/matches/{id}/decision {confirm|reject}`. Confirm merges the profiles, reject splits them / blocks the pair. Audited.
5. **Profiles:** `GET /customers`, `/{id}` and `/{id}/timeline`, built from CustomerEvent rows ingested from mapped event datasets (orders with amount/date, tickets).
6. **Segments:** RFM computed locally (quintiles) plus rule-based segments (`{field, op, value}` conditions), with members.
7. **Notify** the requester when resolution finishes.

## C. PRODUCT 360 FOUNDATION (`apps/products`)

`docs/API_SPECIFICATION.md` Phase 7 is binding.

1. **Models:** ProductSource (dataset + mapping: `sku`, `name`, `brand`, `category`, `price`, `attributes`), ProductSourceRecord, ProductMaster, ProductAttribute, ProductMatch, ProductCorrection (`proposed|approved|rejected|applied`), ProductQualityScore.
2. **Normalize** (202): SKU normalization (strip separators, upper-case, known variant suffixes), brand canonicalization, unit parsing (e.g. 500ml / 0.5 L), and attribute extraction from names by regex rules (size, colour, pack count, material).
3. **Duplicate and variant matching:** blocking on brand+category, rapidfuzz name similarity, SKU-stem match, price proximity. Auto / review thresholds as in C360. Master records via union-find.
4. **Corrections:** a proposed correction is applied ONLY on approve (`products.review`), audited.
5. **Quality score** per product: completeness, validity, consistency.
6. **Search:** `POST /products/search` is semantic via `apps.ai.embeddings` + `apps.ai.vectors` (EmbeddingField on ProductMaster, `nearest()`), with full-text fallback and `search_mode` when the model is unavailable.
7. **Enrich:** `POST /products/enrich` returns 202. Local rule suggestions become proposed corrections. A Gemini path is allowed ONLY through `GeminiService` + the policy gate; product catalog text is VALUES, so it is allowed only for synthetic/public datasets under `synthetic_only`. Default local.

## D. Wave-3 leftovers

1. `data-stats` previous-period values, so the dashboard can show KPI deltas (`*_prev` fields, additive).
2. The pipeline destination "append" mode: either implement real append (new version = previous rows + new rows) or keep refusing it clearly. Decide and document.
3. Optional request from AI/ML: profiled top_values as `values` in `DatasetSemantics.columns_meta` for non-PII dimensions.

## E. Tests

- **Unit (no DB):** normalization, blocking, scoring, union-find, survivorship, RFM, SKU/unit/attribute parsing, product matching, quality score, `publish_frame` (with fakes).
- **Entity-resolution evaluation on `testdata/generated` customers/products:** QA built duplicate and variant SKUs into them. Report precision, recall and F1 against the ground truth the generator provides; read `testdata/generate.py` to find it.
- **DB/API tests** for every route, plus isolation factories and cross-org 404s.
