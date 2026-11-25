# 🚀 NEXORA Enterprise AI: સંપૂર્ણ પ્રોજેક્ટ માર્ગદર્શિકા અને વિશ્લેષણ

આ દસ્તાવેજમાં **NEXORA Enterprise AI** પ્લેટફોર્મનું વિગતવાર પૃથક્કરણ, તેની ટેકનિકલ બનાવટ, તમામ ૮૨ Markdown દસ્તાવેજોનું વિશ્લેષણ, ૧૦ મોડ્યુલ્સ (Modules A–J), સિક્યોરિટી મિકેનિક્સ અને ઉદ્યોગ (Industry) માં તેના વાસ્તવિક ઉપયોગોની સરળ અને ચોક્કસ સમજૂતી આપવામાં આવી છે.

---

## 💡 ૧. સરળ ભાષામાં સમરી: આ પ્રોજેક્ટ શું કામ કરે છે?

જો સાદા શબ્દોમાં સમજીએ તો **NEXORA એ કંપનીઓ માટેનો એક "સુપર-ઇન્ટેલિજન્ટ AI ડેટા ઓફિસર" છે.**

આજે દરેક મોટી કંપનીમાં ડેટા અલગ-અલગ જગ્યાએ વિખરાયેલો હોય છે:
* સેલ્સ ટીમ એક્સેલ (Excel/CSV) શીટ્સ વાપરતી હોય.
* એકાઉન્ટ્સ ટીમ જૂના ડેટાબેઝ વાપરતી હોય.
* ગ્રાહકોના ઇન્વૉઇસ અને કાગળો PDF ફાઇલોમાં પડ્યા હોય.

**NEXORA આ બધી જ અરાજકતાનો ઉકેલ લાવે છે:**
1. **બધો ડેટા એક જગ્યાએ:** ગમે ત્યાંથી આવતો ડેટા (CSV, Excel, JSON, Database) એક ક્લિકમાં ખેંચી લે છે અને ડેટા સાફ કરી આપે છે.
2. **કોડિંગ વગર વાતચીત (Ask AI):** કંપનીના માલિક કે મેનેજરને SQL કે કોડિંગ શીખવાની જરૂર નથી. તેઓ સીધું પૂછી શકે છે: *"ગયા મહિને મુંબઈમાં સૌથી વધુ વેચાયેલી ટોપ ૫ પ્રોડક્ટ કઈ છે અને કયા ગ્રાહકો હવે ખરીદી નથી કરતા?"* સિસ્ટમ જાતે જ સિક્યોર ક્વેરી બનાવી સેકન્ડોમાં ગ્રાફ, ટેબલ અને AI વિશ્લેષણ હાજર કરી દે છે.
3. **ગ્રાહકની ઓળખ (Customer 360):** જો 'અમિત શાહ' એ વેબસાઇટ પરથી ખરીદી કરી હોય અને ઓફલાઇન સ્ટોરમાં 'A. Shah' લખાવ્યું હોય, તો સિસ્ટમ આપમેળે સમજી જાય છે કે આ બંને એક જ વ્યક્તિ છે (Fuzzy Matching).
4. **દસ્તાવેજો વાંચવાની શક્તિ (Document Intelligence):** સ્કેન કરેલા બિલ કે કંપની પોલિસી (અંગ્રેજી, હિન્દી કે ગુજરાતીમાં) વાંચી શકે છે અને તેમાંથી સાચા જવાબો સાઇટેશન (પેજ નંબર) સાથે આપે છે.
5. **ભવિષ્યની આગાહી (No-Code Machine Learning):** કોડિંગ વગર કયો ગ્રાહક કંપની છોડીને જતો રહેશે (Churn Risk) તેનું સચોટ અનુમાન લગાવે છે.

---

## 🏭 ૨. ઇન્ડસ્ટ્રી (ઉદ્યોગો) માં NEXORA નો વાસ્તવિક ઉપયોગ (Industry Use Cases)

### ૧. રિટેલ અને ઈ-કોમર્સ (Retail & E-Commerce)
* **ગ્રાહક રીટેન્શન (Churn Prevention):** કયા ગ્રાહકો છેલ્લા ૩ મહિનાથી ખરીદી નથી કરતા અને શા માટે, તેનું અગાઉથી અનુમાન લગાવીને માર્કેટિંગ ટીમને ઓફર મોકલવાની સુવિધા આપે છે.
* **પ્રોડક્ટ કેટલોગ સુમેળ (Product 360):** અલગ-અલગ સપ્લાયર્સ એક જ વસ્તુને અલગ નામે લખતા હોય (જેમ કે `500 ml` અને `0.5 L`), સિસ્ટમ તેને ઓળખીને એક જ પ્રોડક્ટ તરીકે મેપ કરે છે.

