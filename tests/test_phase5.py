"""
Phase 5 Tests: Attendance Regularization with Audit History (PATCH /attendance/{emp_code}/{date}).
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pymongo import ReturnDocument

from app.main import (
    IST,
    MAX_EPOCH_MS,
    MIN_EPOCH_MS,
    UTC,
    RegularizeRequest,
    app,
    regularize_attendance,
)


class TestPhase5ASGIHelper:
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


class TestRegularizeSuccess(unittest.TestCase):
    """Basic success cases for PATCH /attendance/{emp_code}/{date}."""

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_correct_only_status(self, mock_att, mock_emp):
        """Correcting only status from PRESENT to WFH."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)  # 09:30 IST
        out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC)  # 18:30 IST
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": out_dt,
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            res = dict(existing)
            res.update(update_doc["$set"])
            res["history"] = list(res.get("history", [])) + [update_doc["$push"]["history"]]
            return res

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        payload = {
            "status": "WFH",
            "reason": "Approved work from home request",
            "regularized_by": "hr_manager",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["status"], "WFH")
        self.assertEqual(len(body["history"]), 1)
        self.assertIn("status", body["history"][0]["changes"])
        self.assertEqual(body["history"][0]["changes"]["status"]["from"], "PRESENT")
        self.assertEqual(body["history"][0]["changes"]["status"]["to"], "WFH")
        # Derived fields did not change so should not be in changes
        self.assertNotIn("work_hours", body["history"][0]["changes"])
        self.assertNotIn("late_minutes", body["history"][0]["changes"])

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_correct_only_punch_in_recalculates_late_and_work_hours(self, mock_att, mock_emp):
        """Correcting punch_in from 09:45 (late) to 09:30 (on-time)."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        # In: 09:45 IST (15m late, 8.75h) -> Out: 18:30 IST
        old_in_dt = datetime(2026, 7, 6, 4, 15, 0, tzinfo=UTC)
        out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC)
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": old_in_dt,
            "punch_out": out_dt,
            "work_hours": 8.75,
            "late_minutes": 15,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            res = dict(existing)
            res.update(update_doc["$set"])
            res["history"] = list(res.get("history", [])) + [update_doc["$push"]["history"]]
            return res

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        # New punch_in: 09:30 IST (04:00 UTC) -> 1783310400000 ms
        new_in_ms = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
        payload = {
            "punch_in": new_in_ms,
            "reason": "Corrected entry biometric glitch",
            "regularized_by": "hr_admin",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["late_minutes"], 0)
        self.assertEqual(body["work_hours"], 9.0)
        changes = body["history"][0]["changes"]
        self.assertIn("punch_in", changes)
        self.assertIn("late_minutes", changes)
        self.assertEqual(changes["late_minutes"]["from"], 15)
        self.assertEqual(changes["late_minutes"]["to"], 0)
        self.assertIn("work_hours", changes)
        self.assertEqual(changes["work_hours"]["from"], 8.75)
        self.assertEqual(changes["work_hours"]["to"], 9.0)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_correct_only_punch_out_recalculates_overtime(self, mock_att, mock_emp):
        """Correcting punch_out adds overtime (18:30 -> 19:15 => 45 min overtime)."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)  # 09:30 IST
        old_out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC)  # 18:30 IST
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": old_out_dt,
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            res = dict(existing)
            res.update(update_doc["$set"])
            res["history"] = list(res.get("history", [])) + [update_doc["$push"]["history"]]
            return res

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        # New punch_out: 19:15 IST (13:45 UTC) -> 45 minutes overtime
        new_out_ms = int(datetime(2026, 7, 6, 13, 45, 0, tzinfo=UTC).timestamp() * 1000)
        payload = {
            "punch_out": new_out_ms,
            "reason": "Approved overtime stay",
            "regularized_by": "hr_manager",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["overtime_minutes"], 45)
        self.assertEqual(body["work_hours"], 9.75)
        changes = body["history"][0]["changes"]
        self.assertIn("punch_out", changes)
        self.assertIn("overtime_minutes", changes)
        self.assertEqual(changes["overtime_minutes"]["from"], 0)
        self.assertEqual(changes["overtime_minutes"]["to"], 45)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_correct_several_fields_at_once(self, mock_att, mock_emp):
        """Correcting status, punch_in, and punch_out simultaneously."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        old_in = datetime(2026, 7, 6, 4, 15, 0, tzinfo=UTC)
        old_out = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": old_in,
            "punch_out": old_out,
            "work_hours": 7.75,
            "late_minutes": 15,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            res = dict(existing)
            res.update(update_doc["$set"])
            res["history"] = list(res.get("history", [])) + [update_doc["$push"]["history"]]
            return res

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        new_in_ms = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
        new_out_ms = int(datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC).timestamp() * 1000)
        payload = {
            "status": "ON_DUTY",
            "punch_in": new_in_ms,
            "punch_out": new_out_ms,
            "reason": "Official client visit all day",
            "regularized_by": "dept_head",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["status"], "ON_DUTY")
        self.assertEqual(body["late_minutes"], 0)
        self.assertEqual(body["work_hours"], 9.0)
        changes = body["history"][0]["changes"]
        self.assertIn("status", changes)
        self.assertIn("punch_in", changes)
        self.assertIn("punch_out", changes)
        self.assertIn("late_minutes", changes)
        self.assertIn("work_hours", changes)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_change_presence_to_absent_clears_punches_and_derived(self, mock_att, mock_emp):
        """Rule A: Changing to ABSENT clears punch_in, punch_out, work_hours, resets derived fields."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
        out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC)
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": out_dt,
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            res = dict(existing)
            res.update(update_doc["$set"])
            res["history"] = list(res.get("history", [])) + [update_doc["$push"]["history"]]
            return res

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        payload = {
            "status": "ABSENT",
            "reason": "Unauthorized absence confirmed by manager",
            "regularized_by": "hr_manager",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["status"], "ABSENT")
        self.assertIsNone(body["punch_in"])
        self.assertIsNone(body["punch_out"])
        self.assertIsNone(body["work_hours"])
        self.assertEqual(body["late_minutes"], 0)
        self.assertEqual(body["overtime_minutes"], 0)
        self.assertFalse(body["half_day"])

        changes = body["history"][0]["changes"]
        self.assertIn("status", changes)
        self.assertEqual(changes["status"]["from"], "PRESENT")
        self.assertEqual(changes["status"]["to"], "ABSENT")
        self.assertIn("punch_in", changes)
        self.assertIsNotNone(changes["punch_in"]["from"])
        self.assertIsNone(changes["punch_in"]["to"])
        self.assertIn("punch_out", changes)
        self.assertIsNotNone(changes["punch_out"]["from"])
        self.assertIsNone(changes["punch_out"]["to"])
        self.assertIn("work_hours", changes)
        self.assertEqual(changes["work_hours"]["from"], 9.0)
        self.assertIsNone(changes["work_hours"]["to"])

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_change_presence_to_leave_clears_punches_and_derived(self, mock_att, mock_emp):
        """Rule A: Changing to LEAVE clears punch_in, punch_out, work_hours, resets derived fields."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
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

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            res = dict(existing)
            res.update(update_doc["$set"])
            res["history"] = list(res.get("history", [])) + [update_doc["$push"]["history"]]
            return res

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        payload = {
            "status": "LEAVE",
            "reason": "Applied medical leave retrospectively",
            "regularized_by": "hr_manager",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["status"], "LEAVE")
        self.assertIsNone(body["punch_in"])
        self.assertIsNone(body["punch_out"])
        self.assertIsNone(body["work_hours"])

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_change_absent_to_presence_with_valid_punches(self, mock_att, mock_emp):
        """Rule B: Correcting ABSENT record to PRESENT with supplied punches."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "ABSENT",
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            res = dict(existing)
            res.update(update_doc["$set"])
            res["history"] = list(res.get("history", [])) + [update_doc["$push"]["history"]]
            return res

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        in_ms = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
        out_ms = int(datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC).timestamp() * 1000)
        payload = {
            "status": "PRESENT",
            "punch_in": in_ms,
            "punch_out": out_ms,
            "reason": "Employee was present, biometric machine offline",
            "regularized_by": "hr_manager",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["status"], "PRESENT")
        self.assertEqual(body["punch_in"], in_ms)
        self.assertEqual(body["punch_out"], out_ms)
        self.assertEqual(body["work_hours"], 9.0)
        self.assertFalse(body["half_day"])

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_regularize_record_without_punch_out(self, mock_att, mock_emp):
        """Modifying an open attendance record (no punch-out)."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        old_in = datetime(2026, 7, 6, 4, 20, 0, tzinfo=UTC)  # 09:50 IST (20m late)
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": old_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 20,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            res = dict(existing)
            res.update(update_doc["$set"])
            res["history"] = list(res.get("history", [])) + [update_doc["$push"]["history"]]
            return res

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        # Update punch_in to 09:30 IST
        new_in_ms = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
        payload = {
            "punch_in": new_in_ms,
            "reason": "Corrected in-time while shift is ongoing",
            "regularized_by": "hr_manager",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 200)
        self.assertIsNone(body["punch_out"])
        self.assertIsNone(body["work_hours"])
        self.assertEqual(body["late_minutes"], 0)
        self.assertEqual(body["overtime_minutes"], 0)
        self.assertFalse(body["half_day"])

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_overnight_shift_regularization(self, mock_att, mock_emp):
        """Overnight shift: shift 22:00-06:00, in on date, out next calendar day."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "22:00",
            "shift_end": "06:00",
        }
        # Date: 2026-07-06. Shift 22:00 IST is 16:30 UTC on 2026-07-06.
        old_in = datetime(2026, 7, 6, 16, 30, 0, tzinfo=UTC)
        old_out = datetime(2026, 7, 7, 0, 30, 0, tzinfo=UTC)  # 06:00 IST
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": old_in,
            "punch_out": old_out,
            "work_hours": 8.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            res = dict(existing)
            res.update(update_doc["$set"])
            res["history"] = list(res.get("history", [])) + [update_doc["$push"]["history"]]
            return res

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        # Correct punch_out to 06:40 IST (01:10 UTC next day) -> 40m overtime
        new_out_ms = int(datetime(2026, 7, 7, 1, 10, 0, tzinfo=UTC).timestamp() * 1000)
        payload = {
            "punch_out": new_out_ms,
            "reason": "Overnight overtime shift correction",
            "regularized_by": "hr_manager",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["overtime_minutes"], 40)
        self.assertEqual(body["work_hours"], 8.67)


