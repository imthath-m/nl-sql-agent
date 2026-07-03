"""Schema-linking accuracy: which tables a SQL query touches, and how well the
predicted / retrieved tables match the gold query's tables.

Phase-4 eval component. Rule-based and offline — extracts table names from a SQL
string with a small regex parser (no DB, no API) so it is deterministic and
self-testable. `link_accuracy` compares predicted-vs-gold table sets;
`retrieval_recall` scores the phase-3 retriever's kept tables against the gold.
"""
import re

# Words that can follow FROM/JOIN but are not tables.
_NOISE = {"select", "lateral"}

# Keywords that end a FROM/JOIN table clause.
_STOP = (
    r"WHERE|GROUP|ORDER|LIMIT|HAVING|ON|USING|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|"
    r"NATURAL|UNION|EXCEPT|INTERSECT|SELECT"
)
_CLAUSE = re.compile(
    rf"\b(?:FROM|JOIN)\b\s+(.+?)(?=\b(?:{_STOP})\b|\)|;|$)", re.IGNORECASE | re.DOTALL
)
_STRING = re.compile(r"'(?:[^']|'')*'")


# Leading table token in a ref: a quoted/bracketed name (may contain spaces) or a bare word.
_LEAD = re.compile(r"""^\s*(`[^`]+`|"[^"]+"|\[[^\]]+\]|\w+)""")


def _unquote(tok: str) -> str:
    return tok.strip("`\"[]")


def tables_in_sql(sql: str) -> set[str]:
    """Lower-cased set of table names referenced in the query (FROM + every JOIN).

    Handles comma joins (`FROM a, b`), aliases (`FROM a AS t1`), quoting/backticks
    /brackets, and nested subqueries (each subquery's own FROM/JOIN is captured too).
    String literals are stripped first so values never look like tables.
    """
    s = _STRING.sub("''", sql or "")
    tables: set[str] = set()
    for m in _CLAUSE.finditer(s):
        for ref in m.group(1).split(","):
            ref = ref.strip()
            if not ref or ref.startswith("("):  # subquery — its FROM is matched separately
                continue
            lead = _LEAD.match(ref)
            if not lead:
                continue
            name = _unquote(lead.group(1)).lower()
            if name and name not in _NOISE:
                tables.add(name)
    return tables


def _prf(pred: set, gold: set) -> dict:
    tp = len(pred & gold)
    precision = tp / len(pred) if pred else (1.0 if not gold else 0.0)
    recall = tp / len(gold) if gold else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


def link_accuracy(pred_sql: str, gold_sql: str) -> dict:
    """Table-level schema-linking score of predicted SQL vs gold SQL.

    Returns precision/recall/f1 over table sets plus the missing/extra tables and an
    `exact` flag (the two queries touch exactly the same tables).
    """
    pred, gold = tables_in_sql(pred_sql), tables_in_sql(gold_sql)
    out = _prf(pred, gold)
    out["exact"] = pred == gold
    out["missing_tables"] = sorted(gold - pred)  # gold tables the prediction omitted
    out["extra_tables"] = sorted(pred - gold)  # tables the prediction added
    out["pred_tables"] = sorted(pred)
    out["gold_tables"] = sorted(gold)
    return out


def retrieval_recall(retrieved: list[str] | None, gold_sql: str) -> dict | None:
    """Did the phase-3 retriever keep the tables the gold query needs?

    Returns recall over gold tables plus which gold tables were dropped. None when the
    run has no retrieval (retrieved is None), so callers can skip it for baseline/loop.
    """
    if retrieved is None:
        return None
    kept = {t.lower() for t in retrieved}
    gold = tables_in_sql(gold_sql)
    covered = gold & kept
    recall = len(covered) / len(gold) if gold else 1.0
    return {
        "recall": round(recall, 4),
        "covers_all": gold <= kept,
        "dropped_tables": sorted(gold - kept),  # gold tables the retriever pruned away
    }


def _selfcheck():
    assert tables_in_sql("SELECT * FROM Orders") == {"orders"}
    assert tables_in_sql("SELECT * FROM `a b`") == {"a b"}
    assert tables_in_sql("SELECT * FROM store s, orders o WHERE s.id = o.sid") == {
        "store",
        "orders",
    }
    assert tables_in_sql(
        "SELECT * FROM a AS T1 INNER JOIN b T2 ON T1.id=T2.aid LEFT JOIN c ON c.x=1"
    ) == {"a", "b", "c"}
    # subquery tables are captured; string literals never look like tables
    assert tables_in_sql("SELECT * FROM a WHERE x IN (SELECT y FROM b)") == {"a", "b"}
    assert tables_in_sql("SELECT * FROM a WHERE name = 'from nowhere'") == {"a"}

    acc = link_accuracy("SELECT * FROM a JOIN b ON a.id=b.id", "SELECT * FROM a")
    assert acc["exact"] is False and acc["extra_tables"] == ["b"] and acc["recall"] == 1.0, acc
    acc2 = link_accuracy("SELECT * FROM a", "SELECT * FROM a JOIN b ON a.id=b.id")
    assert acc2["missing_tables"] == ["b"] and acc2["f1"] < 1.0, acc2
    perfect = link_accuracy("SELECT x FROM a", "SELECT y FROM a")
    assert perfect["exact"] and perfect["f1"] == 1.0, perfect

    rr = retrieval_recall(["A", "c"], "SELECT * FROM a JOIN b ON a.id=b.id")
    assert rr["recall"] == 0.5 and rr["dropped_tables"] == ["b"] and rr["covers_all"] is False, rr
    assert retrieval_recall(None, "SELECT * FROM a") is None
    print("schema_link selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
