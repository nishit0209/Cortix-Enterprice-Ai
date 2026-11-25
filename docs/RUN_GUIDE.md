# Cortix Enterprise AI: Run Guide

How to install, run, load demo data and test Cortix on one Windows PC, plus the checklist for production. Run every command in a **PowerShell terminal** (VS Code terminal is fine).

---

## Part 1: One-time setup

### 1.1 What must be installed
| Tool | Version | Check |
|---|---|---|
| PostgreSQL | 16 or 17 (pgvector optional) | the "PostgreSQL" service is running |
| Python | 3.12, with the project's `.venv` | `.\.venv\Scripts\python.exe --version` |
| Node.js | 20+ | `node --version` |
| Tesseract OCR (optional) | with the `eng`, `hin`, `guj` packs | only needed for scanned PDFs |

### 1.2 Fill `.env` (repo root)
Fill these lines; nothing else needs to change for local use:
```
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<your PostgreSQL password>
APP_DB_NAME=nexora_app
WAREHOUSE_DB_NAME=nexora_wh
GEMINI_API_KEY=<your key from https://aistudio.google.com/apikey>   (optional; needed for AI answers the built-in builder cannot handle)
```

### 1.3 Create the databases and tables (one command, safe to re-run)
```powershell
cd "D:\nexora enterprice ai"
.\.venv\Scripts\python.exe scripts\setup_local.py
```
It ends with `DONE. The local database is ready.` Re-run it after every code update, because it also applies new tables.

### 1.4 Install the frontend packages (once, and after updates)
```powershell
cd "D:\nexora enterprice ai\frontend"
npm ci
```

### 1.5 Optional extras
```powershell
cd "D:\nexora enterprice ai\backend"
..\.venv\Scripts\python.exe manage.py download_models      # local multilingual embeddings (~1.1 GB). Without it, search uses full text.
..\.venv\Scripts\python.exe manage.py verify_gemini         # checks the Gemini key and model names (no data is sent)
```

---

## Part 2: Load the demo company (once)
```powershell
cd "D:\nexora enterprice ai\backend"
..\.venv\Scripts\python.exe manage.py seed_demo --password "Cortix@Demo2026"
..\.venv\Scripts\python.exe manage.py create_platform_admin --email demo@cortix.local
```
- `seed_demo` creates the user **demo@cortix.local** and the org **"Cortix Demo"**.
  - It ingests the synthetic sales, customers, products, orders and tickets data through the real pipeline: profiling, quality, warehouse, semantic layer.
  - It also builds a default dashboard and a sample conversation.
  - Running it again does not duplicate anything.
- `create_platform_admin` gives the demo user access to the **Platform console** (`/platform`).

---

## Part 3: Start the app (two terminals)

**Terminal 1: backend**
```powershell
cd "D:\nexora enterprice ai\backend"
..\.venv\Scripts\python.exe manage.py runserver 8000
```

**Terminal 2: frontend (fast production mode)**
```powershell
cd "D:\nexora enterprice ai\frontend"
npm run build
npm run start
```
Open **http://localhost:3000** and log in as `demo@cortix.local` / `Cortix@Demo2026`.

To stop, press **Ctrl+C** in each terminal. After a code update:
1. Re-run `scripts\setup_local.py` and `npm ci`.
2. Rebuild with `npm run build`.
3. Start both terminals again.

`npm run dev` also works, but it compiles every page on first visit, so it is slow.

---

