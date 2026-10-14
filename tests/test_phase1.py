"""
Phase 1 Tests: Project Setup, MongoDB Configuration, and Health Endpoint.
"""
import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pymongo.errors import ConnectionFailure, PyMongoError, ServerSelectionTimeoutError


class TestPhase1(unittest.TestCase):
    def test_app_imports_successfully(self):
        """Verify the FastAPI application imports and has expected metadata."""
        from app.main import app

        self.assertIsNotNone(app)
        self.assertEqual(app.title, "Employee Attendance & Analytics API")
        self.assertEqual(app.version, "2.0.0")

    def test_mongodb_configuration(self):
        """Verify MongoDB client and collection setup."""
        from app.main import attendance_logs_col, client, db, employees_col

        self.assertIsNotNone(client)
        self.assertIsNotNone(db)
        self.assertEqual(employees_col.name, "employees")
        self.assertEqual(attendance_logs_col.name, "attendance_logs")

    @patch("app.main.client")
    def test_health_success_direct(self, mock_client):
        """Verify /health returns 200 {"status": "ok"} when MongoDB ping succeeds."""
        from app.main import health

        mock_client.admin.command.return_value = {"ok": 1.0}
        result = health()

        mock_client.admin.command.assert_called_once_with("ping")
        self.assertEqual(result, {"status": "ok"})

    @patch("app.main.client")
    def test_health_failure_direct(self, mock_client):
        """Verify /health raises 503 when MongoDB ping fails with PyMongoError."""
        from app.main import health

        mock_client.admin.command.side_effect = ServerSelectionTimeoutError("No server found")

        with self.assertRaises(HTTPException) as ctx:
            health()

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "Database unavailable")

    @patch("app.main.client")
    def test_health_connection_failure_direct(self, mock_client):
        """Verify /health raises 503 when connection fails."""
        from app.main import health

        mock_client.admin.command.side_effect = ConnectionFailure("Connection reset by peer")

        with self.assertRaises(HTTPException) as ctx:
            health()

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "Database unavailable")

    @patch("app.main.client")
    def test_health_asgi_http_response_200(self, mock_client):
        """Verify full ASGI HTTP pipeline for /health on 200 OK."""
        from app.main import app

        mock_client.admin.command.return_value = {"ok": 1.0}

        status_code, body = self._run_asgi_request(app, "/health")
        self.assertEqual(status_code, 200)
        data = json.loads(body)
        self.assertEqual(data, {"status": "ok"})

    @patch("app.main.client")
    def test_health_asgi_http_response_503(self, mock_client):
        """Verify full ASGI HTTP pipeline for /health on 503 Service Unavailable."""
        from app.main import app

        mock_client.admin.command.side_effect = ServerSelectionTimeoutError("Timed out")

        status_code, body = self._run_asgi_request(app, "/health")
        self.assertEqual(status_code, 503)
        data = json.loads(body)
        self.assertEqual(data, {"detail": "Database unavailable"})

    def _run_asgi_request(self, app, path: str):
        """Helper to invoke FastAPI app via ASGI without requiring external test clients."""
        async def _call():
            scope = {
                "type": "http",
                "method": "GET",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [],
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
            return res_status, b"".join(res_body).decode("utf-8")

        return asyncio.run(_call())


if __name__ == "__main__":
    unittest.main()
