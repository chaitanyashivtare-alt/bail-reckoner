"""RAG orchestration for NyaySetu: field extraction, retrieval, and a cited case brief.

Design rules
------------
* Claude only writes from numbered sources we hand it: judgment excerpts [C#], statute table
  rows [S#], custody rules [R#], landmark judgments [L#], checklist items [K#].
* The server, not the model, counts outcomes and validates citations. Items whose citations do
  not exist are flagged; items that predict or recommend a bail outcome are dropped.
* If no API key is configured or the call fails, the caller still gets retrieval results and the
  rule-based triggers; only the written brief is missing.
* Personal identifiers are masked before text leaves the server (see ``mask_pii``).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

from services import rag_retrieval

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "bail_reckoner.db"

DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_FACT_CHARS = 9000

DISCLAIMER = ("Informational research support only. This is not legal advice and does not predict, "
              "approve or reject bail. Verify every point against current law and official records "
              "with a qualified lawyer.")

# Phrases that turn explanation into prediction. Items containing them are removed.
_PREDICTION_RE = re.compile(
    r"\b(will|would|should|must|is likely to|are likely to|likely to|probably)\s+(be\s+)?(granted|get|receive|denied|rejected|refused)\b"
    r"|\bbail\s+(will|would)\b|\b(chances?|odds)\s+of\s+(getting\s+)?bail\b|\bI\s+recommend\b",
    re.I)


# ----------------------------------------------------------------------------
# Privacy: mask identifiers before sending text to a hosted model
# ----------------------------------------------------------------------------
_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
_AADHAAR = re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_LABELLED = re.compile(
    r"(?im)^(\s*(?:name|father'?s name|mother'?s name|husband'?s name|address|resident of|r/o)\s*[:\-]\s*)(.+)$")
# Table-style PDFs put the label and the value on separate lines.
_LABEL_THEN_VALUE = re.compile(
    r"(?im)^(\s*(?:name|father'?s name|mother'?s name|husband'?s name)\s*\n)(.+)$")
_ADDRESS_THEN_VALUE = re.compile(r"(?im)^(\s*address\s*\n)(.+(?:\n(?!\s*(?:date|custody|previous|bail)\b).+)?)$")


def mask_pii(text: str, redact_terms: list[str] | None = None) -> tuple[str, dict]:
    """Best-effort masking. Names in running prose cannot be found reliably without the user's
    help, so ``redact_terms`` lets the user list names/places to hide. Returns (masked, counts)."""
    counts: Counter = Counter()

    def sub(rx, token, label, s):
        s, n = rx.subn(token, s)
        counts[label] += n
        return s

    out = text or ""
    for term in sorted({t.strip() for t in (redact_terms or []) if t and len(t.strip()) >= 2}, key=len, reverse=True):
        out, n = re.subn(re.escape(term), "[REDACTED]", out, flags=re.I)
        counts["listed_terms"] += n
    out = sub(_EMAIL, "[EMAIL]", "email", out)
    out = sub(_AADHAAR, "[ID-NUMBER]", "aadhaar_like", out)
    out = sub(_PHONE, "[PHONE]", "phone", out)
    out = sub(_PAN, "[PAN]", "pan", out)
    out, n = _LABELLED.subn(lambda m: m.group(1) + "[REDACTED]", out)
    counts["labelled_lines"] += n
    for rx in (_LABEL_THEN_VALUE, _ADDRESS_THEN_VALUE):
        out, n = rx.subn(lambda m: m.group(1) + "[REDACTED]", out)
        counts["labelled_lines"] += n
    return out, {k: v for k, v in counts.items() if v}


# ----------------------------------------------------------------------------
# Claude client (injectable for tests)
# ----------------------------------------------------------------------------
_client_override = None


def set_client_for_tests(client) -> None:
    global _client_override
    _client_override = client


def llm_available() -> bool:
    return _client_override is not None or bool(os.getenv("ANTHROPIC_API_KEY"))


def _get_client():
    if _client_override is not None:
        return _client_override
    import anthropic
    return anthropic.Anthropic(timeout=90.0, max_retries=2)


def _call_tool(system: str, user: str, tool: dict, max_tokens: int = 4000) -> dict:
    client = _get_client()
    resp = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)
    raise RuntimeError("Model did not return structured output")


# ----------------------------------------------------------------------------
# Step 1: pull structured fields out of the uploaded chargesheet text
# ----------------------------------------------------------------------------
EXTRACT_TOOL = {
    "name": "record_case_fields",
    "description": "Record the facts stated in an Indian police chargesheet / FIR / order text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sections": {"type": "string", "description": "IPC/BNS section numbers only, comma separated, e.g. '302, 34'. Empty if none."},
            "special_laws": {"type": "string", "description": "Special-Act sections, e.g. 'NDPS 20(b)(ii)(B); NDPS 29'. Empty if none."},
            "bail_type": {"type": "string", "enum": ["Regular", "Anticipatory", "Default/statutory", "Interim"],
                          "description": "Only the type the document itself indicates; otherwise 'Regular'."},
            "arrest_date": {"type": "string", "description": "YYYY-MM-DD, empty if not stated or no arrest yet."},
            "charge_sheet_filed": {"type": "boolean"},
            "first_time_offender": {"type": "boolean", "description": "True only if the document says there are no previous cases."},
            "flight_risk": {"type": "boolean", "description": "True only if the document records it as a concern."},
            "witness_risk": {"type": "boolean", "description": "True only if the document records it as a concern."},
            "evidence_risk": {"type": "boolean", "description": "True only if the document records tampering risk as a concern."},
            "court": {"type": "string"},
            "region": {"type": "string", "description": "Indian state of the court / police station, empty if unclear."},
            "case_summary": {"type": "string", "description": "3-5 neutral sentences: what is alleged, evidence recovered, stage of the case."},
            "legal_issues": {"type": "string", "description": "Legal questions a bail court would examine, as a short sentence list."},
            "retrieval_query": {"type": "string", "description": "One compact paragraph of the legally relevant facts for searching past judgments. No personal names."},
            "missing_information": {"type": "array", "items": {"type": "string"},
                                    "description": "Important facts the document does not state."},
        },
        "required": ["sections", "special_laws", "bail_type", "case_summary", "legal_issues", "retrieval_query"],
    },
}

EXTRACT_SYSTEM = (
    "You extract facts from Indian criminal-case documents for a legal research tool. Record only what the "
    "document states. Never guess dates, sections or risk factors; leave them empty or false when absent. "
    "Convert dates to YYYY-MM-DD. Do not give legal opinions or predictions."
)


def extract_fields(text: str, redact_terms: list[str] | None = None) -> dict:
    if not llm_available():
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    masked, counts = mask_pii(text[:MAX_FACT_CHARS * 2], redact_terms)
    data = _call_tool(EXTRACT_SYSTEM, f"<document>\n{masked}\n</document>", EXTRACT_TOOL, max_tokens=1500)
    data["privacy"] = {"masked": counts}
    return data


# ----------------------------------------------------------------------------
# Step 2: reference data from the app's own database (statutes, rules, landmark cases)
# ----------------------------------------------------------------------------

def reference_sources(sections_text: str, special_laws_text: str, bail_type: str,
                      first_time: bool, categories: set[str]) -> dict:
    out = {"offenses": [], "rules": [], "landmarks": [], "checklist": []}
    if not DB_PATH.exists():
        return out
    secs = {s for s in rag_retrieval.parse_sections(sections_text)}
    spec = {s for s in rag_retrieval.parse_sections(special_laws_text)}
    acts = rag_retrieval.detect_acts(f"{special_laws_text} {sections_text}")
    act_words = {"NDPS": "narcotic", "POCSO": "children", "IT_ACT": "information technology", "PC_ACT": "corruption",
                 "UAPA": "unlawful", "PMLA": "money", "ARMS": "arms", "DOWRY": "dowry"}
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            for r in conn.execute("SELECT * FROM offenses"):
                keys = rag_retrieval.parse_sections(r["ipc_section"] or "") | rag_retrieval.parse_sections(r["bns_section"] or "")
                if keys & secs:
                    out["offenses"].append(dict(r))
            for r in conn.execute("SELECT * FROM special_acts_offenses"):
                word = next((w for a, w in act_words.items() if a in acts and w in (r["act_name"] or "").lower()), None)
                if word and rag_retrieval.parse_sections(r["section"] or "") & (spec | secs):
                    out["offenses"].append({**dict(r), "ipc_section": r["section"], "bns_section": "", "act": r["act_name"]})
            for r in conn.execute("SELECT * FROM custody_rules"):
                if r["applies_to"] == "all" or (first_time and r["applies_to"] == "first_time_offender"):
                    out["rules"].append(dict(r))
            cat_keys = set()
            if categories & {"Murder", "Attempt to Murder", "Theft or Robbery", "Kidnapping", "Narcotics", "Extortion"}:
                cat_keys.add("general")
            if "Fraud or Cheating" in categories:
                cat_keys.add("economic")
            if categories & {"Dowry Harassment", "Sexual Offense"}:
                cat_keys.add("women")
            if "Sexual Offense" in categories:
                cat_keys.add("children")
            cat_keys = cat_keys or {"general"}
            for r in conn.execute("SELECT * FROM judgments"):
                applicable = {c.strip() for c in (r["applicable_categories"] or "").split(",")}
                if applicable & cat_keys or "general" in applicable:
                    out["landmarks"].append(dict(r))
            bt = (bail_type or "regular").lower().split("/")[0]
            for r in conn.execute("SELECT * FROM procedural_checklist"):
                if (r["bail_type"] or "").lower() in (bt, "all", "") and (r["category"] in {"general"} | {c.lower() for c in cat_keys}):
                    out["checklist"].append(dict(r))
    except sqlite3.Error:
        pass
    out["landmarks"] = out["landmarks"][:5]
    out["checklist"] = out["checklist"][:8]
    out["offenses"] = out["offenses"][:6]
    return out


# ----------------------------------------------------------------------------
# Step 3: build numbered sources and ask Claude for a cited brief
# ----------------------------------------------------------------------------

def build_sources(similar: list[dict], ref: dict) -> tuple[dict, str]:
    """Return ({id: source-record for the UI}, prompt text)."""
    registry, lines = {}, []

    def add(prefix, n, record, text):
        sid = f"{prefix}{n}"
        registry[sid] = record
        lines.append(f"[{sid}] {text}")

    n = 0
    for case in similar:
        for ex in case.get("excerpts", []):
            n += 1
            head = (f"{case['case_title']} | {case['court']} | {case.get('year') or case['date']} | "
                    f"{case['bail_type']} bail | outcome in that case: {case['bail_outcome']} | passage type: {ex['field']}")
            add("C", n, {"id": f"C{n}", "kind": "judgment", "title": case["case_title"], "court": case["court"],
                         "year": case.get("year") or case["date"], "outcome": case["bail_outcome"], "bail_type": case["bail_type"],
                         "field": ex["field"], "excerpt": ex["text"], "case_id": case["case_id"]},
                f"{head}\n    {ex['text']}")
    for i, r in enumerate(ref["offenses"], 1):
        name = r.get("offense_name", "")
        sect = f"IPC {r.get('ipc_section')} / BNS {r.get('bns_section')}" if r.get("bns_section") else f"{r.get('act', '')} s.{r.get('ipc_section')}"
        txt = f"{sect}: {name}. Bailable: {r.get('bailable')}. Cognizable: {r.get('cognizable')}. Max punishment (years): {r.get('max_punishment_years')}. {r.get('notes') or ''}"
        add("S", i, {"id": f"S{i}", "kind": "statute table", "title": f"{sect} — {name}", "excerpt": txt}, txt)
    for i, r in enumerate(ref["rules"], 1):
        txt = f"{r['rule_name']} ({r['source_provision']}): {r['description']} Notes: {r.get('exclusion_notes') or ''}"
        add("R", i, {"id": f"R{i}", "kind": "custody rule", "title": r["rule_name"], "excerpt": txt}, txt)
    for i, r in enumerate(ref["landmarks"], 1):
        txt = f"{r['case_name']} {r['citation']} ({r['court']}): {r['principle_summary']}"
        add("L", i, {"id": f"L{i}", "kind": "landmark judgment", "title": f"{r['case_name']} {r['citation']}", "excerpt": r["principle_summary"]}, txt)
    for i, r in enumerate(ref["checklist"], 1):
        txt = f"{'Mandatory' if r['is_mandatory'] else 'Optional'} ({r['bail_type']}): {r['requirement']}"
        add("K", i, {"id": f"K{i}", "kind": "checklist", "title": r["requirement"], "excerpt": txt}, txt)
    return registry, "\n".join(lines)


BRIEF_TOOL = {
    "name": "submit_case_brief",
    "description": "Submit the structured, cited research brief.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "3-4 neutral sentences on what the case involves. No outcome prediction."},
            "applicable_provisions": {"type": "array", "items": {"type": "object", "properties": {
                "provision": {"type": "string"}, "explanation": {"type": "string", "description": "Plain-language, 1-2 sentences, from the sources."},
                "citations": {"type": "array", "items": {"type": "string"}}}, "required": ["provision", "explanation", "citations"]}},
            "how_similar_cases_ended": {"type": "array", "items": {"type": "object", "properties": {
                "pattern": {"type": "string", "description": "What the cited judgments show and the reasons they gave. Name differences from the user's case."},
                "citations": {"type": "array", "items": {"type": "string"}}}, "required": ["pattern", "citations"]}},
            "procedural_options": {"type": "array", "items": {"type": "object", "properties": {
                "option": {"type": "string", "description": "A procedural route or issue to raise with counsel, worded as something to discuss, not a recommendation."},
                "basis": {"type": "string", "description": "Why it may be relevant, tied to a fact in the case."},
                "citations": {"type": "array", "items": {"type": "string"}}}, "required": ["option", "basis", "citations"]}},
            "questions_for_lawyer": {"type": "array", "items": {"type": "object", "properties": {
                "question": {"type": "string"}, "reason": {"type": "string"}}, "required": ["question", "reason"]}},
            "missing_information": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "applicable_provisions", "how_similar_cases_ended", "procedural_options", "questions_for_lawyer"],
    },
}

BRIEF_SYSTEM = f"""You write research briefs for people involved in Indian criminal bail matters. A lawyer will review your output.

