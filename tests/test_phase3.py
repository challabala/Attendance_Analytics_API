"""
Phase 3 Tests: MongoDB Indexes, Employee Endpoints, Attendance Listing, and Punch-In.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from app.main import (
    UTC,
    EmployeeCreate,
    PunchInRequest,
    app,
    create_employee,
    init_indexes,
    list_attendance,
    list_employees,
    punch_in,
    serialize_attendance_doc,
    serialize_employee_doc,
)


class TestPhase3ASGIHelper:
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


class TestIndexes(unittest.TestCase):
    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_init_indexes_creates_required_indexes(self, mock_att, mock_emp):
        """Verify that index initialization creates the exact expected indexes."""
        init_indexes()

        # Check employees indexes
        emp_calls = mock_emp.create_index.call_args_list
        emp_index_names = [call.kwargs.get("name") for call in emp_calls]
        self.assertIn("uniq_emp_code", emp_index_names)
        self.assertIn("idx_department_emp_code", emp_index_names)

        # Check unique constraint on emp_code
        uniq_call = next(call for call in emp_calls if call.kwargs.get("name") == "uniq_emp_code")
        self.assertTrue(uniq_call.kwargs.get("unique"))

        # Check attendance indexes
        att_calls = mock_att.create_index.call_args_list
        att_index_names = [call.kwargs.get("name") for call in att_calls]
        self.assertIn("uniq_emp_code_date", att_index_names)
        self.assertIn("idx_date_desc_emp_code_asc", att_index_names)
        self.assertIn("idx_status_date", att_index_names)

        # Check unique compound constraint on (emp_code, date)
        att_uniq_call = next(call for call in att_calls if call.kwargs.get("name") == "uniq_emp_code_date")
        self.assertTrue(att_uniq_call.kwargs.get("unique"))


class TestEmployeeEndpoints(unittest.TestCase):
    @patch("app.main.employees_col")
    def test_create_employee_success_201(self, mock_emp):
        """Valid employee creation returns 201 with serialized employee (no _id)."""
        mock_emp.insert_one.return_value = MagicMock(inserted_id="fake_object_id")

        body = {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        }
        status_code, data = TestPhase3ASGIHelper.request("POST", "/employees", body=body)

        self.assertEqual(status_code, 201)
        self.assertEqual(data["emp_code"], "EMP0001")
        self.assertEqual(data["name"], "Asha Rao")
        self.assertNotIn("_id", data)
        self.assertNotIn("id", data)
        self.assertIsInstance(data["created_at"], int)

    @patch("app.main.employees_col")
    def test_create_employee_duplicate_409(self, mock_emp):
        """Duplicate emp_code triggers DuplicateKeyError and returns 409."""
        mock_emp.insert_one.side_effect = DuplicateKeyError("E11000 duplicate key error")

        body = {
            "emp_code": "EMP0001",
            "name": "Duplicate Employee",
            "email": "dup@example.com",
            "department": "HR",
            "joined_on": "2026-01-05",
        }
        status_code, data = TestPhase3ASGIHelper.request("POST", "/employees", body=body)

        self.assertEqual(status_code, 409)
        self.assertIn("detail", data)

    def test_create_employee_invalid_inputs_422(self):
        """Invalid emp_code, email, shift time, or equal shift times return 422."""
        # Bad emp_code pattern
        bad_code = {
            "emp_code": "INVALID_CODE",
            "name": "Bad",
            "email": "bad@example.com",
            "department": "HR",
            "joined_on": "2026-01-05",
        }
        status_code, _ = TestPhase3ASGIHelper.request("POST", "/employees", body=bad_code)
        self.assertEqual(status_code, 422)

        # Bad email pattern
        bad_email = {
            "emp_code": "EMP0002",
            "name": "Bad Email",
            "email": "not-an-email",
            "department": "HR",
            "joined_on": "2026-01-05",
        }
        status_code, _ = TestPhase3ASGIHelper.request("POST", "/employees", body=bad_email)
        self.assertEqual(status_code, 422)

        # Equal shift start and end
        equal_shift = {
            "emp_code": "EMP0003",
            "name": "Equal Shift",
            "email": "equal@example.com",
            "department": "HR",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-05",
        }
        status_code, _ = TestPhase3ASGIHelper.request("POST", "/employees", body=equal_shift)
        self.assertEqual(status_code, 422)

        # Client attempting to inject created_at or _id (forbidden extra)
        extra_field = {
            "emp_code": "EMP0004",
            "name": "Extra",
            "email": "extra@example.com",
            "department": "HR",
            "joined_on": "2026-01-05",
            "created_at": 1783329000000,
        }
        status_code, _ = TestPhase3ASGIHelper.request("POST", "/employees", body=extra_field)
        self.assertEqual(status_code, 422)

    @patch("app.main.employees_col")
    def test_list_employees_pagination_and_sorting(self, mock_emp):
        """Verify sorting by emp_code, pagination calculation, and department filter."""
        sample_employees = [
            {"emp_code": "EMP0001", "name": "A", "email": "a@x.com", "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05", "created_at": datetime(2026, 1, 5, tzinfo=UTC)},
            {"emp_code": "EMP0002", "name": "B", "email": "b@x.com", "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05", "created_at": datetime(2026, 1, 5, tzinfo=UTC)},
        ]
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.skip.return_value = mock_cursor
        mock_cursor.limit.return_value = sample_employees
        mock_emp.find.return_value = mock_cursor
        mock_emp.count_documents.return_value = 2

        status_code, data = TestPhase3ASGIHelper.request("GET", "/employees", query="department=Eng&page=1&page_size=20")

        self.assertEqual(status_code, 200)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 20)
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["items"][0]["emp_code"], "EMP0001")
        self.assertNotIn("_id", data["items"][0])

        # Verify cursor methods were called properly
        mock_emp.find.assert_called_with({"department": "Eng"})
        mock_cursor.sort.assert_called_with("emp_code", 1)
        mock_cursor.skip.assert_called_with(0)
        mock_cursor.limit.assert_called_with(20)

    def test_list_employees_invalid_pagination_422(self):
        """Invalid page (< 1) or page_size (> 100) returns 422."""
        status_code, _ = TestPhase3ASGIHelper.request("GET", "/employees", query="page=0")
        self.assertEqual(status_code, 422)

        status_code, _ = TestPhase3ASGIHelper.request("GET", "/employees", query="page_size=101")
        self.assertEqual(status_code, 422)


class TestPunchInEndpoint(unittest.TestCase):
    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_punch_in_success_201(self, mock_att, mock_emp):
        """Valid punch-in calculates date and late_minutes, and returns 201."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_att.insert_one.return_value = MagicMock(inserted_id="fake_id")

        # 2026-07-06 04:35:00 UTC = 2026-07-06 10:05:00 IST (35 min late)
        body = {
            "emp_code": "EMP0001",
            "punched_at": 1783312500000,
            "status": "PRESENT",
        }
        status_code, data = TestPhase3ASGIHelper.request("POST", "/attendance/punch-in", body=body)

        self.assertEqual(status_code, 201)
        self.assertEqual(data["emp_code"], "EMP0001")
        self.assertEqual(data["date"], "2026-07-06")
        self.assertEqual(data["status"], "PRESENT")
        self.assertEqual(data["late_minutes"], 35)
        self.assertEqual(data["overtime_minutes"], 0)
        self.assertFalse(data["half_day"])
        self.assertIsNone(data["punch_out"])
        self.assertIsNone(data["work_hours"])
        self.assertEqual(data["history"], [])
        self.assertNotIn("_id", data)
        self.assertNotIn("id", data)

    @patch("app.main.employees_col")
    def test_punch_in_unknown_employee_404(self, mock_emp):
        """Unknown employee returns 404, not 500."""
        mock_emp.find_one.return_value = None

        body = {"emp_code": "EMP9999"}
        status_code, data = TestPhase3ASGIHelper.request("POST", "/attendance/punch-in", body=body)

        self.assertEqual(status_code, 404)
        self.assertEqual(data["detail"], "Employee not found")

    def test_punch_in_invalid_timestamp_types_422(self):
        """String, float, boolean, or seconds timestamps return 422."""
        for bad_ts in ["1783312500000", 1783312500000.5, True, 1783312500]:
            body = {"emp_code": "EMP0001", "punched_at": bad_ts}
            status_code, _ = TestPhase3ASGIHelper.request("POST", "/attendance/punch-in", body=body)
            self.assertEqual(status_code, 422)

    def test_punch_in_invalid_status_422(self):
        """Statuses other than PRESENT, WFH, ON_DUTY return 422."""
        for bad_status in ["ABSENT", "LEAVE", "OTHER"]:
            body = {"emp_code": "EMP0001", "status": bad_status}
            status_code, _ = TestPhase3ASGIHelper.request("POST", "/attendance/punch-in", body=body)
            self.assertEqual(status_code, 422)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_punch_in_duplicate_race_409(self, mock_att, mock_emp):
        """DuplicateKeyError on (emp_code, date) yields 409."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_att.insert_one.side_effect = DuplicateKeyError("E11000 duplicate key error")

        body = {"emp_code": "EMP0001"}
        status_code, data = TestPhase3ASGIHelper.request("POST", "/attendance/punch-in", body=body)

        self.assertEqual(status_code, 409)
        self.assertEqual(data["detail"], "already punched in for this date")

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_punch_in_concurrent_race_handling(self, mock_att, mock_emp):
        """Simulate concurrent duplicate punch-ins: exactly one gets 201 and the other gets 409."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }

        # First insert succeeds; second insert raises DuplicateKeyError
        call_count = 0
        def fake_insert(doc):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise DuplicateKeyError("E11000 duplicate key error")
            return MagicMock(inserted_id="fake_id")

        mock_att.insert_one.side_effect = fake_insert

        req = PunchInRequest(emp_code="EMP0001", punched_at=1783312500000, status="PRESENT")

        results = []
        errors = []

        def worker():
            try:
                res = punch_in(req)
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


