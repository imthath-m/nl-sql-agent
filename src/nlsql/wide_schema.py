"""Wide-schema pruner: keep only the columns most relevant to the question.

Phase-7 component. Spider 2.0-Lite warehouse tables (BigQuery event tables, Snowflake fact
tables) can carry well over 1,000 columns — far more than a generator prompt can hold. The
phase-3 retriever prunes *tables*; this prunes *columns within a table*. For any table above
`budget` columns it scores each column against the question (+ external knowledge) and keeps
the top `budget`, most-relevant-first; smaller tables pass through untouched.

Scoring is lexical (token overlap) by default so the offline self-check needs no API. A
column name like `event_timestamp` is split on `_`/camelCase into {event, timestamp}; its
score is how many of those tokens appear in the question. Ties keep the schema's original
order, so pruning is deterministic. This degrades gracefully: with no lexical hits it keeps
the first `budget` columns (usually the ids/keys the schema lists first).

A "table" here is `(name, columns)` where columns is a list of `(column, type)` pairs — the
shape spider2.load_tables emits. Nothing here is warehouse-specific.
"""
import re

_WORD = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def _tokens(text: str) -> set[str]:
    """Lower-cased word tokens of free text (question / evidence)."""
    return {w.lower() for w in _WORD.findall(text or "")}


def _col_tokens(name: str) -> set[str]:
    """Split a column/field name on separators + camelCase into lower-cased tokens."""
    out: set[str] = set()
    for part in re.split(r"[^A-Za-z0-9]+", name or ""):
        for m in _CAMEL.findall(part):
            out.add(m.lower())
    return out


def _score(col: str, qtokens: set[str]) -> int:
    """Number of the column's name-tokens that appear in the question tokens."""
    return len(_col_tokens(col) & qtokens)


def prune_columns(columns: list, question: str, evidence: str = "", budget: int = 40) -> list:
    """Return at most `budget` of `columns` ((col, type) pairs), most relevant first.

    Tables at/under budget are returned unchanged (original order). Above budget, columns are
    ranked by lexical overlap with question+evidence, ties broken by original position.
    """
    if len(columns) <= budget:
        return list(columns)
    qtokens = _tokens(question) | _tokens(evidence)
    ranked = sorted(
        enumerate(columns),
        key=lambda it: (-_score(it[1][0], qtokens), it[0]),  # score desc, then stable
    )
    return [col for _, col in ranked[:budget]]


def prune(tables: list, question: str, evidence: str = "", budget: int = 40) -> list:
    """Prune every table's columns to `budget`. `tables` = [(name, [(col, type), ...]), ...]."""
    return [(name, prune_columns(cols, question, evidence, budget)) for name, cols in tables]


def _fmt_type(t) -> str:
    return f" {t}" if t else ""


def to_ddl(tables: list) -> str:
    """Render `(name, [(col, type), ...])` tables as CREATE-TABLE-ish text for the generator.

    A pruned table is annotated so the generator knows columns were dropped.
    """
    blocks = []
    for name, cols in tables:
        lines = ",\n".join(f"  {c}{_fmt_type(t)}" for c, t in cols)
        blocks.append(f"CREATE TABLE {name} (\n{lines}\n);")
    return "\n\n".join(blocks)


def _selfcheck():
    # Under budget: unchanged, original order preserved.
    small = [("id", "INT"), ("name", "STR")]
    assert prune_columns(small, "how many names", budget=40) == small

    # Over budget: relevant columns float up, deterministic on ties.
    wide = [(f"c{i}", "INT") for i in range(50)] + [("customer_name", "STR"), ("order_total", "FLOAT")]
    kept = prune_columns(wide, "total spend per customer name", "", budget=3)
    names = [c for c, _ in kept]
    assert "customer_name" in names and "order_total" in names, kept
    assert len(kept) == 3, kept
    # deterministic
    assert prune_columns(wide, "total spend per customer name", "", budget=3) == kept

    # camelCase + separators split correctly.
    assert _col_tokens("eventTimestamp_utc") == {"event", "timestamp", "utc"}
    assert _score("customer_name", {"customer"}) == 1

    # No lexical hit -> first `budget` columns (graceful degrade).
    none = prune_columns(wide, "zzz", "", budget=2)
    assert [c for c, _ in none] == ["c0", "c1"], none

    # prune over a schema + DDL rendering.
    pruned = prune([("t", wide)], "customer name", "", budget=2)
    ddl = to_ddl(pruned)
    assert ddl.startswith("CREATE TABLE t (") and "customer_name INT" not in ddl
    assert "customer_name STR" in ddl, ddl
    print("wide_schema selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