### ૨. બેન્કિંગ અને ફાઇનાન્સ (BFSI)
* **કડક ડેટા સિક્યોરિટી અને શૂન્ય લીકેજ:** બેંકનો ડેટા ક્યારેય પબ્લિક AI (OpenAI કે અન્ય) પર જતો નથી. સિસ્ટમની બાય-ડિફૉલ્ટ પોલિસી `synthetic_only` છે.
* **રો-લેવલ અને કોલમ માસ્કિંગ:** બેંકના કેશિયરને ગ્રાહકનો ક્રેડિટ કાર્ડ નંબર અધૂરો (`****-1234`) દેખાય, જ્યારે ઓડિટરને સંપૂર્ણ ઓડિટ લોગ મળે.

### ૩. હેલ્થકેર અને હોસ્પિટલ્સ (Healthcare & Pharma)
* **મલ્ટિલિંગ્યુઅલ મેડિકલ રેકોર્ડ્સ:** ગુજરાતી, હિન્દી કે અંગ્રેજીમાં લખેલા દર્દીઓના જૂના કાગળો અને લેબ રિપોર્ટ્સનું OCR કરીને ડિજિટલ શોધ શક્ય બનાવે છે.

### ૪. મેન્યુફેક્ચરિંગ અને સપ્લાય ચેઇન (Manufacturing & Supply Chain)
* **ઓર્ડર ડિલિવરી મોનિટરિંગ:** સપ્લાયર્સના પરફોર્મન્સનું રીઅલ-ટાઇમ મોનિટરિંગ અને સમયસર માલ પહોંચાડવા માટે સ્માર્ટ ચેતવણીઓ.

### ૫. B2B એન્ટરપ્રાઇઝ SaaS સોફ્ટવેર
* **હાઇબ્રિડ મલ્ટિ-ટેનન્સી:** એક જ સોફ્ટવેર ૧,૦૦૦ અલગ-અલગ કંપનીઓને વેચી શકાય છે. PostgreSQL ની **FORCE ROW LEVEL SECURITY** ના કારણે કંપની 'A' નો કર્મચારી કંપની 'B' નો એક પણ રેકોર્ડ ક્યારેય જોઈ શકતો નથી.

---

## 🏛️ ૩. પ્રોજેક્ટ આર્કિટેક્ચર અને ગહન સિક્યોરિટી મિકેનિક્સ

### ૧. મોડ્યુલર મોનોલિથ શૈલી (Modular Monolith)
પ્રોજેક્ટ માઇક્રોસર્વિસિસના ઝંઝટ વગર એક જ Django મોનોલિથમાં રચાયેલ છે, જેથી તમામ ડેટાબેઝ ટ્રાન્ઝેક્શન્સ સંપૂર્ણપણે સુરક્ષિત અને ACID રહે. `import-linter` દ્વારા કોડમાં ૬ લેયર્સનું કડક નિયંત્રણ છે:
* Layer 1: Core (ટેનન્સી, ક્રિપ્ટો, ઓડિટ)
* Layer 2: Accounts, Orgs, AI
* Layer 3: Governance
* Layer 4: Connectors, Datasets, Warehouse, Quality, Lineage, Pipelines, Semantic, SQLGuard
* Layer 5: NL2SQL, Documents, ML
* Layer 6: BI, Customers, Products, Automation, Platform Admin

### ૨. હાઇબ્રિડ મલ્ટિ-ટેનન્સી મોડલ (શૂન્ય ડેટા લીકેજ)
* **મેટાડેટા (`nexora_app`):** દરેક ટેબલ પર PostgreSQL ની **FORCE ROW LEVEL SECURITY** લાગુ છે. દરેક ટ્રાન્ઝેક્શનમાં `set_config('app.org_id', '<uuid>', true)` સેટ થાય છે. જો કોઈ સેટિંગ ન હોય તો `NULLIF` તેને NULL બનાવી દે છે જેથી કોઈ રો મેચ થતી નથી (Fail-Closed).
* **વેરહાઉસ (`nexora_wh`):** કંપનીનો એનાલિટિક્સ ડેટા તદ્દન અલગ સ્કીમામાં (`org_<short_id>`) રહે છે. AI દ્વારા બનેલી ક્વેરીઝ માત્ર `wh_q_<short_id>` નામના રીડ-ઓન્લી રોલથી રન થાય છે.

