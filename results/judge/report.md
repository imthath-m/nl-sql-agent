# LLM judge vs EX

- Run file: `/home/user/nl-sql-agent/results/tune/heldout_generator_v2_schema_linking.json`
- Judge model: gpt-5.6-terra (provider openai)
- Command: `python -m nlsql.judge_check `
- Timestamp: 2026-11-14T13:06:01+00:00
- Rows: 200 (judged 200, errors 0)

## Confusion matrix

| | judge correct | judge wrong |
|---|---|---|
| EX=1 | 102 | 12 |
| EX=0 | 32 | 54 |

- Agreement: 78.0%
- Cohen's kappa: 0.538
- EX=0 but judge=correct: 32
- EX=1 but judge=wrong: 12
- Judge cost: $0.3330 (58383 in / 18021 out tokens, 200 calls; cache hits $0)

## Disagreement examples

### q46 — EX=0, judge=correct

- Question: Which state special schools have the highest number of enrollees from grades 1 through 12?
- Judge reason: It filters state special schools and returns the school or schools with the maximum K-12 enrollment, including ties.

Gold:
```sql
SELECT T2.School FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.DOC = 31 ORDER BY T1.`Enrollment (K-12)` DESC LIMIT 1
```
Pred:
```sql
SELECT s.School, f.`Enrollment (K-12)`
FROM schools AS s
JOIN frpm AS f
  ON f.CDSCode = s.CDSCode
WHERE s.DOC = '31'
  AND f.`Enrollment (K-12)` = (
    SELECT MAX(f2.`Enrollment (K-12)`)
    FROM schools AS s2
    JOIN frpm AS f2
      ON f2.CDSCode = s2.CDSCode
    WHERE s2.DOC = '31'
  )
```

### q74 — EX=1, judge=wrong

- Question: What is the lowest grade for the District Special Education Consortia School with National Center for Educational Statistics school district identification number of 0613360?
- Judge reason: The predicted query returns all matching low grades instead of the minimum low grade.

Gold:
```sql
SELECT MIN(T1.`Low Grade`) FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.NCESDist = '0613360' AND T2.EdOpsCode = 'SPECON'
```
Pred:
```sql
SELECT f.`Low Grade`
FROM schools AS s
JOIN frpm AS f
  ON f.CDSCode = s.CDSCode
WHERE s.NCESDist = '0613360'
  AND s.EdOpsCode = 'SPECON'
```

### q234 — EX=0, judge=correct

- Question: How many bonds which involved atom 12 does molecule TR009 have?
- Judge reason: The predicted query correctly counts distinct TR009 bonds involving atom TR009_12 in either endpoint column.

Gold:
```sql
SELECT COUNT(T2.bond_id) FROM bond AS T1 INNER JOIN connected AS T2 ON T1.bond_id = T2.bond_id WHERE T1.molecule_id = 'TR009' AND T2.atom_id = T1.molecule_id || '_1' AND T2.atom_id2 = T1.molecule_id || '_2'
```
Pred:
```sql
SELECT COUNT(DISTINCT b.bond_id)
FROM bond AS b
JOIN connected AS c
  ON c.bond_id = b.bond_id
WHERE b.molecule_id = 'TR009'
  AND (c.atom_id = 'TR009_12' OR c.atom_id2 = 'TR009_12')
```

### q85 — EX=1, judge=wrong

- Question: What is the Percent (%) Eligible Free (K-12) in the school administered by an administrator whose first name is Alusine. List the district code of the school.
- Judge reason: The predicted query incorrectly includes schools where Alusine is listed as the second or third administrator rather than only AdmFName1.

Gold:
```sql
SELECT T1.`Free Meal Count (K-12)` * 100 / T1.`Enrollment (K-12)`, T1.`District Code` FROM frpm AS T1 INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode WHERE T2.AdmFName1 = 'Alusine'
```
Pred:
```sql
SELECT
    f.`Free Meal Count (K-12)` * 100.0 / NULLIF(f.`Enrollment (K-12)`, 0) AS `Percent (%) Eligible Free (K-12)`,
    f.`District Code`
FROM frpm AS f
JOIN schools AS s
    ON s.CDSCode = f.CDSCode
WHERE s.AdmFName1 = 'Alusine'
   OR s.AdmFName2 = 'Alusine'
   OR s.AdmFName3 = 'Alusine'
```

### q363 — EX=0, judge=correct

- Question: How many cards of legalities whose status is restricted are found in a starter deck?
- Judge reason: The query applies the same joins and filters and counts distinct cards, with only case variation in the status literal.