class TestAuditHistory(unittest.TestCase):
    """Audit history generation and serialization tests."""

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_successful_correction_appends_exactly_one_entry(self, mock_att, mock_emp):
        """A successful correction appends exactly one entry to history."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
        out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC)
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": out_dt,
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            res = dict(existing)
            res.update(update_doc["$set"])
            res["history"] = [update_doc["$push"]["history"]]
            return res

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        payload = {
            "status": "WFH",
            "reason": "WFH authorization approved",
            "regularized_by": "alice.hr",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(len(body["history"]), 1)
        entry = body["history"][0]
        self.assertEqual(entry["by"], "alice.hr")
        self.assertEqual(entry["reason"], "WFH authorization approved")
        self.assertIsInstance(entry["at"], int)
        self.assertGreater(entry["at"], 100000000000)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_existing_history_entries_preserved(self, mock_att, mock_emp):
        """Previous history entries remain intact when new entry is appended."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
        out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC)
        hist_1 = {
            "at": datetime(2026, 7, 6, 15, 0, 0, tzinfo=UTC),
            "by": "bob.hr",
            "reason": "Initial manual regularize",
            "changes": {"status": {"from": "ABSENT", "to": "PRESENT"}},
        }
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": out_dt,
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [hist_1],
        }
        mock_att.find_one.return_value = existing

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            res = dict(existing)
            res.update(update_doc["$set"])
            res["history"] = list(existing["history"]) + [update_doc["$push"]["history"]]
            return res

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        payload = {
            "status": "WFH",
            "reason": "Change status to WFH",
            "regularized_by": "alice.hr",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(len(body["history"]), 2)
        # Entry 0 preserved
        self.assertEqual(body["history"][0]["by"], "bob.hr")
        self.assertEqual(body["history"][0]["reason"], "Initial manual regularize")
        # Entry 1 added
        self.assertEqual(body["history"][1]["by"], "alice.hr")
        self.assertEqual(body["history"][1]["reason"], "Change status to WFH")

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_timestamp_serialization_in_nested_history_changes(self, mock_att, mock_emp):
        """Timestamps in changes (from/to) serialize as epoch milliseconds, null as null."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        old_in = datetime(2026, 7, 6, 4, 15, 0, tzinfo=UTC)
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": old_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 15,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            res = dict(existing)
            res.update(update_doc["$set"])
            res["history"] = [update_doc["$push"]["history"]]
            return res

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        new_in_ms = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
        payload = {
            "punch_in": new_in_ms,
            "reason": "Corrected punch in timestamp",
            "regularized_by": "alice.hr",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 200)
        p_in_change = body["history"][0]["changes"]["punch_in"]
        self.assertIsInstance(p_in_change["from"], int)
        self.assertEqual(p_in_change["from"], int(old_in.timestamp() * 1000))
        self.assertIsInstance(p_in_change["to"], int)
        self.assertEqual(p_in_change["to"], new_in_ms)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_missing_history_field_in_older_document_handled(self, mock_att, mock_emp):
        """Documents lacking a history field treat it as empty array."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC),
            "punch_out": datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            # No 'history' key
        }
        mock_att.find_one.return_value = existing

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            res = dict(existing)
            res.update(update_doc["$set"])
            res["history"] = [update_doc["$push"]["history"]]
            return res

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        payload = {
            "status": "WFH",
            "reason": "Change status to WFH",
            "regularized_by": "alice.hr",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(len(body["history"]), 1)


class TestRegularizeValidations(unittest.TestCase):
    """Validation and error tests for PATCH /attendance/{emp_code}/{date}."""

    @patch("app.main.employees_col")
    def test_unknown_employee_returns_404(self, mock_emp):
        """Unknown employee returns 404."""
        mock_emp.find_one.return_value = None
        payload = {
            "reason": "Valid reason here",
            "regularized_by": "hr_manager",
            "status": "PRESENT",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP9999/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 404)
        self.assertEqual(body["detail"], "Employee not found")

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_missing_attendance_record_returns_404(self, mock_att, mock_emp):
        """Employee exists but no attendance record for that date returns 404."""
        mock_emp.find_one.return_value = {"emp_code": "EMP0001"}
        mock_att.find_one.return_value = None
        payload = {
            "reason": "Valid reason here",
            "regularized_by": "hr_manager",
            "status": "PRESENT",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 404)
        self.assertIn("Attendance record not found", body["detail"])

    def test_invalid_date_path_returns_422(self):
        """Invalid date paths (non-date, impossible date) return 422."""
        invalid_dates = ["invalid-date", "2026-02-30", "2026-13-01", "2026-7-6"]
        payload = {
            "reason": "Valid reason here",
            "regularized_by": "hr_manager",
            "status": "PRESENT",
        }
        for d in invalid_dates:
            status_code, body = TestPhase5ASGIHelper.request(
                "PATCH", f"/attendance/EMP0001/{d}", body=payload
            )
            self.assertEqual(status_code, 422, f"Expected 422 for date {d}")

    def test_missing_required_fields_return_422(self):
        """Missing reason or regularized_by returns 422."""
        # Missing reason
        status_code, _ = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={"regularized_by": "hr_admin", "status": "PRESENT"},
        )
        self.assertEqual(status_code, 422)

        # Missing regularized_by
        status_code, _ = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={"reason": "Valid reason", "status": "PRESENT"},
        )
        self.assertEqual(status_code, 422)

    def test_reason_length_validation_422(self):
        """Reason < 5 or > 200 characters returns 422."""
        # Short reason (< 5 chars)
        status_code, _ = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={"reason": "Fix", "regularized_by": "hr_admin", "status": "PRESENT"},
        )
        self.assertEqual(status_code, 422)

        # Long reason (> 200 chars)
        status_code, _ = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={"reason": "A" * 201, "regularized_by": "hr_admin", "status": "PRESENT"},
        )
        self.assertEqual(status_code, 422)

    def test_regularized_by_length_validation_422(self):
        """Regularized_by < 1 or > 50 characters returns 422."""
        # Empty string
        status_code, _ = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={"reason": "Valid reason", "regularized_by": "", "status": "PRESENT"},
        )
        self.assertEqual(status_code, 422)

        # Long regularized_by (> 50 chars)
        status_code, _ = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={"reason": "Valid reason", "regularized_by": "B" * 51, "status": "PRESENT"},
        )
        self.assertEqual(status_code, 422)

    def test_invalid_status_returns_422(self):
        """Invalid status returns 422."""
        status_code, _ = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={"reason": "Valid reason", "regularized_by": "hr_admin", "status": "VACATION"},
        )
        self.assertEqual(status_code, 422)

    def test_invalid_timestamp_types_return_422(self):
        """String, float, bool, seconds, out of range timestamps return 422."""
        bad_values = ["1783310400000", 1783310400000.5, True, False, 1783310400, 50]
        for val in bad_values:
            status_code, _ = TestPhase5ASGIHelper.request(
                "PATCH",
                "/attendance/EMP0001/2026-07-06",
                body={"reason": "Valid reason", "regularized_by": "hr_admin", "punch_in": val},
            )
            self.assertEqual(status_code, 422, f"Expected 422 for punch_in={val}")

    def test_explicit_null_timestamps_return_422(self):
        """Explicit null timestamp returns 422 (non-nullable in schema)."""
        status_code, _ = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={"reason": "Valid reason", "regularized_by": "hr_admin", "punch_in": None},
        )
        self.assertEqual(status_code, 422)

        status_code, _ = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={"reason": "Valid reason", "regularized_by": "hr_admin", "punch_out": None},
        )
        self.assertEqual(status_code, 422)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_punch_in_resolving_to_wrong_date_returns_422(self, mock_att, mock_emp):
        """R1: Punch-in resolving to a different attendance date returns 422."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_att.find_one.return_value = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC),
            "punch_out": None,
            "history": [],
        }
        # 2026-07-07 09:30 IST resolves to 2026-07-07, not 2026-07-06
        wrong_date_ms = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)
        payload = {
            "punch_in": wrong_date_ms,
            "reason": "Accidental punch on wrong date",
            "regularized_by": "hr_admin",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 422)
        self.assertIn("does not match", body["detail"])

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_absent_or_leave_with_supplied_punches_returns_422(self, mock_att, mock_emp):
        """Supplying punch_in or punch_out when final status is ABSENT or LEAVE returns 422."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_att.find_one.return_value = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC),
            "punch_out": None,
            "history": [],
        }
        in_ms = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC).timestamp() * 1000)

        # ABSENT with punch_in
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={
                "status": "ABSENT",
                "punch_in": in_ms,
                "reason": "Setting absent with punch",
                "regularized_by": "hr_admin",
            },
        )
        self.assertEqual(status_code, 422)

        # LEAVE with punch_in
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={
                "status": "LEAVE",
                "punch_in": in_ms,
                "reason": "Setting leave with punch",
                "regularized_by": "hr_admin",
            },
        )
        self.assertEqual(status_code, 422)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_presence_status_without_punch_in_returns_422(self, mock_att, mock_emp):
        """Setting ABSENT record to PRESENT without supplying punch_in returns 422."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_att.find_one.return_value = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "ABSENT",
            "punch_in": None,
            "punch_out": None,
            "history": [],
        }
        payload = {
            "status": "PRESENT",
            "reason": "Marking present without giving punch-in time",
            "regularized_by": "hr_admin",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 422)
        self.assertIn("A presence status requires a valid punch_in", body["detail"])

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_punch_out_before_or_equal_punch_in_returns_422(self, mock_att, mock_emp):
        """punch_out <= punch_in returns 422."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
        mock_att.find_one.return_value = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": None,
            "history": [],
        }
        # Out equal to in
        in_ms = int(in_dt.timestamp() * 1000)
        status_code, _ = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={
                "punch_out": in_ms,
                "reason": "Equal punch out time",
                "regularized_by": "hr_admin",
            },
        )
        self.assertEqual(status_code, 422)

        # Out before in
        earlier_ms = in_ms - 60000
        status_code, _ = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={
                "punch_out": earlier_ms,
                "reason": "Earlier punch out time",
                "regularized_by": "hr_admin",
            },
        )
        self.assertEqual(status_code, 422)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_punch_out_more_than_24h_returns_422(self, mock_att, mock_emp):
        """punch_out > 24 hours after punch_in returns 422."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
        mock_att.find_one.return_value = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": None,
            "history": [],
        }
        out_25h = int((in_dt + timedelta(hours=24, minutes=1)).timestamp() * 1000)
        status_code, _ = TestPhase5ASGIHelper.request(
            "PATCH",
            "/attendance/EMP0001/2026-07-06",
            body={
                "punch_out": out_25h,
                "reason": "Punch out 24+ hours later",
                "regularized_by": "hr_admin",
            },
        )
        self.assertEqual(status_code, 422)

    def test_derived_or_extra_fields_in_request_return_422(self):
        """Clients sending derived fields or extra fields receive 422."""
        forbidden_payloads = [
            {"work_hours": 8.0, "reason": "Setting work hours", "regularized_by": "hr_admin"},
            {"late_minutes": 0, "reason": "Setting late minutes", "regularized_by": "hr_admin"},
            {"overtime_minutes": 0, "reason": "Setting overtime", "regularized_by": "hr_admin"},
            {"half_day": True, "reason": "Setting half day", "regularized_by": "hr_admin"},
            {"extra_field": "test", "reason": "Setting extra field", "regularized_by": "hr_admin"},
        ]
        for p in forbidden_payloads:
            status_code, _ = TestPhase5ASGIHelper.request(
                "PATCH", "/attendance/EMP0001/2026-07-06", body=p
            )
            self.assertEqual(status_code, 422, f"Expected 422 for payload {p}")

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_no_op_request_returns_422(self, mock_att, mock_emp):
        """A request that produces no actual changes returns 422 without appending history."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
        out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC)
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": out_dt,
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing

        # Send exact same status and exact same punch_in
        in_ms = int(in_dt.timestamp() * 1000)
        payload = {
            "status": "PRESENT",
            "punch_in": in_ms,
            "reason": "No real change made here",
            "regularized_by": "hr_admin",
        }
        status_code, body = TestPhase5ASGIHelper.request(
            "PATCH", "/attendance/EMP0001/2026-07-06", body=payload
        )
        self.assertEqual(status_code, 422)
        self.assertIn("no changes", body["detail"])
        mock_att.find_one_and_update.assert_not_called()


