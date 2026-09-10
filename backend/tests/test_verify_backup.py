"""The backup verifier must catch a truncated dump without restoring it.

Unit tests: they run the real script against synthetic gzips. Restoring onto
Postgres would be an integration test of a destructive path this script exists
to avoid.
"""
from __future__ import annotations

import gzip
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_backup.sh"
COMPLETE = b"--\n-- PostgreSQL database dump complete\n--\n"


def _write_gz(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as fh:
        fh.write(payload)
    return path


def _run(dump: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), str(dump)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_verify_backup_accepts_a_complete_dump(tmp_path):
    dump = _write_gz(tmp_path / "twitter_saas_ok.sql.gz", COMPLETE)
    result = _run(dump)
    assert result.returncode == 0, result.stderr
    assert "ok " in result.stdout


def test_verify_backup_rejects_a_truncated_dump(tmp_path):
    dump = _write_gz(tmp_path / "twitter_saas_bad.sql.gz", b"CREATE TABLE tweets;\n")
    result = _run(dump)
    assert result.returncode == 1
    assert "completion marker" in result.stderr


def test_verify_backup_rejects_a_missing_file(tmp_path):
    result = _run(tmp_path / "nope.sql.gz")
    assert result.returncode == 1
    assert "no dump" in result.stderr
