"""The backup script serializes every caller, including manual runs."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backup_pg.sh"


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock is a Linux production tool")
def test_backup_script_skips_a_concurrent_run(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\nsleep 2\nprintf '%s\\n' '-- PostgreSQL database dump complete'\n"
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BACKUP_DIR": str(tmp_path / "backups"),
        "BACKUP_LOCK_FILE": str(tmp_path / "backup.lock"),
    }
    first = subprocess.Popen([str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.2)
    second = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True, check=False)
    first_stdout, first_stderr = first.communicate(timeout=10)

    assert second.returncode == 0
    assert "already running" in second.stderr
    assert first.returncode == 0, first_stderr
    assert "wrote " in first_stdout
