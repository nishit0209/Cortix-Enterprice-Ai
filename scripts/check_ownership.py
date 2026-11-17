"""Fail if an agent branch changed files outside its ownership globs (coordination/OWNERSHIP.md).

Usage:
  python scripts/check_ownership.py --agent backend --base main          # check branch diff vs base
  python scripts/check_ownership.py --owner-of backend/apps/core/x.py    # print owner of a path
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROW = re.compile(r"^\|\s*\d+\s*\|\s*`([^`]+)`[^|]*\|\s*([a-z_]+)\s*\|")


def load_rules() -> list[tuple[str, str]]:
    rules = []
    for line in (ROOT / "coordination" / "OWNERSHIP.md").read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if m:
            rules.append((m.group(1), m.group(2)))
    return rules


def owner_of(path: str, rules: list[tuple[str, str]]) -> str | None:
    path = path.replace("\\", "/")
    for glob, owner in rules:
        if glob == "*":
            if "/" not in path:
                return owner
            continue
        pattern = glob.replace("**", "\x00").replace("*", "[^/]*").replace("\x00", ".*")
        if re.fullmatch(pattern, path) or fnmatch.fnmatch(path, glob):
            return owner
    return None


def changed_files(base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent")
    ap.add_argument("--base", default="main")
    ap.add_argument("--owner-of")
    args = ap.parse_args()
    rules = load_rules()
    if args.owner_of:
        print(owner_of(args.owner_of, rules) or "UNOWNED")
        return 0
    if not args.agent:
        ap.error("--agent is required")
    bad = []
    for f in changed_files(args.base):
        owner = owner_of(f, rules)
        if owner != args.agent:
            bad.append((f, owner or "UNOWNED"))
    if bad:
        print(f"Ownership violations for agent '{args.agent}':")
        for f, owner in bad:
            print(f"  {f}  (owner: {owner})")
        return 1
    print(f"ownership OK for {args.agent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
