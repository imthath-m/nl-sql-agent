## Objective

- Build an NL→SQL analytics agent.
- Start on BIRD-SQL.
- Then move to Spider 2.0-Lite.
- Ship it FDE-grade.

## Why BIRD first

- BIRD is easier than Spider 2.0.
- BIRD releases full gold SQL.
- Local self-scoring is simple.
- It has real-world messy values.
- It teaches external-knowledge grounding.
- Build your harness here cheaply.
- Port the same harness to Spider 2.0-Lite later.

## Model strategy

- Gemini Flash / Flash Lite runs the live agents.
- Frontier model runs offline only.
- Frontier generates synthetic hard cases.
- Frontier acts as the LLM-judge.

## Fixed rules

- Never fine-tune on gold SQL.
- Hold out a split you never tune on.
- Self-score locally each run.
- Treat benchmarks as checkpoints.
- Treat your eval suite as the dev loop.

## Tech Stack

- Full Google stack for building and deploying - ADK, GCP.

---

## Phase 1 — Setup and BIRD baseline

- Clone the BIRD-SQL repo.
- Download the BIRD dev set.
- Load its SQLite databases.
- Load its gold SQL.
- Wire the official execution-accuracy scorer.
- Carve out a 50-example smoke subset.
- Build a single-pass agent.
- Agent takes a question plus schema.
- Agent returns one SQL string.
- Run it on the 50-example subset.
- Log the baseline score.

## Phase 2 — Executor loop on BIRD

- Add an Executor component.
- Executor runs the generated SQL.
- Executor captures errors.
- Feed errors back to the generator.
- Retry up to a fixed cap.
- Add a Result-Explainer component.
- Explainer restates the answer plainly.
- Re-run the subset.
- Record the score delta.

## Phase 3 — Schema retriever on BIRD

- Add a Schema-Retriever component.
- Index all tables and columns.
- Use semantic search over the index.
- Return only relevant tables.
- Pass the pruned schema to the planner.
- Add a Planner component.
- Planner splits the question into steps.
- Run the full BIRD dev set.
- Log the first full BIRD score.

## Phase 4 — Eval suite

- This is the core deliverable.
- Build a failure taxonomy.
- Tag each failure by cause.
- Causes include schema-linking errors.
- Causes include wrong aggregation.
- Causes include dialect errors.
- Add a schema-linking accuracy check.
- Add a regression suite.
- Regression suite reruns after any change.
- Add an LLM-judge check.
- Judge scores "answered the right question."
- Track cost per run.
- Track latency per run.

## Phase 5 — Abstention gate

- Add an Abstention-Gate component.
- Gate scores schema-linking confidence.
- Gate refuses on low confidence.
- Refused queries route to a human.
- Log every abstention.
- Eval the gate on its own split.
- Measure false-refusal rate.
- Measure caught-error rate.
- Note the gate is your addition.
- The benchmark does not reward it.

## Phase 6 — Auditability and traces

- Log every agent step.
- Store each step as a trace.
- Link each answer to its SQL.
- Link each SQL to its retrieved schema.
- Build a thin review view.
- Review view shows the trace.
- Review view shows the confidence.
- Surface low-confidence cases first.

## Phase 7 — Port to Spider 2.0-Lite

- Clone xlang-ai/Spider2.
- Use the Lite setting.
- Load the released examples.
- Load the ground-truth tables.
- Adapt the generator for BigQuery dialect.
- Adapt the generator for Snowflake dialect.
- Handle 1,000-plus-column schemas.
- Reuse the same eval suite.
- Run the held-in split.
- Log the first Spider 2.0-Lite score.

## Phase 8 — Full run and handoff

- Run the full Spider 2.0-Lite set.
- Log the final score.
- Compare against the BIRD score.
- Write the failure analysis.
- Show one failure explained plainly.
- Write the README as a customer handoff.
- Document every component contract.
- Document the eval metrics.

## Tooling note

- Use Claude Code to build faster.
- Run your own agent for the score.
- Never let the tool do the task itself.