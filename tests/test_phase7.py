"""
Phase 7 Tests: Department Monthly Summary Analytics (GET /analytics/departments/summary).
"""
import asyncio
import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from app.main import (
    UTC,
    app,
    build_department_summary_pipeline,
    round_half_up,
)


class TestPhase7ASGIHelper:
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


class TestDepartmentSummaryValidation(unittest.TestCase):
    """Input validation tests for GET /analytics/departments/summary."""

    def test_missing_month_returns_422(self):
        """Missing month parameter returns 422."""
        status_code, _ = TestPhase7ASGIHelper.request("GET", "/analytics/departments/summary")
        self.assertEqual(status_code, 422)

    def test_malformed_month_returns_422(self):
        """Malformed month format returns 422."""
        bad_months = [
            "2026-7",
            "not-a-month",
            "2026/07",
            "2026-07-01",
            "26-07",
            "2026-July",
            "July-2026",
            "2026-7-01",
        ]
        for m in bad_months:
            status_code, _ = TestPhase7ASGIHelper.request(
                "GET", "/analytics/departments/summary", query=f"month={m}"
            )
            self.assertEqual(status_code, 422, f"Expected 422 for malformed month='{m}'")

    def test_impossible_month_returns_422(self):
        """Impossible month numbers return 422."""
        bad_months = ["2026-00", "2026-13", "2026-99"]
        for m in bad_months:
            status_code, _ = TestPhase7ASGIHelper.request(
                "GET", "/analytics/departments/summary", query=f"month={m}"
            )
            self.assertEqual(status_code, 422, f"Expected 422 for impossible month='{m}'")

    @patch("app.main.employees_col")
    def test_valid_month_no_department(self, mock_emp):
        """Valid month with no department filter returns 200."""
        mock_emp.aggregate.return_value = [
            {
                "department": "Engineering",
                "headcount": 10,
                "present_days": 180.5,
                "raw_work_hours_sum": 1500.0,
                "raw_work_hours_count": 180,
                "late_count": 5,
                "total_late_minutes": 75,
                "leave_count": 8,
                "on_duty_count": 3,
            }
        ]
        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["month"], "2026-07")
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["department"], "Engineering")
        self.assertEqual(body["items"][0]["headcount"], 10)
        self.assertEqual(body["items"][0]["present_days"], 180.5)

    @patch("app.main.employees_col")
    def test_valid_month_with_exact_department(self, mock_emp):
        """Valid month with exact department filter passes department to pipeline."""
        mock_emp.aggregate.return_value = [
            {
                "department": "Engineering",
                "headcount": 10,
                "present_days": 180.5,
                "raw_work_hours_sum": 1500.0,
                "raw_work_hours_count": 180,
                "late_count": 5,
                "total_late_minutes": 75,
                "leave_count": 8,
                "on_duty_count": 3,
            }
        ]
        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07&department=Engineering"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(len(body["items"]), 1)
        # Verify pipeline filter in mock call
        call_pipeline = mock_emp.aggregate.call_args[0][0]
        self.assertEqual(call_pipeline[0]["$match"]["department"], "Engineering")

    @patch("app.main.employees_col")
    def test_department_matching_is_case_sensitive(self, mock_emp):
        """Department matching preserves exact case sensitivity without normalization."""
        mock_emp.aggregate.return_value = []
        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07&department=engineering"
        )
        self.assertEqual(status_code, 200)
        call_pipeline = mock_emp.aggregate.call_args[0][0]
        # Must pass "engineering", not capitalized or lowered
        self.assertEqual(call_pipeline[0]["$match"]["department"], "engineering")

    @patch("app.main.employees_col")
    def test_valid_department_filter_no_match_returns_empty_items(self, mock_emp):
        """If valid department filter matches no qualifying department, return 200 with items: []."""
        mock_emp.aggregate.return_value = []
        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07&department=NonExistentDept"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["month"], "2026-07")
        self.assertEqual(body["items"], [])