class TestRegularizeConcurrency(unittest.TestCase):
    """Concurrency and race condition tests."""

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_simultaneous_regularizations_one_200_one_409(self, mock_att, mock_emp):
        """Simulate two simultaneous regularizations: CAS update ensures exactly one 200 and one 409."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
        out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC)
        existing = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": in_dt,
            "punch_out": out_dt,
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        mock_att.find_one.return_value = existing

        update_calls = 0

        def fake_find_one_and_update(filter_doc, update_doc, return_document=None):
            nonlocal update_calls
            update_calls += 1
            if update_calls == 1:
                res = dict(existing)
                res.update(update_doc["$set"])
                res["history"] = [update_doc["$push"]["history"]]
                return res
            return None

        mock_att.find_one_and_update.side_effect = fake_find_one_and_update

        req1 = RegularizeRequest(
            status="WFH",
            reason="Concurrent update 1",
            regularized_by="user_a",
        )
        req2 = RegularizeRequest(
            status="ON_DUTY",
            reason="Concurrent update 2",
            regularized_by="user_b",
        )

        results = []
        errors = []

        def worker(req):
            try:
                res = regularize_attendance("EMP0001", "2026-07-06", req)
                results.append(res)
            except HTTPException as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(worker, req1)
            f2 = executor.submit(worker, req2)
            f1.result()
            f2.result()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].status_code, 409)
        self.assertIn("Concurrent modification", errors[0].detail)

    def test_live_mongodb_integration_concurrency(self):
        """Real MongoDB integration test for atomicity and concurrency when MongoDB is live."""
        import os
        from pymongo import MongoClient

        mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
        client = None
        try:
            client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=500)
            client.admin.command("ping")
        except Exception:
            if client is not None:
                client.close()
            self.skipTest(f"MongoDB not available at {mongo_uri}; skipping live DB concurrency integration test.")

        try:
            db = client["test_phase5_integration"]
            emp_col = db["employees"]
            att_col = db["attendance_logs"]
            emp_col.drop()
            att_col.drop()

            emp_col.insert_one({
                "emp_code": "EMP0099",
                "name": "Integration Test User",
                "email": "test@hrone.local",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2025-01-01",
                "created_at": datetime.now(UTC),
            })

            in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
            out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC)
            att_col.insert_one({
                "emp_code": "EMP0099",
                "date": "2026-07-06",
                "status": "PRESENT",
                "punch_in": in_dt,
                "punch_out": out_dt,
                "work_hours": 9.0,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            })

            with patch("app.main.employees_col", emp_col), patch("app.main.attendance_logs_col", att_col):
                req1 = RegularizeRequest(status="WFH", reason="Integration update 1", regularized_by="user1")
                req2 = RegularizeRequest(status="ON_DUTY", reason="Integration update 2", regularized_by="user2")

                results = []
                errors = []

                def worker(req):
                    try:
                        res = regularize_attendance("EMP0099", "2026-07-06", req)
                        results.append(res)
                    except HTTPException as e:
                        errors.append(e)

                with ThreadPoolExecutor(max_workers=2) as executor:
                    f1 = executor.submit(worker, req1)
                    f2 = executor.submit(worker, req2)
                    f1.result()
                    f2.result()

                # Verify exactly one 200 and one 409
                self.assertEqual(len(results), 1)
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0].status_code, 409)

                # Verify document in MongoDB has exactly 1 history entry
                final_doc = att_col.find_one({"emp_code": "EMP0099", "date": "2026-07-06"})
                self.assertIsNotNone(final_doc)
                self.assertEqual(len(final_doc["history"]), 1)

            emp_col.drop()
            att_col.drop()
        finally:
            if client is not None:
                client.close()


if __name__ == "__main__":
    unittest.main()
