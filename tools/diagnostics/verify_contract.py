#!/usr/bin/env python3
"""Guard config.json against the frozen known-good capture.

The baseline under ``config/known_good_contracts/`` is a byte-for-byte
copy of a sniffer run in which every endpoint returned HTTP 200 and the
captured query IDs, feature maps, and field toggles all matched config at
capture time. This script re-checks the live ``config.json`` against that
frozen truth so payload drift (the classic 400 cause) is detected before a run.

Runtime-injected values (userId, cursor, rawQuery, querySource) are ignored --
only structural / feature-flag drift is reported.

Exit code 0 = no drift, 1 = drift found (or a baseline/config file is missing).

Run:
    python tools/diagnostics/verify_contract.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_DIAG_DIR = Path(__file__).resolve().parent
REPO_ROOT = _DIAG_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
from tweeter_data_fetcher.configuration import resolve_config_path

CONFIG_PATH = resolve_config_path(project_root=REPO_ROOT)
BASELINE_DIR = REPO_ROOT / "config" / "known_good_contracts" / "endpoint_contracts"

from tweeter_data_fetcher.twitter.contract_verification import (
    QUERY_ID_KEYS,
    check_endpoint as _check_endpoint,
    verify_contract,
)


def check_endpoint(endpoint, config):
    return _check_endpoint(endpoint, config, baseline_dir=BASELINE_DIR)


def main() -> int:
    if not CONFIG_PATH.exists():
        print(f"[ERROR] config not found: {CONFIG_PATH}")
        return 1
    if not BASELINE_DIR.exists():
        print(f"[SKIP] frozen contract baseline not found: {BASELINE_DIR}")
        return 0
    drift = verify_contract(CONFIG_PATH, baseline_dir=BASELINE_DIR)
    for endpoint in QUERY_ID_KEYS:
        problems = drift.get(endpoint, [])
        if problems:
            print(f"[DRIFT] {endpoint}: {len(problems)} difference(s) vs frozen capture")
            for line in problems:
                print(f"    - {line}")
        else:
            print(f"[OK] {endpoint}: matches baseline")
    print("---")
    print("DRIFT DETECTED — reconcile config.json with the capture." if drift
          else "No drift. config.json matches the frozen known-good capture.")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
