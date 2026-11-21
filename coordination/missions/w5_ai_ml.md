Owned: backend/apps/{ai,nl2sql,documents,ml}/**.
A. ML fixes so these pass: tests/security/test_ml_security.py::test_no_ml_route_accepts_an_uploaded_pickle and ::test_prediction_log_stores_no_raw_rows,
   and apps/ml/tests/test_api_db.py::test_permissions_and_cross_org. Production promotion goes through approvals per CONTRACT
   (apps.automation.services.request_approval; implement apps.ml.services.apply_promotion). Register the automation steps ml.train,
   ml.batch_predict and documents.process; emit document.processed.
B. Customer 360 support: a documented recipe/helper that creates the churn (classification) and CLV (regression) ML projects from the customers
   + orders testdata features, so seed/demo can train them (used by C360 scoring).
C. Document intelligence completion per API_SPECIFICATION Phase 8: document-collections, POST documents/{id}/extract {schema_key}
   (local rule/regex extraction for invoice/receipt/resume schemas; Gemini only via the policy gate), POST documents/compare (local section diff),
   and POST document-tables/{id}/promote (publish_frame). Tests.
D. Make every DB test in your apps pass.
