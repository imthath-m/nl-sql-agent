"""Executor loop: generate -> execute -> repair on error, up to a cap, then explain.

This is the phase-2 core. agent.generate_sql does the first pass; on an execution failure
the captured error is fed back through agent.repair_sql and the query re-run, up to
max_repairs times. Once the SQL runs, explainer.explain restates the answer plainly.
"""
from dataclasses import dataclass

from . import agent, bird, config, executor, explainer


@dataclass
class Solution:
    sql: str                    # final SQL (last attempt)
    result: executor.ExecResult  # execution result of the final SQL
    attempts: int               # SQL executions run (1 = ran clean, no repair)
    sqls: list[str]             # every SQL tried, in order
    errors: list[str]           # errors captured between attempts, in order
    explanation: str | None     # plain-language answer, or None if SQL never ran


def solve(
    question: str,
    evidence: str,
    db_id: str,
    max_repairs: int = 3,
    model: str | None = None,
    explain: bool = True,
) -> Solution:
    schema = bird.schema_ddl(db_id)
    db = str(config.db_path(db_id))

    sql = agent.generate_sql(question, evidence, schema, model)
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
    return Solution(sql, result, len(sqls), sqls, errors, explanation)
