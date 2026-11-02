"""Read the key facts out of chargesheet / FIR text with plain rules (no AI, no network).

Used when no language model is available, so the Auto-fill button and the analysis still work.
It is deliberately conservative and the user must check every field:

* Sections are taken from the labelled "Sections ..." part of the document, not from the story, because
  a narrative often cites other sections (e.g. "Section 50 NDPS was complied with") that are not charges.
* Dates are read day-first (Indian style), and only after an explicit arrest label.
* Anything it cannot find is left empty and listed in ``missing_information``.
"""
from __future__ import annotations

import re
from datetime import date

from services import rag_retrieval

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}

# one section number: 20, 376A, 20(b)(ii)(B), optionally followed by a short description in brackets
_NUM = r"\d{1,3}[A-Za-z]{0,2}(?:\s*\(\s*[0-9A-Za-z]{1,4}\s*\))*(?:\s*\((?=[A-Za-z]{3})[^)]{3,60}\))?"
_SEP = r"(?:\s*(?:,|;|&|/|\band\b|\bread with\b|\br/w\b)\s*(?:Sections?\s*|Secs?\.?\s*|s\.\s*)?)"
_LIST = rf"{_NUM}(?:{_SEP}{_NUM})*"
_KEY = r"(?:Sections?|Secs?\.?|u/s|U/s|S\.)"

_IPC = r"(?:\bIPC\b|I\.P\.C\.?|Indian Penal Code)"
_BNS = r"(?:\bBNS\b|Bharatiya Nyaya Sanhita)"

_SPECIAL = {  # key -> (display name, regex)
    "NDPS": ("NDPS", r"(?:\bNDPS\b|Narcotic Drugs and Psychotropic Substances)"),
    "POCSO": ("POCSO", r"(?:\bPOCSO\b|Protection of Children from Sexual Offences)"),
    "UAPA": ("UAPA", r"(?:\bUAPA\b|\bUA\(P\)|Unlawful Activities \(?Prevention\)?)"),
    "PMLA": ("PMLA", r"(?:\bPMLA\b|Prevention of Money[- ]Laundering)"),
    "ARMS": ("Arms Act", r"(?:Arms Act)"),
    "IT_ACT": ("IT Act", r"(?:\bIT Act\b|Information Technology Act)"),
    "PC_ACT": ("Prevention of Corruption Act", r"(?:Prevention of Corruption(?: Act)?|\bPC Act\b)"),
    "MCOCA": ("MCOCA", r"(?:\bMCOCA\b)"),
    "DOWRY": ("Dowry Prohibition Act", r"(?:Dowry Prohibition(?: Act)?)"),
    "MV_ACT": ("Motor Vehicles Act", r"(?:Motor Vehicles Act)"),
}

_CITY_STATE = {
    "pune": "Maharashtra", "mumbai": "Maharashtra", "thane": "Maharashtra", "nagpur": "Maharashtra", "nashik": "Maharashtra",
    "kolhapur": "Maharashtra", "aurangabad": "Maharashtra", "delhi": "Delhi", "lucknow": "Uttar Pradesh", "kanpur": "Uttar Pradesh",
    "bengaluru": "Karnataka", "bangalore": "Karnataka", "gurugram": "Haryana", "gurgaon": "Haryana", "chandigarh": "Punjab",
    "chennai": "Tamil Nadu", "patna": "Bihar", "jaipur": "Rajasthan", "bhopal": "Madhya Pradesh", "indore": "Madhya Pradesh",
    "ranchi": "Jharkhand", "kolkata": "West Bengal", "hyderabad": "Telangana", "ahmedabad": "Gujarat", "guwahati": "Assam",
}
_STATES = ["Maharashtra", "Uttar Pradesh", "Karnataka", "Haryana", "Punjab", "Tamil Nadu", "Bihar", "Rajasthan", "Madhya Pradesh",
           "Jharkhand", "West Bengal", "Telangana", "Gujarat", "Assam", "Delhi", "Kerala", "Odisha"]


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _clean_sections(raw: str) -> list[str]:
    out = []
    for m in re.finditer(_NUM, raw):
        token = re.sub(r"\s+", "", re.sub(r"\((?=[A-Za-z]{3})[^)]{3,60}\)$", "", m.group()))
        base = re.match(r"\d+", token)
        if base and 0 < int(base.group()) <= 600 and token not in out:
            out.append(token)
    return out


