import os
import io
import tempfile
import unittest
from pathlib import Path


TEST_DIR = Path(tempfile.mkdtemp(prefix="teamflow-tests-"))
os.environ["TEAMFLOW_DB"] = str(TEST_DIR / "teamflow-test.db")
os.environ["TEAMFLOW_BACKUP_DIR"] = str(TEST_DIR / "backups")
os.environ["TEAMFLOW_INITIAL_PASSWORD"] = "TeamFlow2026!"
os.environ.pop("TEAMFLOW_ENABLE_SCHEDULER", None)

import server  # noqa: E402


class TeamFlowApiTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        response = self.client.post(
            "/api/session",
            json={"username": "admin", "password": "TeamFlow2026!"},
        )
        self.assertEqual(response.status_code, 200)
        self.csrf = response.get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": self.csrf}

    def test_health_reports_database_status(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["database"], "ok")
        self.assertEqual(response.get_json()["gemini_model"], "gemini-2.5-flash")

    def test_render_blueprint_has_production_integrations(self):
        blueprint = (server.BASE_DIR / "render.yaml").read_text(encoding="utf-8")
        for expected in (
            "plan: free",
            "healthCheckPath: /api/health",
            "autoDeployTrigger: checksPass",
            "key: GEMINI_API_KEY",
            "key: LINE_CHANNEL_ACCESS_TOKEN",
            "key: TEAMFLOW_INITIAL_PASSWORD",
            "sync: false",
        ):
            self.assertIn(expected, blueprint)
        self.assertNotIn("disk:", blueprint)

    def test_pwa_service_worker_can_control_the_app(self):
        response = self.client.get("/sw.js", buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Service-Worker-Allowed"], "/")
        response.close()
        manifest = self.client.get("/static/manifest.webmanifest", buffered=True)
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.get_json()["display"], "standalone")
        manifest.close()

    def test_bootstrap_requires_login(self):
        anonymous = server.app.test_client()
        self.assertEqual(anonymous.get("/api/bootstrap").status_code, 401)

    def test_admin_can_reset_member_password(self):
        response = self.client.put(
            "/api/admin/users/2/password",
            json={"new_password": "Nakamura2026!"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)

        member = server.app.test_client()
        login = member.post(
            "/api/session",
            json={"username": "nakamura", "password": "Nakamura2026!"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.get_json()["user"]["role"], "member")

    def test_backup_can_be_created_and_downloaded(self):
        response = self.client.post("/api/admin/backup", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        filename = response.get_json()["filename"]
        path = server.BACKUP_DIR / filename
        self.assertTrue(path.exists())

        download = self.client.get(f"/api/admin/backup/{filename}", buffered=True)
        self.assertEqual(download.status_code, 200)
        self.assertGreater(len(download.data), 0)
        download.close()

    def test_csrf_is_required_for_admin_changes(self):
        response = self.client.post("/api/admin/backup")
        self.assertEqual(response.status_code, 403)

    def test_tasks_can_be_exported_as_excel_friendly_csv(self):
        response = self.client.get("/api/admin/tasks/export")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("title,project,assignee", response.data.decode("utf-8-sig"))

    def test_admin_can_create_project(self):
        response = self.client.post(
            "/api/projects",
            json={
                "name": "Production project",
                "description": "Render registration test",
                "start_date": "2026-06-13",
                "end_date": "2026-09-13",
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["name"], "Production project")

    def test_tasks_can_be_imported_from_csv(self):
        with server.db() as connection:
            project = connection.execute("SELECT name FROM projects ORDER BY id LIMIT 1").fetchone()["name"]
            before = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        content = (
            "title,project,assignee,start_date,due_date,priority,status,progress,description\n"
            f"CSV test task,{project},nakamura,2026-06-13,2026-06-20,high,todo,10,Imported\n"
        ).encode("utf-8")
        response = self.client.post(
            "/api/admin/tasks/import",
            data={"file": (io.BytesIO(content), "tasks.csv")},
            headers=self.headers,
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["imported"], 1)
        with server.db() as connection:
            after = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(after, before + 1)

    def test_csv_import_is_atomic_when_a_row_is_invalid(self):
        with server.db() as connection:
            before = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        content = (
            "title,project,start_date,due_date\n"
            "Invalid task,Missing project,2026-06-13,2026-06-20\n"
        ).encode("utf-8")
        response = self.client.post(
            "/api/admin/tasks/import",
            data={"file": (io.BytesIO(content), "tasks.csv")},
            headers=self.headers,
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        with server.db() as connection:
            after = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
