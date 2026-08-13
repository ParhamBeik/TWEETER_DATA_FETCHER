from pathlib import Path

import importlib

import tweeter_data_fetcher.paths as paths


def test_saas_imports_canonical_fetcher_package():
    canonical = (
        Path(__file__).resolve().parents[4] / "twitter_fetcher" / "src" / "tweeter_data_fetcher"
    )
    assert canonical.is_dir()
    loaded = Path(paths.__file__).resolve()
    assert loaded.parent == canonical.resolve()


def test_tdf_project_root_env_overrides_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("TDF_PROJECT_ROOT", str(tmp_path))
    reloaded = importlib.reload(paths)
    assert reloaded.PROJECT_ROOT == tmp_path.resolve()
    assert reloaded.DATA_DIR == tmp_path.resolve() / "data"
    monkeypatch.delenv("TDF_PROJECT_ROOT", raising=False)
    importlib.reload(paths)
