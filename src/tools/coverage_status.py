#!/usr/bin/env python3
"""Standalone coverage status report for the historical_live raw JSON datastore."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.shared.config.account_tiers import load_tier_config, ordered_accounts
from src.shared.data_pipeline.storage_manager import StorageManager
from src.shared.observability.coverage_inventory import CoverageInventory, ENDPOINTS
from src.shared.observability.pipeline_console import PipelineConsole


def _load_accounts(config_path: Path, only: Optional[List[str]]) -> List[str]:
    import json as json_mod

    with config_path.open("r", encoding="utf-8") as handle:
        config = json_mod.load(handle)
    account_map, _ = load_tier_config(config)
    all_accounts = ordered_accounts(account_map)
    if only:
        selected = {a.strip().lstrip("@").lower() for a in only}
        return [a for a in all_accounts if a.lower() in selected]
    return all_accounts


def run_coverage_status(
    *,
    config_path: str = "src/shared/config/config.json",
    only_accounts: Optional[List[str]] = None,
    only_endpoint: Optional[str] = None,
    output_format: str = "table",
    export_path: Optional[str] = None,
) -> int:
    storage = StorageManager(base_dir=PROJECT_ROOT, subsystem="historical_live")
    console = PipelineConsole("historical")
    accounts = _load_accounts(PROJECT_ROOT / config_path, only_accounts)
    endpoints = [only_endpoint] if only_endpoint else list(ENDPOINTS)

    inventory = CoverageInventory(storage)
    rows = []
    for account in accounts:
        for cov in inventory.scan_account(account, endpoints):
            rows.append(cov.to_row())

    if output_format == "json":
        payload = {
            "generated_at": storage.create_run_id().replace("run_", ""),
            "rows": rows,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if export_path:
            Path(export_path).write_text(text + "\n", encoding="utf-8")
            console.info(f"Wrote JSON report: {export_path}")
        else:
            print(text)
    else:
        console.coverage_table(rows, title="Historical/Live Raw Data Coverage")
        if export_path:
            payload = {"rows": rows}
            Path(export_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            console.info(f"Wrote report: {export_path}")
        else:
            reports_dir = storage.reports_dir
            reports_dir.mkdir(parents=True, exist_ok=True)
            default_path = reports_dir / f"coverage_{storage._jalali_batch_name()}.json"
            default_path.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            console.info(f"Wrote report: {default_path}")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Report raw JSON coverage per account and endpoint.")
    parser.add_argument("--account", action="append", default=[], help="Filter to account(s).")
    parser.add_argument("--endpoint", choices=list(ENDPOINTS), help="Filter to one endpoint.")
    parser.add_argument("--format", choices=["table", "json"], default="table")
    parser.add_argument("--export", dest="export_path", help="Optional export file path.")
    parser.add_argument("--config", default="src/shared/config/config.json")
    args = parser.parse_args()
    only = [p.strip() for v in args.account for p in v.split(",") if p.strip()] or None
    sys.exit(
        run_coverage_status(
            config_path=args.config,
            only_accounts=only,
            only_endpoint=args.endpoint,
            output_format=args.format,
            export_path=args.export_path,
        )
    )


if __name__ == "__main__":
    main()
