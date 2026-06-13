#!/usr/bin/env python3
"""Focused regression tests for decide-tracking.py.

These tests import the script directly to verify pure decision logic without
requiring live search results.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DECIDER = SCRIPT_DIR / "decide-tracking.py"

spec = importlib.util.spec_from_file_location("decide_tracking", DECIDER)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_plain_total_count_is_not_completion_signal_for_partial_new_show() -> None:
    candidates = mod.extract_totals_from_text(
        "《莫离》2026年6月9日首播，全40集，次日起每日两集更新。",
        "https://example.test/mo-li",
    )
    total_40 = [c for c in candidates if c.get("total") == 40]
    assert total_40, candidates
    assert any(c.get("ongoing_hint") for c in total_40), total_40
    assert not any(c.get("completed_hint") for c in total_40), total_40

    best = max(total_40, key=lambda c: c.get("confidence", 0))
    decision = mod.decision_from_total("莫离", "tv", 5, 40, best)
    assert decision["status"] == "ongoing"
    assert decision["total_episodes"] == 40


def test_explicit_completed_partial_source_still_needs_recovery() -> None:
    candidates = mod.extract_totals_from_text(
        "《老剧》已完结，共40集。",
        "https://example.test/old-show",
    )
    total_40 = [c for c in candidates if c.get("total") == 40]
    assert total_40, candidates
    assert any(c.get("completed_hint") for c in total_40), total_40

    best = max(total_40, key=lambda c: c.get("confidence", 0))
    decision = mod.decision_from_total("老剧", "tv", 5, 40, best)
    assert decision["status"] == "needs_recovery"


def main() -> int:
    tests = [
        test_plain_total_count_is_not_completion_signal_for_partial_new_show,
        test_explicit_completed_partial_source_still_needs_recovery,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
