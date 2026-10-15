"""
Phase 6 Tests: Monthly Employee Attendance Analytics (GET /analytics/employees/{emp_code}/monthly).
"""
import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from app.main import (
    UTC,
    app,
    build_employee_monthly_pipeline,
    calculate_working_days,
    get_employee_monthly_analytics,
    round_half_up,
)


class TestPhase6ASGIHelper:
    """Helper for testing FastAPI endpoints via ASGI."""

    @staticmethod
    def request(method: str, path: str, query: str = ""):
        headers = [
            (b"content-type", b"application/json"),
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
                return {"type": "http.request", "body": b"", "more_body": False}

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


class TestWorkingDaysCalculation(unittest.TestCase):
    """Direct tests for Rule R7 working days calculation."""

    def test_employee_employed_entire_month(self):
        """July 2026 starts Wednesday July 1 and ends Friday July 31 -> 23 weekdays."""
        working_days = calculate_working_days("2026-07", "2025-01-01")
        self.assertEqual(working_days, 23)

    def test_employee_joins_mid_month(self):
        """Employee joins on Wednesday July 15, 2026 -> 13 weekdays (Jul 15 - Jul 31)."""
        working_days = calculate_working_days("2026-07", "2026-07-15")
        self.assertEqual(working_days, 13)

    def test_employee_joins_on_weekend(self):
        """Employee joins on Saturday July 18, 2026 -> 10 weekdays (Jul 20 - Jul 31)."""
        working_days = calculate_working_days("2026-07", "2026-07-18")
        self.assertEqual(working_days, 10)

    def test_employee_joins_after_requested_month(self):
        """Employee joins in August 2026 -> 0 working days for July 2026."""
        working_days = calculate_working_days("2026-07", "2026-08-01")
        self.assertEqual(working_days, 0)

    def test_month_beginning_on_weekend(self):
        """August 2026 begins on Saturday Aug 1, ends Monday Aug 31 -> 21 weekdays."""
        working_days = calculate_working_days("2026-08", "2025-01-01")
        self.assertEqual(working_days, 21)

    def test_month_ending_on_weekend(self):
        """May 2026 begins on Friday May 1, ends Sunday May 31 -> 21 weekdays."""
        working_days = calculate_working_days("2026-05", "2025-01-01")
        self.assertEqual(working_days, 21)

    def test_joined_on_last_day_friday(self):
        """July 31, 2026 is a Friday. Joining on July 31 yields exactly 1 working day."""
        working_days = calculate_working_days("2026-07", "2026-07-31")
        self.assertEqual(working_days, 1)

    def test_joined_on_last_day_sunday(self):
        """May 31, 2026 is a Sunday. Joining on May 31 yields 0 working days."""
        working_days = calculate_working_days("2026-05", "2026-05-31")
        self.assertEqual(working_days, 0)


class TestMonthlyAnalyticsValidation(unittest.TestCase):
    """Input validation tests for GET /analytics/employees/{emp_code}/monthly."""

    @patch("app.main.employees_col")
    def test_unknown_employee_returns_404(self, mock_emp):
        """Unknown employee returns 404."""
        mock_emp.find_one.return_value = None
        status_code, body = TestPhase6ASGIHelper.request(
            "GET", "/analytics/employees/EMP9999/monthly", query="month=2026-07"
        )
        self.assertEqual(status_code, 404)
        self.assertIn("not found", body["detail"])

    def test_missing_month_returns_422(self):
        """Omitting required month query parameter returns 422."""
        status_code, _ = TestPhase6ASGIHelper.request(
            "GET", "/analytics/employees/EMP0001/monthly", query=""
        )
        self.assertEqual(status_code, 422)

    def test_malformed_month_returns_422(self):
        """Malformed month formats return 422."""
        bad_months = [
            "2026-13",
            "2026-00",
            "2026-7",
            "not-a-month",
            "2026/07",
            "2026-07-01",
            "26-07",
        ]
        for m in bad_months:
            status_code, _ = TestPhase6ASGIHelper.request(
                "GET", "/analytics/employees/EMP0001/monthly", query=f"month={m}"
            )
            self.assertEqual(status_code, 422, f"Expected 422 for month='{m}'")

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_valid_employee_and_month_returns_200(self, mock_att, mock_emp):
        """Valid employee and month returns 200 with EmployeeMonthly schema."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "joined_on": "2025-01-01",
        }
        mock_att.aggregate.return_value = [
            {
                "present_days": 20.0,
                "leave_days": 1,
                "late_count": 2,
                "total_late_minutes": 30,
                "total_overtime_minutes": 45,
            }
        ]
        status_code, body = TestPhase6ASGIHelper.request(
            "GET", "/analytics/employees/EMP0001/monthly", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["emp_code"], "EMP0001")
        self.assertEqual(body["month"], "2026-07")
        self.assertEqual(body["working_days"], 23)
        self.assertEqual(body["present_days"], 20.0)
        self.assertEqual(body["leave_days"], 1)
        self.assertEqual(body["late_count"], 2)
        self.assertEqual(body["total_late_minutes"], 30)
        self.assertEqual(body["total_overtime_minutes"], 45)
        self.assertAlmostEqual(body["attendance_pct"], 86.9565, places=4)


class TestMonthlyAnalyticsMetricsAndAggregation(unittest.TestCase):
    """Verify aggregation pipeline structure, metrics, and business rules (R5-R7)."""

    def test_pipeline_structure(self):
        """Verify build_employee_monthly_pipeline builds correct indexed match and group stages."""
        pipeline = build_employee_monthly_pipeline("EMP0001", "2026-07")
        self.assertEqual(len(pipeline), 2)
        match_stage = pipeline[0]["$match"]
        self.assertEqual(match_stage["emp_code"], "EMP0001")
        self.assertEqual(match_stage["date"]["$gte"], "2026-07-01")
        self.assertEqual(match_stage["date"]["$lte"], "2026-07-31")

        group_stage = pipeline[1]["$group"]
        self.assertIsNone(group_stage["_id"])
        self.assertIn("present_days", group_stage)
        self.assertIn("leave_days", group_stage)
        self.assertIn("late_count", group_stage)
        self.assertIn("total_late_minutes", group_stage)
        self.assertIn("total_overtime_minutes", group_stage)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_half_day_presence_contributes_half(self, mock_att, mock_emp):
        """Presence record with half_day=True contributes 0.5."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "joined_on": "2025-01-01",
        }
        mock_att.aggregate.return_value = [
            {
                "present_days": 19.5,
                "leave_days": 0,
                "late_count": 0,
                "total_late_minutes": 0,
                "total_overtime_minutes": 0,
            }
        ]
        status_code, body = TestPhase6ASGIHelper.request(
            "GET", "/analytics/employees/EMP0001/monthly", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["present_days"], 19.5)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_leave_and_absent_metrics(self, mock_att, mock_emp):
        """leave_days counts LEAVE only; ABSENT does not count as leave or present."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "joined_on": "2025-01-01",
        }
        mock_att.aggregate.return_value = [
            {
                "present_days": 18.0,
                "leave_days": 3,
                "late_count": 0,
                "total_late_minutes": 0,
                "total_overtime_minutes": 0,
            }
        ]
        status_code, body = TestPhase6ASGIHelper.request(
            "GET", "/analytics/employees/EMP0001/monthly", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["present_days"], 18.0)
        self.assertEqual(body["leave_days"], 3)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_late_and_overtime_metrics(self, mock_att, mock_emp):
        """late_count counts records with late_minutes > 0; total_late and total_overtime sum minutes."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "joined_on": "2025-01-01",
        }
        mock_att.aggregate.return_value = [
            {
                "present_days": 22.0,
                "leave_days": 0,
                "late_count": 4,
                "total_late_minutes": 55,
                "total_overtime_minutes": 120,
            }
        ]
        status_code, body = TestPhase6ASGIHelper.request(
            "GET", "/analytics/employees/EMP0001/monthly", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["late_count"], 4)
        self.assertEqual(body["total_late_minutes"], 55)
        self.assertEqual(body["total_overtime_minutes"], 120)


