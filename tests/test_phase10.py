"""
Phase 10 Tests: MongoDB Explain Diagnostics (GET /admin/explain/{endpoint}).
"""
import asyncio
from datetime import date, datetime, timedelta
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from app.main import (
    UTC,
    app,
    build_attendance_list_query,
    build_department_summary_pipeline,
    build_department_trend_pipeline,
    build_employee_monthly_pipeline,
    build_late_leaderboard_pipeline,
    SUPPORTED_EXPLAIN_ENDPOINTS,
)


class TestPhase10ASGIHelper:
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


def extract_plan_stages(doc) -> list:
    """
    Recursively extracts all 'stage' names from a MongoDB explain document
    (handling classic queryPlanner, executionStats, and nested aggregation stages).
    """
    stages = []
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k == "stage" and isinstance(v, str):
                stages.append(v)
            else:
                stages.extend(extract_plan_stages(v))
    elif isinstance(doc, list):
        for item in doc:
            stages.extend(extract_plan_stages(item))
    return stages


class TestExplainEndpointValidation(unittest.TestCase):
    """Validation tests for GET /admin/explain/{endpoint}."""

    def test_unsupported_endpoint_returns_422(self):
        """Unsupported endpoint name returns 422."""
        for invalid in ["unknown_endpoint", "users", "departments", "123", "explain"]:
            status_code, body = TestPhase10ASGIHelper.request(
                "GET", f"/admin/explain/{invalid}"
            )
            self.assertEqual(status_code, 422)

    # 1. attendance_list validation
    def test_attendance_list_invalid_dates_return_422(self):
        """attendance_list rejects malformed and impossible dates, and date_from > date_to."""
        # Malformed
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/attendance_list", query="date_from=invalid-date"
        )
        self.assertEqual(status_code, 422)

        # Impossible date
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/attendance_list", query="date_to=2026-02-30"
        )
        self.assertEqual(status_code, 422)

        # date_from > date_to
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/attendance_list", query="date_from=2026-07-10&date_to=2026-07-01"
        )
        self.assertEqual(status_code, 422)

    def test_attendance_list_invalid_status_returns_422(self):
        """attendance_list rejects invalid status."""
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/attendance_list", query="status=VACATION"
        )
        self.assertEqual(status_code, 422)

    def test_attendance_list_invalid_pagination_returns_422(self):
        """attendance_list rejects page < 1, page_size < 1, page_size > 100."""
        for bad_q in ["page=0", "page=-1", "page_size=0", "page_size=101"]:
            status_code, _ = TestPhase10ASGIHelper.request(
                "GET", "/admin/explain/attendance_list", query=bad_q
            )
            self.assertEqual(status_code, 422)

    # 2. employee_monthly validation
    def test_employee_monthly_missing_emp_code_returns_422(self):
        """employee_monthly requires emp_code."""
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/employee_monthly", query="month=2026-07"
        )
        self.assertEqual(status_code, 422)

    def test_employee_monthly_missing_month_returns_422(self):
        """employee_monthly requires month."""
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/employee_monthly", query="emp_code=EMP001"
        )
        self.assertEqual(status_code, 422)

    def test_employee_monthly_invalid_month_returns_422(self):
        """employee_monthly rejects malformed or impossible month."""
        for bad_m in ["2026-7", "2026/07", "2026-13", "not-a-month", "2026-00"]:
            status_code, _ = TestPhase10ASGIHelper.request(
                "GET", "/admin/explain/employee_monthly", query=f"emp_code=EMP001&month={bad_m}"
            )
            self.assertEqual(status_code, 422)

    # 3. department_summary validation
    def test_department_summary_missing_month_returns_422(self):
        """department_summary requires month."""
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/department_summary"
        )
        self.assertEqual(status_code, 422)

    def test_department_summary_invalid_month_returns_422(self):
        """department_summary rejects malformed month."""
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/department_summary", query="month=2026-99"
        )
        self.assertEqual(status_code, 422)

    # 4. late_leaderboard validation
    def test_late_leaderboard_missing_month_returns_422(self):
        """late_leaderboard requires month."""
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/late_leaderboard"
        )
        self.assertEqual(status_code, 422)

    def test_late_leaderboard_invalid_limit_returns_422(self):
        """late_leaderboard rejects limit < 1 or limit > 50."""
        for bad_lim in ["0", "-5", "51", "100"]:
            status_code, _ = TestPhase10ASGIHelper.request(
                "GET", "/admin/explain/late_leaderboard", query=f"month=2026-07&limit={bad_lim}"
            )
            self.assertEqual(status_code, 422)

    # 5. department_trend validation
    def test_department_trend_missing_parameters_return_422(self):
        """department_trend requires department, from, and to."""
        # Missing department
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/department_trend", query="from=2026-07-01&to=2026-07-10"
        )
        self.assertEqual(status_code, 422)

        # Missing from
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/department_trend", query="department=Engineering&to=2026-07-10"
        )
        self.assertEqual(status_code, 422)

        # Missing to
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/department_trend", query="department=Engineering&from=2026-07-01"
        )
        self.assertEqual(status_code, 422)

    def test_department_trend_invalid_dates_and_ranges_return_422(self):
        """department_trend rejects impossible dates, to < from, and range > 92 days."""
        # Impossible date
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/department_trend", query="department=Engineering&from=2026-02-30&to=2026-03-05"
        )
        self.assertEqual(status_code, 422)

        # to < from
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/department_trend", query="department=Engineering&from=2026-07-10&to=2026-07-01"
        )
        self.assertEqual(status_code, 422)

        # Range > 92 days
        status_code, _ = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/department_trend", query="department=Engineering&from=2026-07-01&to=2026-10-05"
        )
        self.assertEqual(status_code, 422)


