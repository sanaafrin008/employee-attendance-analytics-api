"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

from pymongo.errors import DuplicateKeyError


@app.on_event("startup")
def create_indexes():
    # Employee codes must be unique.
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employee_code",
    )

    # One attendance record per employee per attendance date.
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_employee_date",
    )

    # Employee department and joining-date lookups.
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="idx_department_joined",
    )

    # Attendance listing and date-range filtering.
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="idx_attendance_date_employee",
    )

    # Monthly employee attendance queries.
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1), ("status", 1)],
        name="idx_attendance_employee_date_status",
    )

    # Department-level attendance filtering and analytics.
    db.attendance_logs.create_index(
        [("status", 1), ("date", 1)],
        name="idx_attendance_status_date",
    )



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def truncate_seconds(value: datetime) -> datetime:
    """Discard fractional seconds and normalize the instant to UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)

def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    """Convert a datetime to epoch milliseconds for API responses."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)




def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    # Database datetimes are stored as naive UTC.
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)

    punch_ist = truncate_seconds(punch_in).astimezone(IST)

    hour, minute = map(int, shift_start.split(":"))
    start = punch_ist.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )

    elapsed = int((punch_ist - start).total_seconds())

    # More than 10 minutes late; count complete elapsed minutes.
    return elapsed // 60 if elapsed > 600 else 0



def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (
        truncate_seconds(punch_out) - truncate_seconds(punch_in)
    ).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))




def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> int:
    """Calculate overtime after scheduled shift end, supporting overnight shifts."""

    punch_out = truncate_seconds(punch_out).astimezone(IST)

    shift_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    end_time = datetime.strptime(shift_end, "%H:%M").time()

    # An overnight shift ends on the following calendar day.
    overnight = end_time <= start_time
    end_date = shift_date + timedelta(days=1) if overnight else shift_date

    shift_end_dt = datetime.combine(end_date, end_time, tzinfo=IST)

    # No overtime if the employee punches out before the shift ends.
    if punch_out <= shift_end_dt:
        return 0

    overtime_minutes = int((punch_out - shift_end_dt).total_seconds() // 60)

    # Count overtime only when it reaches 30 whole minutes.
    return overtime_minutes if overtime_minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str




# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {
            "status": "ok",
            "database": "connected",
        }
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )



@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(ts),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": doc["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
}




