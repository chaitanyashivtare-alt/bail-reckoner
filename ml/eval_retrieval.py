"""Compare the old facts-only TF-IDF search with the new hybrid retriever.

Metric: precision@5 under a PROXY notion of relevance - a retrieved judgment counts as relevant when it
shares a (non-generic) section, after IPC<->BNS mapping, or a special Act with the query case. This
measures whether the retriever respects the statute the user is actually charged under. It does NOT
measure legal relevance; that needs lawyer-labelled pairs (see README, "Next steps").

    python ml/eval_retrieval.py [--n 200]
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RAG_DISABLE_DENSE", "1")

from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: E402
from sklearn.metrics.pairwise import cosine_similarity  # noqa: E402

from services.rag_retrieval import get_index  # noqa: E402


def main(n: int, k: int = 5) -> None:
    ix = get_index()
    cases = ix.cases
    by_id = {c["case_id"]: c for c in cases}
    rng = random.Random(7)
    pool = [c for c in cases if c["_sections"] or c["_acts"]]
    queries = rng.sample(pool, min(n, len(pool)))

    vec = TfidfVectorizer(stop_words="english", max_features=50000)
    matrix = vec.fit_transform([c["search_text"] for c in cases])

    def relevant(q, r):
        if r["row_id"] == q["row_id"]:
            return False
        return bool(ix.expand_sections(q["_sections"]) & r["_sections"]) or bool(set(q["_acts"]) & set(r["_acts"]))

    def score(retrieve):
        hits = total = 0
        for q in queries:
            got = [r for r in retrieve(q) if r["row_id"] != q["row_id"]][:k]
            hits += sum(relevant(q, r) for r in got)
            total += k
        return hits / total

    def baseline(q):  # what engine.search_cases' TF-IDF path does: facts text only
        scores = cosine_similarity(vec.transform([q["facts"]]), matrix).ravel()
        return [cases[i] for i in scores.argsort()[::-1][: k + 1]]

    def hybrid(use_entered):
        def run(q):
            res = ix.search(q["facts"], sections_text=q["ipc_sections"] if use_entered else "",
                            special_laws_text=" ".join(q["_acts"]) if use_entered else "", top_k=k + 1)
            return [by_id[r["case_id"]] for r in res]
        return run

    print(f"{len(queries)} query cases, precision@{k} (proxy relevance: shared section or Act)\n")
    print(f"  Old: facts-only TF-IDF                       {score(baseline):.3f}")
    print(f"  New: hybrid, facts only (no sections typed)  {score(hybrid(False)):.3f}")
    print(f"  New: hybrid, sections + Acts entered         {score(hybrid(True)):.3f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    main(ap.parse_args().n)
