# Generator prompt tuning

- Command: `python -m nlsql.tune --dev 100 --heldout 200 --seed 13 --workers 4`
- Provider: openai  Model: gpt-5.6-luna  Reasoning effort: low  Seed: 13
- Git: e3a6ee33d8a322c48d21bbad0564600642418cb8-dirty  Timestamp: 2026-11-14T13:04:13+00:00
- Split: dev 100 / held-out 200 (disjoint, `splits.json`)

## Dev (selection set)

| prompt | targets | EX % | errors | taxonomy | cost USD |
|---|---|---|---|---|---|
| generator_v1 | - | 67.0 | 0 | correct 67, schema_linking 14, wrong_aggregation 10, wrong_filter 5, wrong_projection 3, join_error 1 | 0.0431 |
| generator_v2_schema_linking | schema_linking | 70.0 | 0 | correct 70, schema_linking 10, wrong_aggregation 9, wrong_filter 5, wrong_projection 4, dialect_error 1, join_error 1 | 0.0469 |
| generator_v2_aggregation | wrong_aggregation | 63.0 | 0 | correct 63, wrong_aggregation 12, schema_linking 11, wrong_filter 7, wrong_projection 6, join_error 1 | 0.0441 |
| generator_v2_filter | wrong_filter | 67.0 | 0 | correct 67, schema_linking 11, wrong_aggregation 9, wrong_projection 6, wrong_filter 4, join_error 2, dialect_error 1 | 0.0444 |

## Variant selection (top v1 dev failure causes)

- schema_linking (14): generator_v2_schema_linking — Ground each phrase in a table.column, honour evidence 'refers to' mappings, and drop tables that add no column or condition.
- wrong_aggregation (10): generator_v2_aggregation — Use evidence formulas exactly with CAST(... AS REAL) division, DISTINCT only when asked, ORDER BY ... LIMIT with NULLs excluded for superlatives.
- wrong_filter (5): generator_v2_filter — Copy filter literals verbatim from evidence/question (casing, spelling, codes) and add no unstated conditions.

## Winner

- Winner (dev EX only, frozen 2026-11-14T12:59:18+00:00): **generator_v2_schema_linking**
- Held-out challenger: generator_v2_schema_linking (winner)

## Held-out (test set, run once after freeze)

| prompt | EX % | errors | cost USD |
|---|---|---|---|
| generator_v1 | 56.0 | 0 | 0.0883 |
| generator_v2_schema_linking | 57.0 | 0 | 0.0919 |

- Delta (challenger - v1): +1.0 pp over 200 paired questions
- Discordant: b=8 (v1 only correct), c=10 (challenger only correct)
- Exact McNemar p = 0.8145
- Paired bootstrap 95% CI: [-3.0, 5.0] pp (10000 resamples, seed 0)

### Held-out EX by difficulty

| prompt | challenging (n) | moderate (n) | simple (n) |
|---|---|---|---|
| generator_v1 | 30.77 (13) | 50.75 (67) | 61.67 (120) |
| generator_v2_schema_linking | 30.77 (13) | 50.75 (67) | 63.33 (120) |

## Cost and latency

- Total $0.3588 (dev $0.1786, held-out $0.1802)

| run | mean s | p50 s | p95 s |
|---|---|---|---|
| dev_generator_v1 | 3.024 | 2.461 | 7.459 |
| dev_generator_v2_schema_linking | 2.88 | 2.327 | 6.546 |
| dev_generator_v2_aggregation | 2.547 | 1.731 | 6.427 |
| dev_generator_v2_filter | 2.631 | 1.951 | 7.251 |
| heldout_generator_v1 | 2.915 | 2.48 | 6.46 |
| heldout_generator_v2_schema_linking | 2.948 | 2.409 | 6.722 |

## Caveats

- Held-out n=200: only differences of several pp are detectable; a non-significant McNemar p is not evidence of no effect.
- Single run per prompt; OpenAI generation uses temperature 0 for gpt-4* models only (newer models reject it; reasoning_effort is set instead), so it is not deterministic, so EX has run-to-run noise of its own.
- Failure causes come from the rule-based taxonomy (structural SQL diff), not a human audit.
- Rows that hit generation/API errors count as EX=0.
- Cost uses recorded token counts (thinking tokens included); NLSQL_CACHE_DIR cache hits count as $0.
