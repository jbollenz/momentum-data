#!/usr/bin/env python3
"""Idempotence gate for the collect workflow.

Writes `skip=true` to $GITHUB_OUTPUT when data/meta.json already holds a
successful run from *this* UTC date whose US price data was fully fresh
(`freshness.US.lag_sessions == 0`). That makes the second daily cron a
no-op after a healthy first attempt while still letting it do the work when
the first attempt was missed, failed, or came back stale.

A `workflow_dispatch` run never skips. Anything unexpected (missing file,
bad JSON, missing fields) also never skips — the safe default is to collect.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

META = Path(__file__).resolve().parents[2] / "data" / "meta.json"


def decide() -> tuple[bool, str]:
    if os.environ.get("EVENT_NAME") == "workflow_dispatch":
        return False, "manual run: always collect"
    if not META.exists():
        return False, "data/meta.json missing: collect"
    try:
        meta = json.loads(META.read_text(encoding="utf-8"))
    except Exception as exc:                       # noqa: BLE001 - be permissive
        return False, f"data/meta.json unreadable ({exc}): collect"
    if not isinstance(meta, dict):
        return False, "data/meta.json is not an object: collect"

    run_utc = str(meta.get("run_utc") or "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if run_utc[:10] != today:
        return False, f"last run {run_utc or 'unknown'} is not from {today}: collect"
    if meta.get("run_ok") is False:
        return False, "last run reported run_ok=false: collect"

    freshness = meta.get("freshness")
    us = freshness.get("US") if isinstance(freshness, dict) else None
    lag = us.get("lag_sessions") if isinstance(us, dict) else None
    if lag == 0:
        return True, f"run {run_utc} today already has US lag_sessions=0: skip"
    return False, f"US lag_sessions={lag!r} (not 0): collect"


def main() -> int:
    skip, reason = decide()
    print(reason)
    output = os.environ.get("GITHUB_OUTPUT")
    line = f"skip={'true' if skip else 'false'}\n"
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(line)
    else:
        print(line.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
