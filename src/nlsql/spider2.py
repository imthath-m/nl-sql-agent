"""Load Spider 2.0-Lite examples, schemas, and gold results from a cloned xlang-ai/Spider2.

Phase-7 loader — the Spider2 analogue of bird.py. It reads the released `spider2-lite/`
layout (see the README "Data setup" section for the user-side clone):

    spider2-lite/
      spider2-lite.jsonl              # examples: instance_id, db, question, external_knowledge
      evaluation_suite/gold/sql/<id>.sql          # gold SQL (dialect-native)
      evaluation_suite/gold/exec_result/<id>.csv  # gold result table (what we score against)
      resource/documents/<name>.md                # external-knowledge docs
      resource/databases/<platform>/<db>/**.json  # per-table schema descriptors
      resource/databases/sqlite/<db>.sqlite       # local SQLite DBs (executable here)

The platform (bigquery / snowflake / sqlite) is inferred from the instance-id prefix
(`bq…`, `sf…`, `local…`). Everything reads `config.SPIDER2_DIR` at call time and raises a
clear FileNotFoundError when an expected file is missing, so a partial clone fails loudly.
Live BigQuery/Snowflake execution is a separate concern (see warehouse.py); this module only
loads text + gold results, so it needs no warehouse credentials.
"""
import json
import random
import sqlite3
from functools import lru_cache
from pathlib import Path

from . import config, wide_schema


def _root() -> Path:
    return Path(config.SPIDER2_DIR)


def platform_of(instance_id: str) -> str:
    """bigquery | snowflake | sqlite, from the instance-id prefix (sqlite is the fallback)."""
    p = instance_id.lower()
    if p.startswith("sf"):
        return "snowflake"
    if p.startswith("bq"):
        return "bigquery"
    if p.startswith("local"):
        return "sqlite"
    return "sqlite"


def _examples_file() -> Path:
    for name in ("spider2-lite.jsonl", "spider2-lite.json"):
        p = _root() / name
        if p.exists():
            return p
    raise FileNotFoundError(
        f"no spider2-lite.jsonl / .json under {_root()} — clone xlang-ai/Spider2 and set "
        f"SPIDER2_DIR to its spider2-lite/ dir (see README 'Data setup')."
    )


def load_examples() -> list[dict]:
    """Released Spider2-Lite examples, each tagged with its `platform`."""
    p = _examples_file()
    if p.suffix == ".jsonl":
        rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    else:
        rows = json.loads(p.read_text())
    for r in rows:
        r["platform"] = platform_of(r["instance_id"])
    return rows


def held_in_split(examples: list[dict], n: int = 50, seed: int = 0) -> list[dict]:
    """Deterministic n-example held-in split (seed fixed, like bird.smoke_subset)."""
    idx = sorted(random.Random(seed).sample(range(len(examples)), min(n, len(examples))))
    return [examples[i] for i in idx]


def external_knowledge(example: dict) -> str:
    """Concatenated text of the example's external-knowledge doc(s); "" when none/missing."""
    ref = example.get("external_knowledge")
    if not ref:
        return ""
    names = ref if isinstance(ref, list) else [ref]
    dirs = [_root() / "resource" / "documents",
            _root() / "resource" / "documentation" / "external_knowledge"]
    out = []
    for name in names:
        for d in dirs:
            p = d / name
            if p.exists():
                out.append(p.read_text())
                break
    return "\n\n".join(out)


def gold_sql(instance_id: str) -> str:
    """Dialect-native gold SQL for an instance."""
    p = _root() / "evaluation_suite" / "gold" / "sql" / f"{instance_id}.sql"
    if not p.exists():
        raise FileNotFoundError(f"missing gold SQL: {p}")
    return p.read_text().strip()


def gold_result_path(instance_id: str) -> Path:
    """Path to the gold execution-result CSV (scored by spider2_score)."""
    return _root() / "evaluation_suite" / "gold" / "exec_result" / f"{instance_id}.csv"


def sqlite_path(db: str) -> Path:
    """Local SQLite file for a `sqlite`-platform db (executable via executor.py)."""
    base = _root() / "resource" / "databases" / "sqlite"
    for cand in (base / f"{db}.sqlite", base / db / f"{db}.sqlite", base / db / f"{db}.db"):
        if cand.exists():
            return cand
    raise FileNotFoundError(f"missing SQLite DB for {db!r} under {base}")