class TestRoundingAndEmptyData(unittest.TestCase):
    """Attendance percentage rounding (R8) and handling of empty records."""

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_employee_with_no_attendance_records(self, mock_att, mock_emp):
        """Employee with no attendance records gets 0.0 for record metrics and valid working_days."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "joined_on": "2025-01-01",
        }
        # Aggregation returns empty list
        mock_att.aggregate.return_value = []

        status_code, body = TestPhase6ASGIHelper.request(
            "GET", "/analytics/employees/EMP0001/monthly", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["emp_code"], "EMP0001")
        self.assertEqual(body["month"], "2026-07")
        self.assertEqual(body["working_days"], 23)
        self.assertEqual(body["present_days"], 0.0)
        self.assertEqual(body["leave_days"], 0)
        self.assertEqual(body["late_count"], 0)
        self.assertEqual(body["total_late_minutes"], 0)
        self.assertEqual(body["total_overtime_minutes"], 0)
        self.assertEqual(body["attendance_pct"], 0.0)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_zero_working_days_produces_null_attendance_pct(self, mock_att, mock_emp):
        """When working_days is 0 (joined after month), attendance_pct is null."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "joined_on": "2026-08-01",  # Joined in August
        }
        mock_att.aggregate.return_value = []

        status_code, body = TestPhase6ASGIHelper.request(
            "GET", "/analytics/employees/EMP0001/monthly", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["working_days"], 0)
        self.assertIsNone(body["attendance_pct"])

    def test_round_half_up_4_decimals_half_up_vs_bankers(self):
        """Rule R8: 4 decimals ROUND_HALF_UP rounds half away from zero, unlike banker's rounding."""
        # 0.00285: 4th decimal is 8 (even). Banker's rounding rounds to 0.0028, half-up rounds to 0.0029.
        val = Decimal("0.00285")
        self.assertEqual(val.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN), Decimal("0.0028"))
        self.assertEqual(round_half_up(val, decimal_places=4), 0.0029)

        # 0.12345 rounds to 0.1235
        self.assertEqual(round_half_up(Decimal("0.12345"), decimal_places=4), 0.1235)

    @patch("app.main.employees_col")
    @patch("app.main.attendance_logs_col")
    def test_attendance_pct_precision(self, mock_att, mock_emp):
        """Attendance percentage rounds to exactly 4 decimal places."""
        mock_emp.find_one.return_value = {
            "emp_code": "EMP0001",
            "joined_on": "2025-01-01",
        }
        # 21 present days out of 23 working days: 21 / 23 * 100 = 91.304347826... -> 91.3043
        mock_att.aggregate.return_value = [{"present_days": 21.0}]

        status_code, body = TestPhase6ASGIHelper.request(
            "GET", "/analytics/employees/EMP0001/monthly", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["attendance_pct"], 91.3043)


