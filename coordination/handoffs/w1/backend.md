# Handoff — backend — wave 1

Branch: `agent/backend/w1`   Base: `fd484d9` (Wave 0)   Contract version: CONTRACT.md W1 (no breaking deviations; additive fields listed below)

## Done

### A. accounts (auth, MFA, API keys)
- Files: `backend/apps/accounts/{views.py,serializers.py,services.py,authentication.py,api_keys.py,mfa.py,urls_public.py,models.py}`, migration `accounts/0002_apikey.py`.
- `GET /auth/csrf` (204 + `csrftoken`), `POST /auth/signup` (201 `{user, org}` + session; org is always `synthetic_only`, a body `ai_data_policy` is ignored), `POST /auth/login` (throttle scope `login`, generic `invalid_credentials`, 5 failures / 15 min per account+IP → exponential backoff `429 login_locked` + `Retry-After`, session key + CSRF rotation), `POST /auth/logout`, `GET/PATCH /me` (full_name, timezone validated against IANA), `POST /auth/password/change` (`{current_password, new_password}`, 204, session kept and rotated).
- CSRF is enforced explicitly on the session-less endpoints (signup, login, invitation accept) to prevent login-CSRF (`403 csrf_failed`).
- TOTP MFA (django-otp): `POST /auth/mfa/totp/setup` → `{otpauth_url, secret}`, `POST /auth/mfa/totp/verify {code}`, `POST /auth/mfa/totp/disable {password, code}`. When `mfa_enabled`, login needs `otp` (`400 mfa_required` with `mfa_required: true`, wrong code `400 mfa_invalid`, counted as a login failure).
- `ApiKey` TenantModel (RLS): `prefix` 8 chars `[a-z0-9]` globally unique, `key_hash = HMAC-SHA256(secret, API_KEY_PEPPER)`, `scopes` text[], `expires_at`, `revoked_at`, `last_used_at` (updated at most once/minute), `created_by`.
- SECURITY DEFINER `nexora_api_key_lookup(prefix)` owned by `nexora_definer`: returns the key only while the org is active AND the creator is an active user with an active membership, plus the creator's current role permissions.
- `ApiKeyAuthentication`: `Bearer nxk_<prefix>_<secret>`, constant-time compare, sets `request._nexora_api_key_tenant = TenantInfo(org_id, slug, role_key="api_key", permissions=scopes ∩ creator perms ∩ allowed scopes)` and `request._nexora_api_key_id`; user = creator. Non-`nxk_` bearer tokens fall through to session auth. Returns 401 with `WWW-Authenticate: Bearer`.
- Key management: `GET/POST /orgs/{org}/api-keys`, `DELETE /orgs/{org}/api-keys/{id}` (revoke, idempotent), perm `api_keys.manage`; secret returned once. Scopes must be ⊆ caller's permissions and never include management codenames (`members.manage`, `roles.manage`, `api_keys.manage`, `org.settings.edit`, `org.ai_policy.consent`, `governance.*`, `approvals.decide`).
- API keys can never reach `/auth/*`, `/me`, `/orgs` (create/list), members, invitations, roles, api-keys, AI-policy writes or governance endpoints (`DenyApiKey` → 403, or 401 on session-only routes).

