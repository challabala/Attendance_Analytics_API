"""
Employee Attendance & Analytics API
HROne Software Engineer Assignment

All production application code remains in this file.
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple, Union
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load local .env if present; system environment variables take precedence
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# Timezone constants
UTC = timezone.utc

# Contract boundary constants for timestamps (openapi.yaml EpochMillis)
MIN_EPOCH_MS = 100_000_000_000  # 1973-03-03T09:46:40Z (rejects epoch seconds)
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01T00:00:00Z

# PyMongo MongoClient configuration:
# - tz_aware=True: Decodes BSON dates as timezone-aware UTC datetimes
# - serverSelectionTimeoutMS=2000: 2 second timeout for server selection
# - connectTimeoutMS=2000: 2 second timeout for socket connection
client: MongoClient = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=2000,
    connectTimeoutMS=2000,
)
db = client[MONGO_DB]

# Collections specified in DATA_MODEL.md
employees_col = db["employees"]
attendance_logs_col = db["attendance_logs"]


# --------------------------------------------------------------------------- #
# Validation & Exceptions
# --------------------------------------------------------------------------- #
class TimestampValidationError(HTTPException, ValueError):
    """
    Raised when an instant timestamp fails contract validation.
    Inherits from HTTPException (status_code=422) for FastAPI HTTP responses
    and ValueError for Pydantic validators and standard unittest assertions.
    """
    def __init__(self, detail: str = "Invalid timestamp: must be an integer count of epoch milliseconds"):
        super().__init__(status_code=422, detail=detail)


# --------------------------------------------------------------------------- #
# Time & Business Rule Helpers (R1–R5, R8)
# --------------------------------------------------------------------------- #
def validate_epoch_ms(value: object, required: bool = True) -> Optional[int]:
    """
    Validates that value is an integer epoch milliseconds within contract bounds.
    Rejects booleans, floats, strings, epoch seconds (< 10^11), and out-of-range values.
    Raises TimestampValidationError on validation failure.
    """
    if value is None:
        if required:
            raise TimestampValidationError("Timestamp is required")
        return None

    # bool is a subclass of int in Python and must be checked explicitly
    if isinstance(value, bool):
        raise TimestampValidationError("Timestamp cannot be a boolean")

    if not isinstance(value, int):
        raise TimestampValidationError(f"Timestamp must be an integer, got {type(value).__name__}")

    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise TimestampValidationError(
            f"Timestamp {value} out of range (must be between {MIN_EPOCH_MS} and {MAX_EPOCH_MS})"
        )

    return value


def truncate_to_seconds(dt: datetime) -> datetime:
    """
    R1: Truncates an instant to whole seconds, discarding microseconds.
    Assumes UTC if datetime is naive.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.replace(microsecond=0)


def epoch_ms_to_utc_datetime(value: int) -> datetime:
    """Converts integer epoch milliseconds to timezone-aware UTC datetime."""
    seconds, millis = divmod(value, 1000)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=millis * 1000)


def utc_datetime_to_epoch_ms(dt: datetime) -> int:
    """Converts datetime to integer epoch milliseconds in UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return int(dt.timestamp() * 1000)


def to_ist(dt: datetime) -> datetime:
    """Converts a datetime to timezone-aware IST (UTC+05:30)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def get_current_utc() -> datetime:
    """Returns current UTC time truncated to whole seconds."""
    return truncate_to_seconds(datetime.now(UTC))


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """R1: A shift is overnight when shift_end <= shift_start (e.g. 22:00 to 06:00)."""
    return shift_end <= shift_start


