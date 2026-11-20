"""Record Gemini cassettes for the golden NL->SQL questions the deterministic builder declines.

    python manage.py record_cassettes --out D:/cassettes                 # every builder-declined golden question
    python manage.py record_cassettes --out D:/cassettes --only g46,g47 --force
    NEXORA_CASSETTES_DIR=D:/cassettes python -m testing.golden --with-cassettes   # QA's runner replays them

It runs ONLY when GEMINI_API_KEY is set: it calls Gemini for real (free tier is fine, it is paced) and must never run
in tests. Nothing else is needed: no database, no Redis.

Each question goes through the REAL nl2sql LLM path (`services._generate`: prompt `nl2sql.generate`, local SQL
validation, at most one `nl2sql.repair`) with QA's golden QuestionContext and anchor date (`testing.golden`), on a
database-free engine (`apps.ai.services.standalone_engine`): the real policy gate (the golden dataset is
synthetic), quota pacing at the model's default limits, rate-limit waits.

Output: one UTF-8 JSON file per question, `<out>/<id>.json` (or `<out>/nl2sql/<id>.json` with --layout nl2sql), in
the `nexora.cassette.v1` format QA's runner reads (backend/testing/golden.py):

    {"format": "nexora.cassette.v1", "id": "g46", "question": "<golden question>", "prompt_key": "nl2sql.generate",
     "model": "<gemini model>", "recorded_at": "<UTC ISO-8601>", "repaired": false,
     "response": {"sql": ..., "explanation": ..., "assumptions": [...], "chart_hint": {...}, "clarification": ...}}

`response` is the dict `generate_structured` returned. When the first answer needed the one repair, the cassette
stores the REPAIRED response (and `"repaired": true`), so replaying it as the `nl2sql.generate` answer yields exactly
the SQL production would have executed. A clarification (no SQL) is recorded as such.

Exit codes: 0 all requested questions recorded/skipped; 1 at least one failed; 2 no GEMINI_API_KEY / no golden set.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

CASSETTE_FORMAT = "nexora.cassette.v1"
PROMPT_KEY = "nl2sql.generate"
MAX_ATTEMPTS = 4  # rate-limit waits per question


class _Capture:
    """Delegates to the engine and keeps every successful structured response (prompt key, model, dict)."""

    def __init__(self, engine):
        self.engine = engine
        self.calls: list[tuple[str, dict]] = []

    def generate_structured(self, **kwargs) -> dict:
        data = self.engine.generate_structured(**kwargs)
        self.calls.append((str(kwargs.get("prompt_key") or ""), copy.deepcopy(data)))
        return data

    def __getattr__(self, name):
        return getattr(self.engine, name)


class Command(BaseCommand):
    help = "Record Gemini responses (cassettes) for the golden NL->SQL questions the builder declines."

    sleep = staticmethod(time.sleep)

    def add_arguments(self, parser):
        parser.add_argument("--out", required=True, help="Directory to write <id>.json cassettes into.")
        parser.add_argument("--only", default="", help="Comma-separated golden ids (default: all builder-declined).")
        parser.add_argument("--force", action="store_true", help="Overwrite cassettes that already exist.")
        parser.add_argument("--layout", choices=("flat", "nl2sql"), default="flat",
                            help="flat: <out>/<id>.json (default); nl2sql: <out>/nl2sql/<id>.json.")
        parser.add_argument("--max-wait", type=float, default=90.0, help="Longest rate-limit wait in seconds.")

    # ---------------------------------------------------------------------------------------- plumbing
    @staticmethod
    def load_golden():
        try:
            from testing import golden
        except ImportError as exc:
            raise CommandError("The golden set (backend/testing/golden.py + tests/ai_eval/golden_sales.yaml) is not "
                               "available; run this from the backend/ directory of a source checkout.",
                               returncode=2) from exc
        return golden

    @staticmethod
    def make_engine():
        from apps.ai.services import standalone_engine

        return standalone_engine()

    @staticmethod
    def target(out: Path, qid: str, layout: str) -> Path:
        return (out / "nl2sql" / f"{qid}.json") if layout == "nl2sql" else (out / f"{qid}.json")

    # ---------------------------------------------------------------------------------------- command
    def handle(self, *args, out, only="", force=False, layout="flat", max_wait=90.0, **options):
        if not str(getattr(settings, "GEMINI_API_KEY", "") or "").strip():
            raise CommandError("GEMINI_API_KEY is not set. record_cassettes calls Gemini for real and only runs with "
                               "a key; nothing was recorded.", returncode=2)
        from apps.ai.services import override_gemini
        from apps.core.tenancy.context import current_org
        from apps.nl2sql.builder import build_sql

        golden = self.load_golden()
        ctx, today = golden.question_context(), golden.today()
        wanted = {s.strip() for s in str(only or "").split(",") if s.strip()}
        cases = [c for c in golden.golden()["questions"] if not wanted or c["id"] in wanted]
        unknown = wanted - {c["id"] for c in cases}
        if unknown:
            raise CommandError(f"Unknown golden id(s): {', '.join(sorted(unknown))}", returncode=2)

        out_dir = Path(out).expanduser()
        engine = self.make_engine()
        capture = _Capture(engine)
        summary = {"recorded": [], "builder": [], "existing": [], "failed": []}
        token = current_org.set(ctx.org_id)  # tenant ContextVar only: the standalone engine needs no database
        try:
            with override_gemini(capture):
                for case in cases:
                    qid, question = case["id"], case["question"]
                    if build_sql(question, ctx, today=today) is not None:
                        summary["builder"].append(qid)
                        self.stdout.write(f"{qid}: answered by the builder, no cassette needed")
                        continue
                    path = self.target(out_dir, qid, layout)
                    if path.exists() and not force:
                        summary["existing"].append(qid)
                        self.stdout.write(f"{qid}: {path} exists (use --force to re-record)")
                        continue
                    cassette, problem = self.record_one(qid, question, ctx, today, capture, engine, max_wait)
                    if cassette is None:
                        summary["failed"].append(qid)
                        self.stdout.write(self.style.ERROR(f"{qid}: not recorded ({problem})"))
                        if problem == "ai_quota_exhausted":
                            self.stdout.write(self.style.ERROR("Daily Gemini quota exhausted; stopping."))
                            break
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(cassette, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    summary["recorded"].append(qid)
                    sql = (cassette["response"].get("sql") or "").strip()
                    what = f"SQL: {sql[:160]}" if sql else "clarification"
                    self.stdout.write(self.style.SUCCESS(f"{qid}: recorded -> {path}") + f"  [{what}]")
        finally:
            current_org.reset(token)

        self.stdout.write(f"recorded {len(summary['recorded'])}, answered by the builder {len(summary['builder'])}, "
                          f"already recorded {len(summary['existing'])}, failed {len(summary['failed'])}")
        if summary["recorded"]:
            self.stdout.write(f"Replay: NEXORA_CASSETTES_DIR={out_dir} python -m testing.golden --with-cassettes")
        if summary["failed"]:
            raise CommandError(f"{len(summary['failed'])} question(s) were not recorded: "
                               f"{', '.join(summary['failed'])}", returncode=1)

    def record_one(self, qid, question, ctx, today, capture, engine, max_wait):
        """(cassette dict, None) or (None, reason)."""
        from apps.nl2sql import services as nl2sql_services

        proposal = None
        for attempt in range(MAX_ATTEMPTS):
            capture.calls.clear()
            proposal = nl2sql_services._generate(question, ctx, today)
            if proposal.blocked_reason == "ai_rate_limited" and attempt < MAX_ATTEMPTS - 1:
                wait = min(float(max_wait), float(getattr(proposal, "retry_after_s", None) or 10) + 1.0)
                self.stdout.write(f"{qid}: rate limited, waiting {wait:.0f} s")
                self.sleep(wait)
                continue
            break
        if proposal.sql and capture.calls:
            prompt_key, response = capture.calls[-1]
        elif proposal.clarification and not proposal.blocked_reason and capture.calls:
            prompt_key, response = capture.calls[0]
        else:
            return None, proposal.blocked_reason or "no usable response"
        records = list(getattr(getattr(engine, "recorder", None), "records", []) or [])
        model = next((r.model for r in reversed(records) if getattr(r, "status", "") == "ok"), None)
        return {
            "format": CASSETTE_FORMAT,
            "id": qid,
            "question": question,
            "prompt_key": PROMPT_KEY,
            "model": model,
            "recorded_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "repaired": prompt_key != PROMPT_KEY,
            "response": response,
        }, None
