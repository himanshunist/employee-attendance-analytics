# DECISIONS.md

**1. Indexes.** `employees.emp_code` (unique) enforces the business key and serves name/department lookups. `employees.(department, joined_on)` serves department headcount and trend, which filter on `joined_on <= end`. `employees.joined_on` serves the R9 headcount match in the summary. `attendance_logs.(emp_code, date)` (unique) enforces one record per employee-day and drives the punch-in race plus per-employee queries. `attendance_logs.(date desc, emp_code asc)` serves the attendance list sort/range and the date-range scans in leaderboard and trend. I considered and rejected `(status, date)`: status is low cardinality and no listed query filters on status alone.

**2. Punch-in race.** Both requests compute the same `(emp_code, date)` and insert. The unique index lets exactly one insert succeed; the other raises `DuplicateKeyError`, which I translate to 409. There is no check-then-insert.

**3. Ties.** The leaderboard uses `$rank` (competition ranking), so ties share a rank and the next rank is skipped. `limit` is applied *after* ranking as `rank <= limit`, so two employees tied at the cutoff rank are both returned — the response may exceed `limit`.

**4. Headcount.** The summary aggregation starts from `employees` with `joined_on <= month_end` and `$lookup`s the logs with `preserveNullAndEmptyArrays`, so an employee with zero logs still becomes a group and counts once.

**5. At 100x.** I would maintain incremental daily per-employee rollups (via `$merge`) and have analytics read those small summaries instead of scanning every log, plus archive old logs by date.
