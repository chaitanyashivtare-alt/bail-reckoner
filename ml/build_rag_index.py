"""Build the OPTIONAL dense index for the RAG retriever.

The retriever works without this (BM25 + rules). Run this once to add sentence-transformer
embeddings of every judgment chunk, which improves recall for paraphrased queries.

    python ml/build_rag_index.py            # needs: pip install sentence-transformers
    python ml/build_rag_index.py --model sentence-transformers/all-MiniLM-L6-v2

Outputs (git-ignored): vector_store/chunk_vectors.npy, vector_store/chunk_model.txt
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.rag_retrieval import CHUNK_MODEL_FILE, CHUNK_VECTORS, build_chunks, load_cases  # noqa: E402


def main(model_name: str) -> None:
    import numpy as np
    from sentence_transformers import SentenceTransformer

    cases = load_cases()
    chunks = build_chunks(cases)
    print(f"{len(cases)} judgments -> {len(chunks)} chunks")
    model = SentenceTransformer(model_name)
    vectors = model.encode([c["index_text"] for c in chunks], normalize_embeddings=True,
                           show_progress_bar=True, batch_size=64)
    CHUNK_VECTORS.parent.mkdir(parents=True, exist_ok=True)
    np.save(CHUNK_VECTORS, np.asarray(vectors, dtype="float32"))
    CHUNK_MODEL_FILE.write_text(model_name, encoding="utf-8")
    print(f"Saved {CHUNK_VECTORS} ({vectors.shape}) using {model_name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    main(ap.parse_args().model)
