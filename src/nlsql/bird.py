"""Load BIRD dev examples, carve the smoke subset, and read a DB's schema DDL."""
import json
import random
import sqlite3
from functools import lru_cache

from . import config


def load_examples() -> list[dict]:
    """Full BIRD dev set. Each item: question_id, db_id, question, evidence, SQL, difficulty."""
    with open(config.DEV_JSON) as f:
        return json.load(f)


def smoke_subset(examples: list[dict], n: int = 50, seed: int = 0) -> list[dict]:
    """Deterministic n-example smoke subset. Seed fixed so the baseline is reproducible."""
    idx = sorted(random.Random(seed).sample(range(len(examples)), n))
    return [examples[i] for i in idx]


@lru_cache(maxsize=None)
def schema_ddl(db_id: str) -> str:
    """CREATE statements for all tables/indexes in the DB — the schema we hand the agent.

    ponytail: raw sqlite_master DDL is enough for a baseline. Column descriptions and
    sample rows are a phase-3 concern (schema retriever), add them there.
    """
    con = sqlite3.connect(config.db_path(db_id))
    try:
        rows = con.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND type IN ('table', 'index') "
            "ORDER BY type DESC"  # tables before indexes
        ).fetchall()
    finally:
        con.close()
    return "\n".join(r[0] for r in rows)
