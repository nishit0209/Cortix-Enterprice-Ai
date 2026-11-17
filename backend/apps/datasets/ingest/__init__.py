"""File ingestion building blocks: scanning, parsing, column naming, type inference, profiling, PII.

Pure functions over files and Polars frames (no ORM), so pipelines (wave 2) can reuse them through
`apps.datasets.services`.
"""