def _region_for_sections(flat: str) -> str:
    """The labelled part of the document that lists the charges, or '' if there is none."""
    m = re.search(r"(?:Sections?\s+of\s+Law|Sections?\s+of\s+the\s+Act|Act\s*(?:&|and)\s*Sections?|Under\s+Sections?|Charged\s+under|Sections?)\s*:?", flat, re.I)
    # prefer an explicit "Sections of Law"-style label, then a plain "Sections:" label
    labels = [r"Sections?\s+of\s+Law", r"Act\s*(?:&|and)\s*Sections?", r"Charged\s+under", r"Sections?\s*:"]
    for lab in labels:
        m = re.search(lab, flat, re.I)
        if m:
            tail = flat[m.end():m.end() + 500]
            for cut in re.finditer(r"\s\d{1,2}\.\s+[A-Z]", tail):       # next numbered field of the form ("5. Type of ...")
                if tail[:cut.start()].rstrip()[-1:] not in (".", ")"):
                    continue                  # "... Section 29. Bharatiya" / "read with 34. ACCUSED" are not field labels
                return tail[:cut.start()]
            return tail[:400]
    return ""


def _find_sections(region: str, strict: bool) -> tuple[list[str], dict[str, list[str]]]:
    """Return (IPC/BNS sections, {special-act key: sections})."""
    keyword = rf"(?:{_KEY}\s*)" + ("" if not strict else "")
    kw_req = rf"{_KEY}\s*" if strict else rf"(?:{_KEY}\s*)?"
    ipc_bns: list[str] = []
    for act in (_IPC, _BNS):
        for pat in (rf"{act}(?:\s+Act)?(?:,?\s*\d{{4}})?\s*[:\-]?\s*{kw_req}({_LIST})",
                    rf"{_KEY}\s*({_LIST})\s*(?:of\s+)?(?:the\s+)?{act}"):
            for m in re.finditer(pat, region, re.I if act is _IPC else 0):
                for s in _clean_sections(m.group(1)):
                    if s not in ipc_bns:
                        ipc_bns.append(s)
    special: dict[str, list[str]] = {}
    for key, (_, act) in _SPECIAL.items():
        found = []
        for pat in (rf"{act}(?:\s+Act)?(?:,?\s*\d{{4}})?\s*[:\-]?\s*{kw_req}({_LIST})",
                    rf"{_KEY}\s*({_LIST})\s*(?:of\s+)?(?:the\s+)?{act}"):
            for m in re.finditer(pat, region, re.I):
                found += [s for s in _clean_sections(m.group(1)) if s not in found]
        if found or re.search(act, region, re.I):
            special[key] = found
    return ipc_bns, special


def _parse_date(chunk: str) -> str:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", chunk)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", chunk)         # day first
        if m:
            d, mo, y = map(int, m.groups())
        else:
            m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", chunk)
            if not m or m.group(2).lower() not in _MONTHS and m.group(2).lower()[:3] not in {k[:3] for k in _MONTHS}:
                return ""
            d, y = int(m.group(1)), int(m.group(3))
            mo = next(v for k, v in _MONTHS.items() if k.startswith(m.group(2).lower()[:3]))
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return ""


