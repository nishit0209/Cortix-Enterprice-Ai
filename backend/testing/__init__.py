"""Shared test infrastructure (QA/Security-owned).

    testing.fixtures   pytest fixtures loaded for every test session (`pytest_plugins` in backend/conftest.py)
    testing.factories  make_user / make_org / add_member / make_job / OrgWorld
    testing.client     CSRF-enforcing, logged-in DRF APIClient helpers
    testing.db         raw-SQL helpers: tenant tables, RLS status, GUC inspection, a second app-role connection
    testing.routes     URLconf enumeration of every tenant route (drives the isolation matrix)
    testing.isolation  per-route resource factories for the isolation matrix
    testing.fakes      FakeGeminiService, FakeRedis (streams subset), NetworkSpy, FakeClock
    testing.security   canary secrets and the CSV/XLSX formula-injection oracle

Everything here uses synthetic data only. Django models are imported lazily so that importing this package
never touches the app registry or the database.
"""
