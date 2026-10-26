import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from receipt_tool.webapp import create_app

ROOT = Path(__file__).resolve().parents[1]


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name))
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.network = patch("requests.Session.request", side_effect=AssertionError("Real network is forbidden"))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.temp.cleanup()

    def uploads(self, csrf, config=None):
        return {
            "csrf": csrf,
            "config": (io.BytesIO(config or (ROOT / "config/organizations.example.yaml").read_bytes()), "organizations.yaml"),
            "receipts": (io.BytesIO((ROOT / "examples/receipts.csv").read_bytes()), "receipts.csv"),
            "items": (io.BytesIO((ROOT / "examples/items.csv").read_bytes()), "items.csv"),
        }

    def token(self):
        self.client.get("/")
        with self.client.session_transaction() as session:
            return session["csrf"]

    def test_local_only_and_csrf_required(self):
        self.assertEqual(self.client.get("/", base_url="http://untrusted.example").status_code, 403)
        self.assertEqual(self.client.post("/prepare", data=self.uploads("")).status_code, 403)

    def test_uploaded_demo_and_report_download_are_offline(self):
        token = self.token()
        response = self.client.post("/prepare", data=self.uploads(token))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Готово: <strong>2</strong>", response.get_data(as_text=True))
        download = self.client.get("/reports/ready.csv")
        self.assertEqual(download.status_code, 200)
        download.close()
        self.assertEqual(self.client.get("/reports/organizations.yaml").status_code, 404)
        self.assertEqual(self.app.test_client().get("/reports/ready.csv").status_code, 404)

    def test_invalid_configuration_does_not_leak_secret_or_hold_lock(self):
        token = self.token()
        response = self.client.post("/prepare", data=self.uploads(token, b"api_secret: [NEVER_ECHO_ME\n"), follow_redirects=True)
        self.assertNotIn("NEVER_ECHO_ME", response.get_data(as_text=True))
        self.assertEqual(self.client.post("/prepare", data=self.uploads(token)).status_code, 200)


if __name__ == "__main__":
    unittest.main()