### B. orgs
- Files: `backend/apps/orgs/{services.py,views.py,serializers.py,urls.py,urls_public.py,models.py,ai_consent.py}`, migration `orgs/0003_aipolicyconsent_invitation.py`.
- `GET/POST /orgs` (list caller's orgs / create, caller becomes owner), `GET/PATCH /orgs/{org}` (routed in `urls_public.py` so the path has no trailing slash; PATCH `{name?, settings?}` with shallow merge, `null` deletes a key, 16 KB cap; `ai_data_policy` in the body → 400).
- Members: `GET/POST /orgs/{org}/members` (POST = invite), `PATCH/DELETE /orgs/{org}/members/{membership_id}` (`{role?, status?}`); last active owner cannot be demoted, suspended or removed (`409 last_owner`); only owners may grant/change/remove the owner role (`403 owner_required`).
- Invitations: `Invitation` TenantModel (RLS, sha256(token) only, 7-day expiry, partial unique (org, email) while pending), email via `send_mail` (Mailpit), re-inviting a pending email rotates the token; `GET/POST /orgs/{org}/invitations` (`?status=all` includes accepted/revoked), `DELETE /orgs/{org}/invitations/{id}` (revoke). SECURITY DEFINER `nexora_invitation_by_token(bytea)`. `POST /invitations/accept {token, password?, full_name?}` handles logged-in user (email must match → `403 invitation_email_mismatch`), existing user (password), or new account (password validated) → 200 `{org, user}` + session.
- `GET /orgs/{org}/roles` (plain array with permission codenames), `GET /permissions` (catalog).

### C. AI data policy (security critical)
- `AIPolicyConsent` append-only ledger (RLS + `MakeAppendOnly`): action grant|revoke, policy, consent_text_version, gemini_tier, actor_id, ip, user_agent.
- `apps.orgs.services.ai_policy_state()` keeps its signature/return contract: `consent_valid` is True for `synthetic_only`; otherwise only if the LATEST ledger row for the org's current policy is a `grant` for `settings.AI_CONSENT_TEXT_VERSION` and the Gemini tier allows the policy (`aggregates_only`/`full` invalid on `free`). Bumping the text version or downgrading the tier immediately invalidates consents.
- Endpoints exactly as CONTRACT.md: `GET /ai-policy`, `GET /ai-policy/consent-text`, `POST /ai-policy/consents` (perm `org.ai_policy.consent`, password re-auth `400 reauth_failed`, `acknowledged=true` + current version else `400 ai_policy_consent_required`, `403 ai_policy_tier_forbidden` on free tier; switching policies writes a revoke for the previous one), `POST /ai-policy/revoke` (idempotent, back to `synthetic_only`). Every change audited (`ai.consent.grant` / `ai.consent.revoke`).

### D. Activity
- `GET /orgs/{org}/activity` (perm `org.view`): latest 50 audit events with `actor_email` / `actor_name` (matches frontend `ActivityEvent`).

### E. governance
- Files: `backend/apps/governance/{models.py,services.py,serializers.py,views.py,urls.py}`, migration `governance/0001_initial.py` (RLS on all 4 tables).
- Models: `DatasetAcl`, `ColumnPolicy` (deny|mask_null|mask_hash|mask_partial, `params.keep_last`, `exempt_role_ids`), `RowPolicy` (predicate `{"all":[{col,op,value}]}`, `$user.<attr>`), `UserAttribute`. Datasets referenced by UUID `dataset_id` (no FK).
- `apps.governance.services.effective_policy(user_id, dataset_id, tables=None, columns_by_table=None, *, privilege="query") -> nexora.contracts.QueryPolicy`. Pure core `compute_policy(...)` (deny wins, most restrictive mask, smallest keep_last, row predicates ANDed, `$user.<attr>` + built-ins `$user.id/.email/.role`, fail-closed on unbindable/unenforceable predicates, owner/admin exempt only via `exempt_role_ids`, sha256 fingerprint over canonical inputs). Also `check_dataset_access`, `visible_dataset_ids`, `validate_predicate`, `policy_to_dict`. Row cap honours org `settings.row_cap` (clamped to 5000).
- REST (perm `governance.policy.edit`, no API keys): `/datasets/{id}/acl`, `/datasets/{id}/column-policies`, `/datasets/{id}/row-policies` (list/create/retrieve/patch/put/delete as per Phase 5 table), `POST /datasets/{id}/policy-preview` (effective policy as a chosen member; no data read), `GET/PUT /users/{id}/attributes`. All mutations audited.

### Core (extended, public signatures unchanged)
- `apps.core.permissions.DenyApiKey`, `apps.core.auth.enforce_csrf`, new problem codes (`Forbidden`, `AIPolicyConsentRequired`, `AIPolicyTierForbidden`), `throttled` → `rate_limited`.
- `apps.core.registry`: `register_asset_exists/asset_exists`, `register_dataset_schema_provider/dataset_schema`.
- **Behaviour change:** `TenantScopedMixin` now returns **401** (not 404) for unauthenticated requests to tenant routes (existence is still never revealed; members of other orgs still get 404).
- `uuid7()` is now monotonic within a process (RFC 9562 counter) so "latest ledger row" ordering is deterministic.

## Tests (real counts)
Command (from `backend/`): `$env:PYTEST_DB_SUFFIX="backend"; & "D:\nexora enterprice ai\.venv\Scripts\python.exe" -m pytest apps --create-db -p no:cacheprovider`
- **Executed and passing: 61** (no DB needed): `apps/governance/tests/test_policy_compute.py` 44, `apps/accounts/tests/test_services_unit.py` 13, `apps/core/tests/test_core_unit.py` 4 → `61 passed`.
- **Not executed: infra unavailable — 128 DB-backed tests** (121 new + 7 pre-existing in `apps/core/tests/test_tenancy.py`): Postgres at `localhost:5433` unreachable (Docker daemon not running: `//./pipe/dockerDesktopLinuxEngine` missing). Written and collected: `accounts/tests/test_auth_api.py` 26, `accounts/tests/test_api_keys.py` 23, `orgs/tests/test_orgs_api.py` 15, `orgs/tests/test_members_invitations.py` 18, `orgs/tests/test_ai_policy.py` 20, `governance/tests/test_governance_api.py` 19. They cover status codes, permissions, cross-org 404s, validation errors, RLS isolation, append-only ledger, CSRF, lockout, MFA and API-key auth. **Please run them once Docker is up.**
- `manage.py check` OK; `makemigrations --check` → no changes; `ruff check` on owned apps OK; `scripts/check_conventions.py` → conventions OK; `scripts/check_ownership.py --agent backend --base fd484d9` → ownership OK.

## Requests (Integrator)
- [setting] `FRONTEND_BASE_URL` (invitation links; currently falls back to `CSRF_TRUSTED_ORIGINS[0]`, link = `<base>/invitations/accept?token=…`). Frontend needs a page at that path.
- [setting] Dedicated throttle scopes/rates `signup` and `reauth` (signup, invitation accept, password change, consent and MFA-disable currently reuse scope `login` = 10/min).
- [contract] Additive fields/endpoints to document in CONTRACT.md: `Org.settings`; `GET /ai-policy` → `configured_policy`; `Org.ai_data_policy` and `ai-policy.policy` are the **effective** policy (synthetic_only when consent is invalid); `consent.granted_by` = grantor email; `Member.role_name`; invitation `created_at` + `email_sent` (on create); `/invitations/accept` returns `{org, user}`; `user.mfa_enabled`; `GET /orgs` (list); `GET /permissions`; MFA endpoints; api-key list item `{id,name,prefix,scopes,status,created_at,created_by,expires_at,revoked_at,last_used_at}`; `/activity` returns `{items, next_cursor:null, previous_cursor:null}`.
- [contract] Error codes/statuses: `invalid_credentials` 400, `login_locked` 429, `mfa_required`/`mfa_invalid` 400, `email_taken` 409, `csrf_failed` 403, `reauth_failed` 400, `ai_policy_consent_required` 400, `ai_policy_tier_forbidden` 403, `last_owner` 409, `owner_required` 403, `already_member` 409, `invitation_invalid` 400, `invitation_email_mismatch` 403, `api_key_not_allowed` 403, `api_key_invalid` 401, `rate_limited` 429.
- [contract] `nexora.contracts.PolicyService.effective_policy(user_id, dataset_id)` → implementation additionally accepts `tables`, `columns_by_table` and keyword `privilege`; consider updating the Protocol.
- [contract] API-key creation: security doc asks for ≤5-min re-auth but CONTRACT body is `{name, scopes}`; propose an optional/required `password` field in a later wave.

## Notes for other agents
- **sqlguard (data_engineering):** `RowPredicate` condition `(col, "in", ())` (empty tuple) means *no row matches* (render FALSE); `(col, "not_in", ())` means TRUE. The policy engine emits empty `in` to fail closed. Values for `in`/`not_in` are tuples; null ops carry `None`.
- **datasets (data_engineering):** please register in `AppConfig.ready()`: `apps.core.registry.register_asset_exists("dataset", fn)` (governance endpoints then 404 unknown datasets) and `register_dataset_schema_provider(fn)` returning `(tables, columns_by_table)`; enforce dataset ACLs with `apps.governance.services.check_dataset_access(user_id, dataset_id, privilege)` / `visible_dataset_ids(...)`.
- **ai (ai_ml):** use `apps.orgs.services.ai_policy_state()` (unchanged signature) — treat `consent_valid=False` as `synthetic_only`.
- **qa_security:** unauthenticated tenant requests now get 401 (not 404). Test helpers: `apps/core/tests/fixtures.py` (synthetic factories).

## Open issues / known limitations
- DB-backed tests not executed (infra down) — first priority once Postgres is available.
- Not implemented (outside the wave-1 brief): password reset request/confirm, custom role CRUD (`POST /roles`, `PATCH/DELETE /roles/{id}`), `audit-events/verify` + hash-chain sealing job, PII tags, retention policies, access history, MFA *enforcement* for owner/admin in production, session absolute 7-day maximum.
- Invitation email is sent synchronously inside the request (failure is logged and reported as `email_sent: false`, the invitation stays valid and can be re-sent).
- `ApiKey.created_by` cascade on user deletion relies on ORM cascade inside a tenant context (users are not deleted in the MVP).
