"""Hybrid retrieval for the NyaySetu RAG pipeline.

Why this exists
---------------
The original search embedded the facts text only. Statutes such as NDPS or POCSO
live in the free text of the judgments (the ``special_laws`` column is almost
always empty) and the sections the user typed were never used, so a query for
IPC 302 + POCSO 6 could return a forgery case.

What it does
------------
1. Splits every judgment into field-level chunks (facts, legal issues, reasoning,
   summary) so an answer can cite the exact passage.
2. Ranks chunks with BM25 (exact words and section numbers) and, if the optional
   dense index has been built, with sentence-transformer embeddings. The two
   rankings are fused with Reciprocal Rank Fusion.
3. Rolls chunk scores up to case level and adds explainable boosts for shared
   sections (IPC and BNS numbering are mapped through the ``offenses`` table),
   shared special Acts, and the same crime category.

Everything works with BM25 alone, so no model download is needed to run it.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_CSV = ROOT / "data" / "processed_judgments.csv"
DB_PATH = ROOT / "bail_reckoner.db"
VECTOR_DIR = ROOT / "vector_store"
CHUNK_VECTORS = VECTOR_DIR / "chunk_vectors.npy"
CHUNK_MODEL_FILE = VECTOR_DIR / "chunk_model.txt"

CHUNK_FIELDS = ("facts", "legal_issues", "judgment_reason", "summary")
MAX_CHUNK_CHARS = 900
RRF_K = 60

# Sections that appear in almost every multi-accused case and say nothing about the offence.
GENERIC_SECTIONS = {"34", "149", "109", "114"}

ACT_PATTERNS = {
    "NDPS": r"\bNDPS\b|Narcotic Drugs",
    "POCSO": r"\bPOCSO\b|Protection of Children from Sexual",
    "UAPA": r"\bUAPA\b|UA\(P\)|Unlawful Activities",
    "PMLA": r"\bPMLA\b|Prevention of Money",
    "ARMS": r"Arms Act",
    "IT_ACT": r"Information Technology Act|\bIT Act\b",
    "PC_ACT": r"Prevention of Corruption|\bPC Act\b",
    "MCOCA": r"\bMCOCA\b|Organised Crime",
    "DOWRY": r"Dowry Prohibition",
    "JJ_ACT": r"Juvenile Justice|\bJJ Act\b",
    "SC_ST": r"SC/ST|Scheduled Castes",
    "MV_ACT": r"Motor Vehicles Act",
}
_ACT_RE = {k: re.compile(v, re.I if k not in {"NDPS", "POCSO", "UAPA", "PMLA", "MCOCA"} else 0)
           for k, v in ACT_PATTERNS.items()}

# Crime categories used in the dataset's ``crime_type`` column.
ACT_TO_CATEGORY = {
    "NDPS": "Narcotics", "POCSO": "Sexual Offense", "IT_ACT": "Cyber Crime",
    "DOWRY": "Dowry Harassment",
}
# Section number -> category (IPC numbering, plus the common BNS numbers that do not clash).
SECTION_TO_CATEGORY = {
    "302": "Murder", "300": "Murder", "103": "Murder", "304": "Murder",
    "307": "Attempt to Murder", "109": "Attempt to Murder",
    "376": "Sexual Offense", "354": "Sexual Offense", "366": "Kidnapping", "64": "Sexual Offense",
    "420": "Fraud or Cheating", "406": "Fraud or Cheating", "468": "Fraud or Cheating",
    "471": "Fraud or Cheating", "409": "Fraud or Cheating", "318": "Fraud or Cheating",
    "379": "Theft or Robbery", "392": "Theft or Robbery", "394": "Theft or Robbery",
    "395": "Theft or Robbery", "397": "Theft or Robbery", "309": "Theft or Robbery",
    "498A": "Dowry Harassment", "304B": "Dowry Harassment", "85": "Dowry Harassment",
    "363": "Kidnapping", "364": "Kidnapping", "364A": "Kidnapping", "365": "Kidnapping",
    "384": "Extortion", "386": "Extortion", "387": "Extortion", "308": "Extortion",
}

_STOP = set("""a an the and or of to in on at for by with from as is are was were be been being this that these those
it its he she they them his her their him who whom which what when where why how not no nor so than then there here
into over under again further once all any both each few more most other some such only own same too very can will
just should now also may might shall upon per vs versus court case accused""".split())

_SECTION_RE = re.compile(r"(?<![\w.])(\d{1,3}[A-Za-z]{0,2})(?:\s*\(\s*[0-9A-Za-z]{1,4}\s*\))*(?![\w])")


# ----------------------------------------------------------------------------
# Small text helpers
# ----------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOP and len(t) > 1]


def parse_sections(text: str) -> set[str]:
    """Pull base section numbers ('376(2)(n)' -> '376', '120b' -> '120B') out of free text."""
    found = set()
    for m in _SECTION_RE.finditer(text or ""):
        base = m.group(1).upper()
        digits = re.match(r"\d+", base)
        if not digits or int(digits.group()) == 0 or int(digits.group()) > 600:
            continue
        found.add(base)
    return found


def detect_acts(text: str) -> set[str]:
    return {name for name, rx in _ACT_RE.items() if rx.search(text or "")}


def split_text(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    sentences = re.split(r"(?<=[.;])\s+", text)
    chunks, current = [], ""
    for s in sentences:
        if current and len(current) + len(s) + 1 > limit:
            chunks.append(current)
            current = s
        else:
            current = f"{current} {s}".strip()
    if current:
        chunks.append(current)
    return chunks


# ----------------------------------------------------------------------------
# Corpus: cases + chunks (deterministic, so the dense index lines up by position)
# ----------------------------------------------------------------------------

def load_cases(csv_path: Path = DATA_CSV) -> list[dict]:
    csv.field_size_limit(10 ** 9)
    with open(csv_path, encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for i, row in enumerate(rows):
        row["row_id"] = i
        blob = " ".join(row.get(k, "") for k in ("special_laws", "legal_issues", "judgment_reason", "summary", "facts", "case_title", "ipc_sections"))
        row["_sections"] = {s for s in parse_sections(row.get("ipc_sections", "")) if s not in GENERIC_SECTIONS}
        row["_acts"] = sorted(detect_acts(blob))
    return rows


def build_chunks(cases: list[dict]) -> list[dict]:
    chunks = []
    for case in cases:
        for field in CHUNK_FIELDS:
            for part_no, part in enumerate(split_text(case.get(field, ""))):
                chunks.append({
                    "chunk_id": f"{case['case_id']}:{field}:{part_no}",
                    "row_id": case["row_id"],
                    "field": field,
                    "text": part,
                    # Index text carries section numbers and statute names so BM25 can match them exactly.
                    "index_text": f"{case.get('ipc_sections', '')} {case.get('crime_type', '')} {' '.join(case['_acts'])} {part}",
                })
    return chunks


def _load_ipc_bns_map(db_path: Path = DB_PATH) -> tuple[dict, dict]:
    ipc_to_bns, bns_to_ipc = {}, {}
    if not db_path.exists():
        return ipc_to_bns, bns_to_ipc
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            for ipc, bns in conn.execute("SELECT ipc_section, bns_section FROM offenses"):
                i, b = parse_sections(ipc or ""), parse_sections(bns or "")
                for x in i:
                    ipc_to_bns.setdefault(x, set()).update(b)
                for y in b:
                    bns_to_ipc.setdefault(y, set()).update(i)
    except sqlite3.Error:
        pass
    return ipc_to_bns, bns_to_ipc


class HybridIndex:
    def __init__(self):
        from rank_bm25 import BM25Okapi
        self.cases = load_cases()
        self.chunks = build_chunks(self.cases)
        self.bm25 = BM25Okapi([tokenize(c["index_text"]) for c in self.chunks])
        self.ipc_to_bns, self.bns_to_ipc = _load_ipc_bns_map()
        self.dense = None
        self._model = None
        self._load_dense()

    # -- dense (optional) ---------------------------------------------------
    def _load_dense(self):
        if os.getenv("RAG_DISABLE_DENSE", "").lower() in {"1", "true", "yes"}:
            return
        if not (CHUNK_VECTORS.exists() and CHUNK_MODEL_FILE.exists()):
            return
        try:
            import numpy as np
            vectors = np.load(CHUNK_VECTORS)
            if vectors.shape[0] != len(self.chunks):
                return  # index is stale relative to the CSV; ignore rather than mis-align
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(CHUNK_MODEL_FILE.read_text(encoding="utf-8").strip())
            self.dense = vectors
        except Exception:
            self.dense = None
            self._model = None

    @property
    def mode(self) -> str:
        return "hybrid (BM25 + dense + rules)" if self.dense is not None else "BM25 + rules"

    # -- query expansion ----------------------------------------------------
    def expand_sections(self, sections: set[str]) -> set[str]:
        out = set(sections)
        for s in sections:
            out |= self.bns_to_ipc.get(s, set())
            out |= self.ipc_to_bns.get(s, set())
        return {s for s in out if s not in GENERIC_SECTIONS}

    def infer_categories(self, sections: set[str], acts: set[str], text: str) -> set[str]:
        cats = {ACT_TO_CATEGORY[a] for a in acts if a in ACT_TO_CATEGORY}
        cats |= {SECTION_TO_CATEGORY[s] for s in sections if s in SECTION_TO_CATEGORY}
        return cats

    # -- search ---------------------------------------------------------------
    def search(self, query: str, sections_text: str = "", special_laws_text: str = "",
               bail_type: str = "", top_k: int = 5, excerpts_per_case: int = 2) -> list[dict]:
        sections = {s for s in parse_sections(sections_text) if s not in GENERIC_SECTIONS}
        acts = detect_acts(f"{special_laws_text} {sections_text} {query}")
        qsections = self.expand_sections(sections)
        categories = self.infer_categories(sections, acts, query)

        full_query = " ".join([query, sections_text, special_laws_text, " ".join(sorted(acts))]).strip()
        if not full_query:
            return []

        n = len(self.chunks)
        pool = min(n, 80)
        bm_scores = self.bm25.get_scores(tokenize(full_query))
        bm_rank = sorted(range(n), key=lambda i: bm_scores[i], reverse=True)[:pool]
        fused: dict[int, float] = defaultdict(float)
        for rank, idx in enumerate(bm_rank):
            if bm_scores[idx] > 0:
                fused[idx] += 1.0 / (RRF_K + rank + 1)
        if self.dense is not None:
            import numpy as np
            qv = self._model.encode([full_query], normalize_embeddings=True)[0]
            sims = self.dense @ qv
            for rank, idx in enumerate(np.argsort(-sims)[:pool]):
                fused[int(idx)] += 1.0 / (RRF_K + rank + 1)
        if not fused:
            return []

        # Roll chunk scores up to case level: best chunk + a smaller credit for the second best.
        per_case: dict[int, list[tuple[float, int]]] = defaultdict(list)
        for idx, score in fused.items():
            per_case[self.chunks[idx]["row_id"]].append((score, idx))
        top_possible = (2 if self.dense is not None else 1) * (1.0 / (RRF_K + 1))

        scored = []
        for row_id, items in per_case.items():
            items.sort(reverse=True)
            base = items[0][0] + (0.3 * items[1][0] if len(items) > 1 else 0.0)
            text_score = min(1.0, base / (top_possible * 1.3))
            case = self.cases[row_id]
            reasons, bonus = [], 0.0
            shared = sorted(qsections & case["_sections"])
            if shared:
                frac = len(shared) / max(1, len(qsections))
                bonus += 0.35 * min(1.0, 0.4 + frac)
                reasons.append("shares section " + ", ".join(shared[:4]))
            shared_acts = sorted(set(acts) & set(case["_acts"]))
            if shared_acts:
                bonus += 0.30
                reasons.append("same Act: " + ", ".join(shared_acts))
            if categories and case.get("crime_type") in categories:
                bonus += 0.10
                reasons.append(f"same category ({case.get('crime_type')})")
            if bail_type and case.get("bail_type", "").lower().startswith(bail_type.lower()[:6]):
                bonus += 0.03
                reasons.append(f"same bail type ({case.get('bail_type')})")
            # An Act the user named that the case does not involve is a strong sign of a wrong match.
            if acts and not shared_acts and not shared:
                bonus -= 0.10
            final = max(0.0, min(1.0, (0.55 * text_score + bonus) / 1.2))
            scored.append((final, text_score, bonus, row_id, items, reasons))

        scored.sort(key=lambda t: t[0], reverse=True)
        results = []
        for final, text_score, bonus, row_id, items, reasons in scored[:top_k]:
            case = self.cases[row_id]
            excerpts = [{"chunk_id": self.chunks[i]["chunk_id"], "field": self.chunks[i]["field"],
                         "text": self.chunks[i]["text"]} for _, i in items[:excerpts_per_case]]
            results.append({
                "case_id": case["case_id"], "case_title": case["case_title"], "court": case["court"],
                "date": case["date"], "year": case.get("year", ""), "region": case.get("region", ""),
                "ipc_sections": case["ipc_sections"], "bail_type": case["bail_type"],
                "bail_outcome": case["bail_outcome"], "crime_type": case.get("crime_type", ""),
                "acts": case["_acts"], "similarity": round(final, 4),
                "text_score": round(text_score, 4), "rule_bonus": round(bonus, 4),
                "match_reasons": reasons or ["matched on wording of the facts only"],
                "excerpts": excerpts,
            })
        return results


_INDEX: HybridIndex | None = None
_LOCK = threading.Lock()


def get_index() -> HybridIndex:
    global _INDEX
    if _INDEX is None:
        with _LOCK:
            if _INDEX is None:
                _INDEX = HybridIndex()
    return _INDEX


def search(query: str, **kwargs) -> list[dict]:
    return get_index().search(query, **kwargs)
