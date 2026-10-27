"""Local language-model client (Ollama). Free, and no case data leaves this machine.

Setup (once):
    1. Install Ollama from https://ollama.com
    2. ollama pull qwen2.5:7b-instruct
    3. Keep Ollama running; the app talks to it over HTTP.

Settings (.env, all optional):
    OLLAMA_URL=http://127.0.0.1:11434
    OLLAMA_MODEL=qwen2.5:7b-instruct
    OLLAMA_TIMEOUT=300          # seconds; local models on a CPU can be slow

Only the Python standard library is used, so no extra package is needed.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:7b-instruct"
_STATUS_TTL_SECONDS = 30.0
_status_cache: dict = {"at": 0.0, "info": None}


class LLMError(RuntimeError):
    """The local model could not be reached or did not return usable output."""


def base_url() -> str:
    return os.getenv("OLLAMA_URL", DEFAULT_URL).rstrip("/")


def model_name() -> str:
    return os.getenv("OLLAMA_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _same_model(installed: str, wanted: str) -> bool:
    """'qwen2.5' matches 'qwen2.5:latest'; 'qwen2.5:7b' matches only itself."""
    if installed == wanted:
        return True
    if ":" not in wanted:
        return installed == f"{wanted}:latest"
    return False


def status(force: bool = False) -> dict:
    """Is the server running, and is the chosen model downloaded? Cached briefly."""
    now = time.time()
    if not force and _status_cache["info"] is not None and now - _status_cache["at"] < _STATUS_TTL_SECONDS:
        return dict(_status_cache["info"])
    info = {"provider": "ollama", "url": base_url(), "model": model_name(),
            "server_running": False, "model_installed": False, "installed_models": [], "error": ""}
    try:
        with urllib.request.urlopen(base_url() + "/api/tags", timeout=3) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
        names = [m.get("name", "") for m in tags.get("models", [])]
        info["server_running"] = True
        info["installed_models"] = names
        info["model_installed"] = any(_same_model(n, model_name()) for n in names)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        info["error"] = type(exc).__name__
    _status_cache.update(at=now, info=info)
    return dict(info)


def available() -> bool:
    info = status()
    return bool(info["server_running"] and info["model_installed"])


def call_structured(system: str, user: str, schema: dict, max_tokens: int = 2000) -> dict:
    """Ask the local model for one JSON object that matches `schema`.

    Ollama constrains the output to the schema (`format`). The schema is also repeated in the
    system prompt, and the caller validates the result again, so a weak model cannot slip past.
    """
    body = {
        "model": model_name(),
        "stream": False,
        "format": schema,
        "options": {"temperature": 0.1, "num_predict": max_tokens},
        "messages": [
            {"role": "system", "content": (
                f"{system}\n\nReply with one JSON object that matches this JSON schema and nothing else:\n"
                f"{json.dumps(schema, ensure_ascii=False)}")},
            {"role": "user", "content": user},
        ],
    }
    request = urllib.request.Request(
        base_url() + "/api/chat",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout = float(os.getenv("OLLAMA_TIMEOUT", "300"))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LLMError(f"Local model not reachable ({type(exc).__name__})") from exc
    content = (data.get("message") or {}).get("content", "")
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise LLMError("Local model returned output that is not JSON") from exc
    if not isinstance(parsed, dict):
        raise LLMError("Local model returned JSON that is not an object")
    return parsed
