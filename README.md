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

## 3. Running Tests

Run the test suite using Python's standard `unittest` runner:
```powershell
python -m unittest tests/test_phase1.py
```

---

## 4. Submission Notes
- Production application code is contained entirely in `app/main.py`.
- No credentials, secrets, `.env`, or `.venv` files are committed.
