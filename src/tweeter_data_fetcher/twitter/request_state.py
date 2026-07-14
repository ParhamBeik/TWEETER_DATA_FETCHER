from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class RequestStateStore:
    def __init__(self, state_dir: Path, *, failure_limit: int = 3):
        self.state_dir = state_dir
        self.failure_limit = failure_limit
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def load(self, filename: str, default: Any) -> Any:
        path = self.state_dir / filename
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, type(default)) else default
        except (OSError, ValueError):
            return default

    def save(self, filename: str, payload: Any) -> Path:
        path = self.state_dir / filename
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def mark_parameter(
        self,
        filename: str,
        endpoint: str,
        value: str,
        *,
        healthy: bool,
    ) -> dict[str, Any]:
        state = self.load(filename, {})
        endpoint_state = state.setdefault(endpoint, {})
        previous = endpoint_state.get(value, {})
        failures = 0 if healthy else int(previous.get("failures", 0) if isinstance(previous, dict) else 0) + 1
        result = {
            "status": "healthy" if healthy else ("stale" if failures >= self.failure_limit else "suspect"),
            "failures": failures,
        }
        endpoint_state[value] = result
        self.save(filename, state)
        return result
