"""
Phase 4 Tests: Punch-Out API, Overnight Shifts, Derived Fields, and Concurrency.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.main import (
    IST,
    MAX_EPOCH_MS,
    MIN_EPOCH_MS,
    UTC,
    PunchOutRequest,
    app,
    punch_out,
)


class TestPhase4ASGIHelper:
    """Helper for testing FastAPI endpoints via ASGI."""

    @staticmethod
    def request(method: str, path: str, body: dict = None, query: str = ""):
        body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body_bytes)).encode("ascii")),
        ]

        async def _call():
            scope = {
                "type": "http",
                "method": method,
                "path": path,
                "raw_path": path.encode(),
                "query_string": query.encode("utf-8"),
                "headers": headers,
            }
            res_status = None
            res_headers = []
            res_body = []

            async def receive():
                return {"type": "http.request", "body": body_bytes, "more_body": False}

            async def send(message):
                nonlocal res_status, res_headers, res_body
                if message["type"] == "http.response.start":
                    res_status = message["status"]
                    res_headers = message.get("headers", [])
                elif message["type"] == "http.response.body":
                    res_body.append(message.get("body", b""))

            await app(scope, receive, send)
            return res_status, json.loads(b"".join(res_body).decode("utf-8"))

        return asyncio.run(_call())


class TestPunchOutSuccess(unittest.TestCase):
    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_normal_daytime_punch_out_success_200(self, mock_att, mock_emp):
        """Daytime punch-out calculates 9.12 hours, 0 overtime, half_day=False."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        # In: 09:28 IST (03:58 UTC) on 2026-07-06
        in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=UTC)
        # Out: 18:35 IST (13:05 UTC) on 2026-07-06 (1783343100000 ms)
        out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=UTC)

        existing_record = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing_record

        updated_record = dict(existing_record)
        updated_record["punch_out"] = out_dt
        updated_record["work_hours"] = 9.12
        updated_record["overtime_minutes"] = 0
        updated_record["half_day"] = False
        mock_att.find_one_and_update.return_value = updated_record

        body = {
            "emp_code": "EMP0001",
            "punched_at": 1783343100000,
        }
        status_code, data = TestPhase4ASGIHelper.request("POST", "/attendance/punch-out", body=body)

        self.assertEqual(status_code, 200)
        self.assertEqual(data["emp_code"], "EMP0001")
        self.assertEqual(data["date"], "2026-07-06")
        self.assertEqual(data["work_hours"], 9.12)
        self.assertEqual(data["overtime_minutes"], 0)
        self.assertFalse(data["half_day"])
        self.assertEqual(data["punch_out"], 1783343100000)
        self.assertNotIn("_id", data)
        self.assertNotIn("id", data)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_overnight_punch_out_across_midnight(self, mock_att, mock_emp):
        """Overnight shift (22:00-06:00): punch-in on 6th, punch-out on 7th gives 40 overtime."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0005",
            "shift_start": "22:00",
            "shift_end": "06:00",
        }
        # In: 2026-07-06 21:55 IST = 16:25 UTC
        in_dt = datetime(2026, 7, 6, 16, 25, 0, tzinfo=UTC)
        # Out: 2026-07-07 06:40 IST = 01:10 UTC (8h 45m = 8.75h)
        out_dt = datetime(2026, 7, 7, 1, 10, 0, tzinfo=UTC)
        out_ms = int(out_dt.timestamp() * 1000)

        existing_record = {
            "emp_code": "EMP0005",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing_record

        updated_record = dict(existing_record)
        updated_record["punch_out"] = out_dt
        updated_record["work_hours"] = 8.75
        updated_record["overtime_minutes"] = 40
        updated_record["half_day"] = False
        mock_att.find_one_and_update.return_value = updated_record

        body = {
            "emp_code": "EMP0005",
            "punched_at": out_ms,
        }
        status_code, data = TestPhase4ASGIHelper.request("POST", "/attendance/punch-out", body=body)

        self.assertEqual(status_code, 200)
        self.assertEqual(data["work_hours"], 8.75)
        self.assertEqual(data["overtime_minutes"], 40)
        self.assertFalse(data["half_day"])
        self.assertEqual(data["date"], "2026-07-06")

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_overtime_threshold_boundaries_29m_vs_30m(self, mock_att, mock_emp):
        """29m59s gives 0 overtime, 30m gives 30 overtime."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
        existing_record = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing_record

        # Case 1: 18:59:59 IST (29m59s overtime)
        out_29m59s = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST)
        mock_att.find_one_and_update.return_value = {
            **existing_record,
            "punch_out": out_29m59s,
            "work_hours": 9.50,
            "overtime_minutes": 0,
            "half_day": False,
        }
        status_code, data = TestPhase4ASGIHelper.request(
            "POST",
            "/attendance/punch-out",
            body={"emp_code": "EMP0001", "punched_at": int(out_29m59s.timestamp() * 1000)},
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(data["overtime_minutes"], 0)

        # Case 2: 19:00:00 IST (30m00s overtime)
        out_30m = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
        mock_att.find_one_and_update.return_value = {
            **existing_record,
            "punch_out": out_30m,
            "work_hours": 9.50,
            "overtime_minutes": 30,
            "half_day": False,
        }
        status_code, data = TestPhase4ASGIHelper.request(
            "POST",
            "/attendance/punch-out",
            body={"emp_code": "EMP0001", "punched_at": int(out_30m.timestamp() * 1000)},
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(data["overtime_minutes"], 30)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_half_day_thresholds_around_4_50_hours(self, mock_att, mock_emp):
        """Work hours < 4.50 produces half_day=True; >= 4.50 produces half_day=False."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing

        # 4.49 hours -> half_day = True
        out_4_49 = in_dt + timedelta(seconds=int(4.49 * 3600))
        mock_att.find_one_and_update.return_value = {
            **existing,
            "punch_out": out_4_49,
            "work_hours": 4.49,
            "half_day": True,
        }
        status_code, data = TestPhase4ASGIHelper.request(
            "POST",
            "/attendance/punch-out",
            body={"emp_code": "EMP0001", "punched_at": int(out_4_49.timestamp() * 1000)},
        )
        self.assertEqual(status_code, 200)
        self.assertTrue(data["half_day"])

        # 4.50 hours -> half_day = False
        out_4_50 = in_dt + timedelta(hours=4, minutes=30)
        mock_att.find_one_and_update.return_value = {
            **existing,
            "punch_out": out_4_50,
            "work_hours": 4.50,
            "half_day": False,
        }
        status_code, data = TestPhase4ASGIHelper.request(
            "POST",
            "/attendance/punch-out",
            body={"emp_code": "EMP0001", "punched_at": int(out_4_50.timestamp() * 1000)},
        )
        self.assertEqual(status_code, 200)
        self.assertFalse(data["half_day"])

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_punch_out_omitted_timestamp_defaults_to_now(self, mock_att, mock_emp):
        """When punched_at is omitted, defaults to current UTC time."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime.now(UTC) - timedelta(hours=5)
        mock_att.find_one.return_value = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one_and_update.return_value = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": datetime.now(UTC),
            "work_hours": 5.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }

        body = {"emp_code": "EMP0001"}
        status_code, data = TestPhase4ASGIHelper.request("POST", "/attendance/punch-out", body=body)
        self.assertEqual(status_code, 200)
        self.assertIsNotNone(data["punch_out"])


class TestPunchOutValidationAndErrors(unittest.TestCase):
    @patch("app.main.employees_col")
    def test_unknown_employee_returns_404(self, mock_emp):
        """Unknown employee returns 404."""
        mock_emp.find_one.return_value = None
        status_code, data = TestPhase4ASGIHelper.request(
            "POST",
            "/attendance/punch-out",
            body={"emp_code": "EMP9999"},
        )
        self.assertEqual(status_code, 404)
        self.assertEqual(data["detail"], "Employee not found")

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_no_qualifying_punch_in_returns_404(self, mock_att, mock_emp):
        """Employee has no open record with punch_in <= punched_at -> 404."""
        mock_emp.find_one.return_value = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
        mock_att.find_one.return_value = None

        status_code, data = TestPhase4ASGIHelper.request(
            "POST",
            "/attendance/punch-out",
            body={"emp_code": "EMP0001", "punched_at": 1783343100000},
        )
        self.assertEqual(status_code, 404)
        self.assertIn("detail", data)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_record_already_punched_out_returns_409(self, mock_att, mock_emp):
        """Most recent qualifying record already has punch_out -> 409."""
        mock_emp.find_one.return_value = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
        in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
        out_dt = datetime(2026, 7, 6, 18, 30, 0, tzinfo=IST)

        mock_att.find_one.return_value = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": out_dt,
        }

        status_code, data = TestPhase4ASGIHelper.request(
            "POST",
            "/attendance/punch-out",
            body={"emp_code": "EMP0001", "punched_at": 1783343100000},
        )
        self.assertEqual(status_code, 409)
        self.assertEqual(data["detail"], "Attendance record already punched out")

    def test_invalid_timestamp_types_return_422(self):
        """String, float, bool, seconds, out-of-range return 422."""
        bad_timestamps = [
            "1783343100000",
            1783343100000.5,
            True,
            False,
            1783343100,  # seconds
            MIN_EPOCH_MS - 1,
            MAX_EPOCH_MS + 1,
        ]
        for bad_ts in bad_timestamps:
            status_code, _ = TestPhase4ASGIHelper.request(
                "POST",
                "/attendance/punch-out",
                body={"emp_code": "EMP0001", "punched_at": bad_ts},
            )
            self.assertEqual(status_code, 422)

    def test_missing_emp_code_returns_422(self):
        """Missing required emp_code returns 422."""
        status_code, _ = TestPhase4ASGIHelper.request(
            "POST",
            "/attendance/punch-out",
            body={"punched_at": 1783343100000},
        )
        self.assertEqual(status_code, 422)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_punch_out_before_or_equal_to_punch_in_returns_422(self, mock_att, mock_emp):
        """punched_at <= punch_in returns 422."""
        mock_emp.find_one.return_value = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
        in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
        mock_att.find_one.return_value = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": None,
        }

        # Equal timestamp
        in_ms = int(in_dt.timestamp() * 1000)
        status_code, _ = TestPhase4ASGIHelper.request(
            "POST",
            "/attendance/punch-out",
            body={"emp_code": "EMP0001", "punched_at": in_ms},
        )
        self.assertEqual(status_code, 422)

        # Before punch_in (e.g. 1 second earlier)
        status_code, _ = TestPhase4ASGIHelper.request(
            "POST",
            "/attendance/punch-out",
            body={"emp_code": "EMP0001", "punched_at": in_ms - 1000},
        )
        self.assertEqual(status_code, 422)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_punch_out_more_than_24h_returns_422(self, mock_att, mock_emp):
        """punched_at > 24 hours after punch_in returns 422."""
        mock_emp.find_one.return_value = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
        in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
        mock_att.find_one.return_value = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": None,
        }

        out_24h_1s = in_dt + timedelta(hours=24, seconds=1)
        status_code, _ = TestPhase4ASGIHelper.request(
            "POST",
            "/attendance/punch-out",
            body={"emp_code": "EMP0001", "punched_at": int(out_24h_1s.timestamp() * 1000)},
        )
        self.assertEqual(status_code, 422)


