"""Trace: an ordered, self-summarizing record of every step the agent takes on one example.

Phase-6 component (auditability). A `Trace` collects `Step`s in the order the pipeline runs
them — retrieve -> plan -> generate -> execute -> (repair -> execute)* -> gate -> explain —
each capturing its name, a compact summary of inputs and outputs, latency, and a timestamp.
`loop.solve(..., trace=True)` builds one and attaches `Trace.to_dict()` to the `Solution`.

Two things it makes explicit, so an auditor can follow the chain end to end:
  - the ordered per-step trace ("log every agent step");
  - the answer -> final SQL -> retrieved schema link (`link()` / `Trace.links`): the
    plain-language answer, the SQL that produced it, and the tables + pruned DDL that SQL
    was written against.

Everything is stored as short summaries (long strings capped), never full prompts, so an
embedded trace stays small enough to live inside the run file next to the result row.
"""
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

_STR_CAP = 600   # per-string summary cap
_DDL_CAP = 4000  # schema DDL cap in the answer->sql->schema link


def _summ(v, cap: int = _STR_CAP):
    """Compact, JSON-safe summary of a step value: long strings truncated, containers shrunk."""
    if isinstance(v, str):
        return v if len(v) <= cap else v[:cap] + f"... (+{len(v) - cap} chars)"
    if isinstance(v, dict):
        return {k: _summ(x, cap) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_summ(x, cap) for x in v[:20]]
    return v


@dataclass
class Step:
    name: str          # retrieve | plan | generate | execute | repair | gate | explain
    inputs: dict       # summarized inputs
    output: dict       # summarized output
    latency_s: float   # wall-clock seconds spent in the step
    ts: str            # UTC ISO-8601 start time


@dataclass
class Trace:
    steps: list = field(default_factory=list)
    links: dict = field(default_factory=dict)

    @contextmanager
    def step(self, name: str, inputs: dict | None = None):
        """Time a step. Yields a mutable dict; set its `output` key inside the block.

            with tr.step("generate", {"question": q}) as s:
                sql = generate(...)
                s["output"] = {"sql": sql}
        """
        rec: dict = {"output": {}}
        ts = datetime.now(timezone.utc).isoformat()
        t0 = time.time()
        try:
            yield rec
        finally:
            self.steps.append(Step(
                name=name,
                inputs=_summ(inputs or {}),
                output=_summ(rec.get("output") or {}),
                latency_s=round(time.time() - t0, 4),
                ts=ts,
            ))

    def link(self, answer: str | None, sql: str, tables: list | None, ddl: str) -> None:
        """Record the answer -> final SQL -> retrieved schema chain (the audit link)."""
        self.links = {
            "answer": _summ(answer) if answer is not None else None,
            "sql": _summ(sql or ""),
            "schema": {
                "tables": tables,  # kept tables, or None when retrieval was off
                "pruned_ddl": _summ(ddl or "", _DDL_CAP),
            },
        }

    def to_dict(self) -> dict:
        return {"steps": [asdict(s) for s in self.steps], "links": self.links}


def _selfcheck():
    tr = Trace()
    with tr.step("retrieve", {"question": "how many stores?", "top_k": 5}) as s:
        s["output"] = {"tables": ["store", "orders"], "scores": [0.9, 0.7]}
    with tr.step("generate", {"schema_tables": ["store"]}) as s:
        s["output"] = {"sql": "SELECT COUNT(*) FROM store"}
    tr.link(answer="There are 12 stores.", sql="SELECT COUNT(*) FROM store",
            tables=["store"], ddl="CREATE TABLE store(id INTEGER, name TEXT)")

    d = tr.to_dict()
    assert [s["name"] for s in d["steps"]] == ["retrieve", "generate"], d
    assert all("latency_s" in s and "ts" in s for s in d["steps"]), d
    assert d["steps"][0]["output"]["tables"] == ["store", "orders"], d
    assert d["links"]["answer"] == "There are 12 stores." and d["links"]["sql"].startswith("SELECT"), d
    assert d["links"]["schema"]["tables"] == ["store"], d
    assert "CREATE TABLE store" in d["links"]["schema"]["pruned_ddl"], d

    # Long strings are capped; the trace stays small.
    big = _summ("x" * (_STR_CAP + 100))
    assert big.endswith("chars)") and len(big) < _STR_CAP + 40, big

    # Answer=None is allowed (SQL never ran / abstained) and stays JSON-safe.
    tr2 = Trace()
    tr2.link(answer=None, sql="SELECT 1", tables=None, ddl="")
    assert tr2.to_dict()["links"]["answer"] is None
    print("trace selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
