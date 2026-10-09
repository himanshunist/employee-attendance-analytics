from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

IST = timezone(timedelta(hours=5, minutes=30))


def ms(year, month, day, hour, minute, second=0):
    return int(datetime(year, month, day, hour, minute, second, tzinfo=IST).timestamp() * 1000)


def make_employee(client, code="EMP0001", department="Eng", joined="2026-07-01",
                  shift_start="09:30", shift_end="18:30", email=None):
    payload = {
        "emp_code": code,
        "name": "Test Person",
        "email": email or f"{code.lower()}@example.com",
        "department": department,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined,
    }
    response = client.post("/employees", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}


def test_create_employee_contract(client):
    body = make_employee(client)
    assert body["shift_start"] == "09:30"
    assert isinstance(body["created_at"], int)
    assert 100_000_000_000 < body["created_at"] < 4_102_444_800_000
    assert "_id" not in body and "id" not in body


def test_create_employee_validations(client):
    make_employee(client)
    duplicate = client.post("/employees", json={
        "emp_code": "EMP0001", "name": "X", "email": "x@e.com",
        "department": "Eng", "joined_on": "2026-07-01"})
    assert duplicate.status_code == 409
    equal_shifts = client.post("/employees", json={
        "emp_code": "EMP0002", "name": "X", "email": "x2@e.com",
        "department": "Eng", "joined_on": "2026-07-01",
        "shift_start": "09:00", "shift_end": "09:00"})
    assert equal_shifts.status_code == 422
    bad_code = client.post("/employees", json={
        "emp_code": "nope", "name": "X", "email": "x3@e.com",
        "department": "Eng", "joined_on": "2026-07-01"})
    assert bad_code.status_code == 422
    bad_date = client.post("/employees", json={
        "emp_code": "EMP0003", "name": "X", "email": "x4@e.com",
        "department": "Eng", "joined_on": "2026-13-01"})
    assert bad_date.status_code == 422


def test_list_employees_pagination_and_filter(client):
    make_employee(client, code="EMP0001", department="Eng")
    make_employee(client, code="EMP0002", department="Eng")
    make_employee(client, code="EMP0003", department="Sales")

    page = client.get("/employees", params={"page": 1, "page_size": 2}).json()
    assert page["total"] == 3
    assert [item["emp_code"] for item in page["items"]] == ["EMP0001", "EMP0002"]

    filtered = client.get("/employees", params={"department": "Eng"}).json()
    assert filtered["total"] == 2

    assert client.get("/employees", params={"page": 0}).status_code == 422
    assert client.get("/employees", params={"page_size": 101}).status_code == 422


def test_punch_in_rules(client):
    make_employee(client)
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    assert client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": 12345}).status_code == 422
    assert client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": 1783312500000.0}).status_code == 422
    assert client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": "1783312500000"}).status_code == 422
    assert client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422

    record = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 9, 40, 0)}).json()
    assert record["date"] == "2026-07-01"
    assert record["late_minutes"] == 0
    assert record["punch_out"] is None and record["work_hours"] is None
    assert record["history"] == []

    again = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 9, 40, 1)})
    assert again.status_code == 409

    second = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 2, 9, 40, 1)}).json()
    assert second["late_minutes"] == 10
    third = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 3, 10, 15, 59)}).json()
    assert third["late_minutes"] == 45


def test_punch_in_truncates_seconds(client):
    make_employee(client)
    value = ms(2026, 7, 1, 9, 30, 0) + 900
    record = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": value}).json()
    assert record["punch_in"] == ms(2026, 7, 1, 9, 30, 0)


def test_punch_out_rules(client):
    make_employee(client)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 9, 30, 0)})

    early = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 9, 0, 0)})
    assert early.status_code == 422
    too_long = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 2, 9, 30, 1)})
    assert too_long.status_code == 422

    closed = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 18, 0, 0)}).json()
    assert closed["work_hours"] == 8.5
    assert closed["half_day"] is False
    assert closed["overtime_minutes"] == 0

    duplicate = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 18, 5, 0)})
    assert duplicate.status_code == 409

    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404


def test_punch_out_work_hours_half_up_and_half_day(client):
    make_employee(client)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 9, 30, 1)})
    record = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 19, 1, 0)}).json()
    assert record["work_hours"] == 9.52
    assert record["overtime_minutes"] == 31