def get_attendance_date(
    punched_at: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    """
    R1: Calculates the attendance date (YYYY-MM-DD in IST).
    For normal shifts, uses the IST calendar date of punch_in.
    For overnight shifts (shift_end <= shift_start), if punch_in falls strictly
    before shift_end, it belongs to the shift starting the previous day.
    """
    truncated = truncate_to_seconds(punched_at)
    ist_dt = to_ist(truncated)

    if is_overnight_shift(shift_start, shift_end):
        end_h, end_m = map(int, shift_end.split(":"))
        shift_end_time = time(end_h, end_m, 0)
        if ist_dt.time() < shift_end_time:
            prev_day = ist_dt.date() - timedelta(days=1)
            return prev_day.isoformat()

    return ist_dt.date().isoformat()


def get_shift_boundaries(
    attendance_date: str,
    shift_start: str,
    shift_end: str,
) -> Tuple[datetime, datetime]:
    """
    Calculates timezone-aware IST datetimes for shift start and shift end.
    For overnight shifts, end datetime falls on attendance_date + 1 day.
    """
    d = date.fromisoformat(attendance_date)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    start_dt = datetime(d.year, d.month, d.day, start_h, start_m, 0, tzinfo=IST)

    if is_overnight_shift(shift_start, shift_end):
        end_date = d + timedelta(days=1)
        end_dt = datetime(end_date.year, end_date.month, end_date.day, end_h, end_m, 0, tzinfo=IST)
    else:
        end_dt = datetime(d.year, d.month, d.day, end_h, end_m, 0, tzinfo=IST)

    return start_dt, end_dt


def calculate_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    """
    R2: Late minutes calculation past shift start after 10-minute (600s) grace period.
    Punch-in is late only if strictly > 600 seconds elapsed since shift start.
    Instants are truncated to whole seconds before calculation.
    """
    p_in = to_ist(truncate_to_seconds(punch_in))
    start_dt, _ = get_shift_boundaries(attendance_date, shift_start, shift_end)

    elapsed_seconds = int((p_in - start_dt).total_seconds())
    if elapsed_seconds <= 600:
        return 0

    return elapsed_seconds // 60


def calculate_overtime_minutes(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    """
    R3: Overtime calculation.
    Whole minutes (floor) between shift_end and punch_out,
    counted only if elapsed time >= 30 minutes (1800 seconds); otherwise 0.
    Instants are truncated to whole seconds before calculation.
    """
    p_out = to_ist(truncate_to_seconds(punch_out))
    _, end_dt = get_shift_boundaries(attendance_date, shift_start, shift_end)

    elapsed_seconds = int((p_out - end_dt).total_seconds())
    if elapsed_seconds < 1800:
        return 0

    return elapsed_seconds // 60


def calculate_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    """
    R4: Calculates work hours as (punch_out - punch_in) in seconds / 3600,
    rounded to 2 decimal places using half-up rounding (R8).
    Rejects intervals <= 0 seconds or > 24 hours.
    """
    p_in = truncate_to_seconds(punch_in).astimezone(UTC)
    p_out = truncate_to_seconds(punch_out).astimezone(UTC)

    elapsed_seconds = int((p_out - p_in).total_seconds())
    if elapsed_seconds <= 0:
        raise ValueError("punch_out must be strictly after punch_in")
    if elapsed_seconds > 24 * 3600:
        raise ValueError("Interval between punch_in and punch_out cannot exceed 24 hours")

    hours = Decimal(elapsed_seconds) / Decimal(3600)
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def calculate_half_day(work_hours: Optional[float]) -> bool:
    """R5: Returns True if rounded work_hours < 4.50, False otherwise or if unclosed."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def round_half_up(
    value: Optional[Union[int, float, str, Decimal]],
    decimal_places: int = 2,
) -> Optional[float]:
    """
    R8: Shared half-up rounding using Decimal ROUND_HALF_UP.
    Returns None if value is None.
    """
    if value is None:
        return None
    d = Decimal(str(value))
    exp = Decimal("10") ** -decimal_places
    rounded = d.quantize(exp, rounding=ROUND_HALF_UP)
    return float(rounded)


def calculate_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7: Counts Monday-through-Friday weekdays in the month from
    max(month_start, joined_on) through month_end inclusive.
    Returns 0 if joined_on is after month_end.
    """
    year = int(month_str[:4])
    m = int(month_str[5:7])
    _, last_day = calendar.monthrange(year, m)
    month_start = date(year, m, 1)
    month_end = date(year, m, last_day)

    try:
        joined_date = date.fromisoformat(joined_on_str)
    except Exception:
        joined_date = month_start

    if joined_date > month_end:
        return 0

    effective_start = max(month_start, joined_date)

    working_days = 0
    curr = effective_start
    while curr <= month_end:
        if curr.weekday() < 5:  # Monday=0 through Friday=4
            working_days += 1
        curr += timedelta(days=1)

    return working_days


# Default field values for new attendance records
INITIAL_ATTENDANCE_DEFAULTS = {
    "punch_out": None,
    "work_hours": None,
    "overtime_minutes": 0,
    "half_day": False,
    "history": [],
}


# --------------------------------------------------------------------------- #
# Document Serialization Helpers
# --------------------------------------------------------------------------- #
def serialize_employee_doc(doc: dict) -> dict:
    """
    Serializes a MongoDB employee document to the public Employee schema,
    converting created_at datetime to integer epoch milliseconds and omitting _id.
    """
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at_ms = utc_datetime_to_epoch_ms(created_at)
    elif isinstance(created_at, int):
        created_at_ms = created_at
    else:
        created_at_ms = None

    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": created_at_ms,
    }


def serialize_attendance_doc(doc: dict) -> dict:
    """
    Serializes an attendance_logs document to the public AttendanceRecord schema,
    converting punch timestamps and audit history timestamps to epoch milliseconds.
    """
    punch_in_val = doc.get("punch_in")
    punch_out_val = doc.get("punch_out")

    punch_in_ms = utc_datetime_to_epoch_ms(punch_in_val) if isinstance(punch_in_val, datetime) else punch_in_val
    punch_out_ms = utc_datetime_to_epoch_ms(punch_out_val) if isinstance(punch_out_val, datetime) else punch_out_val

    serialized = {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": punch_in_ms,
        "punch_out": punch_out_ms,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [],
    }

    raw_history = doc.get("history") or []
    out_history = []
    for entry in raw_history:
        at_val = entry.get("at")
        at_ms = utc_datetime_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val

        h_entry = {
            "at": at_ms,
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": {},
        }
        changes = entry.get("changes") or {}
        for field, change in changes.items():
            c_from = change.get("from")
            c_to = change.get("to")
            if field in ("punch_in", "punch_out"):
                if isinstance(c_from, datetime):
                    c_from = utc_datetime_to_epoch_ms(c_from)
                if isinstance(c_to, datetime):
                    c_to = utc_datetime_to_epoch_ms(c_to)
            h_entry["changes"][field] = {"from": c_from, "to": c_to}
        out_history.append(h_entry)

    serialized["history"] = out_history
    return serialized


# --------------------------------------------------------------------------- #
# Request & Response Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return v

    @model_validator(mode="after")
    def validate_shift_times(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self

    model_config = {
        "extra": "forbid"
    }


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError(f"Invalid punch-in status: '{v}'. Must be one of PRESENT, WFH, ON_DUTY")
        return v

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return validate_epoch_ms(v, required=False)

    model_config = {
        "extra": "forbid"
    }


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return validate_epoch_ms(v, required=False)

    model_config = {
        "extra": "forbid"
    }


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[Any] = None
    punch_out: Optional[Any] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            raise ValueError("status cannot be null")
        if v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError(f"Invalid status: '{v}'. Must be one of PRESENT, ABSENT, LEAVE, WFH, ON_DUTY")
        return v

    @field_validator("punch_in")
    @classmethod
    def validate_punch_in(cls, v: Any) -> Optional[int]:
        if v is None:
            raise ValueError("punch_in cannot be null")
        return validate_epoch_ms(v, required=True)

    @field_validator("punch_out")
    @classmethod
    def validate_punch_out(cls, v: Any) -> Optional[int]:
        if v is None:
            raise ValueError("punch_out cannot be null")
        return validate_epoch_ms(v, required=True)

    model_config = {
        "extra": "forbid"
    }


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int = Field(..., ge=1)
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Database Index Initialization
# --------------------------------------------------------------------------- #
def init_indexes():
    """
    Idempotently initializes indexes on employees and attendance_logs collections:
    - employees: unique emp_code, compound (department, emp_code), joined_on, (department, joined_on).
    - attendance_logs: unique (emp_code, date), (emp_code, punch_in desc), (date desc, emp_code asc), (status, date desc).
    """
    employees_col.create_index([("emp_code", 1)], unique=True, name="uniq_emp_code")
    employees_col.create_index([("department", 1), ("emp_code", 1)], name="idx_department_emp_code")
    employees_col.create_index([("joined_on", 1)], name="idx_joined_on")
    employees_col.create_index([("department", 1), ("joined_on", 1)], name="idx_department_joined_on")
    attendance_logs_col.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_code_date")
    attendance_logs_col.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_emp_code_punch_in_desc")
    attendance_logs_col.create_index([("date", -1), ("emp_code", 1)], name="idx_date_desc_emp_code_asc")
    attendance_logs_col.create_index([("status", 1), ("date", -1)], name="idx_status_date")


# --------------------------------------------------------------------------- #
# Application Lifespan & FastAPI Setup
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize indexes if MongoDB is reachable at startup
    try:
        init_indexes()
    except (PyMongoError, Exception):
        # Allow the application to start; /health reports 503 until database is available
        pass
    yield
    # Close client connection pool on shutdown
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="HROne Employee Attendance & Analytics API backend service",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Health Endpoint
# --------------------------------------------------------------------------- #
@app.get("/health", status_code=status.HTTP_200_OK)
def health() -> Dict[str, str]:
    """
    Readiness probe verifying MongoDB connectivity via active ping.
    Returns 200 {"status": "ok"} when reachable, 503 {"detail": "Database unavailable"} otherwise.
    """
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except (PyMongoError, Exception) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        ) from exc


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    """
    Creates an employee with unique emp_code and server-assigned created_at timestamp.
    """
    now_utc = get_current_utc()
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        employees_col.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="emp_code already exists",
        ) from exc

    return serialize_employee_doc(doc)


