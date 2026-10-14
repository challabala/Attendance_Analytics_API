"""
Employee Attendance & Analytics API - Phase 1 Foundation
HROne Software Engineer Assignment

All production application code remains in this file.
"""
import os
from contextlib import asynccontextmanager
from typing import Dict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Load local .env if present; system environment variables take precedence
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# PyMongo MongoClient configuration:
# - tz_aware=True: Decodes BSON dates as timezone-aware UTC datetimes
# - serverSelectionTimeoutMS=2000: 2 second timeout for server selection
# - connectTimeoutMS=2000: 2 second timeout for socket connection
# Note: MongoClient does not block on initialization, allowing application import
# even if MongoDB is temporarily unreachable.
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Application startup
    yield
    # Application shutdown: cleanly close the MongoDB connection pool
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="HROne Employee Attendance & Analytics API backend service",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Phase 1: Health Endpoint
# --------------------------------------------------------------------------- #
@app.get("/health", status_code=status.HTTP_200_OK)
def health() -> Dict[str, str]:
    """
    Health check endpoint.
    Performs an active MongoDB ping to verify database connectivity.
    Returns HTTP 200 {"status": "ok"} when reachable,
    or HTTP 503 {"detail": "Database unavailable"} when MongoDB is unreachable.
    """
    try:
        # Actively ping MongoDB admin database
        client.admin.command("ping")
        return {"status": "ok"}
    except (PyMongoError, Exception) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        ) from exc


# --------------------------------------------------------------------------- #
# Future Phase Placeholders (Do not implement prematurely in Phase 1)
# --------------------------------------------------------------------------- #
# Phase 2: Time handling, timestamp validation, and business-rule helpers (R1 - R5)
# Phase 3: Index creation and employee, attendance, and punch-in endpoints (R1, R2, R6, R10)
# Phase 4: Punch-out endpoint (R3 - R5)
# Phase 5: Attendance regularization and audit history (PATCH /attendance/{emp_code}/{date})
# Phase 6: Monthly employee summary & department summary analytics (R7, R9)
# Phase 7: Late leaderboard (window functions) and department trend (gap filling, moving avg)
# Phase 8: Explain endpoint (/admin/explain/{endpoint})
