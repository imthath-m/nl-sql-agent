"""Executor loop: (retrieve -> plan ->) generate -> execute -> repair on error, then explain.

The phase-2 core is generate -> execute -> repair (feed the DB error back to the generator,
up to max_repairs) -> explain. Phase 3 bolts two optional stages onto the front: a
Schema-Retriever that prunes the DDL to the tables relevant to the question, and a Planner
that decomposes the question into steps for the generator. Both are opt-in, so the phase-2
behavior is exactly `solve(...)` with the defaults.
"""
from dataclasses import dataclass

from . import agent, bird, config, executor, explainer, gate, planner, retriever
from . import trace as _trace


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
    trace: dict | None = None        # phase-6 step trace, or None if tracing was off


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
    trace: bool = False,
) -> Solution:
    tr = _trace.Trace()

    if retrieve_top_k is not None:
        with tr.step("retrieve", {"question": question, "db_id": db_id, "top_k": retrieve_top_k}) as s:
            scored = retriever.retrieve_scored(question, evidence, db_id, retrieve_top_k)
            tables, scores = [n for n, _ in scored], [t for _, t in scored]
            schema = retriever.pruned_schema_ddl(db_id, tables)
            s["output"] = {"tables": tables, "scores": scores}
    else:
        tables, scores, schema = None, None, bird.schema_ddl(db_id)

    if use_planner:
        with tr.step("plan", {"question": question}) as s:
            steps = planner.plan(question, evidence, schema, model)
            s["output"] = {"steps": steps}
    else:
        steps = None
    db = str(config.db_path(db_id))

    with tr.step("generate", {"question": question, "plan": steps, "schema_tables": tables}) as s:
        sql = agent.generate_sql(question, evidence, schema, model, plan=steps)
        s["output"] = {"sql": sql}
    sqls, errors = [sql], []
    with tr.step("execute", {"sql": sql}) as s:
        result = executor.execute(sql, db)
        s["output"] = {"ok": result.ok, "rows": len(result.rows or []), "error": result.error}

    for _ in range(max_repairs):
        if result.ok:
            break
        errors.append(result.error)
        with tr.step("repair", {"prev_sql": sql, "error": result.error}) as s:
            sql = agent.repair_sql(question, evidence, schema, sql, result.error, model)
            s["output"] = {"sql": sql}
        sqls.append(sql)
        with tr.step("execute", {"sql": sql}) as s:
            result = executor.execute(sql, db)
            s["output"] = {"ok": result.ok, "rows": len(result.rows or []), "error": result.error}

    explanation = None
    if explain and result.ok:
        with tr.step("explain", {"rows": len(result.rows or [])}) as s:
            explanation = explainer.explain(question, result.rows, model)
            s["output"] = {"explanation": explanation}
    sol = Solution(sql, result, len(sqls), sqls, errors, explanation, tables, steps, scores)

    if use_gate:
        with tr.step("gate", {"attempts": len(sqls), "exec_ok": result.ok}) as s:
            d = gate.decide(
                {"retrieval_scores": scores, "retrieved_tables": tables, "pred": sql,
                 "attempts": len(sqls), "exec_ok": result.ok},
                abstain_threshold, max_repairs,
            )
            s["output"] = {"confidence": d["confidence"], "abstain": d["abstain"],
                           "reason": d["reason"], "signals": d["signals"]}
        sol.confidence = d["confidence"]
        sol.abstained = d["abstain"]
        sol.abstain_reason = d["reason"] or None
        sol.gate_signals = d["signals"]

    tr.link(answer=explanation, sql=sql, tables=tables, ddl=schema)
    sol.trace = tr.to_dict() if trace else None
    return sol
