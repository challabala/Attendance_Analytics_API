"""
Phase 8 Tests: Late-Comers Leaderboard (GET /analytics/leaderboard/late).
"""
import asyncio
import calendar
from datetime import date, datetime, timedelta
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from app.main import (
    UTC,
    app,
    build_late_leaderboard_pipeline,
)


class TestPhase8ASGIHelper:
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


class TestLateLeaderboardValidation(unittest.TestCase):
    """Parameter validation tests for GET /analytics/leaderboard/late."""

    def test_missing_month_returns_422(self):
        """Missing required month parameter returns 422."""
        status_code, _ = TestPhase8ASGIHelper.request("GET", "/analytics/leaderboard/late")
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
            status_code, _ = TestPhase8ASGIHelper.request(
                "GET", "/analytics/leaderboard/late", query=f"month={m}"
            )
            self.assertEqual(status_code, 422, f"Expected 422 for malformed month='{m}'")

    def test_impossible_month_returns_422(self):
        """Impossible month values return 422."""
        bad_months = ["2026-00", "2026-13", "2026-99"]
        for m in bad_months:
            status_code, _ = TestPhase8ASGIHelper.request(
                "GET", "/analytics/leaderboard/late", query=f"month={m}"
            )
            self.assertEqual(status_code, 422, f"Expected 422 for impossible month='{m}'")

    @patch("app.main.attendance_logs_col")
    def test_valid_month_with_default_parameters(self, mock_att):
        """Valid month with default parameters (limit=10, department=None) returns 200."""
        mock_att.aggregate.return_value = []
        status_code, body = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["month"], "2026-07")
        self.assertEqual(body["items"], [])

    @patch("app.main.attendance_logs_col")
    def test_limit_boundaries_and_validation(self, mock_att):
        """
        - limit=1 is accepted.
        - limit=50 is accepted.
        - limit=0 returns 422.
        - limit=51 returns 422.
        - Non-integer limit returns 422.
        """
        mock_att.aggregate.return_value = []

        # limit=1
        status_code, _ = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07&limit=1"
        )
        self.assertEqual(status_code, 200)

        # limit=50
        status_code, _ = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07&limit=50"
        )
        self.assertEqual(status_code, 200)

        # limit=0 (out of bounds, min=1)
        status_code, _ = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07&limit=0"
        )
        self.assertEqual(status_code, 422)

        # limit=51 (out of bounds, max=50)
        status_code, _ = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07&limit=51"
        )
        self.assertEqual(status_code, 422)

        # non-integer limit
        for bad_limit in ["abc", "1.5", "-1"]:
            status_code, _ = TestPhase8ASGIHelper.request(
                "GET", "/analytics/leaderboard/late", query=f"month=2026-07&limit={bad_limit}"
            )
            self.assertEqual(status_code, 422, f"Expected 422 for limit='{bad_limit}'")

    @patch("app.main.attendance_logs_col")
    def test_valid_department_matching_no_records_returns_empty_items(self, mock_att):
        """A valid department with no qualifying late employees returns 200 with items: []."""
        mock_att.aggregate.return_value = []
        status_code, body = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07&department=NonExistentDept"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["month"], "2026-07")
        self.assertEqual(body["items"], [])