@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be at least 1")

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100",
        )

    query = {}
    if department is not None:
        query["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(query)

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    from pymongo.errors import DuplicateKeyError

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None and (
        isinstance(body.punched_at, bool)
        or not isinstance(body.punched_at, int)
    ):
        raise HTTPException(
            status_code=422,
            detail="punched_at must be an integer epoch timestamp in milliseconds",
        )

    if body.punched_at is None:
        ts = datetime.now(timezone.utc)
    else:
        try:
            ts = datetime.fromtimestamp(
                body.punched_at / 1000, tz=timezone.utc
            )
        except (OverflowError, OSError, ValueError):
            raise HTTPException(status_code=422, detail="Invalid punched_at")

    ts = truncate_seconds(ts)
    ts_ist = ts.astimezone(IST)

    # Find the most recent open attendance record before this punch-out.
    open_record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$exists": True},
            "punch_out": None,
            "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
        },
        sort=[("punch_in", -1)],
    )

    if open_record is None:
        any_record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code},
            sort=[("date", -1)],
        )
        if any_record and any_record.get("punch_out") is not None:
            raise HTTPException(
                status_code=409,
                detail="Attendance is already punched out",
            )
        raise HTTPException(
            status_code=404,
            detail="No open punch-in record found",
        )

    punch_in_dt = open_record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)

    punch_in_dt = truncate_seconds(punch_in_dt)

    elapsed_seconds = (ts - punch_in_dt).total_seconds()
    if elapsed_seconds <= 0:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in",
        )
    if elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be within 24 hours of punch-in",
        )

    work_hours = compute_work_hours(punch_in_dt, ts)
    overtime_minutes = compute_overtime(
        ts,
        emp["shift_start"],
        emp["shift_end"],
        open_record["date"],
    )
    half_day = work_hours < 4.50

    # Conditional update prevents two concurrent requests from closing the
    # same attendance record.
    result = db.attendance_logs.update_one(
        {
            "_id": open_record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts.replace(tzinfo=None),
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance was already punched out",
        )

    return {
        "emp_code": open_record["emp_code"],
        "date": open_record["date"],
        "status": open_record["status"],
        "punch_in": to_epoch_ms(punch_in_dt),
        "punch_out": to_epoch_ms(ts),
        "work_hours": work_hours,
        "late_minutes": open_record.get("late_minutes", 0),
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
        "history": open_record.get("history", []),
    }



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    from pymongo.errors import DuplicateKeyError

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(
            status_code=422,
            detail="status must be PRESENT, WFH, or ON_DUTY",
        )

    if body.punched_at is not None and (
        isinstance(body.punched_at, bool)
        or not isinstance(body.punched_at, int)
    ):
        raise HTTPException(
            status_code=422,
            detail="punched_at must be an integer epoch timestamp in milliseconds",
        )

    if body.punched_at is None:
        ts = datetime.now(timezone.utc)
    else:
        try:
            ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise HTTPException(status_code=422, detail="Invalid punched_at")

    ts = truncate_seconds(ts)
    ts_ist = ts.astimezone(IST)
    shift_end = emp["shift_end"]
    shift_start = emp["shift_start"]

    attendance_date = ts_ist.date()
    if shift_end <= shift_start and ts_ist.strftime("%H:%M") < shift_end:
        attendance_date -= timedelta(days=1)

    date_str = attendance_date.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts.replace(tzinfo=None),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, shift_start),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance already exists for this employee and date",
        )

    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be at least 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100",
        )
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    total = db.attendance_logs.count_documents(q)
    page_docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for record in page_docs:
        record["punch_in"] = to_epoch_ms(record.get("punch_in"))
        record["punch_out"] = to_epoch_ms(record.get("punch_out"))
        record["history"] = record.get("history", [])

    return {
        "items": page_docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    from datetime import date as date_type

    # Validate the reason and admin identity.
    reason = body.reason.strip()
    regularized_by = body.regularized_by.strip()

    if not 5 <= len(reason) <= 200:
        raise HTTPException(
            status_code=422,
            detail="reason must contain 5 to 200 characters"
        )
    if not 1 <= len(regularized_by) <= 50:
        raise HTTPException(
            status_code=422,
            detail="regularized_by must contain 1 to 50 characters"
        )

    # Validate the attendance date.
    try:
        date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must be in YYYY-MM-DD format"
        )

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    supplied = body.model_fields_set
    updates = {}

    # Validate status if supplied.
    if "status" in supplied:
        if body.status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
            raise HTTPException(status_code=422, detail="Invalid attendance status")
        updates["status"] = body.status

    # Convert epoch milliseconds to UTC datetimes.
    def parse_ms(value, field):
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(
                status_code=422,
                detail=f"{field} must be an integer epoch timestamp in milliseconds"
            )
        try:
            result = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise HTTPException(status_code=422, detail=f"Invalid {field}")
        return truncate_seconds(result).replace(tzinfo=None)

    for field in ("punch_in", "punch_out"):
        if field in supplied:
            value = getattr(body, field)
            if value is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field} must be an integer epoch timestamp in milliseconds"
                )
            updates[field] = parse_ms(value, field)

    new_status = updates.get("status", record["status"])
    new_in = updates.get("punch_in", record.get("punch_in"))
    new_out = updates.get("punch_out", record.get("punch_out"))

    # ABSENT and LEAVE records cannot have punch times.
    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE records cannot have punch times"
            )
        new_in = None
        new_out = None
        updates["punch_in"] = None
        updates["punch_out"] = None

    # Presence statuses require a punch-in.
    if new_status in {"PRESENT", "WFH", "ON_DUTY"} and new_in is None:
        raise HTTPException(
            status_code=422,
            detail="Presence statuses require punch_in"
        )

    if new_in is not None:
        if new_in.tzinfo is None:
            new_in_utc = new_in.replace(tzinfo=timezone.utc)
        else:
            new_in_utc = new_in.astimezone(timezone.utc)

        punch_ist = new_in_utc.astimezone(IST)
        shift_end = emp["shift_end"]
        shift_start = emp["shift_start"]
        expected_date = punch_ist.date()

        if shift_end <= shift_start and punch_ist.strftime("%H:%M") < shift_end:
            expected_date -= timedelta(days=1)

        if expected_date.isoformat() != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in does not belong to the requested attendance date"
            )

    
    # Validate punch-out and recalculate attendance metrics.
    if new_in is not None and new_out is not None:
        in_utc = (
            new_in.replace(tzinfo=timezone.utc)
            if new_in.tzinfo is None else new_in.astimezone(timezone.utc)
        )
        out_utc = (
            new_out.replace(tzinfo=timezone.utc)
            if new_out.tzinfo is None else new_out.astimezone(timezone.utc)
        )
        duration = (out_utc - in_utc).total_seconds()

        if duration <= 0 or duration > 24 * 3600:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours"
            )

        work_hours = compute_work_hours(in_utc, out_utc)
        overtime = compute_overtime(
            out_utc, emp["shift_start"], emp["shift_end"], date
        )

        updates["work_hours"] = work_hours
        updates["overtime_minutes"] = overtime
        updates["half_day"] = work_hours < 4.50

        if "punch_in" in supplied:
            updates["late_minutes"] = compute_late_minutes(
                in_utc, emp["shift_start"]
            )

    elif new_in is not None:
        updates["work_hours"] = None
        updates["overtime_minutes"] = 0
        updates["half_day"] = False

        
        if "punch_in" in supplied and new_in != record.get("punch_in"):
            in_utc = (
                new_in.replace(tzinfo=timezone.utc)
                if new_in.tzinfo is None else new_in.astimezone(timezone.utc)
            )
            updates["late_minutes"] = compute_late_minutes(
                in_utc, emp["shift_start"]
            )


    else:
        updates["work_hours"] = None
        updates["overtime_minutes"] = 0
        updates["half_day"] = False
        updates["late_minutes"] = 0
        # Record only actual changes.
    changes = {}

    for field, new_value in updates.items():
        old_value = record.get(field)

        if old_value != new_value:
            old_ms = (
                to_epoch_ms(old_value)
                if field in {"punch_in", "punch_out"} else old_value
            )
            new_ms = (
                to_epoch_ms(new_value)
                if field in {"punch_in", "punch_out"} else new_value
            )

            changes[field] = {
                "from": old_ms,
                "to": new_ms,
            }

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="No changes to apply"
        )

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": regularized_by,
        "reason": reason,
        "changes": changes,
    }


    # Optimistic concurrency: reject if another correction changed history.
    query = {"_id": record["_id"]}
    if "history" in record:
        query["history"] = record["history"]
    else:
        query["history"] = {"$exists": False}

    result = db.attendance_logs.update_one(
        query,
        {
            "$set": {field: value for field, value in updates.items()
                     if field in changes},
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance changed concurrently; fetch the record and retry"
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated.pop("_id", None)
    updated["punch_in"] = to_epoch_ms(updated.get("punch_in"))
    updated["punch_out"] = to_epoch_ms(updated.get("punch_out"))

    for entry in updated.get("history", []):
        if isinstance(entry.get("at"), datetime):
            entry["at"] = entry["at"].replace(tzinfo=timezone.utc).isoformat()

    return updated




# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, year: int, month: int):
    if month < 1 or month > 12:
        raise HTTPException(status_code=422, detail="month must be between 1 and 12")

    if year < 2000 or year > 2100:
        raise HTTPException(status_code=422, detail="year must be between 2000 and 2100")

    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": f"{year:04d}-{month:02d}-01",
                    "$lt": (
                        f"{year + 1:04d}-01-01"
                        if month == 12
                        else f"{year:04d}-{month + 1:02d}-01"
                    ),
                },
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_days": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            1,
                            0,
                        ]
                    }
                },
                "absent_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]}
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "total_days": 1,
                "present_days": 1,
                "absent_days": 1,
                "leave_days": 1,
                "total_work_hours": {"$round": ["$total_work_hours", 2]},
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    if results:
        summary = results[0]
    else:
        summary = {
            "emp_code": emp_code,
            "total_days": 0,
            "present_days": 0,
            "absent_days": 0,
            "leave_days": 0,
            "total_work_hours": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
        }

    return {
        "employee": employee,
        "year": year,
        "month": month,
        "summary": summary,
    }

