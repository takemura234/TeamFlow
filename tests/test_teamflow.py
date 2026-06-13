import os
import io
import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path


TEST_DIR = Path(tempfile.mkdtemp(prefix="teamflow-tests-"))
os.environ["TEAMFLOW_DB"] = str(TEST_DIR / "teamflow-test.db")
os.environ["TEAMFLOW_BACKUP_DIR"] = str(TEST_DIR / "backups")
os.environ["TEAMFLOW_INITIAL_PASSWORD"] = "TeamFlow2026!"
os.environ["LINE_CHANNEL_SECRET"] = "test-line-channel-secret"
os.environ.pop("GEMINI_API_KEY", None)
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
            "key: LINE_CHANNEL_SECRET",
            "key: TEAMFLOW_INITIAL_PASSWORD",
            "sync: false",
        ):
            self.assertIn(expected, blueprint)
        self.assertNotIn("disk:", blueprint)

    def test_line_account_can_be_linked_by_signed_webhook_code(self):
        code_response = self.client.post("/api/account/line/link-code", headers=self.headers)
        self.assertEqual(code_response.status_code, 200)
        code = code_response.get_json()["code"]
        payload = {
            "events": [{
                "type": "message",
                "source": {"type": "user", "userId": "U1234567890"},
                "message": {"type": "text", "text": code.lower()},
            }]
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        signature = base64.b64encode(
            hmac.new(b"test-line-channel-secret", body, hashlib.sha256).digest()
        ).decode()
        webhook = server.app.test_client().post(
            "/api/line/webhook",
            data=body,
            headers={"Content-Type": "application/json", "X-Line-Signature": signature},
        )
        self.assertEqual(webhook.status_code, 200)
        self.assertEqual(webhook.get_json()["linked"], 1)
        with server.db() as connection:
            account = connection.execute("SELECT line_user_id FROM users WHERE id = 1").fetchone()
        self.assertEqual(account["line_user_id"], "U1234567890")

    def test_line_webhook_rejects_invalid_signature(self):
        response = server.app.test_client().post(
            "/api/line/webhook",
            json={"events": []},
            headers={"X-Line-Signature": "invalid"},
        )
        self.assertEqual(response.status_code, 401)

    def test_gemini_connection_test_reports_missing_key(self):
        response = self.client.post("/api/ai/test", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertIn("GEMINI_API_KEY", response.get_json()["error"])

    def test_pwa_service_worker_can_control_the_app(self):
        response = self.client.get("/sw.js", buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Service-Worker-Allowed"], "/")
        response.close()
        manifest = self.client.get("/static/manifest.webmanifest", buffered=True)
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.get_json()["display"], "standalone")
        manifest.close()

    def test_dashboard_access_qr_is_generated_as_svg(self):
        response = self.client.get(
            "/api/access-qr?url=https%3A%2F%2Fteamflow-lkt4.onrender.com%2Flogin"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "image/svg+xml")
        self.assertIn(b"<svg", response.data)

    def test_access_qr_rejects_invalid_url(self):
        response = self.client.get("/api/access-qr?url=javascript%3Aalert(1)")
        self.assertEqual(response.status_code, 400)

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

    def test_viewer_accounts_can_comment_but_cannot_create_tasks(self):
        for username in ("hirota", "yamazaki"):
            viewer = server.app.test_client()
            login = viewer.post(
                "/api/session",
                json={"username": username, "password": "TeamFlow2026!"},
            )
            self.assertEqual(login.status_code, 200)
            self.assertEqual(login.get_json()["user"]["role"], "viewer")
            csrf = {"X-CSRF-Token": login.get_json()["csrf_token"]}
            denied = viewer.post(
                "/api/tasks",
                json={
                    "title": "Viewer task", "project_id": 1,
                    "start_date": "2026-06-13", "due_date": "2026-06-20",
                },
                headers=csrf,
            )
            self.assertEqual(denied.status_code, 403)
            comment = viewer.post(
                "/api/tasks/1/comments",
                json={"body": f"{username} viewer comment"},
                headers=csrf,
            )
            self.assertEqual(comment.status_code, 201)

    def test_management_notifications_include_both_viewers(self):
        with server.db() as connection:
            recipients = server.management_recipient_ids(connection, weekly_only=True)
        self.assertEqual(recipients, [1, 8, 9])

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

    def test_database_can_be_restored_from_uploaded_backup(self):
        snapshot = server.create_database_backup()
        with server.db() as connection:
            connection.execute(
                "INSERT INTO projects(name, description, start_date, end_date) VALUES (?, ?, ?, ?)",
                ("Restore marker", "must disappear", "2026-06-14", "2026-06-15"),
            )

        with snapshot.open("rb") as backup_file:
            response = self.client.post(
                "/api/admin/restore",
                data={"file": (backup_file, "teamflow-backup.db")},
                headers=self.headers,
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertTrue((server.BACKUP_DIR / result["safety_backup"]).exists())
        with server.db() as connection:
            marker = connection.execute(
                "SELECT id FROM projects WHERE name = ?", ("Restore marker",)
            ).fetchone()
        self.assertIsNone(marker)
        self.assertEqual(self.client.get("/api/bootstrap").status_code, 401)

    def test_database_restore_rejects_invalid_file(self):
        response = self.client.post(
            "/api/admin/restore",
            data={"file": (io.BytesIO(b"not a sqlite database"), "broken.db")},
            headers=self.headers,
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("SQLite", response.get_json()["error"])

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

    def test_project_with_children_requires_explicit_cascade_delete(self):
        project = self.client.post(
            "/api/projects",
            json={"name": "Delete project", "start_date": "2026-06-13", "end_date": "2026-09-13"},
            headers=self.headers,
        ).get_json()
        task = self.client.post(
            "/api/tasks",
            json={
                "title": "Delete with project", "project_id": project["id"],
                "start_date": "2026-06-13", "due_date": "2026-06-20",
            },
            headers=self.headers,
        )
        self.assertEqual(task.status_code, 201)
        blocked = self.client.delete(f"/api/projects/{project['id']}", headers=self.headers)
        self.assertEqual(blocked.status_code, 409)
        deleted = self.client.delete(f"/api/projects/{project['id']}?cascade=1", headers=self.headers)
        self.assertEqual(deleted.status_code, 204)
        with server.db() as connection:
            self.assertIsNone(connection.execute("SELECT id FROM projects WHERE id = ?", (project["id"],)).fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tasks WHERE project_id = ?", (project["id"],)).fetchone()[0], 0)

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
