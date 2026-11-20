# NL-to-SQL run_all results

| mode | n | EX % | EX challenging % | EX moderate % | EX simple % | delta vs baseline (pp) | fixed / broken | est cost USD | cost/example USD | latency mean s | p50 s | p95 s | LLM calls | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 150 | 52.67 | 33.33 | 46.0 | 59.09 | - | - | 0.0704 | 0.00047 | 3.645 | 2.507 | 7.713 | 150 | 0 |
| loop | 150 | 53.33 | 33.33 | 50.0 | 57.95 | +0.67 | 4 / 3 | 0.0888 | 0.00059 | 4.644 | 3.77 | 8.544 | 303 | 0 |
| phase3 | 150 | 47.33 | 33.33 | 36.0 | 55.68 | -5.33 | 3 / 11 | 0.1158 | 0.00077 | 5.528 | 4.786 | 11.683 | 451 | 0 |

Total est. cost: $0.2750

## Top failure causes
- baseline: schema_linking (28), wrong_aggregation (26), wrong_filter (10)
- loop: schema_linking (26), wrong_aggregation (26), wrong_filter (9)
- phase3: schema_linking (32), wrong_aggregation (31), wrong_filter (8)

## Provenance
- Command: `python -m nlsql.run_all --n 150 --seed 0`
- Seed: 0
- Provider: openai  Model: gpt-5.6-luna  Reasoning effort: low
- Commit: e3a6ee33d8a322c48d21bbad0564600642418cb8
- Timestamp (UTC): 2026-11-14T12:54:30Z

Caveats: subset of n=150 BIRD dev examples (seed 0); cost is an estimate from token counts x configured prices; deltas are paired by question_id vs baseline.
