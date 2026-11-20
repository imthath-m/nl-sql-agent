# LLM judge vs EX

- Run file: `/home/user/nl-sql-agent/results/tune/heldout_generator_v2_schema_linking.json`
- Judge model: gpt-4.1 (provider openai)
- Command: `python -m nlsql.judge_check `
- Timestamp: 2026-11-14T05:52:28+00:00
- Rows: 200 (judged 200, errors 0)

## Confusion matrix

| | judge correct | judge wrong |
|---|---|---|
| EX=1 | 100 | 6 |
| EX=0 | 57 | 37 |

- Agreement: 68.5%
- Cohen's kappa: 0.3477
- EX=0 but judge=correct: 57
- EX=1 but judge=wrong: 6
- Judge cost: $0.1821 (57827 in / 8308 out tokens, 200 calls; cache hits $0)

## Disagreement examples

### q22 — EX=0, judge=correct

- Question: Which school in Contra Costa has the highest number of test takers?
- Judge reason: The predicted query finds the Contra Costa school with the most test takers, matching the gold query's intent.

Gold:
```sql
SELECT sname FROM satscores WHERE cname = 'Contra Costa' AND sname IS NOT NULL ORDER BY NumTstTakr DESC LIMIT 1
```
Pred:
```sql
SELECT s.School, sa.NumTstTakr
FROM satscores sa
JOIN schools s ON sa.cds = s.CDSCode
WHERE s.County = 'Contra Costa'
ORDER BY sa.NumTstTakr DESC
LIMIT 1
```

### q134 — EX=1, judge=wrong

- Question: In the branch where the largest number of crimes were committed in 1996, how many accounts were opened?
- Judge reason: The predicted query only considers one district with the highest crimes, not all branches tied for the maximum, and may miss ties handled by the gold query's GROUP BY.

Gold:
```sql
SELECT COUNT(T2.account_id) FROM district AS T1 INNER JOIN account AS T2 ON T1.district_id = T2.district_id GROUP BY T1.A16 ORDER BY T1.A16 DESC LIMIT 1
```
Pred:
```sql
SELECT COUNT(*) AS accounts_opened
FROM account
WHERE district_id = (
    SELECT district_id
    FROM district
    ORDER BY A16 DESC
    LIMIT 1
)
```

### q24 — EX=0, judge=correct

- Question: Give the names of the schools with the percent eligible for free meals in K-12 is more than 0.1 and test takers whose test score is greater than or equal to 1500?
- Judge reason: The predicted query uses equivalent logic and joins to select schools with >10% free meal eligibility and at least one test taker with score >=1500.

Gold:
```sql
SELECT T2.`School Name` FROM satscores AS T1 INNER JOIN frpm AS T2 ON T1.cds = T2.CDSCode WHERE CAST(T2.`Free Meal Count (K-12)` AS REAL) / T2.`Enrollment (K-12)` > 0.1 AND T1.NumGE1500 > 0
```
Pred:
```sql
SELECT schools.School
FROM schools
JOIN frpm ON schools.CDSCode = frpm.CDSCode
JOIN satscores ON schools.CDSCode = satscores.cds
WHERE (frpm.`Free Meal Count (K-12)` / frpm.`Enrollment (K-12)`) > 0.1
  AND satscores.NumGE1500 >= 1
```

### q307 — EX=1, judge=wrong

- Question: Name the atoms' elements that form bond TR000_2_3.
- Judge reason: The predicted query retrieves elements for both atoms in the bond, while the gold only retrieves one; this changes the intent.

Gold:
```sql
SELECT T2.element FROM connected AS T1 INNER JOIN atom AS T2 ON T1.atom_id = T2.atom_id WHERE T1.bond_id = 'TR000_2_3'
```
Pred:
```sql
SELECT a.element
FROM connected c
JOIN atom a ON (a.atom_id = c.atom_id OR a.atom_id = c.atom_id2)
WHERE c.bond_id = 'TR000_2_3'
```

### q49 — EX=0, judge=correct

- Question: Which different county has the most number of closed schools? Please provide the name of each school as well as the closure date.
- Judge reason: The predicted query returns all closed schools and their closure dates in the county with the most closed schools, matching the question's intent.

Gold:
```sql
SELECT DISTINCT County, School, ClosedDate FROM schools WHERE County = ( SELECT County FROM schools WHERE StatusType = 'Closed' GROUP BY County ORDER BY COUNT(School) DESC LIMIT 1 ) AND StatusType = 'Closed' AND school IS NOT NULL
```
Pred:
```sql
SELECT School, ClosedDate
FROM schools
WHERE County = (
    SELECT County
    FROM schools
    WHERE StatusType = 'Closed'
    GROUP BY County
    ORDER BY COUNT(*) DESC
    LIMIT 1
)
AND StatusType = 'Closed'
```

