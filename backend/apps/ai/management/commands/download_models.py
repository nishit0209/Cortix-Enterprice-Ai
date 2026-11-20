"""Prefetch the local embedding model (intfloat/multilingual-e5-base by default, about 1.1 GB) and report where
it is cached.

    python manage.py download_models            # download (or verify) + load once + print the cache path
    python manage.py download_models --check    # only report whether it is cached (no network)
    python manage.py download_models --reembed  # afterwards embed chunks/cache rows stored while it was missing
    python manage.py download_models --json

Exit codes: 0 = model ready (or cached, with --check); 1 = not available (download failed / not cached).
The cache directory is EMBEDDING_CACHE_DIR, else SENTENCE_TRANSFORMERS_HOME / HF_HOME, else the Hugging Face
default (~/.cache/huggingface/hub). No Gemini call is ever made.
"""

from __future__ import annotations

import json
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.ai.embeddings import EmbeddingUnavailable, LocalEmbedder, model_cache_dir


class Command(BaseCommand):
    help = "Download the local embedding model used for semantic search and the NL->SQL semantic cache."

    def add_arguments(self, parser):
        parser.add_argument("--model", default=None, help="Model id (default: settings.EMBEDDING_MODEL).")
        parser.add_argument("--check", action="store_true", help="Only report whether the model is cached.")
        parser.add_argument("--reembed", action="store_true",
                            help="Then embed rows stored while the model was unavailable (needs the database).")
        parser.add_argument("--json", action="store_true", help="Machine-readable output.")

    def handle(self, *args, model=None, check=False, reembed=False, json=False, **options):  # noqa: A002
        embedder = self.make_embedder(model)
        report = {"model": embedder.model_name, "dim": embedder.dim,
                  "cache_dir": embedder.cache_dir or model_cache_dir() or "~/.cache/huggingface/hub"}
        ok = False
        if check:
            path = embedder.cached_path()
            report.update(status="cached" if path else "missing", path=path)
            ok = bool(path)
        else:
            started = time.monotonic()
            try:
                path = embedder.download()
                vec = embedder.embed_queries(["warm-up"])
                report.update(status="ready", path=path, seconds=round(time.monotonic() - started, 1),
                              vector_dim=int(vec.shape[1]))
                ok = True
            except EmbeddingUnavailable as exc:
                report.update(status="failed", reason=exc.reason, error=str(exc))
        if ok and reembed and not check:
            report["reembed"] = self.reembed(embedder)
        self.emit(report, as_json=json)
        if not ok:
            raise CommandError("embedding model not available", returncode=1)

    @staticmethod
    def make_embedder(model: str | None) -> LocalEmbedder:
        return LocalEmbedder(model or settings.EMBEDDING_MODEL, allow_download=True)

    @staticmethod
    def reembed(embedder) -> dict:
        from apps.ai.embeddings import override_embedder
        from apps.ai.reembed import run_reembed_pending

        with override_embedder(embedder):
            return run_reembed_pending(allow_download=False)

    def emit(self, report: dict, *, as_json: bool) -> None:
        if as_json:
            self.stdout.write(json.dumps(report, default=str))
            return
        status = report.get("status")
        style = self.style.SUCCESS if status in ("ready", "cached") else self.style.ERROR
        self.stdout.write(style(f"{report['model']} ({report['dim']}-d): {status}"))
        self.stdout.write(f"  cache directory: {report['cache_dir']}")
        if report.get("path"):
            self.stdout.write(f"  model files:     {report['path']}")
        if report.get("seconds") is not None:
            self.stdout.write(f"  ready in {report['seconds']} s")
        if report.get("error"):
            self.stdout.write(f"  error: {report['error']}")
            self.stdout.write("  Without the model, documents are still processed and searched with full-text; "
                              "semantic search and the NL->SQL semantic cache resume once it is downloaded.")
        if report.get("reembed") is not None:
            self.stdout.write(f"  re-embedded: {report['reembed']}")
