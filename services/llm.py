"""Language-model client with two free options. Pick one in .env; nothing paid is required.

A) Free hosted model (fastest to set up, works on any laptop). Any OpenAI-compatible service:
       LLM_API_KEY=<key>                        # Google AI Studio (Gemini) or Groq, both have free tiers
       LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai   (default, Gemini)
       LLM_MODEL=gemini-2.5-flash
   Groq:  LLM_BASE_URL=https://api.groq.com/openai/v1   LLM_MODEL=llama-3.3-70b-versatile
   Text you submit (after identifier masking) is sent to that service. Use fictional or consented data.

B) Local model with Ollama. No case data leaves this machine.

Setup of B (once):
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


DEFAULT_HOSTED_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_HOSTED_MODEL = "gemini-2.5-flash"


def provider() -> str:
    """'openai' (hosted, OpenAI-compatible) when LLM_API_KEY is set, otherwise 'ollama'. LLM_PROVIDER overrides."""
    forced = os.getenv("LLM_PROVIDER", "").strip().lower()
    if forced in ("ollama", "openai"):
        return forced
    return "openai" if os.getenv("LLM_API_KEY", "").strip() else "ollama"


def hosted_url() -> str:
    return os.getenv("LLM_BASE_URL", DEFAULT_HOSTED_URL).rstrip("/")


def hosted_model() -> str:
    return os.getenv("LLM_MODEL", DEFAULT_HOSTED_MODEL).strip() or DEFAULT_HOSTED_MODEL


def active_model() -> str:
    return hosted_model() if provider() == "openai" else model_name()


def _same_model(installed: str, wanted: str) -> bool:
    """'qwen2.5' matches 'qwen2.5:latest'; 'qwen2.5:7b' matches only itself."""
    if installed == wanted:
        return True
    if ":" not in wanted:
        return installed == f"{wanted}:latest"
    return False


def status(force: bool = False) -> dict:
    """Is the server running, and is the chosen model downloaded? Cached briefly."""
    if provider() == "openai":
        has_key = bool(os.getenv("LLM_API_KEY", "").strip())
        return {"provider": "hosted", "url": hosted_url(), "model": hosted_model(), "server_running": has_key,
                "model_installed": has_key, "installed_models": [], "error": "" if has_key else "LLM_API_KEY is not set"}
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
    if provider() == "openai":
        return _call_hosted(system, user, schema, max_tokens)
    return _call_ollama(system, user, schema, max_tokens)


def _call_ollama(system: str, user: str, schema: dict, max_tokens: int = 2000) -> dict:
    """Ask the local model for one JSON object that matches `schema`.

    Ollama constrains the output to the schema (`format`). The schema is also repeated in the
    system prompt, and the caller validates the result again, so a weak model cannot slip past.
    """
    body = {
        "model": model_name(),
        "stream": False,
        "format": schema,
        # num_ctx must hold the whole prompt (about 5k tokens for a chargesheet); the default can silently cut it.
        "options": {"temperature": 0.1, "num_predict": max_tokens, "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192"))},
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


# ----------------------------------------------------------------------------
# Hosted, OpenAI-compatible endpoint (Gemini, Groq, OpenRouter, ...)
# ----------------------------------------------------------------------------

def _extract_json(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text
    try:
        parsed = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("The model returned output that is not JSON")
        try:
            parsed = json.loads(text[start:end + 1])
        except ValueError as exc:
            raise LLMError("The model returned output that is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise LLMError("The model returned JSON that is not an object")
    return parsed


def _call_hosted(system: str, user: str, schema: dict, max_tokens: int) -> dict:
    key = os.getenv("LLM_API_KEY", "").strip()
    if not key:
        raise LLMError("LLM_API_KEY is not set")
    url = hosted_url() + "/chat/completions"
    body = {
        "model": hosted_model(),
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                f"{system}\n\nReply with one JSON object that matches this JSON schema and nothing else:\n"
                f"{json.dumps(schema, ensure_ascii=False)}")},
            {"role": "user", "content": user},
        ],
    }
    if "generativelanguage.googleapis.com" in url:
        body["reasoning_effort"] = "none"  # keep Gemini's hidden "thinking" from eating the output budget
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    timeout = float(os.getenv("LLM_TIMEOUT", "90"))
    data = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                err = json.loads(exc.read().decode("utf-8", "replace"))
                err = err[0] if isinstance(err, list) and err else err
                detail = str((err.get("error") or {}).get("message", ""))[:200]
            except Exception:
                pass
            # Some providers reject optional parameters: drop them and retry once each.
            if exc.code == 400 and "response_format" in body:
                body.pop("response_format")
                continue
            if exc.code == 400 and "reasoning_effort" in body:
                body.pop("reasoning_effort")
                continue
            if exc.code in (401, 403):
                raise LLMError("The API key was rejected. Check LLM_API_KEY and LLM_BASE_URL.") from exc
            if exc.code == 404:
                raise LLMError(f"Model or URL not found. Check LLM_MODEL and LLM_BASE_URL. {detail}".strip()) from exc
            if exc.code == 429:
                raise LLMError("Free-tier rate limit reached. Wait a minute and try again.") from exc
            raise LLMError(f"The model service returned HTTP {exc.code}. {detail}".strip()) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LLMError(f"Model service not reachable ({type(exc).__name__}). Check the internet connection.") from exc
    if data is None:
        raise LLMError("The model service rejected the request")
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("The model service returned an unexpected response") from exc
    return _extract_json(content)
