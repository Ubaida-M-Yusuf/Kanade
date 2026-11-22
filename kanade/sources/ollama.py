"""Optional local LLM helper (Ollama). Only used for filenames that are still ambiguous.

The app works fully without it: every call fails soft and returns None.
"""
from __future__ import annotations

import json
import logging

import requests

from ..config import settings

log = logging.getLogger("kanade.ollama")


def _base() -> str:
    # "localhost" first tries IPv6 on Windows and waits ~2 s per attempt when nothing listens: use the IPv4 address
    return (settings().get("ollama_url") or "http://127.0.0.1:11434").rstrip("/").replace("//localhost", "//127.0.0.1")


def available() -> bool:
    if not settings().get("ollama_enabled"):
        return False
    try:
        r = requests.get(_base() + "/api/tags", timeout=(0.5, 2))
        return r.ok
    except requests.RequestException:
        return False


def models() -> list[str]:
    try:
        r = requests.get(_base() + "/api/tags", timeout=3)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def _model() -> str | None:
    m = settings().get("ollama_model")
    if m:
        return m
    ms = models()
    return ms[0] if ms else None


def suggest_title(compressed: str, readable: str, year: int | None) -> list[str]:
    """Ask the model for the probable official anime title(s) behind a compressed filename."""
    model = _model()
    if not model:
        return []
    prompt = (
        "You identify anime from compressed file names used by the AnimeThemes archive.\n"
        f"Compressed name: {compressed}\nSplit words: {readable}\n"
        + (f"Approximate year: {year}\n" if year else "")
        + "Reply with JSON only: {\"titles\": [\"<official romaji title>\", \"<english title>\"]} "
          "with your best 1-3 guesses for the anime series this opening/ending belongs to."
    )
    try:
        r = requests.post(_base() + "/api/generate", json={
            "model": model, "prompt": prompt, "stream": False, "format": "json",
            "options": {"temperature": 0.1},
        }, timeout=90)
        r.raise_for_status()
        data = json.loads(r.json().get("response") or "{}")
        titles = [t for t in data.get("titles", []) if isinstance(t, str) and t.strip()]
        return titles[:3]
    except Exception as e:
        log.info("Ollama suggestion failed for %s: %s", compressed, e)
        return []