Hard rules:
1. Every statement about a past judgment, statute, custody rule or checklist item must be supported by a numbered source below and must carry that source's id in `citations`. Use only ids that exist.
2. Never predict or recommend an outcome. Do not say bail will, should, or is likely to be granted or refused, and do not estimate chances. Describe what the cited judgments held and why, and say how the user's facts differ.
3. Do not count or tally outcomes; the system does that. Do not invent case names, citations, section numbers or dates.
4. If the sources do not support a point, leave it out or list it under missing_information. Say so plainly when the retrieved judgments are only loosely related.
5. Procedural options must be phrased as issues to discuss with a lawyer, each tied to a fact from the case and at least one source.
6. Questions for the lawyer must be specific to this case's facts, not generic.
7. Plain language, short sentences. Sections the user typed are unverified; flag IPC/BNS ambiguity when relevant. Today's custody figure is given; use it rather than computing your own."""


def _validate_brief(brief: dict, valid_ids: set[str]) -> tuple[dict, dict]:
    stats = {"dropped_prediction": 0, "uncited_items": 0, "invalid_citations_removed": 0}

    def clean_list(items, text_keys):
        kept = []
        for item in items or []:
            blob = " ".join(str(item.get(k, "")) for k in text_keys)
            if _PREDICTION_RE.search(blob):
                stats["dropped_prediction"] += 1
                continue
            if "citations" in item:
                good = [c for c in item["citations"] if c in valid_ids]
                stats["invalid_citations_removed"] += len(item["citations"]) - len(good)
                item["citations"] = good
                if not good:
                    item["uncited"] = True
                    stats["uncited_items"] += 1
            kept.append(item)
        return kept

    brief["applicable_provisions"] = clean_list(brief.get("applicable_provisions"), ["provision", "explanation"])
    brief["how_similar_cases_ended"] = clean_list(brief.get("how_similar_cases_ended"), ["pattern"])
    brief["procedural_options"] = clean_list(brief.get("procedural_options"), ["option", "basis"])
    brief["questions_for_lawyer"] = clean_list(brief.get("questions_for_lawyer"), ["question", "reason"])
    if _PREDICTION_RE.search(brief.get("summary", "")):
        brief["summary"] = "Summary withheld because it contained outcome language. See the sections below."
        stats["dropped_prediction"] += 1
    return brief, stats


