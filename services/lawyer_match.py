"""Explainable lawyer suggestions.

This is a transparent rule-based match, not a ranking of "the best lawyer". The sample data has no
track-record information, and ranking lawyers by outcomes would be unsupported and, in India, restricted
by Bar Council advertising rules. Every suggestion lists the reasons it was made.
"""
from __future__ import annotations

import json
from pathlib import Path

LAWYERS_FILE = Path(__file__).resolve().parent.parent / "data" / "lawyers.json"

ACT_TO_SPEC = {"NDPS": "NDPS", "PMLA": "PMLA", "POCSO": "POCSO", "PC_ACT": "PC_ACT", "IT_ACT": "IT_ACT",
               "ARMS": "ARMS", "DOWRY": "DOWRY", "MV_ACT": "MV_ACT"}
CATEGORY_TO_SPEC = {"Narcotics": "NDPS", "Sexual Offense": "POCSO", "Cyber Crime": "CYBER",
                    "Fraud or Cheating": "ECONOMIC", "Murder": "MURDER", "Attempt to Murder": "MURDER",
                    "Dowry Harassment": "DOWRY", "Domestic Violence": "DOWRY"}
SPEC_LABEL = {"NDPS": "NDPS Act matters", "PMLA": "PMLA matters", "POCSO": "POCSO matters", "PC_ACT": "corruption cases",
              "IT_ACT": "IT Act matters", "CYBER": "cyber crime", "ECONOMIC": "economic offences", "ARMS": "Arms Act matters",
              "DOWRY": "dowry / matrimonial offences", "MURDER": "serious offences against the person",
              "MV_ACT": "motor vehicle offences", "BAIL": "bail matters"}


def load_lawyers() -> list[dict]:
    data = json.loads(LAWYERS_FILE.read_text(encoding="utf-8"))
    return [{**l, "sample": True} for l in data["lawyers"]]


def get_lawyer(lawyer_id: int) -> dict | None:
    return next((l for l in load_lawyers() if l["id"] == lawyer_id), None)


def case_specializations(acts, categories) -> list[str]:
    specs = [ACT_TO_SPEC[a] for a in acts if a in ACT_TO_SPEC]
    specs += [CATEGORY_TO_SPEC[c] for c in categories if c in CATEGORY_TO_SPEC]
    return list(dict.fromkeys(specs))


def match(acts=(), categories=(), court: str = "", region: str = "", languages=(),
          min_experience: int = 0, availability: str = "", top_k: int = 5) -> list[dict]:
    wanted = case_specializations(acts, categories)
    court_l, region_l = (court or "").lower(), (region or "").lower()
    langs = {x.lower() for x in languages or [] if x and x.lower() != "any"}
    out = []
    for l in load_lawyers():
        if l["experience"] < min_experience:
            continue
        if availability and availability.lower() not in ("any", "") and availability.lower() not in l["availability"].lower():
            continue
        score, reasons = 0, []
        hits = [s for s in wanted if s in l["specializations"]]
        if hits:
            score += 45 + 10 * (len(hits) - 1)
            reasons.append("Handles " + ", ".join(SPEC_LABEL[h] for h in hits))
        if "BAIL" in l["specializations"]:
            score += 10
            reasons.append("Regularly handles bail matters")
        if region_l and region_l in l["state"].lower():
            score += 20
            reasons.append(f"Practises in {l['state']}")
        elif region_l and region_l in l["location"].lower():
            score += 20
            reasons.append(f"Based in {l['location']}")
        if court_l and any(w in c.lower() for c in l["courts"] for w in court_l.replace(",", " ").split() if len(w) > 4):
            score += 10
            reasons.append("Appears in a court named in your case")
        if langs and langs & {x.lower() for x in l["languages"]}:
            score += 10
            reasons.append("Speaks " + ", ".join(sorted(langs & {x.lower() for x in l["languages"]})).title())
        score += min(l["experience"], 20) / 20 * 5
        if l["availability"] == "Available":
            score += 5
        out.append({**l, "fit_score": round(score, 1), "matchReasons": reasons or ["General criminal-law practice"]})
    out.sort(key=lambda x: x["fit_score"], reverse=True)
    for r in out:
        s = r["fit_score"]
        r["match_strength"] = "Strong fit" if s >= 70 else "Good fit" if s >= 45 else "Partial fit"
    return out[:top_k]
