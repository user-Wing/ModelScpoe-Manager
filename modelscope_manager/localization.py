from __future__ import annotations

import json
from pathlib import Path


class LocaleManager:
    def __init__(self, language: str = "zh_CN"):
        self.language = language
        self.translations: dict[str, str] = {}
        self.load(language)

    def load(self, language: str) -> None:
        self.language = language if language in {"zh_CN", "en_US"} else "zh_CN"
        path = Path(__file__).resolve().parent / "locales" / f"{self.language}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.translations = dict(payload.get("ui", {}))
        except (OSError, ValueError, TypeError):
            self.translations = {}

    def text(self, source: str) -> str:
        return self.translations.get(source, source)
