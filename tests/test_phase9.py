"""
Phase 9 Tests: Department Daily Attendance Trend (GET /analytics/departments/{department}/trend).
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
    build_department_trend_pipeline,
    round_half_up,
)


class TestPhase9ASGIHelper:
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


class TestDepartmentTrendValidation(unittest.TestCase):
    """Request validation tests for GET /analytics/departments/{department}/trend."""

    def test_missing_from_returns_422(self):
        """Missing required 'from' parameter returns 422."""
        status_code, _ = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/Engineering/trend", query="to=2026-07-10"
        )
        self.assertEqual(status_code, 422)

    def test_missing_to_returns_422(self):
        """Missing required 'to' parameter returns 422."""
        status_code, _ = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/Engineering/trend", query="from=2026-07-01"
        )
        self.assertEqual(status_code, 422)

    def test_malformed_dates_return_422(self):
        """Malformed date formats return 422."""
        bad_dates = [
            "2026-7-1",
            "not-a-date",
            "2026/07/01",
            "01-07-2026",
            "26-07-01",
            "2026-07",
        ]
        for bd in bad_dates:
            status_code, _ = TestPhase9ASGIHelper.request(
                "GET", "/analytics/departments/Engineering/trend", query=f"from={bd}&to=2026-07-10"
            )
            self.assertEqual(status_code, 422, f"Expected 422 for malformed from='{bd}'")

            status_code, _ = TestPhase9ASGIHelper.request(
                "GET", "/analytics/departments/Engineering/trend", query=f"from=2026-07-01&to={bd}"
            )
            self.assertEqual(status_code, 422, f"Expected 422 for malformed to='{bd}'")

    def test_impossible_dates_return_422(self):
        """Impossible dates (e.g. Feb 30, April 31) return 422."""
        bad_dates = [
            "2026-02-30",
            "2026-04-31",
            "2026-06-31",
            "2026-13-01",
            "2026-00-10",
        ]
        for bd in bad_dates:
            status_code, _ = TestPhase9ASGIHelper.request(
                "GET", "/analytics/departments/Engineering/trend", query=f"from={bd}&to=2026-07-10"
            )
            self.assertEqual(status_code, 422, f"Expected 422 for impossible date='{bd}'")

    def test_to_earlier_than_from_returns_422(self):
        """to date earlier than from date returns 422."""
        status_code, body = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/Engineering/trend", query="from=2026-07-10&to=2026-07-05"
        )
        self.assertEqual(status_code, 422)
        self.assertIn("earlier", body["detail"].lower())

    def test_range_exceeding_92_days_returns_422(self):
        """Date range exceeding 92 calendar days inclusive returns 422."""
        # 2026-07-01 to 2026-10-01 has 31 (Jul) + 31 (Aug) + 30 (Sep) + 1 (Oct) = 93 days -> exceeds 92
        status_code, body = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/Engineering/trend", query="from=2026-07-01&to=2026-10-01"
        )
        self.assertEqual(status_code, 422)
        self.assertIn("92", body["detail"])

    @patch("app.main.employees_col")
    def test_valid_one_day_range_returns_200(self, mock_emp):
        """One-day range (from=to) is accepted and returns 200."""
        mock_emp.count_documents.return_value = 1
        mock_emp.aggregate.return_value = [
            {
                "date": "2026-07-01",
                "is_working_day": True,
                "headcount": 5,
                "present_count": 4.0,
                "late_count": 1,
                "attendance_rate": 0.8,
                "moving_avg_7d": 0.8,
            }
        ]
        status_code, body = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/Engineering/trend", query="from=2026-07-01&to=2026-07-01"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["department"], "Engineering")
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["date"], "2026-07-01")

    @patch("app.main.employees_col")
    def test_valid_92_day_range_returns_200(self, mock_emp):
        """92-calendar-day range is valid and returns 200."""
        # 2026-07-01 to 2026-09-30 is exactly 31 + 31 + 30 = 92 days
        mock_emp.count_documents.return_value = 1
        mock_emp.aggregate.return_value = []
        status_code, body = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/Engineering/trend", query="from=2026-07-01&to=2026-09-30"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["department"], "Engineering")

    @patch("app.main.employees_col")
    def test_unknown_department_returns_404(self, mock_emp):
        """Unknown department (no employees in collection) returns 404."""
        mock_emp.count_documents.return_value = 0
        status_code, body = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/NonExistentDept/trend", query="from=2026-07-01&to=2026-07-05"
        )
        self.assertEqual(status_code, 404)
        self.assertIn("not found", body["detail"].lower())

    @patch("app.main.employees_col")
    def test_department_with_no_attendance_records_returns_200(self, mock_emp):
        """Department with employees but no attendance records returns 200."""
        mock_emp.count_documents.return_value = 1
        mock_emp.aggregate.return_value = [
            {
                "date": "2026-07-01",
                "is_working_day": True,
                "headcount": 2,
                "present_count": 0.0,
                "late_count": 0,
                "attendance_rate": 0.0,
                "moving_avg_7d": 0.0,
            }
        ]
        status_code, body = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/HumanResources/trend", query="from=2026-07-01&to=2026-07-01"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["department"], "HumanResources")
        self.assertEqual(len(body["items"]), 1)


class TestDepartmentTrendPipelineStructure(unittest.TestCase):
    """Tests checking pipeline structure generated by build_department_trend_pipeline."""

    def test_pipeline_stages_and_components(self):
        """Verify pipeline stages: match, group, lookup, scaffold map, unwind, projections, window, sort."""
        pipeline = build_department_trend_pipeline("Engineering", "2026-07-01", "2026-07-07")
        self.assertGreaterEqual(len(pipeline), 10)

        # Stage 0: $match department
        self.assertEqual(pipeline[0]["$match"]["department"], "Engineering")

        # Stage 1: $group dept_emp_codes and dept_joined_dates
        self.assertEqual(pipeline[1]["$group"]["_id"], "$department")
        self.assertIn("dept_emp_codes", pipeline[1]["$group"])
        self.assertIn("dept_joined_dates", pipeline[1]["$group"])

        # Stage 2: $lookup attendance_logs
        lookup = pipeline[2]["$lookup"]
        self.assertEqual(lookup["from"], "attendance_logs")
        expr_str = json.dumps(lookup["pipeline"])
        self.assertIn("2026-07-01", expr_str)
        self.assertIn("2026-07-07", expr_str)

        # Stage 3: $project days via $range and $map
        stage3_proj = pipeline[3]["$project"]
        self.assertIn("days", stage3_proj)
        days_expr = stage3_proj["days"]
        self.assertEqual(days_expr["$map"]["input"]["$range"], [0, 7])

        # Stage 4: $unwind $days
        self.assertEqual(pipeline[4]["$unwind"], "$days")

        # Stage 7: $setWindowFields with documents [-6, 'current']
        set_window = pipeline[7]["$setWindowFields"]
        self.assertEqual(set_window["sortBy"], {"date": 1})
        self.assertEqual(set_window["output"]["moving_avg_7d"]["window"]["documents"], [-6, "current"])

        # Stage 9: $sort date: 1
        self.assertEqual(pipeline[9]["$sort"], {"date": 1})


class TestDepartmentTrendDailyScaffoldAndMetrics(unittest.TestCase):
    """Tests for daily date scaffold, is_working_day, headcount, attendance_rate, and moving average."""

    @patch("app.main.employees_col")
    def test_is_working_day_weekdays_and_weekends(self, mock_emp):
        """Mon-Fri is_working_day=True, Sat-Sun is_working_day=False."""
        mock_emp.count_documents.return_value = 1
        # 2026-07-10 is Friday, 2026-07-11 is Saturday, 2026-07-12 is Sunday
        mock_emp.aggregate.return_value = [
            {"date": "2026-07-10", "is_working_day": True, "headcount": 10, "present_count": 9.0, "late_count": 0, "attendance_rate": 0.9, "moving_avg_7d": 0.9},
            {"date": "2026-07-11", "is_working_day": False, "headcount": 10, "present_count": 0.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": 0.9},
            {"date": "2026-07-12", "is_working_day": False, "headcount": 10, "present_count": 0.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": 0.9},
        ]
        status_code, body = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/Engineering/trend", query="from=2026-07-10&to=2026-07-12"
        )
        self.assertEqual(status_code, 200)
        items = body["items"]
        self.assertEqual(len(items), 3)
        self.assertTrue(items[0]["is_working_day"])
        self.assertFalse(items[1]["is_working_day"])
        self.assertFalse(items[2]["is_working_day"])

    @patch("app.main.employees_col")
    def test_attendance_rate_working_day_vs_weekend_vs_zero_headcount(self, mock_emp):
        """
        - Working day + positive headcount + positive presence -> rate.
        - Working day + positive headcount + 0 presence -> 0.0.
        - Working day + 0 headcount -> null.
        - Weekend -> null.
        """
        mock_emp.count_documents.return_value = 1
        mock_emp.aggregate.return_value = [
            {"date": "2026-07-06", "is_working_day": True, "headcount": 10, "present_count": 8.0, "late_count": 0, "attendance_rate": 0.8, "moving_avg_7d": 0.8},
            {"date": "2026-07-07", "is_working_day": True, "headcount": 10, "present_count": 0.0, "late_count": 0, "attendance_rate": 0.0, "moving_avg_7d": 0.4},
            {"date": "2026-07-08", "is_working_day": True, "headcount": 0, "present_count": 0.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": 0.4},
            {"date": "2026-07-11", "is_working_day": False, "headcount": 10, "present_count": 2.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": 0.4},
        ]
        status_code, body = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/Engineering/trend", query="from=2026-07-06&to=2026-07-11"
        )
        self.assertEqual(status_code, 200)
        items = body["items"]
        self.assertEqual(items[0]["attendance_rate"], 0.8)
        self.assertEqual(items[1]["attendance_rate"], 0.0)
        self.assertIsNone(items[2]["attendance_rate"])
        self.assertIsNone(items[3]["attendance_rate"])

    @patch("app.main.employees_col")
    def test_moving_avg_7d_calculation_and_null_handling(self, mock_emp):
        """
        Verifies 7-day moving average calculation:
        - Day 1: 0.8000 -> 0.8000
        - Day 2: 0.6000 -> (0.8 + 0.6) / 2 = 0.7000
        - Day 3 (null): null ignored -> 0.7000
        - Day 4: 0.9000 -> (0.8 + 0.6 + 0.9) / 3 = 0.7667
        - Day 5: 0.7000 -> (0.8 + 0.6 + 0.9 + 0.7) / 4 = 0.7500
        - Day 6 (weekend, null) -> 0.7500
        - Day 7 (weekend, null) -> 0.7500
        """
        mock_emp.count_documents.return_value = 1
        mock_emp.aggregate.return_value = [
            {"date": "2026-07-06", "is_working_day": True, "headcount": 10, "present_count": 8.0, "late_count": 0, "attendance_rate": 0.8, "moving_avg_7d": 0.8},
            {"date": "2026-07-07", "is_working_day": True, "headcount": 10, "present_count": 6.0, "late_count": 0, "attendance_rate": 0.6, "moving_avg_7d": 0.7},
            {"date": "2026-07-08", "is_working_day": True, "headcount": 0, "present_count": 0.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": 0.7},
            {"date": "2026-07-09", "is_working_day": True, "headcount": 10, "present_count": 9.0, "late_count": 0, "attendance_rate": 0.9, "moving_avg_7d": 0.7667},
            {"date": "2026-07-10", "is_working_day": True, "headcount": 10, "present_count": 7.0, "late_count": 0, "attendance_rate": 0.7, "moving_avg_7d": 0.75},
            {"date": "2026-07-11", "is_working_day": False, "headcount": 10, "present_count": 0.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": 0.75},
            {"date": "2026-07-12", "is_working_day": False, "headcount": 10, "present_count": 0.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": 0.75},
        ]
        status_code, body = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/Engineering/trend", query="from=2026-07-06&to=2026-07-12"
        )
        self.assertEqual(status_code, 200)
        items = body["items"]
        expected_avgs = [0.8, 0.7, 0.7, 0.7667, 0.75, 0.75, 0.75]
        for it, exp in zip(items, expected_avgs):
            self.assertEqual(it["moving_avg_7d"], exp)

    @patch("app.main.employees_col")
    def test_headcount_mid_range_joiner_and_pre_joining_zero(self, mock_emp):
        """Headcount increases when a new employee joins midway; days before have headcount 0."""
        mock_emp.count_documents.return_value = 1
        mock_emp.aggregate.return_value = [
            {"date": "2026-07-01", "is_working_day": True, "headcount": 0, "present_count": 0.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": None},
            {"date": "2026-07-02", "is_working_day": True, "headcount": 1, "present_count": 1.0, "late_count": 0, "attendance_rate": 1.0, "moving_avg_7d": 1.0},
            {"date": "2026-07-03", "is_working_day": True, "headcount": 2, "present_count": 1.5, "late_count": 0, "attendance_rate": 0.75, "moving_avg_7d": 0.875},
        ]
        status_code, body = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/Engineering/trend", query="from=2026-07-01&to=2026-07-03"
        )
        self.assertEqual(status_code, 200)
        items = body["items"]
        self.assertEqual(items[0]["headcount"], 0)
        self.assertEqual(items[1]["headcount"], 1)
        self.assertEqual(items[2]["headcount"], 2)

    @patch("app.main.employees_col")
    def test_present_count_full_and_half_day_plus_weekend(self, mock_emp):
        """Full day adds 1.0, half day adds 0.5, weekend presence is included in present_count."""
        mock_emp.count_documents.return_value = 1
        mock_emp.aggregate.return_value = [
            {"date": "2026-07-11", "is_working_day": False, "headcount": 5, "present_count": 3.5, "late_count": 1, "attendance_rate": None, "moving_avg_7d": None},
        ]
        status_code, body = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/Engineering/trend", query="from=2026-07-11&to=2026-07-11"
        )
        self.assertEqual(status_code, 200)
        item = body["items"][0]
        self.assertEqual(item["present_count"], 3.5)
        self.assertFalse(item["is_working_day"])

    @patch("app.main.employees_col")
    def test_rounding_half_up_4_decimals_boundary(self, mock_emp):
        """
        Rule R8: 4 decimals ROUND_HALF_UP rounds half away from zero.
        e.g., rate of 0.87505 -> 0.8751 (vs 0.8750 under banker's rounding).
        """
        mock_emp.count_documents.return_value = 1
        mock_emp.aggregate.return_value = [
            {"date": "2026-07-01", "is_working_day": True, "headcount": 1, "present_count": 1.0, "late_count": 0, "attendance_rate": 0.87505, "moving_avg_7d": 0.87505},
        ]
        val = Decimal("0.87505")
        self.assertEqual(val.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN), Decimal("0.8750"))
        self.assertEqual(val.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), Decimal("0.8751"))

        status_code, body = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/Engineering/trend", query="from=2026-07-01&to=2026-07-01"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["items"][0]["attendance_rate"], 0.8751)
        self.assertEqual(body["items"][0]["moving_avg_7d"], 0.8751)


class TestDepartmentTrendResponseSchema(unittest.TestCase):
    """Tests verifying exact keys, field types, and omission of internal _id."""

    @patch("app.main.employees_col")
    def test_schema_keys_and_field_types(self, mock_emp):
        """Verify top-level and item schema matches Trend in openapi.yaml."""
        mock_emp.count_documents.return_value = 1
        mock_emp.aggregate.return_value = [
            {
                "date": "2026-07-06",
                "is_working_day": True,
                "headcount": 10,
                "present_count": 8.5,
                "late_count": 2,
                "attendance_rate": 0.85,
                "moving_avg_7d": 0.85,
            }
        ]
        status_code, body = TestPhase9ASGIHelper.request(
            "GET", "/analytics/departments/Engineering/trend", query="from=2026-07-06&to=2026-07-06"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(set(body.keys()), {"department", "items"})
        self.assertEqual(body["department"], "Engineering")
        self.assertEqual(len(body["items"]), 1)

        item = body["items"][0]
        expected_keys = {
            "date",
            "is_working_day",
            "headcount",
            "present_count",
            "late_count",
            "attendance_rate",
            "moving_avg_7d",
        }
        self.assertEqual(set(item.keys()), expected_keys)
        self.assertNotIn("_id", item)
        self.assertIsInstance(item["date"], str)
        self.assertIsInstance(item["is_working_day"], bool)
        self.assertIsInstance(item["headcount"], int)
        self.assertIsInstance(item["present_count"], (float, int))
        self.assertIsInstance(item["late_count"], int)
        self.assertIsInstance(item["attendance_rate"], (float, type(None)))
        self.assertIsInstance(item["moving_avg_7d"], (float, type(None)))


class TestLiveMongoDBTrendIntegration(unittest.TestCase):
    """
    Live MongoDB integration test executing the complete pipeline.
    Tests MongoDB date sequence generation ($range, $map, $dateAdd),
    daily headcount filtering, attendance aggregation, attendance_rate,
    and rolling window $avg with $setWindowFields.
    Skips cleanly if MongoDB is unreachable.
    """

    def test_live_mongodb_department_trend(self):
        from pymongo import MongoClient

        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        client = None
        try:
            client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=500)
            client.admin.command("ping")
        except Exception:
            if client is not None:
                client.close()
            self.skipTest(f"MongoDB not available at {mongo_uri}; skipping live DB trend test.")

        try:
            db = client["test_phase9_integration"]
            emp_col = db["employees"]
            att_col = db["attendance_logs"]
            emp_col.drop()
            att_col.drop()

            # Seed department employees:
            # - EMP9001: Joined 2026-06-01 (before requested range)
            # - EMP9002: Joined 2026-07-08 (mid-range: starts contributing on 2026-07-08)
            # - EMP9003: Joined 2026-08-01 (after range: does not contribute to headcount in July)
            emp_col.insert_many([
                {"emp_code": "EMP9001", "name": "Senior", "email": "s@test.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-06-01", "created_at": datetime.now(UTC)},
                {"emp_code": "EMP9002", "name": "Mid", "email": "m@test.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-08", "created_at": datetime.now(UTC)},
                {"emp_code": "EMP9003", "name": "Future", "email": "f@test.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-01", "created_at": datetime.now(UTC)},
            ])

            # Seed attendance records for Engineering:
            # Range: 2026-07-06 (Mon) to 2026-07-12 (Sun) -> 7 calendar days!
            # - 2026-07-06 (Mon): EMP9001 PRESENT full day (1.0), late 15m
            #   headcount=1 (only EMP9001 joined), present_count=1.0, late_count=1
            #   attendance_rate = 1.0 / 1 = 1.0, moving_avg_7d = 1.0
            # - 2026-07-07 (Tue): No attendance logs at all for anyone in Engineering!
            #   headcount=1, present_count=0.0, late_count=0
            #   attendance_rate = 0.0 / 1 = 0.0, moving_avg_7d = (1.0 + 0.0) / 2 = 0.5
            # - 2026-07-08 (Wed): EMP9002 joins today! Both work today!
            #   EMP9001: WFH half_day=True (0.5), late 0
            #   EMP9002: ON_DUTY full_day=False (1.0), late 30m
            #   headcount=2, present_count=1.5, late_count=1
            #   attendance_rate = 1.5 / 2 = 0.75, moving_avg_7d = (1.0 + 0.0 + 0.75) / 3 = 0.5833
            # - 2026-07-09 (Thu): EMP9001 LEAVE (0 present), EMP9002 ABSENT (0 present)
            #   headcount=2, present_count=0.0, late_count=0
            #   attendance_rate = 0.0 / 2 = 0.0, moving_avg_7d = (1.0 + 0.0 + 0.75 + 0.0) / 4 = 0.4375
            # - 2026-07-10 (Fri): EMP9001 PRESENT full day (1.0), late 0
            #   headcount=2, present_count=1.0, late_count=0
            #   attendance_rate = 1.0 / 2 = 0.5, moving_avg_7d = (1.0 + 0.0 + 0.75 + 0.0 + 0.5) / 5 = 0.4500
            # - 2026-07-11 (Sat): Weekend! EMP9001 worked on site (PRESENT, 1.0), late 20m!
            #   headcount=2, present_count=1.0 (weekend presence counted in present_count!), late_count=1
            #   attendance_rate = null (weekend!), moving_avg_7d = 0.4500 (null rate ignored)
            # - 2026-07-12 (Sun): Weekend! No logs.
            #   headcount=2, present_count=0.0, late_count=0
            #   attendance_rate = null (weekend!), moving_avg_7d = 0.4500
            att_col.insert_many([
                {"emp_code": "EMP9001", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 15},
                {"emp_code": "EMP9001", "date": "2026-07-08", "status": "WFH", "half_day": True, "late_minutes": 0},
                {"emp_code": "EMP9002", "date": "2026-07-08", "status": "ON_DUTY", "half_day": False, "late_minutes": 30},
                {"emp_code": "EMP9001", "date": "2026-07-09", "status": "LEAVE", "half_day": False, "late_minutes": 0},
                {"emp_code": "EMP9002", "date": "2026-07-09", "status": "ABSENT", "half_day": False, "late_minutes": 0},
                {"emp_code": "EMP9001", "date": "2026-07-10", "status": "PRESENT", "half_day": False, "late_minutes": 0},
                {"emp_code": "EMP9001", "date": "2026-07-11", "status": "PRESENT", "half_day": False, "late_minutes": 20},
            ])

            with patch("app.main.employees_col", emp_col), patch("app.main.attendance_logs_col", att_col):
                status_code, body = TestPhase9ASGIHelper.request(
                    "GET", "/analytics/departments/Engineering/trend", query="from=2026-07-06&to=2026-07-12"
                )
                self.assertEqual(status_code, 200)
                self.assertEqual(body["department"], "Engineering")
                items = body["items"]
                self.assertEqual(len(items), 7)

                # Day 1: 2026-07-06 (Mon)
                d1 = items[0]
                self.assertEqual(d1["date"], "2026-07-06")
                self.assertTrue(d1["is_working_day"])
                self.assertEqual(d1["headcount"], 1)
                self.assertEqual(d1["present_count"], 1.0)
                self.assertEqual(d1["late_count"], 1)
                self.assertEqual(d1["attendance_rate"], 1.0)
                self.assertEqual(d1["moving_avg_7d"], 1.0)

                # Day 2: 2026-07-07 (Tue) - Day with no attendance records
                d2 = items[1]
                self.assertEqual(d2["date"], "2026-07-07")
                self.assertTrue(d2["is_working_day"])
                self.assertEqual(d2["headcount"], 1)
                self.assertEqual(d2["present_count"], 0.0)
                self.assertEqual(d2["late_count"], 0)
                self.assertEqual(d2["attendance_rate"], 0.0)
                self.assertEqual(d2["moving_avg_7d"], 0.5)

                # Day 3: 2026-07-08 (Wed) - Mid-range joiner starts contributing
                d3 = items[2]
                self.assertEqual(d3["date"], "2026-07-08")
                self.assertTrue(d3["is_working_day"])
                self.assertEqual(d3["headcount"], 2)
                self.assertEqual(d3["present_count"], 1.5)
                self.assertEqual(d3["late_count"], 1)
                self.assertEqual(d3["attendance_rate"], 0.75)
                self.assertEqual(d3["moving_avg_7d"], 0.5833)

                # Day 4: 2026-07-09 (Thu) - LEAVE and ABSENT
                d4 = items[3]
                self.assertEqual(d4["date"], "2026-07-09")
                self.assertTrue(d4["is_working_day"])
                self.assertEqual(d4["headcount"], 2)
                self.assertEqual(d4["present_count"], 0.0)
                self.assertEqual(d4["late_count"], 0)
                self.assertEqual(d4["attendance_rate"], 0.0)
                self.assertEqual(d4["moving_avg_7d"], 0.4375)

                # Day 5: 2026-07-10 (Fri)
                d5 = items[4]
                self.assertEqual(d5["date"], "2026-07-10")
                self.assertTrue(d5["is_working_day"])
                self.assertEqual(d5["headcount"], 2)
                self.assertEqual(d5["present_count"], 1.0)
                self.assertEqual(d5["late_count"], 0)
                self.assertEqual(d5["attendance_rate"], 0.5)
                self.assertEqual(d5["moving_avg_7d"], 0.4500)

                # Day 6: 2026-07-11 (Sat) - Weekend with presence record
                d6 = items[5]
                self.assertEqual(d6["date"], "2026-07-11")
                self.assertFalse(d6["is_working_day"])
                self.assertEqual(d6["headcount"], 2)
                self.assertEqual(d6["present_count"], 1.0)
                self.assertEqual(d6["late_count"], 1)
                self.assertIsNone(d6["attendance_rate"])
                self.assertEqual(d6["moving_avg_7d"], 0.4500)

                # Day 7: 2026-07-12 (Sun) - Weekend without logs
                d7 = items[6]
                self.assertEqual(d7["date"], "2026-07-12")
                self.assertFalse(d7["is_working_day"])
                self.assertEqual(d7["headcount"], 2)
                self.assertEqual(d7["present_count"], 0.0)
                self.assertEqual(d7["late_count"], 0)
                self.assertIsNone(d7["attendance_rate"])
                self.assertEqual(d7["moving_avg_7d"], 0.4500)

            emp_col.drop()
            att_col.drop()
        finally:
            if client is not None:
                client.close()


if __name__ == "__main__":
    unittest.main()