#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly

@app.post("/attendance/{emp_code}/{date}/recalculate")
def recalculate_attendance(emp_code: str, date: str):
    """Recalculate derived attendance metrics without changing punch times."""
    from datetime import date as date_type

    try:
        date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must use YYYY-MM-DD format",
        )

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
        )

    emp = db.employees.find_one({"emp_code": emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    punch_in = record.get("punch_in")
    punch_out = record.get("punch_out")

    if punch_in is not None and punch_out is not None:
        work_hours = compute_work_hours(punch_in, punch_out)
        overtime = compute_overtime(
            punch_out,
            emp["shift_start"],
            emp["shift_end"],
            date,
        )
        half_day = work_hours < 4.50
    else:
        work_hours = None
        overtime = 0
        half_day = False

    updates = {
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }

    changes = {}
    for field, new_value in updates.items():
        old_value = record.get(field)
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}

    if not changes:
        return {
            "message": "Attendance metrics are already correct",
            "emp_code": emp_code,
            "date": date,
            **updates,
        }

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": "system",
        "reason": "Recalculate derived metrics using current attendance rules",
        "changes": changes,
    }

    query = {"_id": record["_id"]}
    if "history" in record:
        query["history"] = record["history"]
    else:
        query["history"] = {"$exists": False}

    result = db.attendance_logs.update_one(
        query,
        {
            "$set": updates,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance changed concurrently; fetch the record and retry",
        )

    return {
        "message": "Attendance metrics recalculated",
        "emp_code": emp_code,
        "date": date,
        **updates,
    }

#   GET   /analytics/departments/summary


@app.get("/analytics/departments/summary")
def department_attendance_summary():
    pipeline = [
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$code"]}
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "absent_days": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ABSENT"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "leave_days": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_days": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_work_hours": {
                                "$sum": {"$ifNull": ["$work_hours", 0]}
                            },
                        }
                    },
                ],
                "as": "attendance",
            }
        },
        {
            "$unwind": {
                "path": "$attendance",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employee_count": {"$sum": 1},
                "present_days": {
                    "$sum": {"$ifNull": ["$attendance.present_days", 0]}
                },
                "absent_days": {
                    "$sum": {"$ifNull": ["$attendance.absent_days", 0]}
                },
                "leave_days": {
                    "$sum": {"$ifNull": ["$attendance.leave_days", 0]}
                },
                "late_days": {
                    "$sum": {"$ifNull": ["$attendance.late_days", 0]}
                },
                "total_work_hours": {
                    "$sum": {"$ifNull": ["$attendance.total_work_hours", 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "employee_count": 1,
                "present_days": 1,
                "absent_days": 1,
                "leave_days": 1,
                "late_days": 1,
                "total_work_hours": {
                    "$round": ["$total_work_hours", 2]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]

    return list(db.employees.aggregate(pipeline))



#   GET   /analytics/leaderboard/late


@app.get("/analytics/leaderboard/late")
def late_arrival_leaderboard(
    date_from: str,
    date_to: str,
    limit: int = 10,
):
    if date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    if limit < 1 or limit > 100:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 100",
        )

    pipeline = [
        {
            "$match": {
                "date": {"$gte": date_from, "$lte": date_to},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "late_days": {"$sum": 1},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "average_late_minutes": {"$avg": "$late_minutes"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "late_days": 1,
                "total_late_minutes": 1,
                "average_late_minutes": {
                    "$round": ["$average_late_minutes", 2]
                },
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
        {"$limit": limit},
    ]

    return list(db.attendance_logs.aggregate(pipeline))

#   GET   /analytics/departments/{department}/trend


@app.get("/analytics/departments/{department}/trend")
def department_attendance_trend(
    department: str,
    date_from: str,
    date_to: str,
):
    from datetime import date as date_type, timedelta

    try:
        start_date = date_type.fromisoformat(date_from)
        end_date = date_type.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if start_date > end_date:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    employee_codes = [
        employee["emp_code"]
        for employee in db.employees.find(
            {"department": department},
            {"_id": 0, "emp_code": 1},
        )
    ]

    if not employee_codes:
        raise HTTPException(
            status_code=404,
            detail="Department not found or has no employees",
        )

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": employee_codes},
                "date": {
                    "$gte": start_date.isoformat(),
                    "$lte": end_date.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            1,
                            0,
                        ]
                    }
                },
                "absent_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateFromString": {
                        "dateString": "$_id",
                        "format": "%Y-%m-%d",
                    }
                },
                "present_days": 1,
                "absent_days": 1,
            }
        },
        {"$sort": {"date": 1}},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(start_date, datetime.min.time()),
                        datetime.combine(end_date, datetime.min.time()),
                    ],
                },
            }
        },
        {
            "$set": {
                "present_days": {"$ifNull": ["$present_days", 0]},
                "absent_days": {"$ifNull": ["$absent_days", 0]},
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "present_days_7d_avg": {
                        "$avg": "$present_days",
                        "window": {"range": [-6, 0], "unit": "day"},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                    }
                },
                "present_days": 1,
                "absent_days": 1,
                "present_days_7d_avg": {
                    "$round": ["$present_days_7d_avg", 2]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]

    return list(db.attendance_logs.aggregate(pipeline))

#   GET   /admin/explain/{endpoint}


@app.get("/admin/explain/{endpoint}")
def explain_query(endpoint: str):
    allowed_endpoints = {
        "employees": (
            db.employees,
            {"department": "Engineering"},
            [("emp_code", 1)],
        ),
        "attendance": (
            db.attendance_logs,
            {"emp_code": "EMP1001", "date": "2026-10-10"},
            [("date", -1), ("emp_code", 1)],
        ),
    }

    if endpoint not in allowed_endpoints:
        raise HTTPException(
            status_code=400,
            detail="endpoint must be 'employees' or 'attendance'",
        )

    collection, query, sort_spec = allowed_endpoints[endpoint]

    
    explanation = db.command(
        "explain",
        {
            "find": collection.name,
            "filter": query,
            "sort": dict(sort_spec),
        },
        verbosity="executionStats",
    )


    return {
        "endpoint": endpoint,
        "executionStats": explanation.get("executionStats", {}),
        "queryPlanner": explanation.get("queryPlanner", {}),
    }

# --------------------------------------------------------------------------- #
