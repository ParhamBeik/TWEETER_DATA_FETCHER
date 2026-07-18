from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonFilesystem:
    @staticmethod
    def read(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
        except (OSError, ValueError):
            return default

    @staticmethod
    def write(path: Path, payload: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
