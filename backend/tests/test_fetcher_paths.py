"""The engine resolves every path from the scratch root the runner hands it."""
import importlib

import fetcher.config as fetcher_config


def test_tdf_project_root_env_overrides_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("TDF_PROJECT_ROOT", str(tmp_path))
    reloaded = importlib.reload(fetcher_config)
    assert reloaded.PROJECT_ROOT == tmp_path.resolve()
    assert reloaded.DATA_DIR == tmp_path.resolve() / "data"
    assert reloaded.CONFIG_DIR == tmp_path.resolve() / "config"
    monkeypatch.delenv("TDF_PROJECT_ROOT", raising=False)
    importlib.reload(fetcher_config)
