"""Schema-Retriever: semantic search over a DB's tables, return only the relevant ones.

Phase-3 component. Each table (name + column names) is embedded once and cached; the
question+evidence is embedded and matched by cosine similarity, and only the top-k tables'
CREATE statements are handed to the planner/generator instead of the full schema. Small
schemas (<= top_k tables) skip embedding entirely and pass through whole — most BIRD DBs
have few tables, so this keeps the common case free of embedding calls.
"""
import math
import sqlite3
from functools import lru_cache

from google.genai import types

from . import agent, bird, config

# db_id -> table document embeddings, in the same order as _table_meta(db_id).
_doc_cache: dict[str, list[list[float]]] = {}


def _read_meta(db_path: str) -> tuple[tuple[str, str, str], ...]:
    """Per table: (name, embedding_doc, create_sql). Pure SQLite, no API."""
    con = sqlite3.connect(db_path)
    try:
        names = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        meta = []
        for name in names:
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")').fetchall()]
            doc = f"Table {name}. Columns: {', '.join(cols)}."
            create = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()[0]
            meta.append((name, doc, create))
        return tuple(meta)
    finally:
        con.close()


@lru_cache(maxsize=None)
def _table_meta(db_id: str) -> tuple[tuple[str, str, str], ...]:
    return _read_meta(str(config.db_path(db_id)))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _embed(texts: list[str], task_type: str) -> list[list[float]]:
    """Batch-embed with the Gemini embedding model. Reuses the agent's genai client."""
    resp = agent._get_client().models.embed_content(
        model=config.EMBED_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(task_type=task_type),
    )
    return [e.values for e in resp.embeddings]


def _doc_embeddings(db_id: str) -> list[list[float]]:
    if db_id not in _doc_cache:
        _doc_cache[db_id] = _embed([m[1] for m in _table_meta(db_id)], "RETRIEVAL_DOCUMENT")
    return _doc_cache[db_id]


def retrieve(question: str, evidence: str, db_id: str, top_k: int | None = None) -> list[str]:
    """Table names most relevant to the question, by embedding cosine similarity.

    Returns every table when the schema has <= top_k of them (nothing to prune, no API call).
    """
    top_k = top_k or config.RETRIEVE_TOP_K
    names = [m[0] for m in _table_meta(db_id)]
    if len(names) <= top_k:
        return names
    qvec = _embed([f"{question}\n{evidence}".strip()], "RETRIEVAL_QUERY")[0]
    scored = sorted(
        zip(names, _doc_embeddings(db_id)), key=lambda nv: _cosine(qvec, nv[1]), reverse=True
    )
    return [n for n, _ in scored[:top_k]]


def _prune(meta: tuple[tuple[str, str, str], ...], tables: list[str]) -> str:
    keep = set(tables)
    return "\n".join(m[2] for m in meta if m[0] in keep)


def pruned_schema_ddl(db_id: str, tables: list[str]) -> str:
    """CREATE statements for just `tables`. Falls back to the full schema if none match."""
    return _prune(_table_meta(db_id), tables) or bird.schema_ddl(db_id)


def retrieve_schema(
    question: str, evidence: str, db_id: str, top_k: int | None = None
) -> tuple[list[str], str]:
    """(relevant table names, pruned DDL) — the schema the planner/generator actually sees."""
    tables = retrieve(question, evidence, db_id, top_k)
    return tables, pruned_schema_ddl(db_id, tables)


def _selfcheck():
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE store(id INTEGER, name TEXT)")
    con.execute("CREATE TABLE orders(id INTEGER, buyer_id INTEGER, total REAL)")
    con.commit()
    con.close()

    meta = _read_meta(path)
    assert {m[0] for m in meta} == {"orders", "store"}, meta
    assert "Columns: id, buyer_id, total" in dict((m[0], m[1]) for m in meta)["orders"]

    pruned = _prune(meta, ["orders"])
    assert "orders" in pruned and "store" not in pruned, pruned

    assert abs(_cosine([1, 0], [1, 0]) - 1.0) < 1e-9
    assert abs(_cosine([1, 0], [0, 1])) < 1e-9

    os.remove(path)
    print("retriever selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