def test_overtime_threshold(client):
    make_employee(client)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 9, 30, 0)})
    just_under = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 18, 59, 59)}).json()
    assert just_under["overtime_minutes"] == 0

    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 2, 9, 30, 0)})
    exactly = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 2, 19, 0, 0)}).json()
    assert exactly["overtime_minutes"] == 30


def test_overnight_shift_r1_r3(client):
    make_employee(client, shift_start="22:00", shift_end="06:00")
    punch_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 2, 1, 0, 0)}).json()
    assert punch_in["date"] == "2026-07-01"
    assert punch_in["late_minutes"] == 180
    record = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 2, 6, 30, 0)}).json()
    assert record["work_hours"] == 5.5
    assert record["overtime_minutes"] == 30
    assert record["half_day"] is False


def test_attendance_list(client):
    make_employee(client)
    for day in (1, 2, 3):
        client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": ms(2026, 7, day, 9, 30, 0)})
    body = client.get("/attendance", params={"emp_code": "EMP0001"}).json()
    assert body["total"] == 3
    assert [item["date"] for item in body["items"]] == ["2026-07-03", "2026-07-02", "2026-07-01"]
    assert all(isinstance(item["punch_in"], int) for item in body["items"])
    assert all("_id" not in item and "id" not in item for item in body["items"])

    assert client.get("/attendance", params={
        "date_from": "2026-07-05", "date_to": "2026-07-01"}).status_code == 422
    assert client.get("/attendance", params={"status": "BOGUS"}).status_code == 422
    page = client.get("/attendance", params={"page_size": 2}).json()
    assert len(page["items"]) == 2 and page["total"] == 3


def test_regularize_recomputes_and_appends_history(client):
    make_employee(client)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 9, 45, 0)})
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 18, 0, 0)})

    record = client.patch("/attendance/EMP0001/2026-07-01", json={
        "punch_in": ms(2026, 7, 1, 9, 30, 0), "reason": "forgot to swipe",
        "regularized_by": "hr_lead"}).json()
    assert record["late_minutes"] == 0
    assert record["punch_in"] == ms(2026, 7, 1, 9, 30, 0)
    assert len(record["history"]) == 1
    entry = record["history"][0]
    assert entry["by"] == "hr_lead" and entry["reason"] == "forgot to swipe"
    assert set(entry["changes"]) == {"punch_in", "work_hours", "late_minutes"}
    assert entry["changes"]["late_minutes"] == {"from": 15, "to": 0}
    assert entry["changes"]["work_hours"] == {"from": 8.25, "to": 8.5}
    assert entry["changes"]["punch_in"]["from"] == ms(2026, 7, 1, 9, 45, 0)

    second = client.patch("/attendance/EMP0001/2026-07-01", json={
        "status": "LEAVE", "reason": "approved leave", "regularized_by": "hr_lead"}).json()
    assert second["punch_in"] is None and second["punch_out"] is None
    assert len(second["history"]) == 2


def test_regularize_errors(client):
    make_employee(client)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 9, 45, 0)})
    assert client.patch("/attendance/EMP9999/2026-07-01", json={
        "status": "ABSENT", "reason": "unknown person", "regularized_by": "hr"}).status_code == 404
    assert client.patch("/attendance/EMP0001/2026-07-02", json={
        "status": "ABSENT", "reason": "no such record", "regularized_by": "hr"}).status_code == 404
    assert client.patch("/attendance/EMP0001/2026-07-01", json={
        "status": "ABSENT", "punch_in": ms(2026, 7, 1, 9, 0, 0),
        "reason": "absent with punches", "regularized_by": "hr"}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-01", json={
        "status": "PRESENT", "reason": "changes nothing", "regularized_by": "hr"}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-01", json={
        "punch_in": ms(2026, 7, 5, 9, 30, 0), "reason": "wrong day", "regularized_by": "hr"}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-01", json={
        "reason": "short", "regularized_by": "hr"}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-01", json={
        "status": "LEAVE", "reason": "ignored derived", "regularized_by": "hr",
        "work_hours": 99}).status_code == 200


def test_monthly_summary(client):
    make_employee(client, code="EMP0001", joined="2026-07-15")
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 15, 9, 30, 0)})
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 15, 13, 0, 0)})
    record = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert record["working_days"] == 13
    assert record["present_days"] == 0.5
    assert record["late_count"] == 0
    assert record["late_count"] == 0

    future = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2027-01"}).json()
    assert future["working_days"] == 21 and future["attendance_pct"] == 0.0

    assert client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422
    assert client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404