### ૩. SQLGuard: ૧૩-સ્ટેપ સિક્યોરિટી પાઇપલાઇન
AI જે પણ SQL ક્વેરી બનાવે છે, તે સીધી ડેટાબેઝ પર મોકલાતી નથી. SQLGlot એન્જિન નીચેના ૧૩ સ્ટેપ્સમાં તપાસ કરે છે:
1. **Size Limits:** ટેક્સ્ટ ≤ 10 KB, AST નોડ્સ ≤ 1,500, નેસ્ટિંગ ઊંડાઈ ≤ 12.
2. **Single Statement:** માત્ર એક જ સ્ટેટમેન્ટ (કોઈ સેમીકોલોન નહીં).
3. **Root Check:** માત્ર `Select` અથવા સેટ ઓપરેશન્સ (`Union`/`Intersect`/`Except`).
4. **Node Blocklist:** `Insert`, `Update`, `Delete`, `Drop`, `Alter`, `Set` જેવી ઘાતક કમાન્ડ્સ સખત બ્લોક.
5. **Function Allowlist:** ફક્ત સલામત મેથેમેટિકલ અને સ્ટ્રિંગ ફંક્શન્સ માન્ય. `pg_sleep`, `dblink` બ્લોક.
6. **No Set-Returning in FROM:** ટેબલ જનરેટિંગ ફંક્શન્સ પ્રતિબંધિત.
7. **Table Allowlist:** માત્ર માન્ય ટેનન્ટ ટેબલ્સ. `pg_*`, `information_schema` બ્લોક.
8. **Safe Types:** સિસ્ટમ કોલમ્સ (`ctid`, `xmin`) બ્લોક.
9. **Column Qualification:** `*` એક્સપાન્ડ થઈને માત્ર મંજૂર કોલમ્સ રહે છે.
10. **Secure-Source Rewrite:** યુઝરની કોલમ માસ્કિંગ અને રો-લેવલ ફિલ્ટર્સ સાથે ક્વેરી રીરાઇટ થાય છે.
11. **Row Cap Wrap:** ક્વેરીના અંતે `LIMIT 1001` મુકાય છે.
12. **Round-Trip AST Check:** `parse(generate(ast)) == ast` વેરિફિકેશન.
13. **EXPLAIN Plan Check:** ડેટાબેઝમાં ચલાવતા પહેલાં એક્ઝિક્યુશન કોસ્ટ લિમિટ ચકાસવી.

### ૪. AI ડેટા પોલિસી (AIDataPolicy) ના ૪ સ્તરો
1. `synthetic_only` (Default): માત્ર સિન્થેટીક કે પબ્લિક ડેટા.
2. `schema_only`: માત્ર ટેબલ/કોલમના નામો જાય છે, ડેટા વેલ્યુઝ ક્યારેય નહીં. એડમિનની પાસવર્ડ રી-ઓથેન્ટિકેશન સંમતિ જરૂરી.
3. `aggregates_only`: પ્રી-કમ્પ્યુટેડ એગ્રીગેટ્સ જાય છે.
4. `full`: સંપૂર્ણ રો ડેટા (માત્ર એન્ટરપ્રાઇઝ કોન્ટ્રેક્ટ પર).

### ૫. Local Runtime First
સિસ્ટમ Docker કે Redis વગર માત્ર સ્થાનિક PostgreSQL 16/17 પર ચાલે છે:
* `TASK_RUNNER = "thread"` (Redis વગર થ્રેડ પુલ)
* `JOB_EVENTS_BACKEND = "db"` (Redis Streams વગર ડેટાબેઝ આધારિત SSE)
* `STORAGE_BACKEND = "fs"` (MinIO વગર લોકલ ફાઇલ સિસ્ટમ)
* `VECTOR_BACKEND = "array"` (Numpy આધારિત વેક્ટર સર્ચ)

---

## ⚙️ ૪. પ્રોજેક્ટના તમામ ૧૦ મોડ્યુલ્સ (Modules A–J)

