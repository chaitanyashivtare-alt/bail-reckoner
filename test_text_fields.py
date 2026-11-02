"""Tests for the rule-based chargesheet reader (services/text_fields.py). No AI, no network.

    python -m pytest test_text_fields.py -q
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp()
shutil.copy(ROOT / "bail_reckoner.db", Path(_TMP) / "t.db")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP) / 't.db'}"
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"
os.environ.pop("LLM_API_KEY", None)
os.environ["RAG_DISABLE_DENSE"] = "1"

from services import text_fields as tf  # noqa: E402

# A table-style chargesheet as a PDF reader returns it: labels and values on separate, wrapped lines.
TABLE_DOC = """FINAL REPORT / CHARGE SHEET
(Under Section 193 of the Bharatiya Nagarik Suraksha Sanhita, 2023)
IN THE COURT OF THE SPECIAL JUDGE (NDPS), PUNE
4. Sections of Law
NDPS Act 1985: Section 20(b)(ii)(B) and Section 29. Bharatiya Nyaya
Sanhita 2023: Section 61(2) (criminal conspiracy), Section 3(5) (common
intention).
5. Type of Final Report
Charge sheet; accused sent up for trial
Date of Arrest
10-08-2026 at 00:40 hrs
Previous Cases
None. No earlier FIR found.
BRIEF FACTS OF THE CASE
On 09-08-2026 the accused was found carrying 8.5 kg of ganja near a bus stop. The search was recorded.
REMARKS
Compliance with Sections 42, 50, 52, 55 and 57 of the NDPS Act is recorded. The prosecution has not relied on Section 37 of the NDPS Act.
"""


def test_table_style_document():
    d = tf.extract(TABLE_DOC)
    assert d["sections"] == "61(2), 3(5)"
    assert d["special_laws"] == "NDPS 20(b)(ii)(B); NDPS 29"
    assert d["arrest_date"] == "2026-08-10"
    assert d["charge_sheet_filed"] is True and d["first_time_offender"] is True
    assert d["court"] == "Special Judge (NDPS), Pune" and d["region"] == "Maharashtra"
    assert d["method"] == "rules" and d["missing_information"] == []


def test_sections_cited_in_the_story_are_not_treated_as_charges():
    """Remarks cite NDPS 37, 42, 50 ...; those are procedural references, not the charged sections."""
    d = tf.extract(TABLE_DOC)
    for procedural in ("37", "42", "50", "52", "55", "57"):
        assert f"NDPS {procedural}" not in d["special_laws"]


def test_read_with_list_and_ipc():
    d = tf.extract("FIR No. 74/2024\nSections: IPC 302 read with 34.\nACCUSED: X.\nARREST: 15-02-2024.\nCharge sheet filed on 10-05-2024.")
    assert d["sections"] == "302, 34" and d["arrest_date"] == "2024-02-15" and d["charge_sheet_filed"] is True


def test_not_filed_anticipatory_and_no_arrest():
    d = tf.extract("Sections: BNS 85, 115(2), 3(5); Dowry Prohibition Act 3, 4.\nARREST: None so far. Notice issued.\n"
                   "Charge sheet not filed. Accused applied for anticipatory bail.")
    assert d["sections"] == "85, 115(2), 3(5)"
    assert d["special_laws"] == "Dowry Prohibition Act 3; Dowry Prohibition Act 4"
    assert d["arrest_date"] == "" and d["charge_sheet_filed"] is False and d["bail_type"] == "Anticipatory"
    assert any("arrest date" in m for m in d["missing_information"])


def test_date_formats_and_unknowns_stay_empty():
    assert tf.extract("Accused was arrested on 3rd March 2026.")["arrest_date"] == "2026-03-03"
    assert tf.extract("Date of arrest: 2026-03-04")["arrest_date"] == "2026-03-04"
    assert tf.extract("Date of arrest: 31-02-2026")["arrest_date"] == ""        # impossible date is not guessed
    d = tf.extract("A short note with no legal details.")
    assert d["sections"] == "" and d["special_laws"] == "" and d["arrest_date"] == ""
    assert d["flight_risk"] is False and d["witness_risk"] is False and d["evidence_risk"] is False   # never guessed


def test_prior_cases_are_not_read_as_first_time():
    assert tf.extract("PRIOR CASES: Two earlier cases of theft registered in 2022 and 2024.")["first_time_offender"] is False


# ---------------------------------------------------------------- API behaviour
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


def test_analyze_fills_an_empty_form_from_the_document_and_says_so(client):
    r = client.post("/api/rag/analyze", json={"case_facts": TABLE_DOC, "bail_type": "Regular"})
    b = r.json()
    assert r.status_code == 200
    assert set(b["autofilled"]) >= {"sections", "special_laws", "arrest_date", "charge_sheet_filed"}
    assert b["effective_input"]["sections"] == "61(2), 3(5)" and b["custody_days"] > 0
    assert b["brief"]["applicable_provisions"], "provisions should now be found from the document's sections"
    assert "NDPS" in b["acts"]


def test_analyze_never_overwrites_what_the_user_entered(client):
    r = client.post("/api/rag/analyze", json={"case_facts": TABLE_DOC, "sections": "302", "arrest_date": "2026-01-01"})
    b = r.json()
    assert b["autofilled"] == {} and b["effective_input"]["sections"] == "302" and b["effective_input"]["arrest_date"] == "2026-01-01"
