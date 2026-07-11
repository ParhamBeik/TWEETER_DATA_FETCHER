"""
Debug logging module for SearchTimeline pagination diagnostics.

Provides dual-output logging (console + file) and per-request dumps
with automatic secret masking for security.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

# Never write these to disk in cleartext
SECRET_KEYS = {"authorization", "x-csrf-token", "cookie", "x-client-transaction-id"}


def _mask(v: str, keep: int = 6) -> str:
    """Mask sensitive values, keeping only first N chars."""
    return f"{v[:keep]}…[{len(v)} chars]" if v else ""


def setup_logging(run_name: str, logs_root: Path) -> logging.Logger:
    """
    Set up dual-output logging for a search run.
    
    Args:
        run_name: Name of the search (used as subfolder)
        logs_root: Root logs directory (e.g., Path("logs"))
    
    Returns:
        Logger with console (INFO) and file (DEBUG) handlers.
        Logger has a `run_dir` attribute pointing to the timestamped run folder.
    """
    run_dir = logs_root / run_name / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(run_name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # Console handler: clean terminal output (INFO level)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    # File handler: full detail (DEBUG level)
    fileh = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
    fileh.setLevel(logging.DEBUG)
    fileh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
    logger.addHandler(fileh)

    # Attach run_dir for use by dump_request
    logger.run_dir = run_dir
    return logger


def dump_request(
    logger: logging.Logger,
    page: int,
    *,
    method: str,
    url: str,
    params: Dict[str, Any],
    sent_headers: Dict[str, str],
    response: Any,
) -> Dict[str, Any]:
    """
    Dump full request/response details to JSON file and log summary.
    
    Args:
        logger: Logger instance from setup_logging()
        page: Page number (0 for warmup, 1+ for pagination)
        method: HTTP method (e.g., "GET")
        url: Request URL
        params: Query parameters dict
        sent_headers: Headers dict sent with request
        response: requests.Response object
    
    Returns:
        Dict containing the full request/response record
    """
    # Check if tx-id was present (case-insensitive)
    lower_keys = {k.lower() for k in sent_headers}
    tx_id_present = "x-client-transaction-id" in lower_keys

    # Mask sensitive headers
    safe_headers = {
        k: (_mask(v) if k.lower() in SECRET_KEYS else v)
        for k, v in sent_headers.items()
    }

    # Build record
    record = {
        "page": page,
        "request": {
            "method": method,
            "url": url,
            "params": params,
            "headers": safe_headers,
            "tx_id_present": tx_id_present,
        },
        "response": {
            "status": response.status_code,
            "elapsed_ms": int(response.elapsed.total_seconds() * 1000),
            "headers": dict(response.headers),
            "body_preview": response.text[:2000],
        },
    }

    # Write to JSON file
    requests_dir = logger.run_dir / "requests"
    requests_dir.mkdir(exist_ok=True)
    json_path = requests_dir / f"page_{page:02d}.json"
    json_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    # Log summary to console
    flag = "OK " if response.status_code == 200 else "FAIL"
    tx_flag = "yes" if tx_id_present else "no"
    logger.info(
        f"  page={page} [{flag}] status={response.status_code} "
        f"tx_id={tx_flag} {record['response']['elapsed_ms']}ms"
    )

    # Log warnings for non-200 responses
    if response.status_code != 200:
        body_preview = response.text[:300].strip()
        logger.warning(f"  └─ body: {body_preview}")
        
        # Log rate-limit headers if present
        for header in ("x-rate-limit-remaining", "x-rate-limit-reset"):
            if header in response.headers:
                logger.warning(f"  └─ {header}: {response.headers[header]}")

    return record
