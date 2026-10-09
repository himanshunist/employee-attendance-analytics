# REVIEW.md

Defects found in the starter `app/main.py` (line numbers refer to the starter) and how the rewrite fixes them.

| # | Where (function / line) | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` (33-37) | Grace boundary uses `minutes > 10`, so 09:40:01 (10.02 min) returns 0. R2 says late when **strictly more than** 10:00, and `late_minutes` is the full minutes since `shift_start`. Also uses `replace()` which places `shift_start` on the punch-in's calendar date, wrong for after-midnight overnight punches. | Punch in at 09:40:01 for a 09:30 shift → expected 10, got 0. | Compare exact seconds against shift start and `floor(seconds // 60)`; guard `<= 600` → 0. Overnight handled by passing the computed attendance date. |
| 2 | `compute_work_hours` (40-41) | Python `round()` is banker's rounding, not R4 half-up. | 2.125h would round to 2.12 instead of 2.13. | `Decimal(...).quantize(Decimal("0.01"), ROUND_HALF_UP)`. |
| 3 | `compute_overtime` (44-47) | Builds `shift_end` on the record's date (wrong day for overnight), has no 30-minute threshold, `max(0, ...)` hides the shift-end-earlier case, and subtracting a naive `punch_out` from an aware `end` raises `TypeError`. | 18:59:59 punch-out returns 29 instead of 0; overnight raises 500. | Compute shift end on the correct (possibly next) day, floor minutes, return 0 unless `>= 30`, all in UTC-aware datetimes. |
| 4 | everywhere | Instants are never truncated to whole seconds (R1) and `datetime.fromtimestamp` uses the server's local zone, not UTC/IST. | `punched_at` ending in 900 ms is stored/returned with 900 ms. | `to_instant()` truncates with `// 1000`; everything is timezone-aware UTC. |
| 5 | `EmployeeIn` (53-60) | No validation: `emp_code` format, name/email/department bounds, `HH:MM` shift format, real `joined_on`, and `shift_start != shift_end` are all unchecked. | `emp_code="x"` or `joined_on="2026-13-01"` are accepted. | Pydantic `pattern`/length constraints plus `joined_on` and shift validators (equal shifts → 422). |
| 6 | `PunchInIn` (63-66) | `punched_at` is a bare `int` (accepts seconds and floats), and `status` is a free string (accepts `ABSENT`/`LEAVE`). | `punched_at=12345` or `status="ABSENT"` accepted. | `StrictInt` with `ge=100000000000`/`le=4102444800000`; status limited to the presence enum. |
| 7 | `/health` (72-74) | Always returns 200, never pings MongoDB. | API reports ready while the DB is down. | `db.command("ping")`, 503 on `PyMongoError`. |
| 8 | `create_employee` (77-85) | Check-then-insert race (no unique index), naive `datetime.now()`, no `shift_start != shift_end` guard, and returns `created_at` as a `datetime`, not epoch ms. | Two concurrent creates both succeed; response `created_at` is not an integer. | Unique index + catch `DuplicateKeyError` → 409; `created_at` is epoch ms. |
| 9 | `list_employees` (88-96) | `skip = page * page_size` is off by one page; `total = count_documents({})` ignores the filter; no `emp_code` sort; no pagination bounds. | `page=1` skips the first 20; `total` wrong when filtering. | `skip = (page-1)*page_size`, `total` uses the filter, sorted by `emp_code`, bounds via `Query(ge=..., le=...)`. |
| 10 | `punch_in` (99-121) | No 404 for unknown employee (KeyError → 500); local naive timestamp and no second truncation; `date` ignores IST and the overnight rule; check-then-insert race; exposes `id`; returns datetimes. | Unknown employee → 500; late-morning IST punch stored on the wrong day. | Explicit 404, IST-based date (R1), unique index + `DuplicateKeyError`, no `_id`/`id`, epoch-ms serialization. |
| 11 | `list_attendance` (124-150) | Loads the whole collection into memory and sorts/slices in Python; `date_from > date_to` not validated; sorts only by `date` (no `emp_code` tiebreak); exposes `id`; returns datetimes. | Slow on ~100k docs; ties ordered arbitrarily. | DB-side filter/sort/skip/limit with `(date desc, emp_code asc)` index, range validation, clean serialization. |
| 12 | missing endpoints | `punch-out`, the `PATCH` regularizer, all five analytics endpoints and `/admin/explain` were not implemented. | 404 on every call. | Implemented all of them per `openapi.yaml`. |
| 13 | no indexes | No indexes are created at startup, so uniqueness and the explain plans cannot hold. | Duplicate `emp_code` accepted; `COLLSCAN` on 100k. | Idempotent startup indexes for both collections (see DECISIONS #1). |

## Looked at, not a defect

* `load_dotenv()` default behaviour — it does **not** override real environment variables, so `MONGO_URI`/`MONGO_DB` from the environment still win. Correct as written.
* Creating the `MongoClient` at import time (lazy connection) — fine; PyMongo connects on first use.
* `IST = timezone(timedelta(hours=5, minutes=30))` — a correct constant, just not used consistently in the starter.
* Returns `total` reflecting the filtered set — the *intent* matches the contract; only the implementation (row 9) was wrong.