class TestDepartmentSummaryPipelineStructure(unittest.TestCase):
    """Tests checking pipeline structure generated by build_department_summary_pipeline."""

    def test_pipeline_stages_and_dates_july_2026(self):
        """July 2026 pipeline has correct start date (2026-07-01) and end date (2026-07-31)."""
        pipeline = build_department_summary_pipeline("2026-07")
        self.assertGreaterEqual(len(pipeline), 6)

        # Stage 0: $match on employees
        match_stage = pipeline[0]["$match"]
        self.assertEqual(match_stage["joined_on"]["$lte"], "2026-07-31")
        self.assertNotIn("department", match_stage)

        # Stage 1: $lookup on attendance_logs
        lookup_stage = pipeline[1]["$lookup"]
        self.assertEqual(lookup_stage["from"], "attendance_logs")
        lookup_expr = lookup_stage["pipeline"][0]["$match"]["$expr"]["$and"]
        # Check emp_code match, date >= 2026-07-01, date <= 2026-07-31
        expr_str = json.dumps(lookup_expr)
        self.assertIn("2026-07-01", expr_str)
        self.assertIn("2026-07-31", expr_str)

        # Stage 2: $unwind with preserveNullAndEmptyArrays
        unwind_stage = pipeline[2]["$unwind"]
        self.assertEqual(unwind_stage["path"], "$logs")
        self.assertTrue(unwind_stage["preserveNullAndEmptyArrays"])

        # Stage 3: $group
        group_stage = pipeline[3]["$group"]
        self.assertEqual(group_stage["_id"], "$department")
        self.assertEqual(group_stage["emp_codes"], {"$addToSet": "$emp_code"})

        # Stage 4: $project
        project_stage = pipeline[4]["$project"]
        self.assertEqual(project_stage["headcount"], {"$size": "$emp_codes"})

        # Stage 5: $sort
        sort_stage = pipeline[5]["$sort"]
        self.assertEqual(sort_stage["department"], 1)

    def test_pipeline_leap_year_february_2024(self):
        """February in leap year (2024) ends on 2024-02-29."""
        pipeline = build_department_summary_pipeline("2024-02")
        self.assertEqual(pipeline[0]["$match"]["joined_on"]["$lte"], "2024-02-29")
        lookup_expr = pipeline[1]["$lookup"]["pipeline"][0]["$match"]["$expr"]["$and"]
        expr_str = json.dumps(lookup_expr)
        self.assertIn("2024-02-29", expr_str)

    def test_pipeline_department_filter_included(self):
        """Department filter is included in first match stage when provided."""
        pipeline = build_department_summary_pipeline("2026-07", department="Product")
        self.assertEqual(pipeline[0]["$match"]["department"], "Product")
        self.assertEqual(pipeline[0]["$match"]["joined_on"]["$lte"], "2026-07-31")


class TestDepartmentSummaryHeadcountRules(unittest.TestCase):
    """Tests for Headcount calculation (Rule R9)."""

    @patch("app.main.employees_col")
    def test_headcount_employee_with_logs_counted_once(self, mock_emp):
        """An employee with 20 attendance records contributes only 1 to headcount."""
        mock_emp.aggregate.return_value = [
            {
                "department": "Engineering",
                "headcount": 1,
                "present_days": 20.0,
                "raw_work_hours_sum": 160.0,
                "raw_work_hours_count": 20,
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            }
        ]
        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["items"][0]["headcount"], 1)

    @patch("app.main.employees_col")
    def test_headcount_employee_with_no_attendance_records_counted(self, mock_emp):
        """An employee with zero attendance logs still contributes 1 to headcount."""
        mock_emp.aggregate.return_value = [
            {
                "department": "HR",
                "headcount": 1,
                "present_days": 0.0,
                "raw_work_hours_sum": 0.0,
                "raw_work_hours_count": 0,
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            }
        ]
        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        item = body["items"][0]
        self.assertEqual(item["headcount"], 1)
        self.assertEqual(item["present_days"], 0.0)
        self.assertIsNone(item["avg_work_hours"])

    def test_headcount_boundary_date_filtering(self):
        """Verify pipeline strictly excludes employees joined after month ends."""
        pipeline = build_department_summary_pipeline("2026-07")
        match_criteria = pipeline[0]["$match"]["joined_on"]
        self.assertIn("$lte", match_criteria)
        self.assertEqual(match_criteria["$lte"], "2026-07-31")

    def test_headcount_mid_month_and_final_day_joiners(self):
        """Pipeline match stage includes mid-month and final day joiners (joined_on <= last_day)."""
        pipeline = build_department_summary_pipeline("2026-07")
        lte_date = pipeline[0]["$match"]["joined_on"]["$lte"]
        # Joiner on 2026-07-15 is <= 2026-07-31
        self.assertTrue("2026-07-15" <= lte_date)
        # Joiner on 2026-07-31 is <= 2026-07-31
        self.assertTrue("2026-07-31" <= lte_date)
        # Joiner on 2026-08-01 is NOT <= 2026-07-31
        self.assertFalse("2026-08-01" <= lte_date)