class TestAttendanceListing(unittest.TestCase):
    @patch("app.main.attendance_logs_col")
    def test_list_attendance_filters_and_pagination(self, mock_att):
        """Verify date ranges, status filters, database sort and pagination."""
        sample_doc = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 3, 58, tzinfo=UTC),
            "punch_out": datetime(2026, 7, 6, 13, 5, tzinfo=UTC),
            "work_hours": 9.12,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }

        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.skip.return_value = mock_cursor
        mock_cursor.limit.return_value = [sample_doc]
        mock_att.find.return_value = mock_cursor
        mock_att.count_documents.return_value = 1

        query = "emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-10&status=PRESENT&page=1&page_size=20"
        status_code, data = TestPhase3ASGIHelper.request("GET", "/attendance", query=query)

        self.assertEqual(status_code, 200)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 20)
        self.assertEqual(len(data["items"]), 1)

        item = data["items"][0]
        self.assertEqual(item["emp_code"], "EMP0001")
        self.assertIsInstance(item["punch_in"], int)
        self.assertIsInstance(item["punch_out"], int)
        self.assertNotIn("_id", item)
        self.assertNotIn("id", item)

        # Verify query passed to MongoDB
        mock_att.find.assert_called_with({
            "emp_code": "EMP0001",
            "status": "PRESENT",
            "date": {"$gte": "2026-07-01", "$lte": "2026-07-10"},
        })
        mock_cursor.sort.assert_called_with([("date", -1), ("emp_code", 1)])
        mock_cursor.skip.assert_called_with(0)
        mock_cursor.limit.assert_called_with(20)

    def test_list_attendance_date_from_after_date_to_422(self):
        """date_from > date_to returns 422."""
        status_code, data = TestPhase3ASGIHelper.request("GET", "/attendance", query="date_from=2026-07-10&date_to=2026-07-01")
        self.assertEqual(status_code, 422)

    def test_list_attendance_invalid_status_422(self):
        """Invalid status parameter returns 422."""
        status_code, _ = TestPhase3ASGIHelper.request("GET", "/attendance", query="status=FOOBAR")
        self.assertEqual(status_code, 422)


if __name__ == "__main__":
    unittest.main()