def generate_brief(case_input: dict, custody_days: int, assessment: dict, similar: list[dict],
                   ref: dict, redact_terms: list[str] | None = None) -> dict:
    registry, source_text = build_sources(similar, ref)
    facts, mask_counts = mask_pii((case_input.get("facts") or "")[:MAX_FACT_CHARS], redact_terms)
    user = (
        "<case>\n"
        f"Sections entered (unverified): {case_input.get('sections') or 'none'}\n"
        f"Special-law sections entered: {case_input.get('special_laws') or 'none'}\n"
        f"Bail type: {case_input.get('bail_type')}\n"
        f"Arrest date: {case_input.get('arrest_date') or 'not stated'}; days in custody today: {custody_days}\n"
        f"Charge sheet filed: {case_input.get('charge_sheet_filed')}; first-time offender: {case_input.get('first_time_offender')}\n"
        f"Recorded risk factors: flight={case_input.get('flight_risk')}, witness={case_input.get('witness_risk')}, evidence={case_input.get('evidence_risk')}\n"
        f"Facts and legal issues:\n{facts}\n</case>\n\n"
        f"<rule_based_triggers>\n" + "\n".join(f"- {t}" for t in assessment.get("triggers", [])) + "\n</rule_based_triggers>\n\n"
        f"<sources>\n{source_text or '(no sources retrieved)'}\n</sources>\n\n"
        "Write the brief by calling submit_case_brief."
    )
    raw = _call_tool(BRIEF_SYSTEM, user, BRIEF_TOOL, max_tokens=4500)
    brief, stats = _validate_brief(raw, set(registry))
    brief["checks"] = {**stats, "privacy_masked": mask_counts}
    brief["sources"] = registry
    return brief


