"""Tests for the local model client (services/llm.py).

No Ollama and no model are needed: a tiny fake Ollama server runs on localhost for the duration of the tests.

    python -m pytest test_llm.py -q
    (or run without pytest: python test_llm.py)
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from services import llm  # noqa: E402


class _FakeOllama(BaseHTTPRequestHandler):
    tags = {"models": [{"name": "qwen2.5:7b-instruct"}]}
    reply = "{}"
    seen = []

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send(_FakeOllama.tags)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        _FakeOllama.seen.append((self.path, body))
        if self.path == "/api/chat":
            self._send({"message": {"role": "assistant", "content": _FakeOllama.reply}})
        else:
            self.send_response(404)
            self.end_headers()


_server = HTTPServer(("127.0.0.1", 0), _FakeOllama)
threading.Thread(target=_server.serve_forever, daemon=True).start()
FAKE_URL = f"http://127.0.0.1:{_server.server_address[1]}"
os.environ["OLLAMA_URL"] = FAKE_URL
os.environ["OLLAMA_MODEL"] = "qwen2.5:7b-instruct"


def _reset_status_cache():
    llm._status_cache.update(at=0.0, info=None)


def test_status_reports_server_and_model():
    _reset_status_cache()
    info = llm.status(force=True)
    assert info["server_running"] is True
    assert info["model_installed"] is True
    assert llm.available() is True


def test_missing_model_is_reported_not_crashed():
    old = os.environ["OLLAMA_MODEL"]
    try:
        os.environ["OLLAMA_MODEL"] = "llama3.1:8b"
        _reset_status_cache()
        info = llm.status(force=True)
        assert info["server_running"] is True and info["model_installed"] is False
        assert llm.available() is False
    finally:
        os.environ["OLLAMA_MODEL"] = old
        _reset_status_cache()


def test_model_name_matching():
    assert llm._same_model("qwen2.5:latest", "qwen2.5")          # bare name means :latest
    assert llm._same_model("qwen2.5:7b-instruct", "qwen2.5:7b-instruct")
    assert not llm._same_model("qwen2.5:7b", "qwen2.5")          # explicit tag must match exactly
    assert not llm._same_model("qwen2.5:7b-instruct", "qwen2.5:7b")


def test_call_structured_sends_schema_and_returns_object():
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    _FakeOllama.reply = json.dumps({"a": 1})
    _FakeOllama.seen.clear()
    out = llm.call_structured("You are a test.", "hello", schema, max_tokens=100)
    assert out == {"a": 1}
    path, body = _FakeOllama.seen[-1]
    assert path == "/api/chat"
    assert body["format"] == schema                       # Ollama is asked to constrain output to the schema
    assert body["stream"] is False and body["model"] == "qwen2.5:7b-instruct"
    assert body["messages"][0]["role"] == "system" and "JSON schema" in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "hello"}
    assert body["options"]["num_predict"] == 100


def test_non_json_output_raises_llm_error():
    _FakeOllama.reply = "Sure! Here is your answer..."
    try:
        llm.call_structured("s", "u", {"type": "object"})
    except llm.LLMError:
        return
    raise AssertionError("expected LLMError")


def test_json_that_is_not_an_object_raises_llm_error():
    _FakeOllama.reply = json.dumps([1, 2, 3])
    try:
        llm.call_structured("s", "u", {"type": "object"})
    except llm.LLMError:
        return
    raise AssertionError("expected LLMError")


def test_unreachable_server_raises_llm_error_and_status_is_down():
    old = os.environ["OLLAMA_URL"]
    try:
        os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"   # nothing listens on port 9
        _reset_status_cache()
        assert llm.status(force=True)["server_running"] is False
        assert llm.available() is False
        try:
            llm.call_structured("s", "u", {"type": "object"})
        except llm.LLMError:
            return
        raise AssertionError("expected LLMError")
    finally:
        os.environ["OLLAMA_URL"] = old
        _reset_status_cache()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print("PASS", test.__name__)
    print(f"{len(tests)} tests passed")