def _sqlite_tables(path: Path) -> list:
    con = sqlite3.connect(str(path))
    try:
        names = [
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return [
            (n, [(r[1], r[2]) for r in con.execute(f'PRAGMA table_info("{n}")')])
            for n in names
        ]
    finally:
        con.close()


def _json_tables(db_dir: Path) -> list:
    """Tables from BigQuery/Snowflake per-table JSON descriptors under db_dir.

    Uses the flattened nested column list when present (repeated/STRUCT fields matter for
    the generator). Sharded tables that share an identical column signature (e.g. GA4
    events_YYYYMMDD) collapse to one representative, so the prompt is not flooded with copies.
    """
    tables, seen = [], set()
    for jf in sorted(db_dir.glob("**/*.json")):
        try:
            d = json.loads(jf.read_text())
        except (ValueError, OSError):
            continue
        cols = d.get("nested_column_names") or d.get("column_names")
        if not cols:
            continue
        types = d.get("nested_column_types") if d.get("nested_column_names") else d.get("column_types")
        types = types or []
        types = types + [""] * (len(cols) - len(types))
        sig = tuple(cols)
        if sig in seen:  # collapse date-sharded duplicates
            continue
        seen.add(sig)
        name = d.get("table_name") or jf.stem
        tables.append((name, list(zip(cols, types))))
    return tables


@lru_cache(maxsize=None)
def load_tables(db: str, platform: str) -> tuple:
    """Per-table `(name, [(column, type), ...])` for a db. Cached; raises if the db is absent."""
    if platform == "sqlite":
        return tuple(_sqlite_tables(sqlite_path(db)))
    db_dir = _root() / "resource" / "databases" / platform / db
    if not db_dir.exists():
        raise FileNotFoundError(f"missing schema dir for {platform} db {db!r}: {db_dir}")
    tables = _json_tables(db_dir)
    if not tables:
        raise FileNotFoundError(f"no table JSON descriptors under {db_dir}")
    return tuple(tables)


def table_names(db: str, platform: str) -> list[str]:
    return [t[0] for t in load_tables(db, platform)]


def schema_ddl(db: str, platform: str, question: str = "", evidence: str = "",
               col_budget: int | None = None) -> str:
    """CREATE-TABLE-ish DDL for the generator, wide tables pruned to the relevant columns."""
    budget = col_budget or config.SPIDER2_COL_BUDGET
    pruned = wide_schema.prune(list(load_tables(db, platform)), question, evidence, budget)
    return wide_schema.to_ddl(pruned)


def _selfcheck():
    import csv
    import tempfile

    from . import spider2_score

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "spider2-lite"
        (root / "evaluation_suite" / "gold" / "sql").mkdir(parents=True)
        (root / "evaluation_suite" / "gold" / "exec_result").mkdir(parents=True)
        (root / "resource" / "documents").mkdir(parents=True)
        bq_dir = root / "resource" / "databases" / "bigquery" / "ga" / "dataset"
        bq_dir.mkdir(parents=True)
        sq_dir = root / "resource" / "databases" / "sqlite"
        sq_dir.mkdir(parents=True)

        # examples (one BigQuery, one SQLite) + external knowledge doc + gold sql/result
        exs = [
            {"instance_id": "bq001", "db": "ga", "question": "total revenue by channel",
             "external_knowledge": "note.md"},
            {"instance_id": "local001", "db": "shop", "question": "how many orders",
             "external_knowledge": None},
        ]
        (root / "spider2-lite.jsonl").write_text("\n".join(json.dumps(e) for e in exs))
        (root / "resource" / "documents" / "note.md").write_text("revenue = price * qty")
        (root / "evaluation_suite" / "gold" / "sql" / "bq001.sql").write_text("SELECT 1")
        with open(root / "evaluation_suite" / "gold" / "exec_result" / "bq001.csv", "w", newline="") as f:
            csv.writer(f).writerows([["n"], ["1"]])

        # a wide BigQuery table (> budget) + a date-sharded duplicate to test collapse
        wide = {
            "table_name": "events",
            "column_names": [f"c{i}" for i in range(30)] + ["revenue", "channel"],
            "column_types": ["INT"] * 30 + ["FLOAT", "STRING"],
        }
        (bq_dir / "events_20210101.json").write_text(json.dumps(wide))
        (bq_dir / "events_20210102.json").write_text(json.dumps(wide))  # same signature -> collapsed

        # a small SQLite db
        con = sqlite3.connect(str(sq_dir / "shop.sqlite"))
        con.execute("CREATE TABLE orders(id INTEGER, total REAL)")
        con.commit()
        con.close()

        old = config.SPIDER2_DIR
        config.SPIDER2_DIR = root
        load_tables.cache_clear()
        try:
            got = load_examples()
            assert [e["platform"] for e in got] == ["bigquery", "sqlite"], got
            assert platform_of("sf_bq009") == "snowflake"

            split = held_in_split(got, 1)
            assert len(split) == 1 and held_in_split(got, 1) == split  # deterministic

            assert external_knowledge(got[0]) == "revenue = price * qty"
            assert external_knowledge(got[1]) == ""  # no doc
            assert gold_sql("bq001") == "SELECT 1"
            assert spider2_score.score_csv([(1,)], str(gold_result_path("bq001"))) == 1

            # BigQuery schema: sharded duplicate collapsed to one table, wide cols pruned.
            bq_tables = load_tables("ga", "bigquery")
            assert len(bq_tables) == 1, bq_tables  # events_2021010{1,2} collapsed
            ddl = schema_ddl("ga", "bigquery", "total revenue by channel", "", col_budget=2)
            assert "revenue FLOAT" in ddl and "channel STRING" in ddl, ddl
            assert "c0 INT" not in ddl, ddl  # non-relevant columns pruned out

            # SQLite schema + path resolve.
            assert table_names("shop", "sqlite") == ["orders"]
            assert sqlite_path("shop").exists()
        finally:
            config.SPIDER2_DIR = old
            load_tables.cache_clear()
    print("spider2 selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
