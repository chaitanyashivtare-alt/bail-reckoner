"""Offline tests for the RAG pipeline. No server, no API key and no network needed.

    pip install pytest httpx
    python -m pytest test_rag.py -q

The local model (Ollama) is replaced by a fake, so these tests check OUR logic (retrieval, masking,
citation checks, guardrails, fallbacks, lawyer matching, API wiring), not the quality of the model's writing.
The HTTP client itself is tested in test_llm.py.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Run the API against a throwaway copy of the database so tests never touch the real one.
_TMP = tempfile.mkdtemp()
shutil.copy(ROOT / "bail_reckoner.db", Path(_TMP) / "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP) / 'test.db'}"
# Nothing listens on this port, so the local model counts as unavailable in these tests.
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"
os.environ["RAG_DISABLE_DENSE"] = "1"

from services import lawyer_match, rag, rag_retrieval  # noqa: E402


# ---------------------------------------------------------------- fake local model
class FakeLLM:
    """Stands in for services.llm: records each call and returns a fixed structured answer."""

    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def __call__(self, system, user, tool, max_tokens):
        self.calls.append({"system": system, "user": user, "tool": tool, "max_tokens": max_tokens})
        return dict(self.payload)


@pytest.fixture(autouse=True)
def _reset_client():
    rag.set_client_for_tests(None)
    yield
    rag.set_client_for_tests(None)


# ---------------------------------------------------------------- retrieval
def test_parse_sections_and_generic_filter():
    assert rag_retrieval.parse_sections("376(2)(n), 120b; 34") == {"376", "120B", "34"}
    assert rag_retrieval.parse_sections("NDPS 20(b)(ii)(B)") >= {"20"}


def test_ipc_bns_mapping_comes_from_offenses_table():
    ix = rag_retrieval.get_index()
    assert "103" in ix.expand_sections({"302"})  # IPC 302 murder -> BNS 103


def test_pocso_murder_query_does_not_return_unrelated_forgery_case():
    """The screenshot case: IPC 302 + POCSO 6 used to return a 419/420/467/468/471 forgery case."""
    results = rag_retrieval.get_index().search(
        "Accused held for murder and sexual offence against a minor",
        sections_text="302", special_laws_text="POCSO 6", bail_type="Anticipatory", top_k=5)
    assert len(results) == 5
    for r in results:
        assert "POCSO" in r["acts"] or "302" in r["ipc_sections"], r["case_title"]
    assert all(r["match_reasons"] for r in results)


def test_ndps_query_returns_ndps_cases_with_excerpts():
    results = rag_retrieval.get_index().search(
        "Found with 8.5 kg ganja, intermediate quantity, no prior case",
        special_laws_text="NDPS 20(b)(ii)(B)", top_k=5)
    assert results and all("NDPS" in r["acts"] for r in results)
    assert all(r["excerpts"] and r["excerpts"][0]["text"] for r in results)


def test_empty_query_returns_nothing():
    assert rag_retrieval.get_index().search("") == []


# ---------------------------------------------------------------- privacy
def test_mask_pii_hides_identifiers_and_listed_names():
    text = "Name: Ramesh Kadam\nCall 9876543210 or ramesh@mail.com. PAN ABCDE1234F. Aadhaar 1234 5678 9012.\nRamesh was arrested by Mr Verma."
    masked, counts = rag.mask_pii(text, redact_terms=["Verma"])
    for secret in ("Ramesh Kadam", "9876543210", "ramesh@mail.com", "ABCDE1234F", "1234 5678 9012", "Verma"):
        assert secret not in masked, secret
    assert counts["phone"] == 1 and counts["listed_terms"] == 1


def test_mask_pii_handles_table_style_labels_on_separate_lines():
    text = "Name\nVikram Joshi (fictional)\nFather's Name\nSudhir Joshi (fictional)\nOccupation\nCab driver\nAddress\nFlat 204, Sai Residency, Wanowrie, Pune\n411040 area.\nDate of Arrest\n10-08-2026"
    masked, _ = rag.mask_pii(text)
    for secret in ("Vikram", "Sudhir", "Sai Residency"):
        assert secret not in masked, secret
    assert "Cab driver" in masked and "10-08-2026" in masked   # non-identifying facts survive


# ---------------------------------------------------------------- brief checks
def _case_input():
    return {"sections": "", "special_laws": "NDPS 20(b)(ii)(B)", "bail_type": "Regular", "arrest_date": "2026-08-10",
            "charge_sheet_filed": True, "first_time_offender": True, "flight_risk": False, "witness_risk": False,
            "evidence_risk": False, "facts": "Accused found with 8.5 kg ganja. Call 9876543210. No previous case."}


def test_brief_validation_drops_predictions_and_flags_bad_citations():
    payload = {
        "summary": "A first-time accused was found with an intermediate quantity of ganja.",
        "applicable_provisions": [{"provision": "NDPS 20(b)(ii)(B)", "explanation": "Intermediate quantity.", "citations": ["C1", "C99"]}],
        "how_similar_cases_ended": [
            {"pattern": "The accused will be granted bail because of the quantity.", "citations": ["C1"]},
            {"pattern": "In C2 the court examined delay in FSL reports.", "citations": ["C99"]}],
        "procedural_options": [{"option": "Raise Section 50 compliance", "basis": "Search was recorded.", "citations": ["C1"]}],
        "questions_for_lawyer": [{"question": "Was the FSL report filed in time?", "reason": "Pending per chargesheet."}],
    }
    fake = FakeLLM(payload)
    rag.set_client_for_tests(fake)
    similar = rag_retrieval.get_index().search("ganja intermediate quantity", special_laws_text="NDPS", top_k=3)
    brief = rag.generate_brief(_case_input(), 79, {"triggers": []}, similar, rag.reference_sources("", "NDPS 20", "Regular", True, {"Narcotics"}))
    assert brief["checks"]["dropped_prediction"] == 1
    assert brief["checks"]["invalid_citations_removed"] == 2          # both C99 references
    assert brief["checks"]["uncited_items"] == 1                       # second pattern lost its only citation
    assert brief["applicable_provisions"][0]["citations"] == ["C1"]
    assert len(brief["how_similar_cases_ended"]) == 1 and brief["how_similar_cases_ended"][0]["uncited"]
    # personal data never reached the model
    sent = fake.calls[0]["user"]
    assert "9876543210" not in sent and "[PHONE]" in sent
    # the model was asked for the structured brief
    assert fake.calls[0]["tool"]["name"] == "submit_case_brief"


def test_sources_include_statute_rules_and_landmarks():
    ref = rag.reference_sources("302", "POCSO 6", "Regular", True, {"Murder", "Sexual Offense"})
    assert ref["rules"] and ref["landmarks"] and ref["checklist"]
    assert any("Murder" in (o.get("offense_name") or "") for o in ref["offenses"])


def test_analyze_without_local_model_still_returns_retrieval():
    out = rag.analyze(_case_input(), 79, {"triggers": []})
    assert out["brief"] is None and out["brief_status"] == "llm_unavailable"
    assert out["similar_cases"] and out["outcome_summary"]["retrieved"] == len(out["similar_cases"])


def test_analyze_survives_llm_failure():
    def boom(system, user, tool, max_tokens):
        raise RuntimeError("down")
    rag.set_client_for_tests(boom)
    out = rag.analyze(_case_input(), 79, {"triggers": []})
    assert out["brief_status"] == "error" and out["similar_cases"]


def test_extract_fields_masks_text_before_sending():
    fake = FakeLLM({"sections": "", "special_laws": "NDPS 20(b)(ii)(B)", "bail_type": "Regular",
                       "case_summary": "x", "legal_issues": "y", "retrieval_query": "z"})
    rag.set_client_for_tests(fake)
    data = rag.extract_fields("Name: Vikram Joshi\nPhone 9876543210\nGanja recovered.", redact_terms=["Joshi"])
    sent = fake.calls[0]["user"]
    assert "Vikram Joshi" not in sent and "9876543210" not in sent and "Joshi" not in sent
    assert data["special_laws"].startswith("NDPS") and data["privacy"]["masked"]


# ---------------------------------------------------------------- lawyers
def test_lawyer_match_explains_itself_and_prefers_relevant_specialist():
    top = lawyer_match.match(acts=["NDPS"], categories=["Narcotics"], court="Special Court, Pune", region="Maharashtra", languages=["Marathi"])
    assert top[0]["id"] in (2, 5)
    assert any("NDPS" in r for r in top[0]["matchReasons"])
    assert any("Maharashtra" in r for r in top[0]["matchReasons"])
    assert all(l["sample"] for l in top)


def test_lawyer_match_pocso_and_filters():
    assert lawyer_match.match(acts=["POCSO"], region="Maharashtra")[0]["id"] == 7
    assert all(l["experience"] >= 15 for l in lawyer_match.match(min_experience=15, top_k=12))


# ---------------------------------------------------------------- API wiring
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


def test_api_analyze_with_fake_local_model(client):
    payload = {"summary": "S.", "applicable_provisions": [{"provision": "NDPS", "explanation": "e", "citations": ["C1"]}],
               "how_similar_cases_ended": [{"pattern": "In C1 the court looked at quantity.", "citations": ["C1"]}],
               "procedural_options": [], "questions_for_lawyer": [{"question": "q?", "reason": "r"}]}
    rag.set_client_for_tests(FakeLLM(payload))
    r = client.post("/api/rag/analyze", json={"special_laws": "NDPS 20(b)(ii)(B)", "bail_type": "Regular", "arrest_date": "2026-08-10",
                                              "case_facts": "8.5 kg ganja recovered", "region": "Maharashtra", "court": "Special Court Pune"})
    body = r.json()
    assert r.status_code == 200, body
    assert body["brief_status"] == "ok" and "C1" in body["brief"]["sources"]
    assert body["lawyers"] and body["assessment"]["status"] == "Review required" and body["custody_days"] > 0


def test_api_without_local_model_degrades_gracefully(client):
    r = client.post("/api/rag/analyze", json={"sections": "302", "case_facts": "murder case"})
    assert r.status_code == 200 and r.json()["brief_status"] == "llm_unavailable" and r.json()["similar_cases"]
    assert client.post("/api/rag/extract-fields", json={"text": "abc"}).status_code == 503
    assert client.post("/api/rag/analyze", json={}).status_code == 400


def test_api_existing_review_endpoint_unchanged(client):
    r = client.post("/api/case/review", json={"sections": "302", "case_facts": "murder"})
    assert r.status_code == 200 and "similar_cases" in r.json()


def test_api_lawyers(client):
    assert len(client.get("/api/lawyers").json()["lawyers"]) == 12
    assert client.get("/api/lawyers/5").json()["name"].startswith("Adv.")
    assert client.get("/api/lawyers/999").status_code == 404
