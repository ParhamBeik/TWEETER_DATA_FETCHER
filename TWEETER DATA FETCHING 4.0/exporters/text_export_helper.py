#!/usr/bin/env python3
"""
Translation-aware TXT export helpers.

This module is intentionally storage/output oriented. It does not mutate raw
payloads and does not change transport behavior.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


ALLOWED_ORIGINAL_LANGS = {"en", "fa"}


def _normalize_lang(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _normalize_translation_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}
    source_language = _normalize_lang(data.get("source_language"))
    destination_language = _normalize_lang(data.get("destination_language")) or "en"
    full_translation = _clean_text(data.get("translation"))
    preview_translation = _clean_text(data.get("preview_translation"))
    is_available = bool(payload.get("is_available", False))

    return {
        "source_language": source_language,
        "destination_language": destination_language,
        "translation": full_translation,
        "preview_translation": preview_translation,
        "is_available": is_available,
        "has_translation": bool(full_translation or preview_translation),
    }


def extract_translation_meta(raw_obj: Any, scan_limit: int = 500) -> Dict[str, Any]:
    """
    Extract grok translation payload from nested tweet-like objects.

    The payload can appear at different wrapper depths, so this uses a bounded
    graph scan and returns the first valid translation object found.
    """
    stack = [raw_obj]
    scanned = 0

    while stack and scanned < scan_limit:
        node = stack.pop()
        scanned += 1

        if isinstance(node, dict):
            if "grok_translated_post_with_availability" in node:
                payload = node.get("grok_translated_post_with_availability")
                if isinstance(payload, dict):
                    return _normalize_translation_payload(payload)

            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    stack.append(item)

    return {
        "source_language": None,
        "destination_language": "en",
        "translation": "",
        "preview_translation": "",
        "is_available": False,
        "has_translation": False,
    }


def choose_export_text(
    original_text: Any,
    source_language: Optional[str],
    translation_meta: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Choose display text for TXT export:
    - Keep original text for en/fa.
    - Otherwise prefer full translation, then preview translation.
    - If translation missing, keep original + UNKNOWN marker.
    """
    original = _clean_text(original_text)
    src_lang = _normalize_lang(source_language)
    meta = translation_meta if isinstance(translation_meta, dict) else {}
    meta_src_lang = _normalize_lang(meta.get("source_language"))
    effective_src_lang = src_lang or meta_src_lang

    full_translation = _clean_text(meta.get("translation"))
    preview_translation = _clean_text(meta.get("preview_translation"))
    translated_text = full_translation or preview_translation

    if effective_src_lang in ALLOWED_ORIGINAL_LANGS:
        return {"text": original, "note": None, "used_translation": False}

    if translated_text:
        src = effective_src_lang or "unknown"
        return {
            "text": translated_text,
            "note": f"[Translated from {src} -> en]",
            "used_translation": True,
        }

    if effective_src_lang and effective_src_lang not in ALLOWED_ORIGINAL_LANGS:
        return {
            "text": original,
            "note": f"[Translation from {effective_src_lang} -> en : UNKNOWN]",
            "used_translation": False,
        }

    return {"text": original, "note": None, "used_translation": False}
