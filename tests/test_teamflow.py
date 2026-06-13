import os
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


if __name__ == "__main__":
    unittest.main()