class TestDepartmentSummaryPresentDaysCalculations(unittest.TestCase):
    """Tests for Present-Day calculation rules (R5, R6, R7)."""

    @patch("app.main.employees_col")
    def test_present_days_full_and_half_day_increments(self, mock_emp):
        """Full day is 1.0, half day is 0.5; sum can represent fractional values like 12.5."""
        mock_emp.aggregate.return_value = [
            {
                "department": "Design",
                "headcount": 2,
                "present_days": 12.5,
                "raw_work_hours_sum": 105.0,
                "raw_work_hours_count": 13,
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            }
        ]
        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["items"][0]["present_days"], 12.5)

    def test_present_days_pipeline_iso_day_and_half_day_logic(self):
        """Verify pipeline expression uses $isoDayOfWeek <= 5 and half_day conditional."""
        pipeline = build_department_summary_pipeline("2026-07")
        present_days_expr = pipeline[3]["$group"]["present_days"]["$sum"]
        expr_str = json.dumps(present_days_expr)
        # Must check $isoDayOfWeek <= 5
        self.assertIn("$isoDayOfWeek", expr_str)
        # Must check PRESENT, WFH, ON_DUTY
        self.assertIn("PRESENT", expr_str)
        self.assertIn("WFH", expr_str)
        self.assertIn("ON_DUTY", expr_str)
        # Must handle half_day (0.5 vs 1.0)
        self.assertIn("0.5", expr_str)
        self.assertIn("1.0", expr_str)


class TestDepartmentSummaryWorkHourAverages(unittest.TestCase):
    """Tests for avg_work_hours calculation and rounding (R4, R8)."""

    @patch("app.main.employees_col")
    def test_avg_work_hours_per_record_mean(self, mock_emp):
        """
        Verify department average is over qualifying records directly:
        Emp A: 8h, 10h -> sum 18, count 2
        Emp B: 6h      -> sum 6, count 1
        Combined: (18 + 6) / 3 = 8.00 (NOT mean of averages (9+6)/2 = 7.50).
        """
        mock_emp.aggregate.return_value = [
            {
                "department": "Engineering",
                "headcount": 2,
                "present_days": 3.0,
                "raw_work_hours_sum": 24.0,
                "raw_work_hours_count": 3,
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            }
        ]
        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["items"][0]["avg_work_hours"], 8.0)

    @patch("app.main.employees_col")
    def test_avg_work_hours_halfway_boundary_rounding(self, mock_emp):
        """
        Rule R8: 8.725 must round to 8.73 under ROUND_HALF_UP (vs 8.72 under banker's rounding).
        e.g., raw_sum = 34.9, count = 4 -> 34.9 / 4 = 8.725.
        """
        mock_emp.aggregate.return_value = [
            {
                "department": "Engineering",
                "headcount": 2,
                "present_days": 4.0,
                "raw_work_hours_sum": 34.9,
                "raw_work_hours_count": 4,
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            }
        ]
        # Banker's rounding check
        val = Decimal("34.9") / Decimal("4")
        self.assertEqual(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN), Decimal("8.72"))
        self.assertEqual(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), Decimal("8.73"))

        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["items"][0]["avg_work_hours"], 8.73)

    @patch("app.main.employees_col")
    def test_avg_work_hours_null_when_no_qualifying_records(self, mock_emp):
        """A department with no qualifying work-hour records returns avg_work_hours as null."""
        mock_emp.aggregate.return_value = [
            {
                "department": "Finance",
                "headcount": 3,
                "present_days": 0.0,
                "raw_work_hours_sum": 0.0,
                "raw_work_hours_count": 0,
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            }
        ]
        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertIsNone(body["items"][0]["avg_work_hours"])

    @patch("app.main.employees_col")
    def test_avg_work_hours_zero_log_employee_does_not_distort_average(self, mock_emp):
        """Zero-log employees are in headcount (e.g. 5) but do not inflate/distort avg_work_hours denominator."""
        mock_emp.aggregate.return_value = [
            {
                "department": "Engineering",
                "headcount": 5,
                "present_days": 2.0,
                "raw_work_hours_sum": 16.0,
                "raw_work_hours_count": 2,  # Only the 2 qualifying records, not 5
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            }
        ]
        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["items"][0]["headcount"], 5)
        self.assertEqual(body["items"][0]["avg_work_hours"], 8.0)