def outcome_summary(similar: list[dict]) -> dict:
    c = Counter(r.get("bail_outcome", "") for r in similar)
    return {"retrieved": len(similar), "granted": c.get("Granted", 0), "rejected": c.get("Rejected", 0),
            "note": "Counts describe only the retrieved judgments. They are not a probability for this case."}


# ----------------------------------------------------------------------------
# Public entry point used by the API
# ----------------------------------------------------------------------------

def analyze(case_input: dict, custody_days: int, assessment: dict, top_k: int = 6,
            redact_terms: list[str] | None = None, retrieval_query: str = "") -> dict:
    facts = case_input.get("facts") or ""
    query = (retrieval_query or "").strip() or facts[:1500]
    index = rag_retrieval.get_index()
    similar = index.search(query, sections_text=case_input.get("sections", ""),
                           special_laws_text=case_input.get("special_laws", ""),
                           bail_type=case_input.get("bail_type", ""), top_k=top_k)
    sections = rag_retrieval.parse_sections(case_input.get("sections", ""))
    acts = rag_retrieval.detect_acts(f"{case_input.get('special_laws', '')} {case_input.get('sections', '')} {query}")
    categories = index.infer_categories(sections, acts, query)
    result = {"similar_cases": similar, "outcome_summary": outcome_summary(similar),
              "retrieval_mode": index.mode, "categories": sorted(categories), "acts": sorted(acts),
              "brief": None, "brief_status": "ok", "disclaimer": DISCLAIMER}
    if not llm_available():
        result["brief_status"] = "no_api_key"
        result["brief_message"] = "Set ANTHROPIC_API_KEY on the server to enable the written brief. Similar judgments and rule-based triggers are shown below."
        return result
    try:
        ref = reference_sources(case_input.get("sections", ""), case_input.get("special_laws", ""),
                                case_input.get("bail_type", ""), bool(case_input.get("first_time_offender")), categories)
        result["brief"] = generate_brief(case_input, custody_days, assessment, similar, ref, redact_terms)
    except Exception as exc:  # network, auth, rate-limit, malformed output: keep the rest of the page working
        result["brief_status"] = "error"
        result["brief_message"] = f"The written brief could not be generated ({type(exc).__name__}). Retrieval results are still shown."
    return result