@app.get("/employees", status_code=status.HTTP_200_OK)
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Lists employees sorted by emp_code ascending, with optional exact department filter.
    """
    q: dict = {}
    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size
    total = employees_col.count_documents(q)
    cursor = employees_col.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    """
    Records an employee punch-in.
    Calculates attendance date (R1) and late minutes (R2). Unique compound index
    on (emp_code, date) prevents duplicate punch-ins for the same attendance date.
    """
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = truncate_to_seconds(epoch_ms_to_utc_datetime(body.punched_at))
    else:
        punch_in_dt = get_current_utc()

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = calculate_late_minutes(punch_in_dt, emp["shift_start"], emp["shift_end"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        attendance_logs_col.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date",
        ) from exc

    return serialize_attendance_doc(doc)


# --------------------------------------------------------------------------- #
# Punch-Out Endpoint
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutRequest):
    """
    Records an employee punch-out and computes shift duration metrics.

    Closes the employee's most recent open attendance record whose punch-in was at
    or before punched_at (supporting overnight shifts across midnight). Computes
    work_hours (R4), overtime_minutes (R3), and half_day status (R5).

    Uses an atomic compare-and-swap so concurrent punch-out requests cannot close
    the same attendance record twice.
    """
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = truncate_to_seconds(epoch_ms_to_utc_datetime(body.punched_at))
    else:
        punch_out_dt = get_current_utc()

    # Find employee's most recent record whose punch_in <= punch_out_dt
    rec = attendance_logs_col.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_out_dt},
        },
        sort=[("punch_in", -1)],
    )

    if not rec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No qualifying punch-in record found for employee",
        )

    if rec.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Attendance record already punched out",
        )

    punch_in_dt = truncate_to_seconds(rec["punch_in"]).astimezone(UTC)
    punch_out_dt = truncate_to_seconds(punch_out_dt).astimezone(UTC)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly after punch_in",
        )

    elapsed_seconds = int((punch_out_dt - punch_in_dt).total_seconds())
    if elapsed_seconds > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punch-out cannot be more than 24 hours after punch-in",
        )

    work_hours = calculate_work_hours(punch_in_dt, punch_out_dt)
    overtime_minutes = calculate_overtime_minutes(
        punch_out_dt,
        emp["shift_start"],
        emp["shift_end"],
        rec["date"],
    )
    half_day = calculate_half_day(work_hours)

    # Atomic update requiring punch_out to remain None
    update_filter = {
        "emp_code": rec["emp_code"],
        "date": rec["date"],
        "punch_out": None,
    }
    update_ops = {
        "$set": {
            "punch_out": punch_out_dt,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }
    }

    updated_doc = attendance_logs_col.find_one_and_update(
        update_filter,
        update_ops,
        return_document=ReturnDocument.AFTER,
    )

    if updated_doc is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Attendance record already punched out",
        )

    return serialize_attendance_doc(updated_doc)


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_param: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Tuple[Dict[str, Any], List[Tuple[str, int]], int, int]:
    """
    Builds the MongoDB query filter, sort specification, skip, and limit
    for GET /attendance. Shared with /admin/explain/attendance_list.
    """
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status_param:
        q["status"] = status_param
    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q

    skip = (page - 1) * page_size
    sort_fields = [("date", -1), ("emp_code", 1)]
    return q, sort_fields, skip, page_size


@app.get("/attendance", status_code=status.HTTP_200_OK)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_param: Optional[str] = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Lists attendance records with pagination and optional filtering by employee,
    status, and inclusive date range. Records are ordered by date descending,
    then emp_code ascending.
    """
    # Validate date range parameters
    if date_from:
        try:
            date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(status_code=422, detail="date_from must be a valid date in YYYY-MM-DD format")
    if date_to:
        try:
            date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(status_code=422, detail="date_to must be a valid date in YYYY-MM-DD format")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    # Validate attendance status
    if status_param is not None:
        if status_param not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status: '{status_param}'. Must be one of PRESENT, ABSENT, LEAVE, WFH, ON_DUTY",
            )

    q, sort_fields, skip, limit = build_attendance_list_query(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status_param=status_param,
        page=page,
        page_size=page_size,
    )
    total = attendance_logs_col.count_documents(q)
    cursor = (
        attendance_logs_col.find(q)
        .sort(sort_fields)
        .skip(skip)
        .limit(limit)
    )
    items = [serialize_attendance_doc(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance Regularization & Audit History
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}", status_code=status.HTTP_200_OK)
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """
    Regularizes (manually corrects) an attendance record for (emp_code, date).

    Recalculates late_minutes, work_hours, overtime_minutes, and half_day status
    from the corrected state, appending exactly one audit-history entry capturing
    the fields that changed.

    Rejects no-op requests and protects against lost updates using atomic
    compare-and-swap concurrency checks.
    """
    # Validate date parameter format
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError()
    except Exception:
        raise HTTPException(
            status_code=422,
            detail="date must be a valid calendar date in YYYY-MM-DD format",
        )

    # Reject explicit null timestamps if client passed null
    if "punch_in" in body.model_fields_set and body.punch_in is None:
        raise HTTPException(status_code=422, detail="punch_in cannot be null")
    if "punch_out" in body.model_fields_set and body.punch_out is None:
        raise HTTPException(status_code=422, detail="punch_out cannot be null")

    # Verify employee exists
    emp = employees_col.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    # Verify attendance record exists for the date
    rec = attendance_logs_col.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Attendance record not found for {emp_code} on {date}",
        )

    # Extract existing values
    old_status = rec.get("status")
    old_punch_in = rec.get("punch_in")
    old_punch_out = rec.get("punch_out")
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = bool(rec.get("half_day", False))

    target_status = body.status if body.status is not None else old_status

    has_punch_in_in_req = "punch_in" in body.model_fields_set and body.punch_in is not None
    has_punch_out_in_req = "punch_out" in body.model_fields_set and body.punch_out is not None

    # Setting status to ABSENT or LEAVE clears timestamps and duration metrics
    if target_status in ("ABSENT", "LEAVE"):
        if has_punch_in_in_req or has_punch_out_in_req:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when setting status to ABSENT or LEAVE",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence statuses (PRESENT, WFH, ON_DUTY) require punch timestamps and recalculate metrics
        if has_punch_in_in_req:
            new_punch_in = truncate_to_seconds(epoch_ms_to_utc_datetime(body.punch_in))
        else:
            new_punch_in = old_punch_in
            if new_punch_in is not None:
                new_punch_in = truncate_to_seconds(new_punch_in).astimezone(UTC)

        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="A presence status requires a valid punch_in",
            )

        # Enforce R1: punch_in must resolve to this attendance date
        computed_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in resolves to attendance date {computed_date}, which does not match {date}",
            )

        if has_punch_out_in_req:
            new_punch_out = truncate_to_seconds(epoch_ms_to_utc_datetime(body.punch_out))
        else:
            new_punch_out = old_punch_out
            if new_punch_out is not None:
                new_punch_out = truncate_to_seconds(new_punch_out).astimezone(UTC)

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be strictly after punch_in",
                )
            elapsed_seconds = int((new_punch_out - new_punch_in).total_seconds())
            if elapsed_seconds > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch-out cannot be more than 24 hours after punch-in",
                )

        # Recalculate derived fields
        new_late_minutes = calculate_late_minutes(new_punch_in, emp["shift_start"], emp["shift_end"], date)
        if new_punch_out is not None:
            new_work_hours = calculate_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = calculate_overtime_minutes(
                new_punch_out,
                emp["shift_start"],
                emp["shift_end"],
                date,
            )
            new_half_day = calculate_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Detect genuine field changes for the audit entry
    changes: Dict[str, Any] = {}

    if old_status != target_status:
        changes["status"] = {"from": old_status, "to": target_status}

    stored_old_punch_in = truncate_to_seconds(old_punch_in).astimezone(UTC) if old_punch_in is not None else None
    stored_old_punch_out = truncate_to_seconds(old_punch_out).astimezone(UTC) if old_punch_out is not None else None

    punch_in_changed = False
    if (old_punch_in is None) != (new_punch_in is None):
        punch_in_changed = True
    elif old_punch_in is not None and new_punch_in is not None:
        if truncate_to_seconds(old_punch_in).astimezone(UTC) != truncate_to_seconds(new_punch_in).astimezone(UTC):
            punch_in_changed = True
    if punch_in_changed:
        changes["punch_in"] = {"from": stored_old_punch_in, "to": new_punch_in}

    punch_out_changed = False
    if (old_punch_out is None) != (new_punch_out is None):
        punch_out_changed = True
    elif old_punch_out is not None and new_punch_out is not None:
        if truncate_to_seconds(old_punch_out).astimezone(UTC) != truncate_to_seconds(new_punch_out).astimezone(UTC):
            punch_out_changed = True
    if punch_out_changed:
        changes["punch_out"] = {"from": stored_old_punch_out, "to": new_punch_out}

    work_hours_changed = False
    if (old_work_hours is None) != (new_work_hours is None):
        work_hours_changed = True
    elif old_work_hours is not None and new_work_hours is not None:
        if float(old_work_hours) != float(new_work_hours):
            work_hours_changed = True
    if work_hours_changed:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}

    if int(old_late_minutes) != int(new_late_minutes):
        changes["late_minutes"] = {"from": int(old_late_minutes), "to": int(new_late_minutes)}

    if int(old_overtime_minutes) != int(new_overtime_minutes):
        changes["overtime_minutes"] = {"from": int(old_overtime_minutes), "to": int(new_overtime_minutes)}

    if bool(old_half_day) != bool(new_half_day):
        changes["half_day"] = {"from": bool(old_half_day), "to": bool(new_half_day)}

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="Request resulted in no changes to the attendance record",
        )

    now_utc = get_current_utc()
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic compare-and-swap update; rejects conflicting concurrent updates
    old_hist = rec.get("history") or []
    update_filter: Dict[str, Any] = {
        "emp_code": emp_code,
        "date": date,
        "status": old_status,
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
    }
    if old_hist:
        update_filter["history"] = {"$size": len(old_hist)}
    else:
        update_filter["$or"] = [{"history": {"$size": 0}}, {"history": {"$exists": False}}, {"history": []}]

    update_ops = {
        "$set": {
            "status": target_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {
            "history": history_entry,
        },
    }

    updated_doc = attendance_logs_col.find_one_and_update(
        update_filter,
        update_ops,
        return_document=ReturnDocument.AFTER,
    )

    if updated_doc is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification detected on attendance record",
        )

    return serialize_attendance_doc(updated_doc)


