"""Executor loop: (retrieve -> plan ->) generate -> execute -> repair on error, then explain.

The phase-2 core is generate -> execute -> repair (feed the DB error back to the generator,
up to max_repairs) -> explain. Phase 3 bolts two optional stages onto the front: a
Schema-Retriever that prunes the DDL to the tables relevant to the question, and a Planner
that decomposes the question into steps for the generator. Both are opt-in, so the phase-2
behavior is exactly `solve(...)` with the defaults.
"""
from dataclasses import dataclass

from . import agent, bird, config, executor, explainer, planner, retriever


@dataclass
class Solution:
    sql: str                    # final SQL (last attempt)
    result: executor.ExecResult  # execution result of the final SQL
    attempts: int               # SQL executions run (1 = ran clean, no repair)
    sqls: list[str]             # every SQL tried, in order
    errors: list[str]           # errors captured between attempts, in order
    explanation: str | None     # plain-language answer, or None if SQL never ran
    tables: list[str] | None    # tables the retriever kept, or None if retrieval was off
    plan: list[str] | None      # planner steps, or None if planning was off


def solve(
    question: str,
    evidence: str,
    db_id: str,
    max_repairs: int = 3,
    model: str | None = None,
    explain: bool = True,
    retrieve_top_k: int | None = None,
    use_planner: bool = False,
) -> Solution:
    if retrieve_top_k is not None:
        tables, schema = retriever.retrieve_schema(question, evidence, db_id, retrieve_top_k)
    else:
        tables, schema = None, bird.schema_ddl(db_id)
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
    return Solution(sql, result, len(sqls), sqls, errors, explanation, tables, steps)
