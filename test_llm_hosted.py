"""Tests for the hosted (OpenAI-compatible) model path: Gemini, Groq, OpenRouter, ...

A fake HTTP server stands in for the provider, so no key, account or internet is needed.

    python -m pytest test_llm_hosted.py -q
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from services import llm, rag  # noqa: E402


class _Fake(BaseHTTPRequestHandler):
    mode = "ok"            # ok | fenced | reject_format | 429 | 401 | 404 | bad_shape
    content = "{}"
    seen = []              # (path, headers, body)

    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Fake.seen.append((self.path, dict(self.headers), body))
        m = _Fake.mode
        if m in ("429", "401", "404"):
            return self._reply(int(m), {"error": {"message": f"fake {m}"}})
        if m == "reject_format" and "response_format" in body:
            return self._reply(400, {"error": {"message": "response_format not supported"}})
        if m == "bad_shape":
            return self._reply(200, {"unexpected": True})
        text = "```json\n" + _Fake.content + "\n```" if m == "fenced" else _Fake.content
        self._reply(200, {"choices": [{"message": {"role": "assistant", "content": text}}]})


_server = HTTPServer(("127.0.0.1", 0), _Fake)
threading.Thread(target=_server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{_server.server_address[1]}/v1"
KEY = "test-secret-key-123"


@pytest.fixture(autouse=True)
def hosted(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setenv("LLM_BASE_URL", URL)
    monkeypatch.setenv("LLM_MODEL", "fake-model")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")
    llm._status_cache.update(at=0.0, info=None)
    _Fake.mode, _Fake.content = "ok", "{}"
    _Fake.seen.clear()
    rag.set_client_for_tests(None)
    yield
    rag.set_client_for_tests(None)
    llm._status_cache.update(at=0.0, info=None)


SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


def test_provider_selection(monkeypatch):
    assert llm.provider() == "openai"
    monkeypatch.delenv("LLM_API_KEY")
    assert llm.provider() == "ollama"
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert llm.provider() == "ollama"


def test_status_and_availability_without_calling_the_network(monkeypatch):
    assert llm.status()["server_running"] is True and llm.available() and rag.llm_available()
    assert llm.active_model() == "fake-model"
    monkeypatch.delenv("LLM_API_KEY")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert llm.status()["server_running"] is False and not llm.available()


def test_request_shape_and_parsing():
    _Fake.content = json.dumps({"a": 1})
    out = llm.call_structured("sys", "hello", SCHEMA, max_tokens=123)
    assert out == {"a": 1}
    path, headers, body = _Fake.seen[-1]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert body["model"] == "fake-model" and body["max_tokens"] == 123
    assert body["response_format"] == {"type": "json_object"}
    assert "reasoning_effort" not in body                      # only sent to Google's endpoint
    assert body["messages"][0]["role"] == "system" and "JSON schema" in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "hello"}


def test_markdown_fenced_json_is_accepted():
    _Fake.mode, _Fake.content = "fenced", json.dumps({"a": 2})
    assert llm.call_structured("s", "u", SCHEMA) == {"a": 2}


def test_provider_rejecting_json_mode_is_retried_without_it():
    _Fake.mode, _Fake.content = "reject_format", json.dumps({"a": 3})
    assert llm.call_structured("s", "u", SCHEMA) == {"a": 3}
    assert "response_format" in _Fake.seen[0][2] and "response_format" not in _Fake.seen[-1][2]


@pytest.mark.parametrize("mode,fragment", [("429", "rate limit"), ("401", "key was rejected"), ("404", "not found"), ("bad_shape", "unexpected")])
def test_errors_are_clear_and_never_leak_the_key(mode, fragment):
    _Fake.mode = mode
    with pytest.raises(llm.LLMError) as err:
        llm.call_structured("s", "u", SCHEMA)
    assert fragment in str(err.value).lower() and KEY not in str(err.value)


def test_unreachable_service_is_an_llm_error(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")
    with pytest.raises(llm.LLMError):
        llm.call_structured("s", "u", SCHEMA)


def test_non_json_reply_is_an_llm_error():
    _Fake.content = "Sure, here you go!"
    with pytest.raises(llm.LLMError):
        llm.call_structured("s", "u", SCHEMA)


def test_google_endpoint_gets_reasoning_effort_none(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", URL)  # fake server; emulate a Google URL via the path check
    body_seen = {}

    def fake_open(req, timeout=0):
        body_seen.update(json.loads(req.data))
        raise llm.urllib.error.URLError("stop")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(llm, "hosted_url", lambda: "https://generativelanguage.googleapis.com/v1beta/openai")
    with pytest.raises(llm.LLMError):
        llm.call_structured("s", "u", SCHEMA)
    assert body_seen["reasoning_effort"] == "none"


def test_full_analysis_through_the_hosted_path():
    """analyze() -> hosted model -> validation. The fake returns one good and one badly-cited item."""
    _Fake.content = json.dumps({
        "summary": "A neutral summary.",
        "applicable_provisions": [{"provision": "NDPS 20", "explanation": "Quantity categories.", "citations": ["C1", "C99"]}],
        "how_similar_cases_ended": [{"pattern": "Only an invented source supports this.", "citations": ["C99"]}],
        "procedural_options": [], "questions_for_lawyer": [{"question": "Was the FSL report filed?", "reason": "Pending."}]})
    case = {"sections": "", "special_laws": "NDPS 20(b)(ii)(B)", "bail_type": "Regular", "arrest_date": "2026-08-10",
            "charge_sheet_filed": True, "first_time_offender": True, "facts": "8.5 kg ganja. Phone 9876543210."}
    out = rag.analyze(case, 79, {"triggers": []})
    assert out["brief_status"] == "ok" and out["brief"].get("mode") != "extractive"
    assert out["brief"]["applicable_provisions"][0]["citations"] == ["C1"]
    assert out["brief"]["how_similar_cases_ended"][0]["uncited"] is True
    sent = _Fake.seen[-1][2]["messages"][1]["content"]
    assert "9876543210" not in sent                       # identifiers masked before leaving the server


def test_hosted_failure_falls_back_to_extractive_brief_with_reason():
    _Fake.mode = "429"
    case = {"sections": "302", "special_laws": "", "bail_type": "Regular", "arrest_date": "", "facts": "murder"}
    out = rag.analyze(case, 0, {"triggers": []})
    assert out["brief_status"] == "extractive" and out["brief"]["mode"] == "extractive"
    assert "rate limit" in out["brief_message"].lower()
