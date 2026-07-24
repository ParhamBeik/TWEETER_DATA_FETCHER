from pathlib import Path


class TextExporter:
    @staticmethod
    def write(path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