class TestDepartmentSummaryOtherMetrics(unittest.TestCase):
    """Tests for lateness, leave, and on-duty metrics."""

    @patch("app.main.employees_col")
    def test_lateness_leave_and_onduty_metrics(self, mock_emp):
        """
        - late_count counts records with late_minutes > 0.
        - total_late_minutes sums stored late values.
        - leave_count counts status == LEAVE.
        - on_duty_count counts status == ON_DUTY.
        """
        mock_emp.aggregate.return_value = [
            {
                "department": "Operations",
                "headcount": 5,
                "present_days": 80.0,
                "raw_work_hours_sum": 640.0,
                "raw_work_hours_count": 80,
                "late_count": 14,
                "total_late_minutes": 245,
                "leave_count": 6,
                "on_duty_count": 11,
            }
        ]
        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        item = body["items"][0]
        self.assertEqual(item["late_count"], 14)
        self.assertEqual(item["total_late_minutes"], 245)
        self.assertEqual(item["leave_count"], 6)
        self.assertEqual(item["on_duty_count"], 11)

    @patch("app.main.employees_col")
    def test_missing_derived_fields_handled_safely(self, mock_emp):
        """Missing optional fields in aggregated doc default safely without raising errors."""
        mock_emp.aggregate.return_value = [
            {
                "department": "Support",
                "headcount": 2,
            }
        ]
        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        item = body["items"][0]
        self.assertEqual(item["headcount"], 2)
        self.assertEqual(item["present_days"], 0.0)
        self.assertIsNone(item["avg_work_hours"])
        self.assertEqual(item["late_count"], 0)
        self.assertEqual(item["total_late_minutes"], 0)
        self.assertEqual(item["leave_count"], 0)
        self.assertEqual(item["on_duty_count"], 0)


