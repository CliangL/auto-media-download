#!/usr/bin/env python3
"""Regression checks for total episode lookup and tracking decisions."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DECIDER = SCRIPT_DIR / "decide-tracking.py"


def run_decision(title: str, media_type: str, current: int, source_path: str = "") -> dict:
    proc = subprocess.run(
        [
            "python3",
            str(DECIDER),
            "--title",
            title,
            "--media-type",
            media_type,
            "--current",
            str(current),
            "--source-path",
            source_path,
        ],
        text=True,
        capture_output=True,
        timeout=90,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "decide-tracking failed").strip())
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid decide-tracking json: {exc}") from exc


def emit(prefix: str, data: dict) -> None:
    print(f"{prefix}_STATUS={data.get('status')}")
    print(f"{prefix}_TOTAL={data.get('total_episodes')}")
    print(f"{prefix}_CONFIDENCE={data.get('confidence')}")
    print(f"{prefix}_ORIGIN={data.get('origin')}")
    print(f"{prefix}_EVIDENCE={data.get('evidence')}")
    trace = data.get("source_trace") or []
    print(f"{prefix}_SOURCE_TRACE={json.dumps(trace, ensure_ascii=False)}")


def main() -> int:
    title = sys.argv[1] if len(sys.argv) > 1 else "权力的游戏"
    media_type = sys.argv[2] if len(sys.argv) > 2 else "tv"
    expected_total = int(sys.argv[3]) if len(sys.argv) > 3 else 73
    partial_current = int(sys.argv[4]) if len(sys.argv) > 4 else 10

    completed = run_decision(title, media_type, expected_total)
    partial = run_decision(title, media_type, partial_current)

    emit("TRACK_COMPLETED", completed)
    emit("TRACK_PARTIAL", partial)

    completed_ok = (
        completed.get("status") == "completed"
        and int(completed.get("total_episodes") or 0) == expected_total
    )
    partial_total_ok = int(partial.get("total_episodes") or 0) == expected_total
    partial_status_ok = partial.get("status") in {"ongoing", "needs_recovery"}
    partial_under_total_ok = partial_current < int(partial.get("total_episodes") or 0)

    print(f"TRACK_TOTAL_LOOKUP_OK={1 if completed_ok and partial_total_ok else 0}")
    print(f"TRACK_COMPLETED_DECISION_OK={1 if completed_ok else 0}")
    print(f"TRACK_PARTIAL_DECISION_OK={1 if partial_status_ok and partial_under_total_ok else 0}")

    if not completed_ok:
        print("TRACK_REGRESSION_OK=0")
        return 1
    if not (partial_total_ok and partial_status_ok and partial_under_total_ok):
        print("TRACK_REGRESSION_OK=0")
        return 1

    print("TRACK_REGRESSION_OK=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
