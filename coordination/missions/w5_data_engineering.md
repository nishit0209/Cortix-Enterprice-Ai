Owned: backend/apps/{connectors,datasets,pipelines,quality,lineage,warehouse,semantic,sqlguard,customers,products}/**.
A. Register the automation steps pipelines.run, customers.resolve, customers.score and products.normalize (CONTRACT "W5 additions";
   import lazily). Emit the events dataset.ingest_finished and pipeline.run_failed / pipeline.run_succeeded via apps.automation.events.emit.
B. Customer 360 scoring-runs (ML is merged now): churn and CLV through apps.ml.services.predict_frame on customer features (RFM, tenure,
   order stats, ticket counts) when a production/staging model exists for the org's customer project. Store CustomerScore with the
   explanation. Keep 409 ml_model_required otherwise. Next-best-action rules from segment + score. Endpoints per API_SPECIFICATION Phase 6.
C. Fixes: connector SSRF errors must use code connector_host_invalid (or another code from the QA test's set), not "invalid"; C360/P360 permission
   tests (apps/customers/tests/test_api.py::test_permissions_settings_and_policy, apps/products/tests/test_api.py::test_settings_and_permissions).
   Make every DB test in your apps pass.