### q319 — EX=1, judge=wrong

- Question: How many elements are contained in bond_id TR001_3_4?
- Judge reason: The predicted query may count more atoms by including both atom_id and atom_id2, potentially double-counting elements, while the gold query only considers atom_id from connected.

Gold:
```sql
SELECT COUNT(DISTINCT T1.element) FROM atom AS T1 INNER JOIN connected AS T2 ON T1.atom_id = T2.atom_id WHERE T2.bond_id = 'TR001_3_4'
```
Pred:
```sql
SELECT COUNT(DISTINCT element) AS element_count
FROM atom
WHERE atom_id IN (
  SELECT atom_id FROM connected WHERE bond_id = 'TR001_3_4'
  UNION
  SELECT atom_id2 FROM connected WHERE bond_id = 'TR001_3_4'
)
```

### q120 — EX=0, judge=correct

- Question: From Year 1995 to 2000, who are the accounts holders from 'east Bohemia'. State the account ID the frequency of statement issuance.
- Judge reason: The predicted query retrieves the same information, with only minor differences in date filtering and district column name, which are likely equivalent.

Gold:
```sql
SELECT T1.account_id, T1.frequency FROM account AS T1 INNER JOIN district AS T2 ON T1.district_id = T2.district_id WHERE T2.A3 = 'east Bohemia' AND STRFTIME('%Y', T1.date) BETWEEN '1995' AND '2000'
```
Pred:
```sql
SELECT account.account_id, account.frequency
FROM account
JOIN district ON account.district_id = district.district_id
WHERE district.A4 = 'east Bohemia'
  AND account.date BETWEEN '1995-01-01' AND '2000-12-31'
```

### q382 — EX=1, judge=wrong

- Question: What are the cards that only available in paper and Japanese language?
- Judge reason: The predicted query adds unnecessary grouping and HAVING clauses, which may exclude correct results and does not guarantee only cards available in paper and Japanese.

Gold:
```sql
SELECT T1.name FROM cards AS T1 INNER JOIN foreign_data AS T2 ON T1.uuid = T2.uuid WHERE T1.availability = 'paper' AND T2.language = 'Japanese'
```
Pred:
```sql
SELECT cards.name
FROM cards
LEFT JOIN foreign_data ON cards.uuid = foreign_data.uuid AND foreign_data.language = 'Japanese'
WHERE cards.availability = 'paper'
GROUP BY cards.uuid
HAVING COUNT(DISTINCT cards.availability) = 1
   AND COUNT(DISTINCT foreign_data.language) = 1
```

### q131 — EX=0, judge=correct

- Question: Which district has highest active loan?
- Judge reason: The predicted query selects the district with the highest sum of active loan amounts, matching the gold query's intent.

Gold:
```sql
SELECT T2.A3 FROM account AS T1 INNER JOIN district AS T2 ON T1.district_id = T2.district_id INNER JOIN loan AS T3 ON T1.account_id = T3.account_id WHERE T3.status IN ('C', 'D') GROUP BY T2.A3 ORDER BY SUM(T3.amount) DESC LIMIT 1
```
Pred:
```sql
SELECT d.A3 AS district_name
FROM loan l
JOIN account a ON l.account_id = a.account_id
JOIN district d ON a.district_id = d.district_id
WHERE l.status IN ('C', 'D')
GROUP BY d.district_id
ORDER BY SUM(l.amount) DESC
LIMIT 1
```

### q1098 — EX=1, judge=wrong

- Question: What is Ajax's highest chance creation passing score and what is it classified as?
- Judge reason: The predicted query may return a mismatched class if multiple classes share the max score, due to grouping by class rather than selecting the class for the max score.

Gold:
```sql
SELECT t2.chanceCreationPassing, t2.chanceCreationPassingClass FROM Team AS t1 INNER JOIN Team_Attributes AS t2 ON t1.team_api_id = t2.team_api_id WHERE t1.team_long_name = 'Ajax' ORDER BY t2.chanceCreationPassing DESC LIMIT 1
```
Pred:
```sql
SELECT MAX(`chanceCreationPassing`) AS highest_chance_creation_passing, 
       `chanceCreationPassingClass` 
FROM `Team_Attributes` 
JOIN `Team` ON `Team_Attributes`.`team_api_id` = `Team`.`team_api_id` 
WHERE `team_long_name` = 'Ajax' 
GROUP BY `chanceCreationPassingClass` 
ORDER BY highest_chance_creation_passing DESC 
LIMIT 1
```