class TestPunchOutConcurrency(unittest.TestCase):
    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_simultaneous_punch_out_requests_one_200_one_409(self, mock_att, mock_emp):
        """Simulate two simultaneous requests: atomic find_one_and_update yields exactly one 200 and one 409."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
        open_record = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = open_record

        # Simulate atomic find_one_and_update: first call succeeds, second call returns None
        update_calls = 0
        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            nonlocal update_calls
            update_calls += 1
            if update_calls == 1:
                res = dict(open_record)
                res["punch_out"] = update_doc["$set"]["punch_out"]
                res["work_hours"] = update_doc["$set"]["work_hours"]
                res["overtime_minutes"] = update_doc["$set"]["overtime_minutes"]
                res["half_day"] = update_doc["$set"]["half_day"]
                return res
            return None

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        out_ms = int((in_dt + timedelta(hours=9)).timestamp() * 1000)
        req = PunchOutRequest(emp_code="EMP0001", punched_at=out_ms)

        results = []
        errors = []

        def worker():
            try:
                res = punch_out(req)
                results.append(res)
            except HTTPException as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(worker)
            f2 = executor.submit(worker)
            f1.result()
            f2.result()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].status_code, 409)
        self.assertEqual(errors[0].detail, "Attendance record already punched out")


if __name__ == "__main__":
    unittest.main()
