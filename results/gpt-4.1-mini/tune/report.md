# Generator prompt tuning

- Command: `python -m nlsql.tune --dev 100 --heldout 200 --seed 13 --workers 4`
- Provider: openai  Model: gpt-4.1-mini  Seed: 13
- Git: 52f2b6bbf903f958f50f986b955fdfbc69f78a6f-dirty  Timestamp: 2026-11-14T05:51:48+00:00
- Split: dev 100 / held-out 200 (disjoint, `splits.json`)

## Dev (selection set)

| prompt | targets | EX % | errors | taxonomy | cost USD |
|---|---|---|---|---|---|
| generator_v1 | - | 58.0 | 0 | correct 58, schema_linking 15, wrong_aggregation 12, wrong_filter 6, wrong_projection 6, join_error 2, execution_error 1 | 0.0446 |
| generator_v2_schema_linking | schema_linking | 59.0 | 0 | correct 59, wrong_aggregation 15, schema_linking 14, wrong_projection 5, wrong_filter 4, join_error 2, execution_error 1 | 0.0481 |
| generator_v2_aggregation | wrong_aggregation | 58.0 | 0 | correct 58, wrong_aggregation 13, schema_linking 12, wrong_filter 7, wrong_projection 6, execution_error 2, join_error 2 | 0.0504 |
| generator_v2_filter | wrong_filter | 56.0 | 0 | correct 56, schema_linking 14, wrong_aggregation 14, wrong_filter 6, wrong_projection 6, execution_error 2, join_error 2 | 0.0482 |

## Variant selection (top v1 dev failure causes)

- schema_linking (15): generator_v2_schema_linking — Ground each phrase in a table.column, honour evidence 'refers to' mappings, and drop tables that add no column or condition.
- wrong_aggregation (12): generator_v2_aggregation — Use evidence formulas exactly with CAST(... AS REAL) division, DISTINCT only when asked, ORDER BY ... LIMIT with NULLs excluded for superlatives.
- wrong_filter (6): generator_v2_filter — Copy filter literals verbatim from evidence/question (casing, spelling, codes) and add no unstated conditions.

## Winner

- Winner (dev EX only, frozen 2026-11-14T05:49:54+00:00): **generator_v2_schema_linking**
- Held-out challenger: generator_v2_schema_linking (winner)

## Held-out (test set, run once after freeze)

| prompt | EX % | errors | cost USD |
|---|---|---|---|
| generator_v1 | 49.5 | 0 | 0.0890 |
| generator_v2_schema_linking | 53.0 | 0 | 0.0967 |

- Delta (challenger - v1): +3.5 pp over 200 paired questions
- Discordant: b=5 (v1 only correct), c=12 (challenger only correct)
- Exact McNemar p = 0.1435
- Paired bootstrap 95% CI: [-0.5, 7.5] pp (10000 resamples, seed 0)

### Held-out EX by difficulty

| prompt | challenging (n) | moderate (n) | simple (n) |
|---|---|---|---|
| generator_v1 | 53.85 (13) | 35.82 (67) | 56.67 (120) |
| generator_v2_schema_linking | 61.54 (13) | 38.81 (67) | 60.0 (120) |

## Cost and latency

- Total $0.3770 (dev $0.1913, held-out $0.1857)

| run | mean s | p50 s | p95 s |
|---|---|---|---|
| dev_generator_v1 | 1.305 | 0.941 | 4.766 |
| dev_generator_v2_schema_linking | 0.847 | 0.81 | 1.237 |
| dev_generator_v2_aggregation | 0.872 | 0.841 | 1.266 |
| dev_generator_v2_filter | 0.91 | 0.843 | 1.453 |
| heldout_generator_v1 | 1.216 | 0.828 | 1.336 |
| heldout_generator_v2_schema_linking | 0.907 | 0.81 | 1.304 |

## Caveats

- Held-out n=200: only differences of several pp are detectable; a non-significant McNemar p is not evidence of no effect.
- Single run per prompt; OpenAI generation uses temperature 0 but is still not fully deterministic, so EX has run-to-run noise of its own.
- Failure causes come from the rule-based taxonomy (structural SQL diff), not a human audit.
- Rows that hit generation/API errors count as EX=0.
- Cost uses recorded token counts (thinking tokens included); NLSQL_CACHE_DIR cache hits count as $0.