## Part 4: What to test in the browser (click-through)
| # | Where | What to do | Expected |
|---|---|---|---|
| 1 | Dashboard | Open the home page | KPI cards, the default dashboard widgets with real numbers |
| 2 | Data → Datasets | Open `sales` | Profile, column types, PII flags, quality score, preview |
| 3 | Data → Upload | Upload any CSV (mark it synthetic for AI) | Progress, then a ready dataset with a profile |
| 4 | Ask AI | "total revenue by region" | Streaming stages, SQL shown, table and bar chart, insights |
| 5 | Ask AI | 👍, then "Pin to dashboard" | The widget appears on the dashboard |
| 6 | Ask AI → SQL editor | `SELECT region, sum(revenue) FROM sales GROUP BY 1` | A result. `DELETE ...` or `pg_sleep` is refused |
| 7 | Data → Pipelines | Build source → filter → aggregate → destination, preview, run | Run history, a new dataset, lineage graph |
| 8 | Customer 360 | Add the customers source, run resolution, review matches | Profiles, timeline, RFM segments |
| 9 | ML Workspace | New project: dataset `customers` / churn target, run EDA, train 2 models | Metrics, comparison, SHAP. Register, then promote to production (an approval request appears) |
| 10 | Automation → Approvals | Approve the promotion (re-enter password) | The model is in production. Customer 360 → Scoring run → churn scores |
| 11 | Product 360 | Add the products source, normalize, review matches/corrections | Masters, attributes, quality scores |
| 12 | Documents | Upload a PDF/DOCX, search, ask a question | Pages, entities, an answer with citations (extractive if the AI policy blocks it) |
| 13 | Automation | Create a workflow (schedule or webhook trigger), run it | A step timeline and notifications |
| 14 | Settings → Governance | Mask a column for a member, then "Preview as member" | The column shows as masked |
| 15 | Settings → AI policy | View the policy (default `synthetic_only`) | Explains what may be sent to Gemini |
| 16 | `/platform` | Platform console | Orgs (counts only), health, jobs, AI usage, logs |

When something does not work, note the screen, what you clicked and the error message.

---

## Part 5: Run the automated tests
```powershell
# backend: about 5,000 tests, using a separate test database (your data is not touched)
cd "D:\nexora enterprice ai\backend"
..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --create-db

# frontend
cd "D:\nexora enterprice ai\frontend"
npm run lint; npm run typecheck; npm test -- --run; npm run build
```
The current results are in `docs/FEATURE_STATUS.md` and `backend/tests/FINAL_VERIFICATION.md`.

---

## Part 6: Production checklist (before selling to customers)
1. **Server and HTTPS.**
   - Deploy on a Linux VM. Use Docker Compose with Caddy for automatic HTTPS; the steps are in `docs/DEPLOYMENT_ARCHITECTURE.md`.
   - Set `DJANGO_SETTINGS_MODULE=nexora.settings.prod` and serve the frontend and API from one domain.
   - Run the API with `uvicorn nexora.asgi:application --workers 4`.
2. **Secrets.**
   - Set strong unique values for `SECRET_KEY` (64+ characters), `API_KEY_PEPPER` and `NEXORA_KEK` (32 random bytes, base64), plus `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`.
   - `prod` settings **refuse to start** with development values.
   - Changing `SECRET_KEY` changes the internal DB passwords, so re-run `scripts\setup_local.py` afterwards.
3. **Gemini.**
   - The **free tier is only for synthetic/demo data.** Google may use free-tier prompts, and it is not allowed for EU/UK users.
   - For real customer data, use a **paid Gemini API key or Vertex AI** (`GEMINI_TIER=paid|vertex`), and keep the AI data policy appropriate per org.
4. **Scale.** For many users, add Redis (`REDIS_URL`) and Celery workers (`TASK_RUNNER=celery`), S3/MinIO storage (`S3_ENDPOINT_URL`), and pgvector.
5. **Backups.** Nightly `pg_dump` of both databases, plus a copy of `var/storage` (or the S3 bucket). Test a restore.
6. **Email.** Configure SMTP (`EMAIL_HOST` …) for invitations and notifications.
7. **Legal.** Terms of service, privacy policy, a data-processing agreement, and the sub-processor list (Google Gemini).
8. **Load test** with realistic data volumes (`perf/` has k6 scripts) and monitor logs (JSON lines).

---

## Command reference
| Command (from `backend`) | Purpose |
|---|---|
| `..\.venv\Scripts\python.exe ..\scripts\setup_local.py` | create/update the DBs, users and tables |
| `manage.py seed_demo [--password X] [--reset]` | demo company and data |
| `manage.py create_platform_admin --email X [--revoke]` | grant or revoke platform-console access |
| `manage.py check_warehouse --provision-test` | verify the warehouse roles |
| `manage.py verify_gemini` | check the Gemini key and models |
| `manage.py download_models` | download the local embedding model |
| `manage.py ml_customer_recipe --org <org-slug> --user demo@cortix.local --train --promote staging --attest` | build and train the churn/CLV models for Customer 360 (the org slug is printed by `seed_demo`) |
| `manage.py record_cassettes --out DIR` | record Gemini answers for the offline AI evaluation (needs a key) |
