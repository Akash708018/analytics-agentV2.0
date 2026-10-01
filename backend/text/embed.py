"""Text embeddings for keyword grouping. The ONLY place numpy/sklearn are used (D-B7-1).

Backends, chosen per run and recorded:
  ollama   a local Ollama server's /api/embed (default model bge-m3, multilingual: Hinglish)
  chargram TF-IDF over character 2-4-grams, L2-normalised -- offline, deterministic
Vectors are cached by (backend, model, sha256(text)).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import numpy as np

OLLAMA_URL = os.environ.get("AA_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("AA_EMBED_MODEL", "bge-m3")


class Cache:
    def __init__(self, path: Path | str | None = None):
        self.con = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self.con.execute("CREATE TABLE IF NOT EXISTS vec (k TEXT PRIMARY KEY, v TEXT)")

    @staticmethod
    def key(backend: str, text: str) -> str:
        return backend + ":" + hashlib.sha256(text.encode()).hexdigest()

    def get(self, backend: str, text: str):
        r = self.con.execute("SELECT v FROM vec WHERE k=?", (self.key(backend, text),)).fetchone()
        return None if r is None else np.array(json.loads(r[0]))

    def put(self, backend: str, text: str, vec) -> None:
        self.con.execute("INSERT OR REPLACE INTO vec VALUES (?, ?)",
                         (self.key(backend, text), json.dumps([float(x) for x in vec])))
        self.con.commit()


def ollama_available(timeout: float = 1.0) -> bool:
    try:
        import httpx
        return httpx.get(f"{OLLAMA_URL}/api/tags", timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001 -- unreachable is the answer
        return False


def embed_ollama(texts: list[str], cache: Cache) -> np.ndarray:
    import httpx
    tag = f"ollama/{OLLAMA_MODEL}"
    todo = [t for t in texts if cache.get(tag, t) is None]
    if todo:
        r = httpx.post(f"{OLLAMA_URL}/api/embed", json={"model": OLLAMA_MODEL, "input": todo},
                       timeout=120)
        r.raise_for_status()
        for t, v in zip(todo, r.json()["embeddings"], strict=True):
            cache.put(tag, t, v)
    m = np.array([cache.get(tag, t) for t in texts], dtype=float)
    return m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-12, None)


def embed_chargram(texts: list[str]) -> np.ndarray:
    from sklearn.feature_extraction.text import TfidfVectorizer
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), sublinear_tf=True)
    return vec.fit_transform(texts).toarray()      # rows are L2-normalised by default


#: What goes into the vector besides the model (B9 concept 1). Any change here, or in the model
#: or its dimensions, is a new GENERATION: groups of different generations are never compared.
INPUT_SPEC = "topic words of the keyword (facet words removed), lower-case; L2-normalised"


def version_of(used: str, dims: int) -> dict:
    """The embedding version contract: backend/model, dimensions, normalisation, input."""
    gen = hashlib.sha256(f"{used}|{dims if used.startswith('ollama') else 'vocab'}|"
                         f"{INPUT_SPEC}".encode()).hexdigest()[:12]
    return {"embedding": used, "dims": dims if used.startswith("ollama") else None,
            "dims_note": None if used.startswith("ollama") else "chargram vectors are fit per "
            "run; comparable only within one run", "normalisation": "l2",
            "input": INPUT_SPEC, "generation": gen}


def embed(texts: list[str], backend: str = "auto", cache: Cache | None = None
          ) -> tuple[np.ndarray, str]:
    if backend == "auto":
        backend = "ollama" if ollama_available() else "chargram"
    if backend == "ollama":
        return embed_ollama(texts, cache or Cache()), f"ollama/{OLLAMA_MODEL}"
    return embed_chargram(texts), "chargram/tfidf-char2-4"
