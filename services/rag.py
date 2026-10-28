"""RAG orchestration for NyaySetu: field extraction, retrieval, and a cited case brief.

Design rules
------------
* The local model (Ollama) only writes from numbered sources we hand it: judgment excerpts [C#], statute table
  rows [S#], custody rules [R#], landmark judgments [L#], checklist items [K#].
* The server, not the model, counts outcomes and validates citations. Items whose citations do
  not exist are flagged; items that predict or recommend a bail outcome are dropped.
* If the local model is not running or the call fails, the caller still gets retrieval results and the
  rule-based triggers; only the written brief is missing.
* Personal identifiers are masked before text reaches the model (see ``mask_pii``).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

from services import llm, rag_retrieval

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "bail_reckoner.db"

DEFAULT_MODEL = llm.DEFAULT_MODEL
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
# Local model access (Ollama, see services/llm.py). Replaceable in tests.
# ----------------------------------------------------------------------------
_llm_override = None  # test hook: fn(system, user, tool, max_tokens) -> dict


def set_client_for_tests(fake) -> None:
    """Swap the local model for a fake. Pass None to use the real local model again."""
    global _llm_override
    _llm_override = fake


def llm_available() -> bool:
    return _llm_override is not None or llm.available()


def _call_tool(system: str, user: str, tool: dict, max_tokens: int = 4000) -> dict:
    """One structured answer that matches the tool's input_schema."""
    if _llm_override is not None:
        return _llm_override(system, user, tool, max_tokens)
    return llm.call_structured(system, user, tool["input_schema"], max_tokens)


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
        raise RuntimeError("The local AI model (Ollama) is not running")
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
# Step 3: build numbered sources and ask the local model for a cited brief
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
        "Return the brief as one JSON object that follows the schema."
    )
    raw = _call_tool(BRIEF_SYSTEM, user, BRIEF_TOOL, max_tokens=4500)
    brief, stats = _validate_brief(raw, set(registry))
    brief["checks"] = {**stats, "privacy_masked": mask_counts}
    brief["sources"] = registry
    return brief


# ----------------------------------------------------------------------------
# No-AI fallback: assemble the same cited brief from the retrieved sources with fixed rules
# ----------------------------------------------------------------------------

def _trim(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[:end + 1] if end > limit * 0.5 else cut.rstrip() + "…")