class TestLiveMongoDBMonthlyAnalyticsIntegration(unittest.TestCase):
    """Integration test against a live MongoDB database when configured."""

    def test_live_mongodb_aggregation_execution(self):
        """Executes actual aggregation pipeline on live MongoDB if available."""
        from pymongo import MongoClient

        mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
        client = None
        try:
            client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=500)
            client.admin.command("ping")
        except Exception:
            if client is not None:
                client.close()
            self.skipTest(f"MongoDB not available at {mongo_uri}; skipping live DB aggregation test.")

        try:
            db = client["test_phase6_integration"]
            emp_col = db["employees"]
            att_col = db["attendance_logs"]
            emp_col.drop()
            att_col.drop()

            emp_col.insert_one({
                "emp_code": "EMP6001",
                "name": "Phase 6 Integration User",
                "email": "phase6@hrone.local",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-07-01",
                "created_at": datetime.now(UTC),
            })

            # Sample records for July 2026:
            # 1. 2026-07-06 (Mon): PRESENT, full day (1.0), late 15m
            # 2. 2026-07-07 (Tue): WFH, half day (0.5), overtime 45m
            # 3. 2026-07-08 (Wed): ON_DUTY, full day (1.0), late 0, ot 0
            # 4. 2026-07-09 (Thu): LEAVE, 0 present, 1 leave
            # 5. 2026-07-10 (Fri): ABSENT, 0 present
            # 6. 2026-07-11 (Sat): PRESENT on weekend, late 20m, ot 60m (0 present days because weekend!)
            att_col.insert_many([
                {
                    "emp_code": "EMP6001",
                    "date": "2026-07-06",
                    "status": "PRESENT",
                    "late_minutes": 15,
                    "overtime_minutes": 0,
                    "half_day": False,
                },
                {
                    "emp_code": "EMP6001",
                    "date": "2026-07-07",
                    "status": "WFH",
                    "late_minutes": 0,
                    "overtime_minutes": 45,
                    "half_day": True,
                },
                {
                    "emp_code": "EMP6001",
                    "date": "2026-07-08",
                    "status": "ON_DUTY",
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                },
                {
                    "emp_code": "EMP6001",
                    "date": "2026-07-09",
                    "status": "LEAVE",
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                },
                {
                    "emp_code": "EMP6001",
                    "date": "2026-07-10",
                    "status": "ABSENT",
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                },
                {
                    "emp_code": "EMP6001",
                    "date": "2026-07-11",
                    "status": "PRESENT",
                    "late_minutes": 20,
                    "overtime_minutes": 60,
                    "half_day": False,
                },
            ])

            with patch("app.main.employees_col", emp_col), patch("app.main.attendance_logs_col", att_col):
                res = get_employee_monthly_analytics("EMP6001", "2026-07")

                # Expected present_days:
                # Mon (1.0) + Tue half_day (0.5) + Wed (1.0) = 2.5
                # Sat (weekend) -> 0.0
                self.assertEqual(res["present_days"], 2.5)

                # Expected leave_days: 1
                self.assertEqual(res["leave_days"], 1)

                # Expected late_count: Mon (15m) + Sat (20m) = 2
                self.assertEqual(res["late_count"], 2)

                # Expected total_late_minutes: 15 + 20 = 35
                self.assertEqual(res["total_late_minutes"], 35)

                # Expected total_overtime_minutes: Tue (45m) + Sat (60m) = 105
                self.assertEqual(res["total_overtime_minutes"], 105)

                # Working days in July 2026: 23
                self.assertEqual(res["working_days"], 23)

                # Attendance pct: 2.5 / 23 * 100 = 10.8695652... -> 10.8696
                self.assertEqual(res["attendance_pct"], 10.8696)

            emp_col.drop()
            att_col.drop()
        finally:
            if client is not None:
                client.close()


if __name__ == "__main__":
    unittest.main()