class TestLateLeaderboardPipelineStructure(unittest.TestCase):
    """Tests checking pipeline structure generated by build_late_leaderboard_pipeline."""

    def test_pipeline_stages_and_components(self):
        """Verify build_late_leaderboard_pipeline builds all required stages."""
        pipeline = build_late_leaderboard_pipeline("2026-07", limit=10, department="Engineering")
        self.assertGreaterEqual(len(pipeline), 10)

        # Stage 0: $match date range
        stage0 = pipeline[0]["$match"]
        self.assertEqual(stage0["date"]["$gte"], "2026-07-01")
        self.assertEqual(stage0["date"]["$lte"], "2026-07-31")

        # Stage 1: $group by emp_code
        stage1 = pipeline[1]["$group"]
        self.assertEqual(stage1["_id"], "$emp_code")
        self.assertIn("total_late_minutes", stage1)
        self.assertIn("late_count", stage1)

        # Stage 2: $match total_late_minutes > 0
        stage2 = pipeline[2]["$match"]
        self.assertEqual(stage2["total_late_minutes"], {"$gt": 0})

        # Stage 3: $lookup from employees
        stage3 = pipeline[3]["$lookup"]
        self.assertEqual(stage3["from"], "employees")
        self.assertEqual(stage3["localField"], "_id")
        self.assertEqual(stage3["foreignField"], "emp_code")
        self.assertEqual(stage3["as"], "emp")

        # Stage 4: $unwind emp (drops orphaned records)
        stage4 = pipeline[4]["$unwind"]
        self.assertEqual(stage4["path"] if isinstance(stage4, dict) and "path" in stage4 else stage4, "$emp")

        # Stage 5: $match department before ranking
        stage5 = pipeline[5]["$match"]
        self.assertEqual(stage5["emp.department"], "Engineering")

        # Stage 6: $setWindowFields with $rank
        stage6 = pipeline[6]["$setWindowFields"]
        self.assertEqual(stage6["sortBy"], {"total_late_minutes": -1})
        self.assertEqual(stage6["output"]["rank"], {"$rank": {}})

        # Stage 7: $match rank <= limit
        stage7 = pipeline[7]["$match"]
        self.assertEqual(stage7["rank"], {"$lte": 10})

        # Stage 8: $project response fields without _id
        stage8 = pipeline[8]["$project"]
        self.assertEqual(stage8["_id"], 0)
        self.assertEqual(stage8["emp_code"], "$_id")
        self.assertEqual(stage8["name"], "$emp.name")
        self.assertEqual(stage8["department"], "$emp.department")
        self.assertIn("total_late_minutes", stage8)
        self.assertIn("late_count", stage8)

        # Stage 9: $sort total_late_minutes desc, emp_code asc
        stage9 = pipeline[9]["$sort"]
        self.assertEqual(stage9["total_late_minutes"], -1)
        self.assertEqual(stage9["emp_code"], 1)

    def test_pipeline_without_department_filter_omits_department_match(self):
        """When department is None, the pipeline does not filter by department."""
        pipeline = build_late_leaderboard_pipeline("2026-07", limit=5, department=None)
        # Check that no stage matches emp.department
        for stage in pipeline:
            if "$match" in stage:
                self.assertNotIn("emp.department", stage["$match"])


