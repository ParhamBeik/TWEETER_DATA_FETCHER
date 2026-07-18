"""Regression guard for operational diagnostic path constants.

Diagnostic scripts live in ``diagnostics/`` and must:
- read config through canonical resolution
- write run output beside each script (``probe_runs/``, ``sniffer_runs/``)
- never resolve paths outside the repo

These tests pin those constants so the same class of breakage can\'t slip back in.
"""

import shutil
import unittest
from pathlib import Path

from diagnostics import verify_contract
from diagnostics import probe_sequence, probe_txid, traffic_sniffer

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DIAGNOSTICS = PROJECT_ROOT / "diagnostics"
CONFIG_JSON = PROJECT_ROOT / "config" / "config.json"


class ConfigPathTests(unittest.TestCase):
    """Every diagnostic script must read the canonical local config."""

    def test_config_paths_point_at_moved_config(self):
        for mod in (probe_txid, probe_sequence, traffic_sniffer, verify_contract):
            with self.subTest(module=mod.__name__):
                self.assertEqual(mod.CONFIG_PATH.resolve(), CONFIG_JSON)

    def test_config_dir_actually_exists(self):
        # If this fails, the config was moved again and the scripts are blind.
        self.assertTrue(CONFIG_JSON.parent.is_dir(), CONFIG_JSON.parent)

    def test_verify_contract_baseline_under_config(self):
        self.assertTrue(
            verify_contract.BASELINE_DIR.resolve().is_relative_to(
                PROJECT_ROOT / "config" / "known_good_contracts"
            ),
            verify_contract.BASELINE_DIR,
        )


class OutputDirTests(unittest.TestCase):
    """Run outputs must land under diagnostics/, never escape the repo."""

    def test_probe_run_dirs_are_under_diagnostics(self):
        for mod in (probe_txid, probe_sequence):
            with self.subTest(module=mod.__name__):
                self.assertEqual(
                    mod.PROBE_RUNS_DIR.resolve(), DIAGNOSTICS / "probe_runs"
                )

    def test_sniffer_default_output_is_under_diagnostics(self):
        out = traffic_sniffer._default_output_dir()  # mkdirs a timestamped leaf
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        self.assertEqual(out.resolve().parent, DIAGNOSTICS / "sniffer_runs")

    def test_no_constant_escapes_the_repo(self):
        constants = {
            "probe_txid.PROBE_RUNS_DIR": probe_txid.PROBE_RUNS_DIR,
            "probe_sequence.PROBE_RUNS_DIR": probe_sequence.PROBE_RUNS_DIR,
            "probe_txid.CONFIG_PATH": probe_txid.CONFIG_PATH,
            "traffic_sniffer.CONFIG_PATH": traffic_sniffer.CONFIG_PATH,
            "verify_contract.CONFIG_PATH": verify_contract.CONFIG_PATH,
        }
        for name, path in constants.items():
            with self.subTest(constant=name):
                self.assertTrue(
                    path.resolve().is_relative_to(PROJECT_ROOT),
                    f"{name} escapes repo: {path}",
                )


if __name__ == "__main__":
    unittest.main()