# --------------------------------------------------------------------------- #
# Monthly Employee Attendance Analytics
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    """
    Constructs the MongoDB aggregation pipeline for monthly attendance analytics of an employee.

    Matches attendance records for emp_code within [YYYY-MM-01, YYYY-MM-last_day]
    and computes:
    - present_days: sum of 0.5 (half_day=True) or 1.0 for presence statuses on weekdays (Mon-Fri)
    - leave_days: count of status == 'LEAVE'
    - late_count: count of records with late_minutes > 0
    - total_late_minutes: sum of late_minutes (null treated as 0)
    - total_overtime_minutes: sum of overtime_minutes (null treated as 0)
    """
    year = int(month[:4])
    m = int(month[5:7])
    _, last_day = calendar.monthrange(year, m)
    start_date = f"{year:04d}-{m:02d}-01"
    end_date = f"{year:04d}-{m:02d}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": start_date,
                    "$lte": end_date,
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                        "format": "%Y-%m-%d",
                                                    }
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
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
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthly,
    status_code=status.HTTP_200_OK,
)
def get_employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Calculates monthly attendance analytics for an employee (Rules R5–R8).

    - working_days: Mon-Fri weekdays from max(month_start, joined_on) through month_end (R7).
    - present_days: Half-day = 0.5, weekdays only (R5, R7).
    - leave_days: Total count of LEAVE records.
    - late_count: Total count of records with late_minutes > 0.
    - total_late_minutes: Sum of late minutes.
    - total_overtime_minutes: Sum of overtime minutes.
    - attendance_pct: (present_days / working_days * 100) rounded half-up to 4 decimals (R8),
      or null if working_days is 0.
    """
    # Validate calendar month
    try:
        parsed_dt = datetime.strptime(month, "%Y-%m")
        if parsed_dt.strftime("%Y-%m") != month:
            raise ValueError()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="month must be a valid calendar month in YYYY-MM format",
        )

    # Check employee exists
    emp = employees_col.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{emp_code}' not found",
        )

    # Calculate working days considering employee joining date (R7)
    joined_on = emp.get("joined_on", "1970-01-01")
    working_days = calculate_working_days(month, joined_on)

    # Execute MongoDB aggregation pipeline
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    results = list(attendance_logs_col.aggregate(pipeline))

    if results:
        agg_doc = results[0]
        present_days = round(float(agg_doc.get("present_days", 0.0)), 1)
        leave_days = int(agg_doc.get("leave_days", 0))
        late_count = int(agg_doc.get("late_count", 0))
        total_late_minutes = int(agg_doc.get("total_late_minutes", 0))
        total_overtime_minutes = int(agg_doc.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    # Calculate attendance_pct with half-up rounding (R8); null when working_days is 0
    if working_days == 0:
        attendance_pct = None
    else:
        raw_pct = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100")
        attendance_pct = round_half_up(raw_pct, decimal_places=4)

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


# --------------------------------------------------------------------------- #
# Department Monthly Summary Analytics
# --------------------------------------------------------------------------- #
def build_department_summary_pipeline(
    month: str,
    department: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Constructs the MongoDB aggregation pipeline for department monthly summary analytics
    across the employees and attendance_logs collections (Rules R5–R9).

    Pipeline stages:
    1. Match employees joined on or before month end (R9 headcount) and optional department.
    2. Lookup attendance logs for matching emp_code within the calendar month.
    3. Unwind logs (preserving employees without attendance records).
    4. Group by department accumulating headcount (distinct emp_codes), present days (weekdays only),
       work hour totals, late counts, total late minutes, leave count, and on-duty count.
    5. Project calculated metrics and compute average work hours with half-up rounding (R8).
    6. Sort alphabetically by department ascending.
    """
    year = int(month[:4])
    m = int(month[5:7])
    _, last_day = calendar.monthrange(year, m)
    start_date_str = f"{year:04d}-{m:02d}-01"
    end_date_str = f"{year:04d}-{m:02d}-{last_day:02d}"

    match_stage: Dict[str, Any] = {
        "joined_on": {"$lte": end_date_str}
    }
    if department is not None:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date_str]},
                                    {"$lte": ["$date", end_date_str]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": {
                                                            "$ifNull": ["$logs.date", "2026-01-01"]
                                                        },
                                                        "format": "%Y-%m-%d",
                                                    }
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": "$present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                                                    100,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                100,
                            ]
                        },
                        None,
                    ]
                },
                "raw_work_hours_sum": "$work_hours_sum",
                "raw_work_hours_count": "$work_hours_count",
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
        {
            "$sort": {
                "department": 1,
            }
        },
    ]


