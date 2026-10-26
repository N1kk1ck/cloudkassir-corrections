import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from receipt_tool.cli import main
from receipt_tool.core import InputError, prepare
from receipt_tool.sending import State, digest, refresh_statuses, send_batch
from test_core import item, organization, receipt


def live_organization():
    result = organization()
    result.update(demo=False, fiscal_confirmed=True)
    result["credentials"] = {"public_id": "synthetic-public", "api_secret": "synthetic-secret"}
    result["fiscal"].update(organization_inn="1234567890", correction_number="TEST-001")
    return result


class FakeClient:
    calls = 0
    mode = "success"
    account_hash = digest("synthetic-public")

    def __init__(self, organization):
        pass

    def safe_message(self, value):
        return "simulated error"

    def send(self, preview):
        FakeClient.calls += 1
        if self.mode == "timeout":
            raise TimeoutError()
        if self.mode == "reject":
            return {"Success": False, "Message": "rejected"}
        if self.mode == "malformed":
            return {"Success": True}
        return {"Success": True, "Model": {"Id": "synthetic-cloud-id"}}

    def status(self, cloud_id):
        if self.mode == "bad_status":
            return {"Success": False, "Model": "Processed"}
        return {"Success": True, "Model": "Processed"}


class SendingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"
        self.state = State(self.path, create=True)
        self.orgs = {"arbitrary_entity": live_organization()}
        self.preview = prepare(self.orgs, [receipt()], [item()]).ready[0]
        FakeClient.calls, FakeClient.mode = 0, "success"
        self.network_patch = patch("requests.Session.request", side_effect=AssertionError("Real network is forbidden"))
        self.network_patch.start()

    def tearDown(self):
        self.state.close()
        self.network_patch.stop()
        self.temp.cleanup()

    def test_claim_is_durable_and_shared_by_two_connections(self):
        second = State(self.path)
        try:
            self.assertIsNone(self.state.claim(self.preview, FakeClient.account_hash))
            self.assertEqual(second.claim(self.preview, FakeClient.account_hash), "sending")
        finally:
            second.close()

    def test_changed_payload_cannot_reuse_claim(self):
        self.state.claim(self.preview, FakeClient.account_hash)
        changed = copy.deepcopy(self.preview)
        changed["payload"]["correctionReceiptData"]["paymentPlace"] = "changed"
        with self.assertRaises(InputError):
            self.state.claim(changed, FakeClient.account_hash)

    def test_new_receipt_cannot_change_existing_entity_account(self):
        self.state.claim(self.preview, FakeClient.account_hash)
        another = prepare(self.orgs, [receipt(receipt_id="payment-2")], [item()]).ready[0]
        with self.assertRaises(InputError):
            self.state.claim(another, digest("different-public"))

    def test_queue_is_not_final_and_second_send_is_skipped(self):
        result = send_batch([self.preview], self.orgs, self.state, delay=0, client_factory=FakeClient)
        self.assertEqual(result["queued"], 1)
        self.assertEqual(self.state.rows()[0]["status"], "queued")
        send_batch([self.preview], self.orgs, self.state, delay=0, client_factory=FakeClient)
        self.assertEqual(FakeClient.calls, 1)
        refresh_statuses(self.orgs, self.state, client_factory=FakeClient)
        self.assertEqual(self.state.rows()[0]["status"], "processed")

    def test_timeout_and_missing_id_are_protected_from_retry(self):
        for mode in ["timeout", "malformed"]:
            with self.subTest(mode=mode):
                preview = prepare(self.orgs, [receipt(receipt_id=mode)], [item()]).ready[0]
                FakeClient.mode = mode
                send_batch([preview], self.orgs, self.state, delay=0, client_factory=FakeClient)
                self.assertEqual(next(r for r in self.state.rows() if r["receipt_id"] == mode)["status"], "unknown")
                calls = FakeClient.calls
                send_batch([preview], self.orgs, self.state, delay=0, client_factory=FakeClient)
                self.assertEqual(FakeClient.calls, calls)

    def test_business_error_is_not_retried(self):
        FakeClient.mode = "reject"
        send_batch([self.preview], self.orgs, self.state, delay=0, client_factory=FakeClient)
        self.assertEqual(self.state.rows()[0]["status"], "error")
        send_batch([self.preview], self.orgs, self.state, delay=0, client_factory=FakeClient)
        self.assertEqual(FakeClient.calls, 1)

    def test_failed_status_response_cannot_mark_processed(self):
        send_batch([self.preview], self.orgs, self.state, delay=0, client_factory=FakeClient)
        FakeClient.mode = "bad_status"
        with self.assertRaises(InputError):
            refresh_statuses(self.orgs, self.state, client_factory=FakeClient)
        self.assertEqual(self.state.rows()[0]["status"], "queued")

    def test_new_entity_first_batch_cannot_exceed_one(self):
        previews = prepare(self.orgs, [receipt(), receipt(receipt_id="p2", order_id="o2")], [item(), item(order_id="o2")]).ready
        with self.assertRaises(InputError):
            send_batch(previews, self.orgs, self.state, delay=0, client_factory=FakeClient)
        self.assertEqual(FakeClient.calls, 0)

    def test_demo_config_blocks_live_calls(self):
        self.orgs["arbitrary_entity"]["demo"] = True
        with self.assertRaises(InputError):
            send_batch([self.preview], self.orgs, self.state, delay=0, client_factory=FakeClient)
        self.assertEqual(FakeClient.calls, 0)

    def test_missing_existing_or_foreign_state_is_never_silently_created(self):
        with self.assertRaises(InputError):
            State(Path(self.temp.name) / "missing.db")
        with self.assertRaises(InputError):
            State(self.path, create=True)
        foreign = Path(self.temp.name) / "foreign.db"
        foreign.write_bytes(b"")
        with self.assertRaises(InputError):
            State(foreign)

    def test_cli_guards_before_loading_private_inputs(self):
        self.assertEqual(main(["send", "--receipts", "missing.csv", "--items", "missing.csv"]), 2)
        self.assertEqual(main(["init-state", "--state", str(self.path)]), 2)

    def test_report_cannot_overwrite_state_database(self):
        self.assertEqual(main(["report", "--state", str(self.path), "--output", str(self.path)]), 2)
        check = State(self.path)
        check.close()

    def test_cli_limit_counts_new_receipts_after_processed_rows(self):
        from receipt_tool.core import write_csv
        config = Path(self.temp.name) / "organizations.yaml"
        config.write_text(yaml.safe_dump({"organizations": self.orgs}), encoding="utf-8")
        receipts = [receipt(), receipt(receipt_id="payment-2", order_id="order-2")]
        items = [item(), item(order_id="order-2")]
        receipts_path, items_path = Path(self.temp.name) / "receipts.csv", Path(self.temp.name) / "items.csv"
        write_csv(receipts_path, list(receipts[0]), receipts)
        write_csv(items_path, list(items[0]), items)
        self.state.claim(self.preview, FakeClient.account_hash)
        self.state.update(self.preview["receipt_key"], "processed", "synthetic-first")
        with patch("receipt_tool.cli.send_batch", side_effect=lambda *a, **k: send_batch(*a, **k, client_factory=FakeClient)):
            code = main(["send", "--config", str(config), "--receipts", str(receipts_path), "--items", str(items_path),
                         "--state", str(self.path), "--send", "--confirm", "SEND_CORRECTIONS", "--limit", "1", "--delay", "0"])
        self.assertEqual(code, 3)
        self.assertEqual(FakeClient.calls, 1)
        self.assertEqual({r["receipt_id"]: r["status"] for r in self.state.rows()}, {"payment-1": "processed", "payment-2": "queued"})


if __name__ == "__main__":
    unittest.main()