def _arrest_date(flat: str) -> str:
    label = r"(?:date\s*(?:and time\s*)?of\s*arrest|arrested\s+on|was\s+arrested\s+on|arrest\s*date|arrested\s+at)"
    for m in re.finditer(label, flat, re.I):
        d = _parse_date(flat[m.end():m.end() + 70])
        if d:
            return d
    m = re.search(r"\bARREST\s*:\s*(?:Accused\s+)?(?:arrested\s+)?(?:on\s+)?([^.]{0,60})", flat)
    return _parse_date(m.group(1)) if m else ""


def _tidy_court(court: str) -> str:
    if not court.isupper():
        return court
    out = court.title()
    for acronym in ("NDPS", "PMLA", "POCSO", "UAPA", "CBI", "NIA"):
        out = re.sub(rf"\b{acronym.title()}\b", acronym, out)
    return out


def extract(text: str) -> dict:
    flat = _flat(text)
    missing: list[str] = []

    region = _region_for_sections(flat)
    ipc_bns, special = _find_sections(region, strict=False) if region else _find_sections(flat[:1500], strict=True)
    sections = ", ".join(ipc_bns)
    laws = []
    for key, secs in special.items():
        name = _SPECIAL[key][0]
        laws += [f"{name} {s}" for s in secs] or [name]
    special_laws = "; ".join(laws)
    if not (sections or special_laws):
        missing.append("No section numbers were found in the text; enter them by hand.")

    arrest = _arrest_date(flat)
    if not arrest:
        missing.append("No arrest date was found in the text.")

    head = flat[:400].lower()
    not_filed = re.search(r"charge\s*-?sheet\s*(?:is\s*|has\s*)?(?:not|yet to be)\s*(?:yet\s*)?(?:been\s*)?(?:filed|submitted)|no charge\s*-?sheet|charge\s*-?sheet\s*pending", flat, re.I)
    filed = bool(not not_filed and (re.search(r"charge\s*-?sheet\s*(?:was\s*)?(?:filed|submitted)|charge\s*-?sheet\s*no|final report|prosecution complaint|complaint\s+(?:was\s+)?filed\s+on", flat, re.I)
                                    or re.search(r"charge\s*-?sheet|final report", head)))
    first_time = bool(re.search(r"no\s+(?:previous|prior|earlier)\s+(?:case|record|fir|conviction)|first[- ]time\s+offender|previous cases?\s*:?\s*none|no criminal antecedents?", flat, re.I))

    low = flat.lower()
    if "anticipatory" in low:
        bail = "Anticipatory"
    elif re.search(r"interim bail", low):
        bail = "Interim"
    elif not filed and re.search(r"default bail|statutory bail|beyond the statutory period|statutory period (?:has )?(?:expired|lapsed)", low):
        bail = "Default/statutory"
    else:
        bail = "Regular"

    court = ""
    m = re.search(r"IN THE COURT OF\s+(?:THE\s+)?([^\n]{5,90})", text or "", re.I)
    if m:
        court = m.group(1).strip(" .,:;")
    head_txt = flat[:700].lower()
    region_name = next((s for s in _STATES if s.lower() in head_txt), "") or next((st for city, st in _CITY_STATE.items() if re.search(rf"\b{city}\b", head_txt)), "")

    clean = re.sub(r"Fictional sample document[^.]*\.\s*(?:Not a real police record\.)?|\bPage \d+\b", " ", flat)
    facts = re.search(r"BRIEF FACTS(?: OF THE CASE)?\s*:?\s*(.{80,900})", clean)
    query = _flat(facts.group(1) if facts else clean[:900])
    if facts and "." in query[300:]:
        query = query[:query.rfind(".") + 1]                               # stop at a sentence end

    return {
        "sections": sections, "special_laws": special_laws, "bail_type": bail, "arrest_date": arrest,
        "charge_sheet_filed": filed, "first_time_offender": first_time,
        "flight_risk": False, "witness_risk": False, "evidence_risk": False,
        "court": _tidy_court(court), "region": region_name,
        "case_summary": query[:400], "legal_issues": "", "retrieval_query": query,
        "missing_information": missing, "method": "rules",
    }
