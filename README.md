# HROne Employee Attendance & Analytics API

Backend service for employee attendance tracking, regularization, and analytics built with FastAPI, PyMongo, and MongoDB.

---

## 1. Prerequisites

- **Python**: Version 3.11 or newer (Python 3.13 supported).
- **MongoDB**: Version 6.0 or newer (MongoDB 7.0 / Atlas M0 supported).
- **Shell**: Windows PowerShell (or Command Prompt / bash).

---

## 2. Windows PowerShell Setup Guide

### Step 1: Create a Virtual Environment
From the repository root directory:
```powershell
python -m venv .venv
```

### Step 2: Activate the Virtual Environment
```powershell
.\.venv\Scripts\Activate.ps1
```
*(If execution policies restrict running scripts in PowerShell, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first, or activate via `.\.venv\Scripts\activate.bat` in CMD).*

### Step 3: Install Required Dependencies
```powershell
pip install -r requirements.txt
```

### Step 4: Configure Environment Variables
Copy the template configuration file:
```powershell
Copy-Item .env.example .env
```
Ensure `.env` contains your target MongoDB instance settings:
```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### Step 5: Start MongoDB
Ensure MongoDB is running locally or provide a connection URI in `.env` (such as MongoDB Atlas):
```powershell
# If using Docker for local MongoDB 7:
docker run -d -p 27017:27017 --name mongodb mongo:7
```

### Step 6: Seed Sample Data (Optional)
To seed the 9 sample documents from `sample_data/`:
```powershell
python sample_seed.py
```

### Step 7: Run the Application
Start the Uvicorn ASGI server:
```powershell
uvicorn app.main:app --port 8000
```
*(Add `--reload` during active development if auto-reloading is desired).*

### Step 8: Check the Health Endpoint
In a separate PowerShell prompt:
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -Method Get
```
- **When MongoDB is connected:** Returns HTTP `200 OK`:
  ```json
  {
    "status": "ok"
  }
  ```
- **When MongoDB is unreachable:** Returns HTTP `503 Service Unavailable`:
  ```json
  {
    "detail": "Database unavailable"
  }
  ```

### Step 9: Interactive API Documentation
Open your web browser and navigate to:
- **Swagger UI:** [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc:** [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)

---

## 3. Implemented Endpoints (Phases 1 - 10)

| Method | Endpoint | Description | Phase |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Dynamic readiness probe checking MongoDB connection (200 / 503). | Phase 1 |
| `POST` | `/employees` | Creates employee with unique `emp_code`. Server sets `created_at`. | Phase 3 |
| `GET` | `/employees` | Lists employees sorted by `emp_code` with case-sensitive `department` filter & pagination. | Phase 3 |
| `POST` | `/attendance/punch-in` | Creates day's record (R1, R2); race-safe with `(emp_code, date)` unique index. | Phase 3 |
| `POST` | `/attendance/punch-out` | Closes most recent record (R3 - R5); atomic concurrency-safe update; handles overnight shifts. | Phase 4 |
| `GET` | `/attendance` | Filterable by `emp_code`, `date_from`, `date_to`, and `status`. Sorted by `date` desc, `emp_code` asc. | Phase 3 |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularizes attendance record; recalculates R2–R5; atomic compare-and-swap audit history append. | Phase 5 |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee attendance summary (R5–R8) via indexed MongoDB aggregation. | Phase 6 |
| `GET` | `/analytics/departments/summary` | Monthly department summary analytics (R5–R9) via `$lookup` aggregation across employees & attendance. | Phase 7 |
| `GET` | `/analytics/leaderboard/late` | Monthly late-comers leaderboard with standard competition ranking ($rank via $setWindowFields). | Phase 8 |
| `GET` | `/analytics/departments/{department}/trend` | Daily department attendance trend with MongoDB gap-filling date scaffold, daily headcount (R9), and 7-day moving avg. | Phase 9 |
| `GET` | `/admin/explain/{endpoint}` | MongoDB explain diagnostics running with `executionStats` on shared query/pipeline builders for all five analytics and listing operations. | Phase 10 |

### Database Indexes Initialized at Startup
- **`employees`**:
  - `uniq_emp_code`: Unique index on `emp_code` (enforces uniqueness atomically).
  - `idx_department_emp_code`: Compound index on `(department, emp_code)` for department filtering and ascending sorting.
  - `idx_joined_on`: Index on `joined_on` for monthly employee eligibility range scans (R9).
  - `idx_department_joined_on`: Compound index on `(department, joined_on)` for department-filtered eligibility (R9).
- **`attendance_logs`**:
  - `uniq_emp_code_date`: Unique compound index on `(emp_code, date)` (guarantees at most 1 record per employee per day and resolves concurrent punch-in races).
  - `idx_emp_code_punch_in_desc`: Compound index on `(emp_code, punch_in: -1)` for fast punch-out record selection.
  - `idx_date_desc_emp_code_asc`: Compound index on `(date: -1, emp_code: 1)` for fast attendance listing and date range scans.
  - `idx_status_date`: Compound index on `(status: 1, date: -1)` for status filtering.

---

## 4. Running Tests

Run the full test suite using Python's standard `unittest` discovery:
```powershell
python -m unittest discover -s tests -p "test_phase*.py" -v
```
Or run individual test modules:
```powershell
python -m unittest tests/test_phase1.py
python -m unittest tests/test_phase2.py
python -m unittest tests/test_phase3.py
python -m unittest tests/test_phase4.py
python -m unittest tests/test_phase5.py
python -m unittest tests/test_phase6.py
python -m unittest tests/test_phase7.py
python -m unittest tests/test_phase8.py
python -m unittest tests/test_phase9.py
python -m unittest tests/test_phase10.py
```

---

## 5. Submission Notes
- Production application code is contained entirely in `app/main.py`.
- No credentials, secrets, `.env`, or `.venv` files are committed.