def test_department_summary_includes_zero_log_employees(client):
    make_employee(client, code="EMP0001", department="Eng")
    make_employee(client, code="EMP0002", department="Zero")
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 9, 30, 0)})
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 18, 0, 0)})
    body = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert body["month"] == "2026-07"
    by_department = {item["department"]: item for item in body["items"]}
    assert by_department["Zero"]["headcount"] == 1
    assert by_department["Zero"]["avg_work_hours"] is None
    assert by_department["Eng"]["headcount"] == 1
    assert by_department["Eng"]["avg_work_hours"] == 8.5


def test_leaderboard_competition_ranking(client):
    make_employee(client, code="EMP0001")
    make_employee(client, code="EMP0002")
    make_employee(client, code="EMP0003")
    for code in ("EMP0001", "EMP0002"):
        client.post("/attendance/punch-in", json={
            "emp_code": code, "punched_at": ms(2026, 7, 1, 10, 0, 0)})
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0003", "punched_at": ms(2026, 7, 1, 9, 55, 0)})

    body = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1}).json()
    assert [item["emp_code"] for item in body["items"]] == ["EMP0001", "EMP0002"]
    assert all(item["rank"] == 1 for item in body["items"])
    assert all(item["total_late_minutes"] == 30 for item in body["items"])

    limited = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 50})
    assert limited.status_code == 200
    assert client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422


def test_department_trend(client):
    make_employee(client, code="EMP0001", department="Eng", joined="2026-07-02")
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 2, 9, 30, 0)})
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 2, 18, 0, 0)})
    body = client.get("/analytics/departments/Eng/trend", params={
        "from": "2026-07-01", "to": "2026-07-05"}).json()
    assert body["department"] == "Eng"
    assert len(body["items"]) == 5
    assert body["items"][0]["headcount"] == 0
    assert body["items"][0]["attendance_rate"] is None
    assert body["items"][1]["headcount"] == 1
    assert body["items"][1]["present_count"] == 1
    assert body["items"][1]["attendance_rate"] == 1.0
    assert body["items"][4]["is_working_day"] is False
    assert body["items"][4]["attendance_rate"] is None

    assert client.get("/analytics/departments/Nope/trend", params={
        "from": "2026-07-01", "to": "2026-07-05"}).status_code == 404
    assert client.get("/analytics/departments/Eng/trend", params={
        "from": "2026-07-05", "to": "2026-07-01"}).status_code == 422
    assert client.get("/analytics/departments/Eng/trend", params={
        "from": "2026-01-01", "to": "2026-07-01"}).status_code == 422


def test_explain_endpoints(client):
    from app import main

    make_employee(client, code="EMP0001", department="Eng")
    docs = []
    for index in range(3000):
        day = datetime(2026, 1, 1) + timedelta(days=index)
        docs.append({
            "emp_code": "EMP0001", "date": day.strftime("%Y-%m-%d"), "status": "PRESENT",
            "punch_in": day.replace(hour=9, minute=30, tzinfo=IST),
            "punch_out": day.replace(hour=18, minute=30, tzinfo=IST),
            "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0,
            "half_day": False, "history": [],
        })
    main.db.attendance_logs.insert_many(docs)

    explain = client.get("/admin/explain/attendance_list", params={
        "date_from": "2026-02-01", "date_to": "2026-02-03"}).json()
    plan = str(explain["explain"])
    assert "IXSCAN" in plan and "COLLSCAN" not in plan

    for endpoint, params in (
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-02"}),
        ("department_summary", {"month": "2026-02"}),
        ("late_leaderboard", {"month": "2026-02"}),
        ("department_trend", {"department": "Eng", "from": "2026-02-01", "to": "2026-02-03"}),
    ):
        response = client.get(f"/admin/explain/{endpoint}", params=params)
        assert response.status_code == 200
        assert response.json()["endpoint"] == endpoint

    assert client.get("/admin/explain/employee_monthly").status_code == 422
    assert client.get("/admin/explain/nope").status_code == 422


def test_punch_in_race_is_atomic(client):
    make_employee(client)
    payload = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 9, 30, 0)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: client.post("/attendance/punch-in", json=payload).status_code, range(8)))
    assert codes.count(201) == 1
    assert codes.count(409) == 7


def test_punch_out_race_is_atomic(client):
    make_employee(client)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 9, 30, 0)})
    payload = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 1, 18, 0, 0)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: client.post("/attendance/punch-out", json=payload).status_code, range(8)))
    assert codes.count(200) == 1
    assert codes.count(409) == 7