class TestDepartmentSummaryResponseAndSorting(unittest.TestCase):
    """Tests checking exact response schema and alphabetical sorting."""

    @patch("app.main.employees_col")
    def test_schema_keys_and_sorting(self, mock_emp):
        """Departments must be sorted ascending by department name."""
        mock_emp.aggregate.return_value = [
            {
                "department": "Design",
                "headcount": 4,
                "present_days": 70.0,
                "raw_work_hours_sum": 560.0,
                "raw_work_hours_count": 70,
                "late_count": 2,
                "total_late_minutes": 30,
                "leave_count": 1,
                "on_duty_count": 0,
            },
            {
                "department": "Engineering",
                "headcount": 12,
                "present_days": 210.5,
                "raw_work_hours_sum": 1780.0,
                "raw_work_hours_count": 210,
                "late_count": 8,
                "total_late_minutes": 140,
                "leave_count": 4,
                "on_duty_count": 2,
            },
            {
                "department": "Marketing",
                "headcount": 3,
                "present_days": 55.0,
                "raw_work_hours_sum": 440.0,
                "raw_work_hours_count": 55,
                "late_count": 1,
                "total_late_minutes": 15,
                "leave_count": 2,
                "on_duty_count": 1,
            },
        ]
        status_code, body = TestPhase7ASGIHelper.request(
            "GET", "/analytics/departments/summary", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        items = body["items"]
        self.assertEqual(len(items), 3)
        self.assertEqual([it["department"] for it in items], ["Design", "Engineering", "Marketing"])

        # Check required keys in each item
        expected_keys = {
            "department",
            "headcount",
            "present_days",
            "avg_work_hours",
            "late_count",
            "total_late_minutes",
            "leave_count",
            "on_duty_count",
        }
        for it in items:
            self.assertEqual(set(it.keys()), expected_keys)
            self.assertIsInstance(it["department"], str)
            self.assertIsInstance(it["headcount"], int)
            self.assertIsInstance(it["present_days"], float)
            self.assertIsInstance(it["avg_work_hours"], (float, type(None)))
            self.assertIsInstance(it["late_count"], int)
            self.assertIsInstance(it["total_late_minutes"], int)
            self.assertIsInstance(it["leave_count"], int)
            self.assertIsInstance(it["on_duty_count"], int)


class TestLiveMongoDBDepartmentSummaryIntegration(unittest.TestCase):
    """
    Live MongoDB integration test executing the real aggregation pipeline.
    Tests MongoDB $lookup, $unwind with preserveNullAndEmptyArrays,
    $group with $addToSet, and ISO day of week calculation.
    Skips cleanly if MongoDB is unavailable.
    """

    def test_live_mongodb_department_summary(self):
        from pymongo import MongoClient

        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        client = None
        try:
            client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=500)
            client.admin.command("ping")
        except Exception:
            if client is not None:
                client.close()
            self.skipTest(f"MongoDB not available at {mongo_uri}; skipping live DB aggregation test.")

        try:
            db = client["test_phase7_integration"]
            emp_col = db["employees"]
            att_col = db["attendance_logs"]
            emp_col.drop()
            att_col.drop()

            # Seed employees:
            # Engineering:
            # - EMP7001: Joined 2026-06-01 (before month), has 3 attendance logs
            # - EMP7002: Joined 2026-07-15 (mid month), has 1 attendance log
            # - EMP7003: Joined 2026-07-31 (final day of month), has 0 attendance logs (still counts in headcount!)
            # - EMP7004: Joined 2026-08-01 (after month), excluded!
            # HR:
            # - EMP7005: Joined 2026-01-01, has 0 attendance logs (headcount = 1, avg_work_hours = null)
            # Sales:
            # - EMP7006: Joined 2026-08-10 (after month, so Sales headcount = 0 and omitted!)
            emp_col.insert_many([
                {
                    "emp_code": "EMP7001",
                    "name": "Eng Senior",
                    "email": "eng1@test.com",
                    "department": "Engineering",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                    "joined_on": "2026-06-01",
                    "created_at": datetime.now(UTC),
                },
                {
                    "emp_code": "EMP7002",
                    "name": "Eng Mid",
                    "email": "eng2@test.com",
                    "department": "Engineering",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                    "joined_on": "2026-07-15",
                    "created_at": datetime.now(UTC),
                },
                {
                    "emp_code": "EMP7003",
                    "name": "Eng Joiner",
                    "email": "eng3@test.com",
                    "department": "Engineering",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                    "joined_on": "2026-07-31",
                    "created_at": datetime.now(UTC),
                },
                {
                    "emp_code": "EMP7004",
                    "name": "Eng Future",
                    "email": "eng4@test.com",
                    "department": "Engineering",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                    "joined_on": "2026-08-01",
                    "created_at": datetime.now(UTC),
                },
                {
                    "emp_code": "EMP7005",
                    "name": "HR Solo",
                    "email": "hr1@test.com",
                    "department": "Human Resources",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                    "joined_on": "2026-01-01",
                    "created_at": datetime.now(UTC),
                },
                {
                    "emp_code": "EMP7006",
                    "name": "Sales Future",
                    "email": "sales1@test.com",
                    "department": "Sales",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                    "joined_on": "2026-08-10",
                    "created_at": datetime.now(UTC),
                },
            ])

            # Seed attendance logs for July 2026:
            # EMP7001 (Engineering):
            # 1. 2026-07-06 (Mon): PRESENT, full day (half_day=False), work_hours=8.72, late_minutes=15
            # 2. 2026-07-07 (Tue): WFH, half day (half_day=True), work_hours=4.20, late_minutes=0
            # 3. 2026-07-11 (Sat): ON_DUTY on weekend, work_hours=8.00, late_minutes=30 (weekend -> 0 present days, but lateness counted!)
            # EMP7002 (Engineering):
            # 4. 2026-07-16 (Thu): ON_DUTY, full day, work_hours=8.73, late_minutes=0
            # 5. 2026-07-17 (Fri): LEAVE, work_hours=null, late_minutes=0 (status LEAVE -> 0 present days)
            # Total qualifying records for Engineering work_hours:
            # - Mon (8.72), Tue (4.20), Sat (8.00), Thu (8.73) -> 4 records
            # Sum of work hours: 8.72 + 4.20 + 8.00 + 8.73 = 29.65
            # Mean = 29.65 / 4 = 7.4125 -> ROUND_HALF_UP to 2 decimals = 7.41
            # Present days for Engineering:
            # - Mon: 1.0
            # - Tue: 0.5 (half_day)
            # - Sat: 0.0 (weekend)
            # - Thu: 1.0
            # - Fri: 0.0 (leave)
            # Total present days = 2.5
            # Lateness for Engineering:
            # - Mon: late_minutes=15 (>0 -> late_count=1)
            # - Tue: 0
            # - Sat: late_minutes=30 (>0 -> late_count=1)
            # - Thu: 0
            # - Fri: 0
            # Total late_count = 2, total_late_minutes = 45
            # Leave count = 1 (Fri)
            # On-duty count = 2 (Sat + Thu)
            # Headcount = 3 (EMP7001, EMP7002, EMP7003)
            att_col.insert_many([
                {
                    "emp_code": "EMP7001",
                    "date": "2026-07-06",
                    "status": "PRESENT",
                    "work_hours": 8.72,
                    "late_minutes": 15,
                    "half_day": False,
                },
                {
                    "emp_code": "EMP7001",
                    "date": "2026-07-07",
                    "status": "WFH",
                    "work_hours": 4.20,
                    "late_minutes": 0,
                    "half_day": True,
                },
                {
                    "emp_code": "EMP7001",
                    "date": "2026-07-11",
                    "status": "ON_DUTY",
                    "work_hours": 8.00,
                    "late_minutes": 30,
                    "half_day": False,
                },
                {
                    "emp_code": "EMP7002",
                    "date": "2026-07-16",
                    "status": "ON_DUTY",
                    "work_hours": 8.73,
                    "late_minutes": 0,
                    "half_day": False,
                },
                {
                    "emp_code": "EMP7002",
                    "date": "2026-07-17",
                    "status": "LEAVE",
                    "work_hours": None,
                    "late_minutes": 0,
                    "half_day": False,
                },
            ])

            with patch("app.main.employees_col", emp_col), patch("app.main.attendance_logs_col", att_col):
                status_code, body = TestPhase7ASGIHelper.request(
                    "GET", "/analytics/departments/summary", query="month=2026-07"
                )
                self.assertEqual(status_code, 200)
                self.assertEqual(body["month"], "2026-07")
                items = body["items"]

                # Should have Engineering and Human Resources; Sales is omitted (headcount=0)
                dept_names = [it["department"] for it in items]
                self.assertEqual(dept_names, ["Engineering", "Human Resources"])

                # Check Engineering
                eng = items[0]
                self.assertEqual(eng["department"], "Engineering")
                self.assertEqual(eng["headcount"], 3)  # EMP7001, EMP7002, and EMP7003 (0 logs)
                self.assertEqual(eng["present_days"], 2.5)
                self.assertEqual(eng["avg_work_hours"], 7.41)
                self.assertEqual(eng["late_count"], 2)
                self.assertEqual(eng["total_late_minutes"], 45)
                self.assertEqual(eng["leave_count"], 1)
                self.assertEqual(eng["on_duty_count"], 2)

                # Check Human Resources
                hr = items[1]
                self.assertEqual(hr["department"], "Human Resources")
                self.assertEqual(hr["headcount"], 1)  # EMP7005 (0 logs)
                self.assertEqual(hr["present_days"], 0.0)
                self.assertIsNone(hr["avg_work_hours"])
                self.assertEqual(hr["late_count"], 0)
                self.assertEqual(hr["total_late_minutes"], 0)
                self.assertEqual(hr["leave_count"], 0)
                self.assertEqual(hr["on_duty_count"], 0)

            emp_col.drop()
            att_col.drop()
        finally:
            if client is not None:
                client.close()


if __name__ == "__main__":
    unittest.main()