class TestLateLeaderboardRankingRules(unittest.TestCase):
    """Tests for Standard Competition Ranking (1, 2, 2, 4) and Cutoff Ties."""

    @patch("app.main.attendance_logs_col")
    def test_standard_competition_ranking_with_ties(self, mock_att):
        """
        Verify standard competition ranking:
        Employee late minutes:
        E101: 120 -> rank 1
        E102: 90  -> rank 2
        E103: 90  -> rank 2
        E104: 75  -> rank 4 (rank 3 is skipped!)
        E105: 60  -> rank 5
        """
        mock_att.aggregate.return_value = [
            {"rank": 1, "emp_code": "E101", "name": "Asha", "department": "Eng", "total_late_minutes": 120, "late_count": 8},
            {"rank": 2, "emp_code": "E102", "name": "Ravi", "department": "Eng", "total_late_minutes": 90, "late_count": 5},
            {"rank": 2, "emp_code": "E103", "name": "Meera", "department": "Eng", "total_late_minutes": 90, "late_count": 6},
            {"rank": 4, "emp_code": "E104", "name": "Dev", "department": "Eng", "total_late_minutes": 75, "late_count": 4},
            {"rank": 5, "emp_code": "E105", "name": "Pooja", "department": "Eng", "total_late_minutes": 60, "late_count": 3},
        ]
        status_code, body = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07&limit=5"
        )
        self.assertEqual(status_code, 200)
        items = body["items"]
        ranks = [it["rank"] for it in items]
        self.assertEqual(ranks, [1, 2, 2, 4, 5])
        self.assertNotIn(3, ranks, "Rank 3 must be skipped after a 2-way tie at rank 2.")

    @patch("app.main.attendance_logs_col")
    def test_limit_applies_after_ranking_including_cutoff_ties(self, mock_att):
        """
        When limit=2 and two employees tie at rank 2, both are retained,
        returning 3 rows total (rank <= limit).
        """
        # Pipeline with rank <= 2 returns all three rows
        mock_att.aggregate.return_value = [
            {"rank": 1, "emp_code": "E101", "name": "Asha", "department": "Eng", "total_late_minutes": 120, "late_count": 8},
            {"rank": 2, "emp_code": "E102", "name": "Ravi", "department": "Eng", "total_late_minutes": 90, "late_count": 5},
            {"rank": 2, "emp_code": "E103", "name": "Meera", "department": "Eng", "total_late_minutes": 90, "late_count": 6},
        ]
        status_code, body = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07&limit=2"
        )
        self.assertEqual(status_code, 200)
        items = body["items"]
        self.assertEqual(len(items), 3, "Expected 3 items returned for limit=2 due to tie at rank 2.")
        self.assertEqual([it["rank"] for it in items], [1, 2, 2])

    @patch("app.main.attendance_logs_col")
    def test_tie_at_rank_1_with_limit_1(self, mock_att):
        """When multiple employees tie for rank 1, limit=1 returns all of them."""
        mock_att.aggregate.return_value = [
            {"rank": 1, "emp_code": "E101", "name": "Asha", "department": "Eng", "total_late_minutes": 100, "late_count": 4},
            {"rank": 1, "emp_code": "E102", "name": "Ravi", "department": "Eng", "total_late_minutes": 100, "late_count": 3},
        ]
        status_code, body = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07&limit=1"
        )
        self.assertEqual(status_code, 200)
        items = body["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["rank"], 1)
        self.assertEqual(items[1]["rank"], 1)

    @patch("app.main.attendance_logs_col")
    def test_large_tie_group_at_cutoff_not_truncated(self, mock_att):
        """A large tie group at the cutoff rank is not truncated."""
        # 1 at rank 1, and 6 tied at rank 2. With limit=2, all 7 must be returned.
        mock_rows = [{"rank": 1, "emp_code": "E000", "name": "Lead", "department": "Eng", "total_late_minutes": 150, "late_count": 5}]
        for i in range(1, 7):
            mock_rows.append({
                "rank": 2,
                "emp_code": f"E00{i}",
                "name": f"Tied {i}",
                "department": "Eng",
                "total_late_minutes": 90,
                "late_count": 3,
            })
        mock_att.aggregate.return_value = mock_rows

        status_code, body = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07&limit=2"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(len(body["items"]), 7)
        self.assertEqual(body["items"][0]["rank"], 1)
        for it in body["items"][1:]:
            self.assertEqual(it["rank"], 2)

    @patch("app.main.attendance_logs_col")
    def test_secondary_sort_by_emp_code_ascending(self, mock_att):
        """Employees with identical total late minutes are sorted by emp_code ascending."""
        mock_att.aggregate.return_value = [
            {"rank": 1, "emp_code": "EMP001", "name": "Alice", "department": "Eng", "total_late_minutes": 80, "late_count": 4},
            {"rank": 1, "emp_code": "EMP002", "name": "Bob", "department": "Eng", "total_late_minutes": 80, "late_count": 5},
            {"rank": 1, "emp_code": "EMP003", "name": "Charlie", "department": "Eng", "total_late_minutes": 80, "late_count": 2},
        ]
        status_code, body = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        items = body["items"]
        emp_codes = [it["emp_code"] for it in items]
        self.assertEqual(emp_codes, ["EMP001", "EMP002", "EMP003"])


class TestLateLeaderboardDepartmentFiltering(unittest.TestCase):
    """Tests for exact case-sensitive department filtering and per-department ranking."""

    @patch("app.main.attendance_logs_col")
    def test_department_filter_is_case_sensitive(self, mock_att):
        """Department filtering preserves exact case sensitivity without normalization."""
        mock_att.aggregate.return_value = []
        status_code, body = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07&department=engineering"
        )
        self.assertEqual(status_code, 200)
        call_pipeline = mock_att.aggregate.call_args[0][0]
        # Must filter exactly by "engineering"
        dept_match = next(s["$match"] for s in call_pipeline if "$match" in s and "emp.department" in s["$match"])
        self.assertEqual(dept_match["emp.department"], "engineering")

    @patch("app.main.attendance_logs_col")
    def test_ranking_restarts_inside_department(self, mock_att):
        """
        When department is filtered, ranking is scoped to that department.
        An employee with 40 late minutes in Marketing receives rank 1 in Marketing,
        even if an employee in Engineering has 120 late minutes.
        """
        mock_att.aggregate.return_value = [
            {"rank": 1, "emp_code": "M101", "name": "Mina", "department": "Marketing", "total_late_minutes": 40, "late_count": 2},
        ]
        status_code, body = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07&department=Marketing"
        )
        self.assertEqual(status_code, 200)
        items = body["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["rank"], 1)
        self.assertEqual(items[0]["department"], "Marketing")
        self.assertEqual(items[0]["total_late_minutes"], 40)


class TestLateLeaderboardResponseSchema(unittest.TestCase):
    """Tests verifying response schema, field types, and omission of _id."""

    @patch("app.main.attendance_logs_col")
    def test_schema_keys_and_field_types(self, mock_att):
        """Verify each item contains exactly the required fields with correct JSON types."""
        mock_att.aggregate.return_value = [
            {
                "rank": 1,
                "emp_code": "EMP1001",
                "name": "Jane Doe",
                "department": "Engineering",
                "total_late_minutes": 150,
                "late_count": 10,
            }
        ]
        status_code, body = TestPhase8ASGIHelper.request(
            "GET", "/analytics/leaderboard/late", query="month=2026-07"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["month"], "2026-07")
        self.assertIn("items", body)
        self.assertEqual(len(body["items"]), 1)

        item = body["items"][0]
        expected_keys = {"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"}
        self.assertEqual(set(item.keys()), expected_keys)
        self.assertNotIn("_id", item)
        self.assertIsInstance(item["rank"], int)
        self.assertGreaterEqual(item["rank"], 1)
        self.assertIsInstance(item["emp_code"], str)
        self.assertIsInstance(item["name"], str)
        self.assertIsInstance(item["department"], str)
        self.assertIsInstance(item["total_late_minutes"], int)
        self.assertIsInstance(item["late_count"], int)


class TestLiveMongoDBLeaderboardIntegration(unittest.TestCase):
    """
    Live MongoDB integration test executing the real aggregation pipeline.
    Tests MongoDB $group, $lookup, $setWindowFields with $rank,
    competition ranking, cutoff ties, and department filtering.
    Skips cleanly if MongoDB is unavailable.
    """

    def test_live_mongodb_late_leaderboard(self):
        from pymongo import MongoClient

        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        client = None
        try:
            client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=500)
            client.admin.command("ping")
        except Exception:
            if client is not None:
                client.close()
            self.skipTest(f"MongoDB not available at {mongo_uri}; skipping live DB leaderboard test.")

        try:
            db = client["test_phase8_integration"]
            emp_col = db["employees"]
            att_col = db["attendance_logs"]
            emp_col.drop()
            att_col.drop()

            # Seed employees:
            # - EMP8001: Asha (Engineering)
            # - EMP8002: Ravi (Engineering)
            # - EMP8003: Meera (Engineering)
            # - EMP8004: Dev (Engineering)
            # - EMP8005: OnSite (Engineering) -> 0 late minutes (must NOT appear)
            # - EMP8006: Zoya (Marketing) -> 150 late minutes (different department)
            emp_col.insert_many([
                {"emp_code": "EMP8001", "name": "Asha", "email": "asha@test.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime.now(UTC)},
                {"emp_code": "EMP8002", "name": "Ravi", "email": "ravi@test.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime.now(UTC)},
                {"emp_code": "EMP8003", "name": "Meera", "email": "meera@test.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime.now(UTC)},
                {"emp_code": "EMP8004", "name": "Dev", "email": "dev@test.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime.now(UTC)},
                {"emp_code": "EMP8005", "name": "OnSite", "email": "onsite@test.com", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime.now(UTC)},
                {"emp_code": "EMP8006", "name": "Zoya", "email": "zoya@test.com", "department": "Marketing", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime.now(UTC)},
            ])

            # Seed attendance logs for July 2026:
            # - EMP8001: 2 records -> 60m + 60m = 120m, late_count = 2
            # - EMP8002: 2 records -> 45m + 45m = 90m, late_count = 2
            # - EMP8003: 3 records -> 50m + 40m + 0m = 90m, late_count = 2
            # - EMP8004: 1 record -> 75m = 75m, late_count = 1
            # - EMP8005: 1 record -> 0m (late_count = 0, total = 0 -> excluded)
            # - EMP8006: 1 record -> 150m (Marketing)
            # - EMP9999 (non-existent employee): 1 record -> 200m (must be ignored)
            # - Record outside month (2026-06-30): EMP8001 with 50m (must not be counted in July)
            att_col.insert_many([
                {"emp_code": "EMP8001", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 60},
                {"emp_code": "EMP8001", "date": "2026-07-07", "status": "PRESENT", "late_minutes": 60},
                {"emp_code": "EMP8001", "date": "2026-06-30", "status": "PRESENT", "late_minutes": 50},  # previous month
                {"emp_code": "EMP8002", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 45},
                {"emp_code": "EMP8002", "date": "2026-07-07", "status": "PRESENT", "late_minutes": 45},
                {"emp_code": "EMP8003", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 50},
                {"emp_code": "EMP8003", "date": "2026-07-07", "status": "PRESENT", "late_minutes": 40},
                {"emp_code": "EMP8003", "date": "2026-07-08", "status": "PRESENT", "late_minutes": 0},  # not late
                {"emp_code": "EMP8004", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 75},
                {"emp_code": "EMP8005", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 0},  # zero late
                {"emp_code": "EMP8006", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 150},
                {"emp_code": "EMP9999", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 200},  # orphaned
            ])

            with patch("app.main.employees_col", emp_col), patch("app.main.attendance_logs_col", att_col):
                # Test 1: Full company leaderboard with limit=2
                # Overall ranks:
                # 1. Zoya (150m, rank 1)
                # 2. Asha (120m, rank 2)
                # 3. Ravi (90m, rank 3) - excluded because rank <= 2
                status_code, body = TestPhase8ASGIHelper.request(
                    "GET", "/analytics/leaderboard/late", query="month=2026-07&limit=2"
                )
                self.assertEqual(status_code, 200)
                items = body["items"]
                self.assertEqual(len(items), 2)
                self.assertEqual(items[0]["emp_code"], "EMP8006")  # Zoya
                self.assertEqual(items[0]["rank"], 1)
                self.assertEqual(items[1]["emp_code"], "EMP8001")  # Asha
                self.assertEqual(items[1]["rank"], 2)

                # Test 2: Engineering department with limit=2 (should demonstrate cutoff tie!)
                # In Engineering:
                # Asha: 120m -> rank 1
                # Ravi: 90m  -> rank 2
                # Meera: 90m -> rank 2
                # Dev: 75m   -> rank 4
                # With limit=2: Asha, Ravi, and Meera are all retained (3 rows!)
                status_code, body = TestPhase8ASGIHelper.request(
                    "GET", "/analytics/leaderboard/late", query="month=2026-07&limit=2&department=Engineering"
                )
                self.assertEqual(status_code, 200)
                eng_items = body["items"]
                self.assertEqual(len(eng_items), 3)
                self.assertEqual([it["rank"] for it in eng_items], [1, 2, 2])
                self.assertEqual(eng_items[0]["emp_code"], "EMP8001")
                self.assertEqual(eng_items[0]["total_late_minutes"], 120)
                self.assertEqual(eng_items[0]["late_count"], 2)

                # Ravi and Meera both have 90m; EMP8002 comes before EMP8003
                self.assertEqual(eng_items[1]["emp_code"], "EMP8002")
                self.assertEqual(eng_items[1]["rank"], 2)
                self.assertEqual(eng_items[1]["total_late_minutes"], 90)
                self.assertEqual(eng_items[1]["late_count"], 2)

                self.assertEqual(eng_items[2]["emp_code"], "EMP8003")
                self.assertEqual(eng_items[2]["rank"], 2)
                self.assertEqual(eng_items[2]["total_late_minutes"], 90)
                self.assertEqual(eng_items[2]["late_count"], 2)

                # Orphaned EMP9999 and zero-late EMP8005 must NOT appear
                returned_codes = [it["emp_code"] for it in eng_items]
                self.assertNotIn("EMP9999", returned_codes)
                self.assertNotIn("EMP8005", returned_codes)

            emp_col.drop()
            att_col.drop()
        finally:
            if client is not None:
                client.close()


if __name__ == "__main__":
    unittest.main()
