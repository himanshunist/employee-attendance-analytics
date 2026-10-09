"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience, real
      environment variables always win).

openapi.yaml defines the HTTP contract and the business rules R1-R10.
DATA_MODEL.md defines what MongoDB stores. All application code is in this file.
"""

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

logger = logging.getLogger("attendance")

EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

GRACE_SECONDS = 10 * 60
OVERTIME_MIN_MINUTES = 30
MAX_PUNCH_SECONDS = 24 * 3600
MAX_TREND_DAYS = 92

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")

StatusLiteral = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatusLiteral = Literal["PRESENT", "WFH", "ON_DUTY"]

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
EXPLAIN_ENDPOINT_PATTERN = (
    r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"
)

ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]
CHANGABLE_FIELDS = (
    "status",
    "punch_in",
    "punch_out",
    "work_hours",
    "late_minutes",
    "overtime_minutes",
    "half_day",
)

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="employees_emp_code_unique"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="employees_department_joined_on",
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="employees_joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="logs_emp_code_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="logs_date_emp_code"
    )
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:
        logger.warning("could not create indexes at startup: %s", exc)
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


def to_instant(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms // 1000, tz=UTC)


def now_instant() -> datetime:
    return datetime.now(tz=UTC).replace(microsecond=0)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def parse_iso_date(value: str) -> date:
    if not re.fullmatch(DATE_PATTERN, value):
        raise ValueError("expected a date in YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("not a real calendar date") from exc


def hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < hhmm(shift_end):
        day -= timedelta(days=1)
    return day


def shift_start_dt(day: date, shift_start: str) -> datetime:
    return datetime.combine(day, hhmm(shift_start), tzinfo=IST)


def shift_end_dt(day: date, shift_start: str, shift_end: str) -> datetime:
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    return datetime.combine(end_day, hhmm(shift_end), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, day: date) -> int:
    delta = (punch_in - shift_start_dt(day, shift_start)).total_seconds()
    if delta <= GRACE_SECONDS:
        return 0
    return int(delta // 60)


def compute_overtime_minutes(
    punch_out: datetime, shift_start: str, shift_end: str, day: date
) -> int:
    delta = (punch_out - shift_end_dt(day, shift_start, shift_end)).total_seconds()
    minutes = int(delta // 60)
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    value = Decimal(seconds) / Decimal(3600)
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up(value: Optional[float], places: int) -> Optional[float]:
    if value is None:
        return None
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def derive_fields(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    employee: dict,
    day: date,
) -> dict:
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    late = compute_late_minutes(punch_in, employee["shift_start"], day)
    if punch_out is None:
        return {
            "work_hours": None,
            "late_minutes": late,
            "overtime_minutes": 0,
            "half_day": False,
        }
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(
            punch_out, employee["shift_start"], employee["shift_end"], day
        ),
        "half_day": work_hours < 4.5,
    }


def checked_date(name: str, value: str) -> date:
    try:
        return parse_iso_date(value)
    except ValueError as exc:
        raise HTTPException(422, f"{name}: {exc}") from None


def checked_date_range(date_from: Optional[str], date_to: Optional[str]) -> None:
    start = checked_date("date_from", date_from) if date_from else None
    end = checked_date("date_to", date_to) if date_to else None
    if start is not None and end is not None and start > end:
        raise HTTPException(422, "date_from must not be after date_to")


def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        before = change.get("from")
        after = change.get("to")
        if field in ("punch_in", "punch_out"):
            before = to_epoch_ms(before) if isinstance(before, datetime) else before
            after = to_epoch_ms(after) if isinstance(after, datetime) else after
        changes[field] = {"from": before, "to": after}
    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by", ""),
        "reason": entry.get("reason", ""),
        "changes": changes,
    }


def serialize_record(record: dict) -> dict:
    return {
        "emp_code": record["emp_code"],
        "date": record["date"],
        "status": record["status"],
        "punch_in": to_epoch_ms(record.get("punch_in")),
        "punch_out": to_epoch_ms(record.get("punch_out")),
        "work_hours": record.get("work_hours"),
        "late_minutes": int(record.get("late_minutes") or 0),
        "overtime_minutes": int(record.get("overtime_minutes") or 0),
        "half_day": bool(record.get("half_day", False)),
        "history": [serialize_history_entry(e) for e in (record.get("history") or [])],
    }


def serialize_employee(employee: dict) -> dict:
    return {
        "emp_code": employee["emp_code"],
        "name": employee["name"],
        "email": employee["email"],
        "department": employee["department"],
        "shift_start": employee["shift_start"],
        "shift_end": employee["shift_end"],
        "joined_on": employee["joined_on"],
        "created_at": to_epoch_ms(employee.get("created_at")),
    }


def month_bounds(month: str) -> tuple[date, date]:
    first = date.fromisoformat(month + "-01")
    if first.month == 12:
        next_first = date(first.year + 1, 1, 1)
    else:
        next_first = date(first.year, first.month + 1, 1)
    return first, next_first - timedelta(days=1)


def working_days_between(start: date, end: date) -> int:
    count = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, value: str) -> str:
        parse_iso_date(value)
        return value

    @model_validator(mode="after")
    def _shifts_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    status: PresenceStatusLiteral = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)


class RegularizeRequest(BaseModel):
    status: Optional[StatusLiteral] = None
    punch_in: Optional[StrictInt] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    punch_out: Optional[StrictInt] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


def _present_days_sum(status_field: str, date_field: str, half_day_field: str) -> dict:
    return {
        "$sum": {
            "$cond": [
                {
                    "$and": [
                        {"$in": [status_field, list(PRESENCE_STATUSES)]},
                        {
                            "$lte": [
                                {
                                    "$isoDayOfWeek": {
                                        "$dateFromString": {"dateString": date_field}
                                    }
                                },
                                5,
                            ]
                        },
                    ]
                },
                {"$cond": [{"$eq": [half_day_field, True]}, 0.5, 1]},
                0,
            ]
        }
    }


def _late_count_sum(late_field: str) -> dict:
    return {"$sum": {"$cond": [{"$gt": [{"$ifNull": [late_field, 0]}, 0]}, 1, 0]}}


def _sum_field(field: str) -> dict:
    return {"$sum": {"$ifNull": [field, 0]}}


def _has_work_hours(status_field: str, work_field: str) -> dict:
    return {
        "$and": [
            {"$in": [status_field, list(PRESENCE_STATUSES)]},
            {"$ne": [work_field, None]},
        ]
    }


def _round_half_up_expr(value_expr, places: int) -> dict:
    factor = 10 ** places
    return {
        "$divide": [
            {"$floor": {"$add": [{"$multiply": [value_expr, factor]}, 0.5]}},
            factor,
        ]
    }


def attendance_list_filter(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


def attendance_list_command(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
    page: int,
    page_size: int,
) -> dict:
    return {
        "find": "attendance_logs",
        "filter": attendance_list_filter(emp_code, date_from, date_to, status),
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


def explain_aggregate_command(collection: str, pipeline: list) -> dict:
    return {"aggregate": collection, "pipeline": pipeline, "cursor": {}}


def employee_monthly_pipeline(emp_code: str, date_from: str, date_to: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": date_from, "$lte": date_to}}},
        {
            "$group": {
                "_id": None,
                "present_days": _present_days_sum("$status", "$date", "$half_day"),
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": _late_count_sum("$late_minutes"),
                "total_late_minutes": _sum_field("$late_minutes"),
                "total_overtime_minutes": _sum_field("$overtime_minutes"),
            }
        },
    ]


def department_summary_pipeline(
    date_from: str, date_to: str, department: Optional[str]
) -> list:
    employee_match: dict = {"joined_on": {"$lte": date_to}}
    if department is not None:
        employee_match["department"] = department
    work_condition = _has_work_hours("$logs.status", "$logs.work_hours")
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "logs",
                "pipeline": [
                    {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "late_minutes": 1,
                            "overtime_minutes": 1,
                            "work_hours": 1,
                            "half_day": 1,
                            "date": 1,
                        }
                    },
                ],
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$emp_code",
                "department": {"$first": "$department"},
                "present_days": _present_days_sum(
                    "$logs.status", "$logs.date", "$logs.half_day"
                ),
                "late_count": _late_count_sum("$logs.late_minutes"),
                "total_late_minutes": _sum_field("$logs.late_minutes"),
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
                "work_sum": {
                    "$sum": {"$cond": [work_condition, "$logs.work_hours", 0]}
                },
                "work_count": {"$sum": {"$cond": [work_condition, 1, 0]}},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "work_sum": {"$sum": "$work_sum"},
                "work_count": {"$sum": "$work_count"},
            }
        },
        {
            "$addFields": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_count", 0]},
                        {"$divide": ["$work_sum", "$work_count"]},
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(
    date_from: str, date_to: str, limit: int, department: Optional[str]
) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": _sum_field("$late_minutes"),
                "late_count": _late_count_sum("$late_minutes"),
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipeline


def department_trend_pipeline(
    department: str, from_str: str, to_str: str, from_dt: datetime, to_dt: datetime
) -> list:
    return [
        {"$match": {"date": {"$gte": from_str, "$lte": to_str}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": _late_count_sum("$late_minutes"),
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {"$project": {"_id": from_str}},
                ],
            }
        },
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt + timedelta(days=1)],
                },
            }
        },
        {
            "$group": {
                "_id": "$day",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$addFields": {
                "day_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "day_str",
                "foreignField": "joined_on",
                "as": "joined_today",
                "pipeline": [{"$match": {"department": department}}],
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "as": "joined_before",
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "joined_on": {"$lt": from_str},
                        }
                    },
                    {"$project": {"_id": 1}},
                ],
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "joined_today_count": {"$size": "$joined_today"},
                "joined_before_count": {"$size": "$joined_before"},
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "cumulative_joiners": {
                        "$sum": "$joined_today_count",
                        "window": {"documents": ["unbounded", "current"]},
                    }
                },
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$add": ["$joined_before_count", "$cumulative_joiners"]
                },
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$_id"}, 5]},
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        _round_half_up_expr(
                            {"$divide": ["$present_count", "$headcount"]}, 4
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "rate_numeric_sum": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$attendance_rate", None]},
                                0,
                                "$attendance_rate",
                            ]
                        },
                        "window": {"documents": [-6, 0]},
                    },
                    "rate_numeric_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$attendance_rate", None]},
                                0,
                                1,
                            ]
                        },
                        "window": {"documents": [-6, 0]},
                    },
                },
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$rate_numeric_count", 0]},
                        _round_half_up_expr(
                            {
                                "$divide": [
                                    "$rate_numeric_sum",
                                    "$rate_numeric_count",
                                ]
                            },
                            4,
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/health")
def health() -> dict:
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable") from None
    return {"status": "ok", "database": "connected"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate) -> dict:
    employee = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_instant(),
    }
    try:
        db.employees.insert_one(employee)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code or email already exists") from None
    return serialize_employee(employee)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(employee) for employee in cursor],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest) -> dict:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = (
        to_instant(body.punched_at) if body.punched_at is not None else now_instant()
    )
    day = attendance_date_for(
        punched_at, employee["shift_start"], employee["shift_end"]
    )
    date_str = day.isoformat()
    record = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    record.update(derive_fields(body.status, punched_at, None, employee, day))
    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(
            409, f"attendance already recorded for {body.emp_code} on {date_str}"
        ) from None
    return serialize_record(record)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> dict:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = (
        to_instant(body.punched_at) if body.punched_at is not None else now_instant()
    )

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$type": "date", "$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        future = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$type": "date"}},
            sort=[("punch_in", ASCENDING)],
        )
        if future is not None:
            raise HTTPException(422, "punched_at is before the open punch-in")
        raise HTTPException(404, "no punch-in found")

    if record["punch_out"] is not None:
        raise HTTPException(409, "attendance record was already punched out")

    punch_in = record["punch_in"]
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=UTC)
    delta = (punched_at - punch_in).total_seconds()
    if delta <= 0 or delta > MAX_PUNCH_SECONDS:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    record_day = date.fromisoformat(record["date"])
    derived = derive_fields(record["status"], punch_in, punched_at, employee, record_day)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record was already punched out")
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusLiteral] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    checked_date_range(date_from, date_to)
    query = attendance_list_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_record(record) for record in cursor],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str, date: str, body: RegularizeRequest
) -> dict:
    day = checked_date("date", date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    current_status = record["status"]
    current_punch_in = record.get("punch_in")
    current_punch_out = record.get("punch_out")

    new_status = body.status if body.status is not None else current_status
    new_punch_in = (
        to_instant(body.punch_in) if body.punch_in is not None else current_punch_in
    )
    new_punch_out = (
        to_instant(body.punch_out) if body.punch_out is not None else current_punch_out
    )

    if new_status not in PRESENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422, "punch times are only allowed for PRESENT, WFH or ON_DUTY"
            )
        new_punch_in = None
        new_punch_out = None
    else:
        if new_punch_in is None:
            raise HTTPException(422, "punch_in is required for a presence status")
        if (
            attendance_date_for(
                new_punch_in, employee["shift_start"], employee["shift_end"]
            )
            != day
        ):
            raise HTTPException(
                422, "punch_in does not fall on the record's attendance date"
            )
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > MAX_PUNCH_SECONDS:
                raise HTTPException(422, "punch range must not exceed 24 hours")

    derived = derive_fields(new_status, new_punch_in, new_punch_out, employee, day)

    changes = {}
    candidates = (
        ("status", current_status, new_status),
        ("punch_in", current_punch_in, new_punch_in),
        ("punch_out", current_punch_out, new_punch_out),
        ("work_hours", record.get("work_hours"), derived["work_hours"]),
        ("late_minutes", int(record.get("late_minutes") or 0), derived["late_minutes"]),
        (
            "overtime_minutes",
            int(record.get("overtime_minutes") or 0),
            derived["overtime_minutes"],
        ),
        ("half_day", bool(record.get("half_day", False)), derived["half_day"]),
    )
    for field, old_value, new_value in candidates:
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(422, "request does not change the record")

    entry = {
        "at": now_instant(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    history_filter = {
        "emp_code": emp_code,
        "date": date,
        "status": current_status,
        "punch_in": current_punch_in,
        "punch_out": current_punch_out,
        "$expr": {
            "$eq": [
                {"$size": {"$ifNull": ["$history", []]}},
                len(record.get("history") or []),
            ]
        },
    }
    update = {
        "$set": {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            **derived,
        },
        "$push": {"history": entry},
    }
    updated = db.attendance_logs.find_one_and_update(
        history_filter, update, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed since it was read")
    return serialize_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)
) -> dict:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    result = next(
        db.attendance_logs.aggregate(
            employee_monthly_pipeline(emp_code, first.isoformat(), last.isoformat())
        ),
        None,
    )
    present_days = result["present_days"] if result else 0
    leave_days = int(result["leave_days"]) if result else 0
    late_count = int(result["late_count"]) if result else 0
    total_late_minutes = int(result["total_late_minutes"]) if result else 0
    total_overtime_minutes = int(result["total_overtime_minutes"]) if result else 0
    joined_on = date.fromisoformat(employee["joined_on"])
    start = joined_on if joined_on > first else first
    working_days = working_days_between(start, last)
    attendance_pct = (
        round_half_up(100.0 * present_days / working_days, 4)
        if working_days > 0
        else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: Optional[str] = None,
) -> dict:
    first, last = month_bounds(month)
    rows = db.employees.aggregate(
        department_summary_pipeline(first.isoformat(), last.isoformat(), department)
    )
    items = [
        {
            "department": row["department"],
            "headcount": int(row["headcount"]),
            "present_days": row["present_days"],
            "avg_work_hours": round_half_up(row["avg_work_hours"], 2),
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        }
        for row in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict:
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(
        late_leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)
    )
    items = [
        {
            "rank": int(row["rank"]),
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        }
        for row in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
) -> dict:
    from_day = checked_date("from", from_)
    to_day = checked_date("to", to)
    if to_day < from_day:
        raise HTTPException(422, "to must not be before from")
    if (to_day - from_day).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, "trend range must not exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "unknown department")
    from_str, to_str = from_day.isoformat(), to_day.isoformat()
    from_dt = datetime.combine(from_day, time(0, 0), tzinfo=UTC)
    to_dt = datetime.combine(to_day, time(0, 0), tzinfo=UTC)
    rows = db.attendance_logs.aggregate(
        department_trend_pipeline(department, from_str, to_str, from_dt, to_dt)
    )
    items = [
        {
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": row["present_count"],
            "late_count": int(row["late_count"]),
            "attendance_rate": row["attendance_rate"],
            "moving_avg_7d": row["moving_avg_7d"],
        }
        for row in rows
    ]
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=EXPLAIN_ENDPOINT_PATTERN),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusLiteral] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    if endpoint == "attendance_list":
        if date_from is not None:
            checked_date("date_from", date_from)
        if date_to is not None:
            checked_date("date_to", date_to)
        checked_date_range(date_from, date_to)
        collection = "attendance_logs"
        command = attendance_list_command(
            emp_code, date_from, date_to, status, page, page_size
        )
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        first, last = month_bounds(month)
        collection = "attendance_logs"
        command = explain_aggregate_command(
            collection,
            employee_monthly_pipeline(emp_code, first.isoformat(), last.isoformat()),
        )
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        first, last = month_bounds(month)
        collection = "employees"
        command = explain_aggregate_command(
            collection,
            department_summary_pipeline(first.isoformat(), last.isoformat(), department),
        )
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        first, last = month_bounds(month)
        collection = "attendance_logs"
        command = explain_aggregate_command(
            collection,
            late_leaderboard_pipeline(
                first.isoformat(), last.isoformat(), limit, department
            ),
        )
    else:
        if not from_ or not to:
            raise HTTPException(422, "department, from and to are required")
        from_day = checked_date("from", from_)
        to_day = checked_date("to", to)
        if to_day < from_day:
            raise HTTPException(422, "to must not be before from")
        from_str, to_str = from_day.isoformat(), to_day.isoformat()
        from_dt = datetime.combine(from_day, time(0, 0), tzinfo=UTC)
        to_dt = datetime.combine(to_day, time(0, 0), tzinfo=UTC)
        collection = "attendance_logs"
        command = explain_aggregate_command(
            collection,
            department_trend_pipeline(department, from_str, to_str, from_dt, to_dt),
        )

    explain = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
