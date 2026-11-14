# NL-to-SQL run_all results

| mode | n | EX % | EX challenging % | EX moderate % | EX simple % | delta vs baseline (pp) | fixed / broken | est cost USD | cost/example USD | latency mean s | p50 s | p95 s | LLM calls | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 150 | 46.67 | 66.67 | 42.0 | 46.59 | - | - | 0.0710 | 0.00047 | 1.69 | 0.889 | 1.597 | 150 | 0 |
| loop | 150 | 47.33 | 66.67 | 42.0 | 47.73 | +0.67 | 2 / 1 | 0.0907 | 0.00060 | 1.579 | 1.45 | 2.155 | 303 | 0 |
| phase3 | 150 | 42.0 | 50.0 | 40.0 | 42.05 | -4.67 | 6 / 13 | 0.1572 | 0.00105 | 3.08 | 2.769 | 4.219 | 455 | 0 |

Total est. cost: $0.3189

## Top failure causes
- baseline: schema_linking (24), wrong_aggregation (24), wrong_filter (16)
- loop: schema_linking (24), wrong_aggregation (23), wrong_filter (16)
- phase3: wrong_aggregation (34), schema_linking (29), wrong_filter (11)

## Provenance
- Command: `python -m nlsql.run_all --n 150 --seed 0`
- Seed: 0
- Provider: openai  Model: gpt-4.1-mini
- Commit: fd5f0b6f3f4efb3a5c4d96505c5c90ea690f23e6
- Timestamp (UTC): 2026-11-14T05:48:10Z

Caveats: subset of n=150 BIRD dev examples (seed 0); cost is an estimate from token counts x configured prices; deltas are paired by question_id vs baseline.
