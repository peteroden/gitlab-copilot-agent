#!/usr/bin/env python3
"""Compare current ruff violations against a (file, code) count baseline.

Exits 0 if no (file, code) pair exceeds its baseline count.
Exits 1 if any pair increased, printing the regressions.

Usage:
    uv run python scripts/check_lint_baseline.py [--update]
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BASELINE_PATH = Path(".lint-baseline.json")


def get_current_counts(paths: list[str]) -> dict[str, dict[str, int]]:
    """Run ruff and return {file: {code: count}} from JSON output."""
    result = subprocess.run(
        ["uv", "run", "ruff", "check", "--output-format=json", *paths],
        capture_output=True,
        text=True,
    )
    violations = json.loads(result.stdout) if result.stdout.strip() else []
    cwd = str(Path.cwd())
    counts: dict[str, dict[str, int]] = {}
    for v in violations:
        rel = v["filename"]
        if rel.startswith(cwd):
            rel = rel[len(cwd) :].lstrip("/")
        code = v["code"]
        counts.setdefault(rel, {})
        counts[rel][code] = counts[rel].get(code, 0) + 1
    return counts


def load_baseline() -> dict[str, dict[str, int]]:
    """Load the committed baseline file."""
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text())


def save_baseline(counts: dict[str, dict[str, int]]) -> None:
    """Write current counts as the new baseline."""
    BASELINE_PATH.write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n")


def check(current: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]) -> list[str]:
    """Return list of regression descriptions."""
    regressions: list[str] = []
    for filepath, codes in sorted(current.items()):
        for code, count in sorted(codes.items()):
            allowed = baseline.get(filepath, {}).get(code, 0)
            if count > allowed:
                regressions.append(
                    f"  {filepath} {code}: {allowed} → {count} (+{count - allowed})"
                )
    return regressions


def main() -> int:
    """Run the baseline check or update."""
    update_mode = "--update" in sys.argv
    lint_paths = ["src/", "tests/"]
    current = get_current_counts(lint_paths)

    if update_mode:
        save_baseline(current)
        total = sum(c for codes in current.values() for c in codes.values())
        print(f"Baseline updated: {total} violations across {len(current)} files")
        return 0

    baseline = load_baseline()
    regressions = check(current, baseline)

    if regressions:
        print("Lint ratchet FAILED — new violations exceed baseline:\n")
        print("\n".join(regressions))
        print("\nFix the violations or run: uv run python scripts/check_lint_baseline.py --update")
        return 1

    # Report improvements
    current_total = sum(c for codes in current.values() for c in codes.values())
    baseline_total = sum(c for codes in baseline.values() for c in codes.values())
    if current_total < baseline_total:
        print(
            f"Lint ratchet OK — improved! {baseline_total} → {current_total} "
            f"(-{baseline_total - current_total})"
        )
        print("Run: uv run python scripts/check_lint_baseline.py --update")
    else:
        print(f"Lint ratchet OK — {current_total} violations (baseline: {baseline_total})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