1. **Module A (Executive & Custom Dashboard):** ડ્રેગ-એન્ડ-ડ્રોપ ગ્રીડ, KPI કાર્ડ્સ, અને Ask AI ના જવાબોને ડેશબોર્ડ પર પિન કરવાની સુવિધા (યુઝરના અધિકારો મુજબ ક્વેરી રી-રન થાય છે).
2. **Module B (Data Engineering Hub):** CSV/Excel/DB કનેક્ટર્સ, Polars એન્જિન દ્વારા PII ડીટેક્શન અને ક્વોલિટી સ્કોરિંગ, ૧૩ સ્ટેપ્સનો વિઝ્યુઅલ DAG પાઇપલાઇન બિલ્ડર અને લિનેજ ગ્રાફ.
3. **Module C (Conversational BI / Ask AI):** અંગ્રેજી પ્રશ્નોમાંથી સુરક્ષિત SQL ક્વેરી જનરેશન, ડિટર્મિનેસ્ટિક કેશિંગ, લાઈવ SSE સ્ટેજ પ્રોગ્રેસ અને એક્સપોર્ટ સિક્યોરિટી.
4. **Module D (Customer 360):** RapidFuzz દ્વારા ઓળખની એકરૂપતા (Fuzzy Matching), યુનિફાઇડ ટાઇમલાઇન, RFM સેગમેન્ટેશન અને ML Churn સ્કોરિંગ.
5. **Module E (Product 360):** SKU નોર્મલાઇઝેશન, યુનિટ કન્વર્ઝન (500ml = 0.5L), એટ્રિબ્યુટ એક્સટ્રેક્શન અને હ્યુમન રિવ્યુ વર્કફ્લો.
6. **Module F (Document Intelligence):** અંગ્રેજી, હિન્દી અને ગુજરાતીમાં Tesseract OCR, સ્થાનિક E5 વેક્ટર એમ્બેડિંગ્સ અને પેજ સાઇટેશન સાથે RAG પ્રશ્નોત્તરી.
7. **Module G (ML Workspace):** નો-કોડ વિઝાર્ડ, ઓટોમેટેડ લીકેજ ડિટેક્શન, લોકલ CPU ટ્રેઇનિંગ (scikit-learn, XGBoost), SHAP ગ્રાફ્સ અને મેનેજર અપ્રુવલ-ગેટેડ પ્રોડક્શન પ્રમોશન.
8. **Module H (Workflow Automation & Approvals):** શિડ્યુલ અને વેબહૂક ટ્રિગર્સ, સ્ટેપ રજિસ્ટ્રી, અને હાઇ-ઇમ્પેક્ટ એક્શન્સ માટે પાસવર્ડ રી-ઓથેન્ટિકેશન અપ્રુવલ ઇનબોક્સ.
9. **Module I (Governance & Security):** ૭ સ્તરનું RBAC, કોલમ માસ્કિંગ (Allow, Deny, Mask), પોલિસી પ્રિવ્યૂ અને ક્રિપ્ટોગ્રાફિક ટેમ્પર-રેઝિસ્ટન્ટ ઓડિટ લોગ (SHA-256 હેશ ચેઇન).
10. **Module J (Admin Console):** સભ્યોનું સંચાલન, રોલ્સ, TOTP MFA, દૈનિક AI બજેટિંગ અને પ્લેટફોર્મ સુપર-એડમિન કન્સોલ.

---

## 🗂️ ૫. કોડબેઝની તમામ ૮૨ Markdown (`.md`) ફાઇલોનું વર્ગીકરણ

