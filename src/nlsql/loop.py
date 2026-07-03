"""Executor loop: (retrieve -> plan ->) generate -> execute -> repair on error, then explain.

The phase-2 core is generate -> execute -> repair (feed the DB error back to the generator,
up to max_repairs) -> explain. Phase 3 bolts two optional stages onto the front: a
Schema-Retriever that prunes the DDL to the tables relevant to the question, and a Planner
that decomposes the question into steps for the generator. Both are opt-in, so the phase-2
behavior is exactly `solve(...)` with the defaults.
"""
from dataclasses import dataclass

from . import agent, bird, config, executor, explainer, gate, planner, retriever


@dataclass
class Solution:
    sql: str                        # final SQL (last attempt)
    result: executor.ExecResult      # execution result of the final SQL
    attempts: int                   # SQL executions run (1 = ran clean, no repair)
    sqls: list[str]                 # every SQL tried, in order
    errors: list[str]               # errors captured between attempts, in order
    explanation: str | None         # plain-language answer, or None if SQL never ran
    tables: list[str] | None        # tables the retriever kept, or None if retrieval was off
    plan: list[str] | None          # planner steps, or None if planning was off
    retrieval_scores: list[float] | None = None  # cosine of the kept tables (gate input)
    confidence: float | None = None  # gate confidence, or None if the gate was off
    abstained: bool = False          # gate refused — route to a human instead of answering
    abstain_reason: str | None = None  # why it refused (weakest signal), or None
    gate_signals: dict | None = None  # the per-signal breakdown behind `confidence`


def solve(
    question: str,
    evidence: str,
    db_id: str,
    max_repairs: int = 3,
    model: str | None = None,
    explain: bool = True,
    retrieve_top_k: int | None = None,
    use_planner: bool = False,
    use_gate: bool = False,
    abstain_threshold: float | None = None,
) -> Solution:
    if retrieve_top_k is not None:
        scored = retriever.retrieve_scored(question, evidence, db_id, retrieve_top_k)
        tables, scores = [n for n, _ in scored], [s for _, s in scored]
        schema = retriever.pruned_schema_ddl(db_id, tables)
    else:
        tables, scores, schema = None, None, bird.schema_ddl(db_id)
    steps = planner.plan(question, evidence, schema, model) if use_planner else None
    db = str(config.db_path(db_id))

    sql = agent.generate_sql(question, evidence, schema, model, plan=steps)
    sqls, errors = [sql], []
    result = executor.execute(sql, db)

    for _ in range(max_repairs):
        if result.ok:
            break
        errors.append(result.error)
        sql = agent.repair_sql(question, evidence, schema, sql, result.error, model)
        sqls.append(sql)
        result = executor.execute(sql, db)

    explanation = explainer.explain(question, result.rows, model) if explain and result.ok else None
    sol = Solution(sql, result, len(sqls), sqls, errors, explanation, tables, steps, scores)

    if use_gate:
        d = gate.decide(
            {"retrieval_scores": scores, "retrieved_tables": tables, "pred": sql,
             "attempts": len(sqls), "exec_ok": result.ok},
            abstain_threshold, max_repairs,
        )
        sol.confidence = d["confidence"]
        sol.abstained = d["abstain"]
        sol.abstain_reason = d["reason"] or None
        sol.gate_signals = d["signals"]
    return sol