def extractive_brief(case_input: dict, custody_days: int, assessment: dict, similar: list[dict], ref: dict) -> dict:
    """A brief with no language model. Wording is fixed by rules; facts come from the sources.
    Every citation points at a source that exists, so nothing here can be invented."""
    registry, _ = build_sources(similar, ref)
    index = rag_retrieval.get_index()
    rows = {c["case_id"]: c for c in index.cases}
    n = max([int(k[1:]) for k in registry if k.startswith("C")] or [0])
    case_ids = {}   # case_id -> (reasoning source id, other excerpt ids)
    for case in similar[:4]:
        row = rows.get(case["case_id"], {})
        text = row.get("judgment_reason") or row.get("summary") or ""
        others = [sid for sid, rec in registry.items() if rec.get("case_id") == case["case_id"]]
        rid = None
        if text:
            n += 1
            rid = f"C{n}"
            registry[rid] = {"id": rid, "kind": "judgment", "title": case["case_title"], "court": case["court"],
                             "year": case.get("year") or case["date"], "outcome": case["bail_outcome"],
                             "bail_type": case["bail_type"], "field": "judgment_reason", "excerpt": text, "case_id": case["case_id"]}
        case_ids[case["case_id"]] = (rid, others)

    secs = (case_input.get("sections") or "").strip()
    spec = (case_input.get("special_laws") or "").strip()
    filed = bool(case_input.get("charge_sheet_filed"))
    entered = "; ".join(x for x in (secs and f"IPC/BNS {secs}", spec) if x) or "no sections entered"
    summary = (f"Details entered: {entered}. Bail type: {case_input.get('bail_type') or 'not stated'}. "
               + (f"{custody_days} days in custody as of today. " if case_input.get("arrest_date") else "No arrest date entered. ")
               + ("Charge sheet reported as filed. " if filed else "Charge sheet not confirmed. ")
               + f"{len(similar)} similar judgments were retrieved by wording, section and statute.")

    provisions, missing = [], []
    offenses = ref.get("offenses", [])
    for i, r in enumerate(offenses[:4], 1):
        sect = f"IPC {r.get('ipc_section')} / BNS {r.get('bns_section')}" if r.get("bns_section") else f"{r.get('act', 'Special Act')} s.{r.get('ipc_section')}"
        bits = [r.get("offense_name") or "", r.get("bailable") or "", r.get("cognizable") or ""]
        if r.get("max_punishment_years"):
            bits.append(f"maximum punishment {r['max_punishment_years']:g} years")
        if r.get("death_or_life"):
            bits.append("death or life imprisonment possible")
        if r.get("triable_by"):
            bits.append(f"triable by {r['triable_by']}")
        provisions.append({"provision": sect, "explanation": "; ".join(b for b in bits if b) + ".", "citations": [f"S{i}"]})
    if not offenses:
        missing.append("The sections entered did not match any row in the built-in statute table; read the provision text itself.")

    similar_items = []
    for case in similar[:4]:
        rid, others = case_ids[case["case_id"]]
        row = rows.get(case["case_id"], {})
        reason = _trim(row.get("judgment_reason") or row.get("summary") or "", 380)
        why = ", ".join(case.get("match_reasons") or [])
        cites = ([rid] if rid else []) + [o for o in others if o != rid][:1]
        similar_items.append({"pattern": f"{case['case_title']} ({case['court']}, {case.get('year') or case['date']}): bail {case['bail_outcome'].lower()}. "
                                         f"Court's reasoning in that case: {reason} Retrieved because: {why}.", "citations": cites})

    options = []
    primary = next((r for r in offenses if r.get("max_punishment_years") and not r.get("death_or_life")), None)
    for i, rule in enumerate(ref.get("rules", []), 1):
        frac = rule.get("threshold_fraction")
        if frac and primary:
            days = frac * float(primary["max_punishment_years"]) * 365
            state = "has been reached" if custody_days >= days else "has not yet been reached"
            basis = (f"{rule['source_provision']}. For {primary.get('offense_name')} the stated maximum is {primary['max_punishment_years']:g} years, "
                     f"so this threshold is about {int(days)} days; {custody_days} days have been served, so it {state}. "
                     f"Exclusions to verify: {_trim(rule.get('exclusion_notes') or 'none listed', 200)}")
        else:
            basis = f"{rule['source_provision']}: {_trim(rule['description'], 260)}"
        options.append({"option": f"Check whether this custody rule applies: {rule['rule_name']}", "basis": basis, "citations": [f"R{i}"]})
    bt = (case_input.get("bail_type") or "").lower()
    if bt.startswith("default") or (not filed and custody_days >= 60):
        options.append({"option": "Confirm whether the period for filing the charge sheet has run out",
                        "basis": f"No charge sheet is confirmed and {custody_days} days have passed since arrest. The 60/90/180-day periods are not in the built-in sources, so check them against the statute.",
                        "citations": []})
    for i, r in enumerate(ref.get("landmarks", [])[:3], 1):
        options.append({"option": f"Consider the principle in {r['case_name']} {r['citation']}", "basis": _trim(r["principle_summary"], 300), "citations": [f"L{i}"]})
    checklist = ref.get("checklist", [])
    if checklist:
        options.append({"option": f"Prepare the standard documents for a {case_input.get('bail_type') or 'regular'} bail application",
                        "basis": "; ".join(_trim(c["requirement"], 80) for c in checklist[:5]),
                        "citations": [f"K{i}" for i in range(1, min(len(checklist), 5) + 1)]})

    qs = []
    qs.append(("Is continued custody still necessary now that the charge sheet is filed?", "The details entered say the charge sheet has been filed.") if filed
              else ("Has the time allowed for filing the charge sheet expired, and does that give a right to default bail?", f"No charge sheet is confirmed and {custody_days} days have passed since arrest."))
    if case_input.get("first_time_offender"):
        qs.append(("Does the first-time-offender rule in the custody provisions apply to this case?", "The details entered say there is no previous case."))
    acts = rag_retrieval.detect_acts(f"{spec} {secs}")
    if "NDPS" in acts:
        qs.append(("Which quantity category applies (small, intermediate or commercial), and do the Section 37 conditions apply?", "NDPS bail rules depend on the quantity recovered."))
    if "POCSO" in acts:
        qs.append(("What do the POCSO provisions require at the bail stage, and what protection applies to the child witness?", "A POCSO section was entered."))
    if acts & {"PMLA", "UAPA"}:
        qs.append(("Do the stricter twin conditions for this Act apply, and how have courts applied them?", "A special Act with stricter bail conditions was entered."))
    if "PC_ACT" in acts:
        qs.append(("Was sanction for prosecution obtained, and how does it affect bail?", "A corruption-law section was entered."))
    for flag, label in (("flight_risk", "flight"), ("witness_risk", "witness-tampering"), ("evidence_risk", "evidence-tampering")):
        if case_input.get(flag):
            qs.append((f"Which bail conditions could answer the {label} concern that is recorded?", f"A {label} risk is recorded in the details."))
    qs.append(("Which documents and upcoming hearing dates should be confirmed with the court record?", "The details here are user-entered and unverified."))

    if not case_input.get("arrest_date"):
        missing.append("No arrest date was entered, so custody length could not be calculated.")
    if not secs and not spec:
        missing.append("No sections were entered; the match is based only on the wording of the facts.")

    brief = {"summary": summary, "applicable_provisions": provisions, "how_similar_cases_ended": similar_items,
             "procedural_options": options, "questions_for_lawyer": [{"question": q, "reason": r} for q, r in qs],
             "missing_information": missing, "mode": "extractive"}
    brief, stats = _validate_brief(brief, set(registry))
    brief["checks"] = {**stats, "privacy_masked": {}}
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
    try:
        ref = reference_sources(case_input.get("sections", ""), case_input.get("special_laws", ""),
                                case_input.get("bail_type", ""), bool(case_input.get("first_time_offender")), categories)
    except Exception:
        ref = {"offenses": [], "rules": [], "landmarks": [], "checklist": []}
    if llm_available():
        try:
            result["brief"] = generate_brief(case_input, custody_days, assessment, similar, ref, redact_terms)
            return result
        except Exception as exc:  # network, rate limit, malformed output: fall back instead of showing nothing
            result["brief_message"] = (f"The AI writer could not be used ({type(exc).__name__}: {str(exc)[:160]}). "
                                       "This brief was assembled directly from the retrieved sources instead.")
    else:
        result["brief_message"] = ("No AI model is configured, so this brief was assembled directly from the retrieved sources "
                                   "using fixed rules. Set LLM_API_KEY (free) or start Ollama for AI-written text.")
    try:
        result["brief"] = extractive_brief(case_input, custody_days, assessment, similar, ref)
        result["brief_status"] = "extractive"
    except Exception as exc:
        result["brief_status"] = "error"
        result["brief_message"] = f"The brief could not be built ({type(exc).__name__}). Retrieval results are still shown."
    return result