| શ્રેણી | ફાઇલોની સંખ્યા | મુખ્ય ફાઇલો અને ઉપયોગ |
|---|---|---|
| **૧. રૂટ અને ઇન્ડેક્સ** | ૨ | [README.md](file:///d:/nexora%20enterprice%20ai/README.md) (પ્રોજેક્ટ ઓવરવ્યૂ, સ્ટેક, સેટઅપ), [docs/README.md](file:///d:/nexora%20enterprice%20ai/docs/README.md) (દસ્તાવેજોનો વાંચન ક્રમ). |
| **૨. આર્કિટેક્ચર અને રોડમેપ** | ૧૯ | `PRODUCT_OVERVIEW.md`, `PROJECT_VISION.md`, `SYSTEM_ARCHITECTURE.md`, `MULTI_TENANCY_DESIGN.md`, `SECURITY_ARCHITECTURE.md`, `AI_ARCHITECTURE.md`, `DATA_ENGINEERING_DESIGN.md`, `ML_ARCHITECTURE.md`, `DATABASE_SCHEMA.md`, `DATABASE_ERD.md`, `API_SPECIFICATION.md`, `UI_UX_SPECIFICATION.md`, `TESTING_STRATEGY.md`, `DEPLOYMENT_ARCHITECTURE.md`, `IMPLEMENTATION_ROADMAP.md`, `MASTER_PLAN.md` (૫૭ KB માસ્ટર પ્લાન), `AGENT_DEVELOPMENT_STRATEGY.md`, `FEATURE_STATUS.md`, `RUN_GUIDE.md`. |
| **૩. API કોન્ટ્રેક્ટ્સ** | ૧ | [contracts/api/CONTRACT.md](file:///d:/nexora%20enterprice%20ai/contracts/api/CONTRACT.md) (W1-W5 બેકએન્ડ અને ફ્રન્ટએન્ડ વચ્ચેનો બંધનકર્તા કોન્ટ્રેક્ટ). |
| **૪. કોઓર્ડિનેશન** | ૨ | [coordination/AGENT_GUIDE.md](file:///d:/nexora%20enterprice%20ai/coordination/AGENT_GUIDE.md) (એજન્ટ નિયમો), [coordination/OWNERSHIP.md](file:///d:/nexora%20enterprice%20ai/coordination/OWNERSHIP.md) (ફાઇલ માલિકી હક). |
| **૫. મિશન્સ અને હેન્ડઓફ્સ** | ૪૦ | `coordination/missions/` (૧૮ મિશન ફાઇલો W2-W5), `coordination/handoffs/` (૨૨ હેન્ડઓફ ફાઇલો W1-W5). |
| **૬. ટેસ્ટિંગ, ફ્રન્ટએન્ડ અને ડેટા** | ૫ | [backend/tests/FINAL_VERIFICATION.md](file:///d:/nexora%20enterprice%20ai/backend/tests/FINAL_VERIFICATION.md), [frontend/README.md](file:///d:/nexora%20enterprice%20ai/frontend/README.md), [e2e/README.md](file:///d:/nexora%20enterprice%20ai/e2e/README.md), [perf/README.md](file:///d:/nexora%20enterprice%20ai/perf/README.md), [testdata/README.md](file:///d:/nexora%20enterprice%20ai/testdata/README.md). |
| **કુલ** | **૮૨ Markdown ફાઇલો** | **તમામ ફાઇલો ચકાસાયેલ છે.** |

---

## 🔄 ૬. સિસ્ટમનો સ્ટેપ-બાય-સ્ટેપ ઉપયોગ કેવી રીતે કરવો?

### સ્ટેપ ૧: સેટઅપ અને પ્રિપેરેશન
```powershell
# ૧. ડેટાબેઝ અને રોલ્સ સેટઅપ (એક જ કમાન્ડથી)
.\.venv\Scripts\python.exe scripts\setup_local.py

# ૨. ફ્રન્ટએન્ડ પેકેજીસ ઇન્સ્ટોલ કરવા
cd frontend && npm ci
```

### સ્ટેપ ૨: ડેમો ડેટા લોડિંગ
```powershell
cd backend
..\.venv\Scripts\python.exe manage.py seed_demo --password "Nexora@Demo2026"
..\.venv\Scripts\python.exe manage.py create_platform_admin --email demo@nexora.local
```

### સ્ટેપ ૩: સર્વર શરૂ કરવું
* **ટર્મિનલ ૧ (બેકએન્ડ):** `manage.py runserver 8000`
* **ટર્મિનલ ૨ (ફ્રન્ટએન્ડ):** `npm run build && npm run start`
* બ્રાઉઝરમાં `http://localhost:3000` ખોલીને `demo@nexora.local` / `Nexora@Demo2026` થી લૉગિન કરવું.

---

## 📊 ૭. પ્રોજેક્ટની વર્તમાન સ્થિતિ

* **બેકએન્ડ ટેસ્ટ્સ:** ૫,૨૧૯ પાસ, ૦ ફેલ (વાસ્તવિક PostgreSQL 17 પર).
* **ફ્રન્ટએન્ડ ટેસ્ટ્સ:** ૨૮૮ / ૨૮૮ પાસ (Vitest) અને Next.js Build સફળ.
* **સિક્યોરિટી અને લિન્ટિંગ:** Ruff, Import-linter, Ownership ચેક્સ સંપૂર્ણ પાસ.
