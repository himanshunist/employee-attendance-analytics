# Employee Attendance & Analytics API

FastAPI + MongoDB service for employee attendance, manual corrections and analytics, implementing
`openapi.yaml` and the business rules R1-R10. All application code lives in `app/main.py`.

## Requirements

- Python 3.10+
- MongoDB 6.0 or newer (7.0 recommended); a local `mongod` or an Atlas M0 works

## Setup

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # set MONGO_URI / MONGO_DB
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` come from the environment; a local `.env` is read for convenience but
real environment variables always win. Indexes are created at startup, idempotently. MongoDB must
be reachable before the runner polls `GET /health`.

Seed the nine sample documents (optional):

```bash
python sample_seed.py
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Readiness probe (pings MongoDB) |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees (filter + pagination) |
| POST | `/attendance/punch-in` | Open the day's record |
| POST | `/attendance/punch-out` | Close the most recent open record |
| GET | `/attendance` | List records (filter + pagination) |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize a day's record (audited) |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary |
| GET | `/analytics/departments/summary` | Department summary |
| GET | `/analytics/leaderboard/late` | Late-comers leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily attendance trend |
| GET | `/admin/explain/{endpoint}` | Raw MongoDB explain for an endpoint |

## Design notes

- **Instants** are epoch **milliseconds** (integers). Seconds-looking values, floats and strings are
  rejected with 422. **Calendar values** (`date`, `month`, `HH:MM`) are IST strings.
- **R1-R10** are implemented exactly as documented in `openapi.yaml`; derived fields
  (`work_hours`, `late_minutes`, `overtime_minutes`, `half_day`) are computed by the API and ignored
  if sent in a request.
- **Concurrency.** A unique index on `(emp_code, date)` makes duplicate punch-ins return exactly one
  201 and the rest 409; punch-outs use an atomic `find_one_and_update` filtered on `punch_out: null`.
- **Analytics** run as MongoDB aggregations; the trend gap-fills every calendar day with `$densify`
  and computes moving averages with `$setWindowFields`. Rounding is half-up (implemented explicitly,
  since `$round` is half-to-even).
- **Indexes** cover every explained query: plans contain `IXSCAN` and no `COLLSCAN`.

`REVIEW.md` lists the starter defects and fixes; `DECISIONS.md` records the main engineering choices.

## Tests

The suite uses `MONGO_DB=attendance_test` and drops that database between tests.

```bash
pytest -q
```

## Submit

Public Git repository with `app/main.py`, `requirements.txt`, `README.md`, `REVIEW.md` and
`DECISIONS.md`. No `.env`, no secrets, no Dockerfile.