@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummary,
    status_code=status.HTTP_200_OK,
)
def get_department_monthly_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
):
    """
    Monthly attendance summary aggregated across departments (Rules R5–R9).

    - Headcount includes all employees joined on or before month end (R9).
    - Omits departments with 0 qualifying headcount.
    - If department filter matches no qualifying department, returns items: [].
    - Results sorted alphabetically by department name.
    """
    # Validate calendar month
    try:
        parsed_dt = datetime.strptime(month, "%Y-%m")
        if parsed_dt.strftime("%Y-%m") != month:
            raise ValueError()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="month must be a valid calendar month in YYYY-MM format",
        )

    # Execute aggregation pipeline across employees and attendance logs
    pipeline = build_department_summary_pipeline(month, department)
    results = list(employees_col.aggregate(pipeline))

    items = []
    for doc in results:
        raw_sum = doc.get("raw_work_hours_sum")
        raw_count = doc.get("raw_work_hours_count")
        if raw_count is not None and raw_count > 0 and raw_sum is not None:
            mean_val = Decimal(str(raw_sum)) / Decimal(str(raw_count))
            avg_work_hours = round_half_up(mean_val, decimal_places=2)
        elif doc.get("avg_work_hours") is not None:
            avg_work_hours = round_half_up(doc["avg_work_hours"], decimal_places=2)
        else:
            avg_work_hours = None

        items.append({
            "department": doc["department"],
            "headcount": int(doc.get("headcount") or 0),
            "present_days": round(float(doc.get("present_days") or 0.0), 1),
            "avg_work_hours": avg_work_hours,
            "late_count": int(doc.get("late_count") or 0),
            "total_late_minutes": int(doc.get("total_late_minutes") or 0),
            "leave_count": int(doc.get("leave_count") or 0),
            "on_duty_count": int(doc.get("on_duty_count") or 0),
        })

    return {
        "month": month,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# Late-Comers Leaderboard
# --------------------------------------------------------------------------- #
def build_late_leaderboard_pipeline(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Constructs the MongoDB aggregation pipeline for the late-comers leaderboard.

    - Matches attendance records in the given calendar month.
    - Groups by emp_code to accumulate total_late_minutes and late_count.
    - Filters out employees with total_late_minutes <= 0.
    - Joins employee details; drops non-existent employees.
    - Applies optional department filter before ranking.
    - Uses standard competition ranking ($rank via $setWindowFields; ties share rank).
    - Retains all employees whose rank <= limit (retains ties at cutoff).
    - Sorts by total_late_minutes descending, emp_code ascending.
    """
    year = int(month[:4])
    m = int(month[5:7])
    _, last_day = calendar.monthrange(year, m)
    start_date_str = f"{year:04d}-{m:02d}-01"
    end_date_str = f"{year:04d}-{m:02d}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        # Match attendance records within requested month
        {
            "$match": {
                "date": {"$gte": start_date_str, "$lte": end_date_str}
            }
        },
        # Aggregate late minutes and late count per employee
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        # Filter to employees with late minutes
        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },
        # Join employee details
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        # Unwind employee details (drops non-existent employees)
        {
            "$unwind": "$emp"
        },
    ]

    # Filter by department before ranking if specified
    if department is not None:
        pipeline.append({
            "$match": {
                "emp.department": department
            }
        })

    # Standard competition ranking ($rank window function; ties share rank)
    pipeline.append({
        "$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {
                    "$rank": {}
                }
            },
        }
    })

    # Retain all employees through cutoff rank (including ties)
    pipeline.append({
        "$match": {
            "rank": {"$lte": limit}
        }
    })

    # Project response fields
    pipeline.append({
        "$project": {
            "_id": 0,
            "rank": "$rank",
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": "$total_late_minutes",
            "late_count": "$late_count",
        }
    })

    # Sort by total late minutes descending, emp_code ascending
    pipeline.append({
        "$sort": {
            "total_late_minutes": -1,
            "emp_code": 1,
        }
    })

    return pipeline


@app.get(
    "/analytics/leaderboard/late",
    response_model=Leaderboard,
    status_code=status.HTTP_200_OK,
)
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None),
):
    """
    Ranks employees by total late minutes for a month, highest first.

    Uses standard competition ranking (ties share rank, next rank skipped).
    Supports optional department filtering and configurable rank limits.
    """
    # Validate calendar month
    try:
        parsed_dt = datetime.strptime(month, "%Y-%m")
        if parsed_dt.strftime("%Y-%m") != month:
            raise ValueError()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="month must be a valid calendar month in YYYY-MM format",
        )

    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    results = list(attendance_logs_col.aggregate(pipeline))

    items = []
    for doc in results:
        items.append({
            "rank": int(doc["rank"]),
            "emp_code": doc["emp_code"],
            "name": doc["name"],
            "department": doc["department"],
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"]),
        })

    return {
        "month": month,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# Department Daily Attendance Trend
# --------------------------------------------------------------------------- #
def build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str,
) -> List[Dict[str, Any]]:
    """
    Constructs the MongoDB aggregation pipeline for daily department attendance trends.

    - Generates a complete daily date scaffold from from_date to to_date inclusive.
    - Identifies working days (Mon-Fri) and daily department headcount (R9).
    - Aggregates present_count and late_count for department employees.
    - Computes attendance_rate (present_count / headcount) with half-up rounding (R8).
    - Computes a 7-day moving average using $setWindowFields with documents: [-6, "current"].
    - Sorts chronologically by date ascending.
    """
    f_dt = date.fromisoformat(from_date)
    t_dt = date.fromisoformat(to_date)
    num_days = (t_dt - f_dt).days + 1

    return [
        # Match target department employees
        {
            "$match": {
                "department": department
            }
        },
        # Collect department employee codes and joining dates
        {
            "$group": {
                "_id": "$department",
                "dept_emp_codes": {"$push": "$emp_code"},
                "dept_joined_dates": {"$push": "$joined_on"},
            }
        },
        # Lookup and aggregate department attendance logs within date range
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_codes": "$dept_emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$emp_codes"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": "$date",
                            "daily_present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "daily_late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily_att",
            }
        },
        # Build date scaffold across full range with daily headcount and attendance
        {
            "$project": {
                "department": "$_id",
                "days": {
                    "$map": {
                        "input": {"$range": [0, num_days]},
                        "as": "offset",
                        "in": {
                            "$let": {
                                "vars": {
                                    "day_str": {
                                        "$dateToString": {
                                            "format": "%Y-%m-%d",
                                            "date": {
                                                "$dateAdd": {
                                                    "startDate": {
                                                        "$dateFromString": {
                                                            "dateString": from_date,
                                                            "format": "%Y-%m-%d",
                                                        }
                                                    },
                                                    "unit": "day",
                                                    "amount": "$$offset",
                                                }
                                            },
                                        }
                                    }
                                },
                                "in": {
                                    "date": "$$day_str",
                                    "is_working_day": {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$$day_str",
                                                        "format": "%Y-%m-%d",
                                                    }
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                    "headcount": {
                                        "$size": {
                                            "$filter": {
                                                "input": "$dept_joined_dates",
                                                "as": "j_date",
                                                "cond": {"$lte": ["$$j_date", "$$day_str"]},
                                            }
                                        }
                                    },
                                    "att": {
                                        "$filter": {
                                            "input": "$daily_att",
                                            "as": "a",
                                            "cond": {"$eq": ["$$a._id", "$$day_str"]},
                                        }
                                    },
                                },
                            }
                        },
                    }
                },
            }
        },
        # Unwind into individual daily records
        {
            "$unwind": "$days"
        },
        # Project day-level counts, defaulting missing days to zero
        {
            "$project": {
                "_id": 0,
                "date": "$days.date",
                "is_working_day": "$days.is_working_day",
                "headcount": "$days.headcount",
                "present_count": {
                    "$ifNull": [{"$arrayElemAt": ["$days.att.daily_present_count", 0]}, 0.0]
                },
                "late_count": {
                    "$ifNull": [{"$arrayElemAt": ["$days.att.daily_late_count", 0]}, 0]
                },
            }
        },
        # Calculate attendance_rate on working days with active headcount (R8)
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {"$divide": ["$present_count", "$headcount"]},
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        # 7-day moving average over preceding 6 days and current day
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        # Round 7-day moving average half-up to 4 decimal places (R8)
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": ["$moving_avg_7d", 10000]},
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        # Chronological sort
        {
            "$sort": {"date": 1}
        },
    ]


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=Trend,
    status_code=status.HTTP_200_OK,
)
def get_department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    """
    Daily attendance trend for a department over an inclusive date range (up to 92 days).

    Returns exactly one row per calendar day with working-day flag, daily headcount (R9),
    present count, late count, attendance rate, and 7-day rolling moving average.
    """
    # Validate from date format
    try:
        f_dt = date.fromisoformat(from_)
        if f_dt.isoformat() != from_:
            raise ValueError()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="from must be a valid date in YYYY-MM-DD format",
        )

    # Validate to date format
    try:
        t_dt = date.fromisoformat(to)
        if t_dt.isoformat() != to:
            raise ValueError()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="to must be a valid date in YYYY-MM-DD format",
        )

    # Validate date range ordering
    if t_dt < f_dt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="to date must not be earlier than from date",
        )

    # Validate maximum range limit (92 calendar days)
    delta_days = (t_dt - f_dt).days + 1
    if delta_days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"date range ({delta_days} days) exceeds maximum of 92 calendar days",
        )

    # Verify department exists in employees collection
    dept_exists = employees_col.count_documents({"department": department}, limit=1) > 0
    if not dept_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Department '{department}' not found",
        )

    # Execute MongoDB aggregation pipeline
    pipeline = build_department_trend_pipeline(department, from_, to)
    results = list(employees_col.aggregate(pipeline))

    items = []
    for doc in results:
        rate = doc.get("attendance_rate")
        if rate is not None:
            rate = round_half_up(rate, decimal_places=4)

        m_avg = doc.get("moving_avg_7d")
        if m_avg is not None:
            m_avg = round_half_up(m_avg, decimal_places=4)

        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc["is_working_day"]),
            "headcount": int(doc["headcount"]),
            "present_count": round(float(doc.get("present_count") or 0.0), 1),
            "late_count": int(doc.get("late_count") or 0),
            "attendance_rate": rate,
            "moving_avg_7d": m_avg,
        })

    return {
        "department": department,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# MongoDB Explain Diagnostics
# --------------------------------------------------------------------------- #
SUPPORTED_EXPLAIN_ENDPOINTS = (
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
)


@app.get(
    "/admin/explain/{endpoint}",
    response_model=ExplainResponse,
    status_code=status.HTTP_200_OK,
)
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None),
    department: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    status_param: Optional[str] = Query(default=None, alias="status"),
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None),
    page: Optional[int] = Query(default=None),
    page_size: Optional[int] = Query(default=None),
):
    """
    Runs MongoDB explain with executionStats verbosity for endpoint queries or aggregations.

    Executes against the real database using the same query builders and parameter
    validation as the production endpoints. Used to verify index utilization
    and avoid collection scans (COLLSCAN).
    """
    if endpoint not in SUPPORTED_EXPLAIN_ENDPOINTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported endpoint '{endpoint}'. Supported endpoints: {', '.join(SUPPORTED_EXPLAIN_ENDPOINTS)}",
        )

    if endpoint == "attendance_list":
        if date_from:
            try:
                date.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(status_code=422, detail="date_from must be a valid date in YYYY-MM-DD format")
        if date_to:
            try:
                date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(status_code=422, detail="date_to must be a valid date in YYYY-MM-DD format")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

        if status_param is not None:
            if status_param not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid status: '{status_param}'. Must be one of PRESENT, ABSENT, LEAVE, WFH, ON_DUTY",
                )

        page_val = 1 if page is None else page
        if page_val < 1:
            raise HTTPException(status_code=422, detail="page must be >= 1")

        page_size_val = 20 if page_size is None else page_size
        if page_size_val < 1 or page_size_val > 100:
            raise HTTPException(status_code=422, detail="page_size must be between 1 and 100")

        q, sort_fields, skip, limit_count = build_attendance_list_query(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status_param=status_param,
            page=page_val,
            page_size=page_size_val,
        )
        collection_name = "attendance_logs"
        cmd = {
            "find": collection_name,
            "filter": q,
            "sort": dict(sort_fields),
            "skip": skip,
            "limit": limit_count,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not emp_code.strip():
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly explain")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly explain")
        try:
            parsed_dt = datetime.strptime(month, "%Y-%m")
            if parsed_dt.strftime("%Y-%m") != month:
                raise ValueError()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month must be a valid calendar month in YYYY-MM format",
            )

        collection_name = "attendance_logs"
        pipeline = build_employee_monthly_pipeline(emp_code=emp_code, month=month)
        cmd = {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary explain")
        try:
            parsed_dt = datetime.strptime(month, "%Y-%m")
            if parsed_dt.strftime("%Y-%m") != month:
                raise ValueError()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month must be a valid calendar month in YYYY-MM format",
            )

        collection_name = "employees"
        pipeline = build_department_summary_pipeline(month=month, department=department)
        cmd = {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard explain")
        try:
            parsed_dt = datetime.strptime(month, "%Y-%m")
            if parsed_dt.strftime("%Y-%m") != month:
                raise ValueError()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month must be a valid calendar month in YYYY-MM format",
            )

        limit_val = 10 if limit is None else limit
        if limit_val < 1 or limit_val > 50:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 50")

        collection_name = "attendance_logs"
        pipeline = build_late_leaderboard_pipeline(month=month, limit=limit_val, department=department)
        cmd = {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "department_trend":
        if not department or not department.strip():
            raise HTTPException(status_code=422, detail="department is required for department_trend explain")
        if not from_:
            raise HTTPException(status_code=422, detail="from date is required for department_trend explain")
        if not to:
            raise HTTPException(status_code=422, detail="to date is required for department_trend explain")

        try:
            f_dt = date.fromisoformat(from_)
            if f_dt.isoformat() != from_:
                raise ValueError()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="from must be a valid date in YYYY-MM-DD format",
            )

        try:
            t_dt = date.fromisoformat(to)
            if t_dt.isoformat() != to:
                raise ValueError()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="to must be a valid date in YYYY-MM-DD format",
            )

        if t_dt < f_dt:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="to date must not be earlier than from date",
            )

        delta_days = (t_dt - f_dt).days + 1
        if delta_days > 92:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"date range ({delta_days} days) exceeds maximum of 92 calendar days",
            )

        collection_name = "employees"
        pipeline = build_department_trend_pipeline(department=department, from_date=from_, to_date=to)
        cmd = {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        }

    explain_result = db.command("explain", cmd, verbosity="executionStats")
    raw_explain = json.loads(json_util.dumps(explain_result))

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": raw_explain,
    }