class TestExplainResponseSchemaAndFidelity(unittest.TestCase):
    """Tests checking response structure and query command fidelity for each endpoint."""

    @patch("app.main.db.command")
    def test_explain_attendance_list_command_and_response(self, mock_cmd):
        """attendance_list builds find command on attendance_logs with sort, skip, limit, and executionStats."""
        fake_explain = {
            "queryPlanner": {
                "plannerVersion": 1,
                "winningPlan": {
                    "stage": "FETCH",
                    "inputStage": {
                        "stage": "IXSCAN",
                        "indexName": "idx_date_desc_emp_code_asc",
                    },
                },
            },
            "executionStats": {
                "executionSuccess": True,
                "nReturned": 15,
            },
        }
        mock_cmd.return_value = fake_explain

        status_code, body = TestPhase10ASGIHelper.request(
            "GET",
            "/admin/explain/attendance_list",
            query="emp_code=EMP001&date_from=2026-07-01&date_to=2026-07-15&status=PRESENT&page=2&page_size=15",
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["endpoint"], "attendance_list")
        self.assertEqual(body["collection"], "attendance_logs")
        self.assertIn("queryPlanner", body["explain"])
        self.assertIn("executionStats", body["explain"])

        # Verify db.command invocation
        mock_cmd.assert_called_once()
        cmd_name, cmd_dict = mock_cmd.call_args[0]
        kwargs = mock_cmd.call_args[1]
        self.assertEqual(cmd_name, "explain")
        self.assertEqual(kwargs.get("verbosity"), "executionStats")
        self.assertEqual(cmd_dict["find"], "attendance_logs")
        self.assertEqual(cmd_dict["filter"]["emp_code"], "EMP001")
        self.assertEqual(cmd_dict["filter"]["status"], "PRESENT")
        self.assertEqual(cmd_dict["filter"]["date"]["$gte"], "2026-07-01")
        self.assertEqual(cmd_dict["filter"]["date"]["$lte"], "2026-07-15")
        self.assertEqual(cmd_dict["sort"], {"date": -1, "emp_code": 1})
        self.assertEqual(cmd_dict["skip"], 15)  # (page 2 - 1) * 15
        self.assertEqual(cmd_dict["limit"], 15)

    @patch("app.main.db.command")
    def test_explain_attendance_list_defaults(self, mock_cmd):
        """attendance_list with no query params uses default page=1, page_size=20, and empty filter."""
        mock_cmd.return_value = {"queryPlanner": {}, "executionStats": {}}

        status_code, body = TestPhase10ASGIHelper.request(
            "GET", "/admin/explain/attendance_list"
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["endpoint"], "attendance_list")
        self.assertEqual(body["collection"], "attendance_logs")

        cmd_name, cmd_dict = mock_cmd.call_args[0]
        self.assertEqual(cmd_dict["find"], "attendance_logs")
        self.assertEqual(cmd_dict["filter"], {})
        self.assertEqual(cmd_dict["sort"], {"date": -1, "emp_code": 1})
        self.assertEqual(cmd_dict["skip"], 0)
        self.assertEqual(cmd_dict["limit"], 20)

    @patch("app.main.db.command")
    def test_explain_employee_monthly_command_and_response(self, mock_cmd):
        """employee_monthly builds aggregate command on attendance_logs."""
        fake_explain = {
            "stages": [
                {
                    "$cursor": {
                        "queryPlanner": {
                            "winningPlan": {
                                "stage": "IXSCAN",
                                "indexName": "uniq_emp_code_date",
                            }
                        },
                        "executionStats": {
                            "executionSuccess": True,
                        },
                    }
                }
            ]
        }
        mock_cmd.return_value = fake_explain

        status_code, body = TestPhase10ASGIHelper.request(
            "GET",
            "/admin/explain/employee_monthly",
            query="emp_code=EMP001&month=2026-07",
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["endpoint"], "employee_monthly")
        self.assertEqual(body["collection"], "attendance_logs")

        mock_cmd.assert_called_once()
        cmd_name, cmd_dict = mock_cmd.call_args[0]
        self.assertEqual(cmd_name, "explain")
        self.assertEqual(mock_cmd.call_args[1].get("verbosity"), "executionStats")
        self.assertEqual(cmd_dict["aggregate"], "attendance_logs")
        self.assertEqual(cmd_dict["cursor"], {})
        # Verify pipeline matches helper
        expected_pipeline = build_employee_monthly_pipeline("EMP001", "2026-07")
        self.assertEqual(cmd_dict["pipeline"], expected_pipeline)

    @patch("app.main.db.command")
    def test_explain_department_summary_command_and_response(self, mock_cmd):
        """department_summary builds aggregate command on employees."""
        mock_cmd.return_value = {"stages": [{"$cursor": {"queryPlanner": {}}}]}

        # With department filter
        status_code, body = TestPhase10ASGIHelper.request(
            "GET",
            "/admin/explain/department_summary",
            query="month=2026-07&department=Engineering",
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["endpoint"], "department_summary")
        self.assertEqual(body["collection"], "employees")

        cmd_name, cmd_dict = mock_cmd.call_args[0]
        self.assertEqual(cmd_dict["aggregate"], "employees")
        expected_pipeline = build_department_summary_pipeline("2026-07", "Engineering")
        self.assertEqual(cmd_dict["pipeline"], expected_pipeline)

        # Without department filter (all departments)
        mock_cmd.reset_mock()
        status_code, body = TestPhase10ASGIHelper.request(
            "GET",
            "/admin/explain/department_summary",
            query="month=2026-07",
        )
        self.assertEqual(status_code, 200)
        cmd_name, cmd_dict = mock_cmd.call_args[0]
        expected_pipeline_all = build_department_summary_pipeline("2026-07", None)
        self.assertEqual(cmd_dict["pipeline"], expected_pipeline_all)

    @patch("app.main.db.command")
    def test_explain_late_leaderboard_command_and_response(self, mock_cmd):
        """late_leaderboard builds aggregate command on attendance_logs."""
        mock_cmd.return_value = {"stages": [{"$cursor": {"queryPlanner": {}}}]}

        status_code, body = TestPhase10ASGIHelper.request(
            "GET",
            "/admin/explain/late_leaderboard",
            query="month=2026-07&limit=25&department=Sales",
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["endpoint"], "late_leaderboard")
        self.assertEqual(body["collection"], "attendance_logs")

        cmd_name, cmd_dict = mock_cmd.call_args[0]
        self.assertEqual(cmd_dict["aggregate"], "attendance_logs")
        expected_pipeline = build_late_leaderboard_pipeline("2026-07", 25, "Sales")
        self.assertEqual(cmd_dict["pipeline"], expected_pipeline)

    @patch("app.main.db.command")
    def test_explain_department_trend_command_and_response(self, mock_cmd):
        """department_trend builds aggregate command on employees."""
        mock_cmd.return_value = {"stages": [{"$cursor": {"queryPlanner": {}}}]}

        status_code, body = TestPhase10ASGIHelper.request(
            "GET",
            "/admin/explain/department_trend",
            query="department=Engineering&from=2026-07-01&to=2026-07-15",
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(body["endpoint"], "department_trend")
        self.assertEqual(body["collection"], "employees")

        cmd_name, cmd_dict = mock_cmd.call_args[0]
        self.assertEqual(cmd_dict["aggregate"], "employees")
        expected_pipeline = build_department_trend_pipeline("Engineering", "2026-07-01", "2026-07-15")
        self.assertEqual(cmd_dict["pipeline"], expected_pipeline)


class TestExplainQueryBuilders(unittest.TestCase):
    """Unit tests verifying shared query builders produce identical structures."""

    def test_build_attendance_list_query_various_filters(self):
        """build_attendance_list_query correctly maps query parameters into MongoDB find options."""
        # Empty parameters
        q, sort_fields, skip, limit = build_attendance_list_query()
        self.assertEqual(q, {})
        self.assertEqual(sort_fields, [("date", -1), ("emp_code", 1)])
        self.assertEqual(skip, 0)
        self.assertEqual(limit, 20)

        # Full parameters
        q, sort_fields, skip, limit = build_attendance_list_query(
            emp_code="EMP101",
            date_from="2026-07-01",
            date_to="2026-07-31",
            status_param="WFH",
            page=3,
            page_size=10,
        )
        self.assertEqual(q["emp_code"], "EMP101")
        self.assertEqual(q["status"], "WFH")
        self.assertEqual(q["date"]["$gte"], "2026-07-01")
        self.assertEqual(q["date"]["$lte"], "2026-07-31")
        self.assertEqual(sort_fields, [("date", -1), ("emp_code", 1)])
        self.assertEqual(skip, 20)  # (3 - 1) * 10
        self.assertEqual(limit, 10)


class TestExplainPlanInspection(unittest.TestCase):
    """Tests checking recursive plan inspection functions on realistic MongoDB explain outputs."""

    def test_extract_stages_classic_find_plan(self):
        """Find plan stages (FETCH, IXSCAN) are correctly extracted."""
        plan = {
            "queryPlanner": {
                "winningPlan": {
                    "stage": "FETCH",
                    "inputStage": {
                        "stage": "IXSCAN",
                        "indexName": "idx_date_desc_emp_code_asc",
                    },
                }
            },
            "executionStats": {
                "executionStages": {
                    "stage": "FETCH",
                    "inputStage": {
                        "stage": "IXSCAN",
                        "indexName": "idx_date_desc_emp_code_asc",
                    },
                }
            },
        }
        stages = extract_plan_stages(plan)
        self.assertIn("IXSCAN", stages)
        self.assertIn("FETCH", stages)
        self.assertNotIn("COLLSCAN", stages)

    def test_extract_stages_collscan_detected(self):
        """COLLSCAN is detected when present in winning plan."""
        plan = {
            "queryPlanner": {
                "winningPlan": {
                    "stage": "COLLSCAN",
                    "direction": "forward",
                }
            }
        }
        stages = extract_plan_stages(plan)
        self.assertIn("COLLSCAN", stages)
        self.assertNotIn("IXSCAN", stages)

    def test_extract_stages_nested_aggregation_stages(self):
        """Nested aggregation pipeline stages inside $cursor are extracted."""
        plan = {
            "stages": [
                {
                    "$cursor": {
                        "queryPlanner": {
                            "winningPlan": {
                                "stage": "IXSCAN",
                                "indexName": "uniq_emp_code_date",
                            }
                        }
                    }
                },
                {"$group": {"_id": "$department"}},
            ]
        }
        stages = extract_plan_stages(plan)
        self.assertIn("IXSCAN", stages)


class TestLiveMongoDBExplainIntegration(unittest.TestCase):
    """Integration test against live MongoDB daemon if available."""

    def test_live_mongodb_explain_all_five_endpoints(self):
        """Runs real MongoDB explain commands for all five endpoints if MongoDB is running."""
        from pymongo import MongoClient

        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        client = None
        try:
            client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=500)
            client.admin.command("ping")
        except Exception:
            if client is not None:
                client.close()
            self.skipTest(f"MongoDB not available at {mongo_uri}; skipping live DB explain test.")

        try:
            db = client["test_phase10_integration"]
            emp_col = db["employees"]
            att_col = db["attendance_logs"]
            emp_col.drop()
            att_col.drop()

            # Create standard application indexes
            emp_col.create_index([("emp_code", 1)], unique=True, name="uniq_emp_code")
            emp_col.create_index([("department", 1), ("emp_code", 1)], name="idx_department_emp_code")
            emp_col.create_index([("joined_on", 1)], name="idx_joined_on")
            emp_col.create_index([("department", 1), ("joined_on", 1)], name="idx_department_joined_on")
            att_col.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_code_date")
            att_col.create_index([("date", -1), ("emp_code", 1)], name="idx_date_desc_emp_code_asc")
            att_col.create_index([("status", 1), ("date", -1)], name="idx_status_date")

            # Seed minimal employee and attendance records
            emp_col.insert_one({
                "emp_code": "EMP1001",
                "name": "Alice",
                "department": "Engineering",
                "joined_on": "2026-01-01",
            })
            att_col.insert_one({
                "emp_code": "EMP1001",
                "date": "2026-07-06",
                "status": "PRESENT",
                "late_minutes": 10,
                "work_hours": 8.0,
            })

            with patch("app.main.db", db), patch("app.main.employees_col", emp_col), patch("app.main.attendance_logs_col", att_col):
                # 1. attendance_list explain
                st1, body1 = TestPhase10ASGIHelper.request(
                    "GET", "/admin/explain/attendance_list", query="emp_code=EMP1001&date_from=2026-07-01&date_to=2026-07-31"
                )
                self.assertEqual(st1, 200)
                stages1 = extract_plan_stages(body1["explain"])
                self.assertIn("IXSCAN", stages1)

                # 2. employee_monthly explain
                st2, body2 = TestPhase10ASGIHelper.request(
                    "GET", "/admin/explain/employee_monthly", query="emp_code=EMP1001&month=2026-07"
                )
                self.assertEqual(st2, 200)
                stages2 = extract_plan_stages(body2["explain"])
                self.assertIn("IXSCAN", stages2)

                # 3. department_summary explain
                st3, body3 = TestPhase10ASGIHelper.request(
                    "GET", "/admin/explain/department_summary", query="month=2026-07&department=Engineering"
                )
                self.assertEqual(st3, 200)
                stages3 = extract_plan_stages(body3["explain"])
                self.assertIn("IXSCAN", stages3)

                # 4. late_leaderboard explain
                st4, body4 = TestPhase10ASGIHelper.request(
                    "GET", "/admin/explain/late_leaderboard", query="month=2026-07&limit=10"
                )
                self.assertEqual(st4, 200)
                stages4 = extract_plan_stages(body4["explain"])
                self.assertIn("IXSCAN", stages4)

                # 5. department_trend explain
                st5, body5 = TestPhase10ASGIHelper.request(
                    "GET", "/admin/explain/department_trend", query="department=Engineering&from=2026-07-01&to=2026-07-07"
                )
                self.assertEqual(st5, 200)
                stages5 = extract_plan_stages(body5["explain"])
                self.assertIn("IXSCAN", stages5)

            emp_col.drop()
            att_col.drop()
        finally:
            if client is not None:
                client.close()


if __name__ == "__main__":
    unittest.main()