Gold:
```sql
SELECT COUNT(DISTINCT T1.id) FROM cards AS T1 INNER JOIN legalities AS T2 ON T1.uuid = T2.uuid WHERE T2.status = 'Restricted' AND T1.isStarter = 1
```
Pred:
```sql
SELECT COUNT(DISTINCT c.uuid)
FROM cards AS c
JOIN legalities AS l ON l.uuid = c.uuid
WHERE l.status = 'restricted'
  AND c.isStarter = 1
```

### q120 — EX=1, judge=wrong

- Question: From Year 1995 to 2000, who are the accounts holders from 'east Bohemia'. State the account ID the frequency of statement issuance.
- Judge reason: The predicted query filters owners by their client district, whereas the gold filters accounts by the account's district.

Gold:
```sql
SELECT T1.account_id, T1.frequency FROM account AS T1 INNER JOIN district AS T2 ON T1.district_id = T2.district_id WHERE T2.A3 = 'east Bohemia' AND STRFTIME('%Y', T1.date) BETWEEN '1995' AND '2000'
```
Pred:
```sql
SELECT DISTINCT
    a.account_id,
    a.frequency
FROM account AS a
JOIN disp AS dp
    ON dp.account_id = a.account_id
JOIN client AS c
    ON c.client_id = dp.client_id
JOIN district AS d
    ON d.district_id = c.district_id
WHERE dp.type = 'OWNER'
  AND d.A3 = 'east Bohemia'
  AND a.date >= '1995-01-01'
  AND a.date < '2001-01-01'
```

### q371 — EX=0, judge=correct

- Question: What is the percentage of cards whose language is French among the Story Spotlight cards?
- Judge reason: It correctly computes French Story Spotlight cards divided by all Story Spotlight cards, multiplied by 100.

Gold:
```sql
SELECT CAST(SUM(CASE WHEN T2.language = 'French' THEN 1 ELSE 0 END) AS REAL) * 100 / COUNT(T1.id) FROM cards AS T1 INNER JOIN foreign_data AS T2 ON T1.uuid = T2.uuid WHERE T1.isStorySpotlight = 1
```
Pred:
```sql
SELECT 100.0 * (
    SELECT COUNT(fd.id)
    FROM foreign_data AS fd
    JOIN cards AS c ON c.uuid = fd.uuid
    WHERE fd.language = 'French'
      AND c.isStorySpotlight = 1
) / NULLIF((
    SELECT COUNT(c.id)
    FROM cards AS c
    WHERE c.isStorySpotlight = 1
), 0) AS percentage
```

### q147 — EX=1, judge=wrong

- Question: How many female clients' accounts are in the region of South Bohemia?
- Judge reason: The gold query counts female clients in South Bohemia, while the predicted query counts distinct accounts linked to those clients.

Gold:
```sql
SELECT COUNT(T1.client_id) FROM client AS T1 INNER JOIN district AS T2 ON T1.district_id = T2.district_id WHERE T1.gender = 'F' AND T2.A3 = 'south Bohemia'
```
Pred:
```sql
SELECT COUNT(DISTINCT a.account_id)
FROM account AS a
JOIN disp AS dp ON dp.account_id = a.account_id
JOIN client AS c ON c.client_id = dp.client_id
JOIN district AS d ON d.district_id = c.district_id
WHERE c.gender = 'F'
  AND d.A3 = 'south Bohemia'
```

### q406 — EX=0, judge=correct

- Question: Lists by ID all Creature-type cards with legal status.
- Judge reason: The predicted query correctly returns IDs of Creature cards with legal status, with DISTINCT avoiding duplicates.

Gold:
```sql
SELECT T1.id FROM cards AS T1 INNER JOIN rulings AS T2 ON T1.uuid = T2.uuid INNER JOIN legalities AS T3 ON T1.uuid = T3.uuid WHERE T3.status = 'Legal' AND T1.types = 'Creature'
```
Pred:
```sql
SELECT DISTINCT c.id
FROM cards AS c
JOIN legalities AS l ON l.uuid = c.uuid
WHERE c.types = 'Creature'
  AND l.status = 'legal'
```

### q284 — EX=1, judge=wrong

- Question: Determine the bond type that is formed in the chemical compound containing element Carbon.
- Judge reason: The predicted query returns only bonds directly connected to carbon, whereas the gold returns all bond types in molecules containing carbon.

Gold:
```sql
SELECT DISTINCT T2.bond_type FROM atom AS T1 INNER JOIN bond AS T2 ON T1.molecule_id = T2.molecule_id WHERE T1.element = 'c'
```
Pred:
```sql
SELECT DISTINCT b.bond_type
FROM atom AS a
JOIN connected AS c
  ON a.atom_id = c.atom_id OR a.atom_id = c.atom_id2
JOIN bond AS b
  ON b.bond_id = c.bond_id
WHERE a.element = 'c'
```

