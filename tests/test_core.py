import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from receipt_tool.core import (InputError, ITEM_REQUIRED, RECEIPT_REQUIRED,
    build_payload, load_inputs, load_organizations, prepare, read_table,
    read_yaml, request_id, write_csv, write_preparation)

ROOT = Path(__file__).resolve().parents[1]


def organization():
    return load_organizations(ROOT / "config/organizations.example.yaml")["example_shop"]


def receipt(**overrides):
    return {"entity_id": "arbitrary_entity", "receipt_id": "payment-1", "order_id": "order-1",
            "receipt_type": "income", "amount": "100.00", "currency": "RUB",
            "status": "completed", "email": "demo@example.com", **overrides}


def item(**overrides):
    return {"entity_id": "arbitrary_entity", "order_id": "order-1", "line_id": "1",
            "name": "Example item", "quantity": "1", "price": "100.00", "amount": "100.00", **overrides}


class PreparationTests(unittest.TestCase):
    def test_demo_is_offline_with_expected_reconciliation(self):
        with patch("requests.Session.request", side_effect=AssertionError("Network is forbidden")):
            organizations, result = load_inputs(ROOT / "config/organizations.example.yaml", ROOT / "examples/receipts.csv", ROOT / "examples/items.csv")
            self.assertEqual((len(result.ready), len(result.review)), (2, 1))
            self.assertIn("до копейки", result.review[0]["reason"])
            self.assertEqual(result.ready[1]["payload"]["correctionReceiptData"]["correctionReceiptType"], 2)
            with tempfile.TemporaryDirectory() as directory:
                summary = write_preparation(Path(directory), result)
                preview = (Path(directory) / "payload_preview.jsonl").read_text(encoding="utf-8")
                self.assertEqual(summary["ready"], 2)
                self.assertNotIn("api_secret", preview)
                self.assertNotIn("public_id", preview)

    def test_arbitrary_entities_and_same_order_do_not_mix(self):
        orgs = {f"tenant_{n}": organization() for n in range(7)}
        receipts = [receipt(entity_id=entity) for entity in orgs]
        items = [item(entity_id=entity) for entity in orgs]
        result = prepare(orgs, receipts, items)
        self.assertEqual(len(result.ready), 7)
        self.assertEqual(len({r["request_id"] for r in result.ready}), 7)

    def test_duplicate_receipts_block_preparation(self):
        with self.assertRaises(InputError):
            prepare({"arbitrary_entity": organization()}, [receipt(), receipt()], [item()])

    def test_duplicate_item_lines_block_preparation(self):
        with self.assertRaises(InputError):
            prepare({"arbitrary_entity": organization()}, [receipt()], [item(), item()])

    def test_unknown_entity_is_rejected(self):
        with self.assertRaises(InputError):
            prepare({"different": organization()}, [receipt()], [item()])

    def test_shared_order_requires_explicit_per_receipt_composition(self):
        orgs = {"arbitrary_entity": organization()}
        receipts = [receipt(), receipt(receipt_id="payment-2")]
        result = prepare(orgs, receipts, [item()])
        self.assertEqual(len(result.review), 2)
        result = prepare(orgs, receipts, [item(receipt_id="payment-1"), item(receipt_id="payment-2")])
        self.assertEqual(len(result.ready), 2)

    def test_mixed_scoped_and_unscoped_items_are_rejected(self):
        result = prepare({"arbitrary_entity": organization()}, [receipt()], [item(), item(line_id="2", receipt_id="payment-1")])
        self.assertEqual(len(result.review), 1)

    def test_no_inferred_discount_and_no_kopeck_tolerance(self):
        for amount in ["0.00", "0", "99.99", "90.00", "NaN", "Infinity", "-100"]:
            with self.subTest(amount=amount), self.assertRaises(InputError):
                build_payload(receipt(amount=amount), [item()], organization())

    def test_explicit_discount_amount_is_preserved(self):
        payload = build_payload(receipt(amount="90.00"), [item(amount="90.00")], organization())
        self.assertEqual(payload["correctionReceiptData"]["items"][0]["amount"], 90)

    def test_invalid_currency_status_type_and_contact_are_rejected(self):
        for changes in [{"currency": "USD"}, {"status": "failed"}, {"receipt_type": "expense"}, {"email": "invalid"}, {"email": "", "phone": ""}]:
            with self.subTest(changes=changes), self.assertRaises(InputError):
                build_payload(receipt(**changes), [item()], organization())

    def test_refund_requires_confirmation(self):
        with self.assertRaises(InputError):
            build_payload(receipt(receipt_type="income_return"), [item()], organization())
        payload = build_payload(receipt(receipt_type="income_return", items_confirmed="true"), [item()], organization())
        self.assertEqual(payload["correctionReceiptData"]["correctionReceiptType"], 2)

    def test_invalid_quantity_price_amount_and_label(self):
        for changes in [{"quantity": "0"}, {"quantity": "-1"}, {"quantity": "1.001"}, {"price": "NaN"}, {"price": "-1"}, {"price": "99.99"}, {"name": "x" * 129}, {"amount": "100.001"}]:
            with self.subTest(changes=changes), self.assertRaises(InputError):
                build_payload(receipt(), [item(**changes)], organization())

    def test_request_id_is_deterministic_and_type_specific(self):
        self.assertEqual(request_id(receipt()), request_id(copy.deepcopy(receipt())))
        self.assertNotEqual(request_id(receipt()), request_id(receipt(receipt_type="income_return")))

    def test_xlsx_column_mapping_and_named_sheet(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.xlsx"
            workbook = Workbook()
            workbook.active.title = "Data"
            workbook.create_sheet("Other")
            workbook["Data"].append(["Company", "Payment", "Order", "Type", "Total", "Currency", "State"])
            workbook["Data"].append(["arbitrary_entity", "p1", "o1", "income", 100, "RUB", "completed"])
            workbook.save(path)
            with self.assertRaises(InputError):
                read_table(path, RECEIPT_REQUIRED)
            rows = read_table(path, RECEIPT_REQUIRED, {"sheet": "Data", "columns": dict(zip(["entity_id", "receipt_id", "order_id", "receipt_type", "amount", "currency", "status"], ["Company", "Payment", "Order", "Type", "Total", "Currency", "State"]))})
            self.assertEqual(rows[0]["amount"], "100")

    def test_yaml_parser_does_not_echo_secret_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.yaml"
            path.write_text("api_secret: [SUPER_PRIVATE_TOKEN\n", encoding="utf-8")
            with self.assertRaises(InputError) as context:
                read_yaml(path)
            self.assertNotIn("SUPER_PRIVATE_TOKEN", str(context.exception))
            path.write_text("organizations: {}\norganizations: {}\n", encoding="utf-8")
            with self.assertRaises(InputError):
                read_yaml(path)

    def test_header_offset_and_value_translations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.csv"
            path.write_text("Report title\nentity_id,receipt_id,order_id,receipt_type,amount,currency,status\nx,p1,o1,Оплата,100,RUB,Завершена\n", encoding="utf-8")
            rows = read_table(path, RECEIPT_REQUIRED, {"header_row": 2, "values": {"receipt_type": {"Оплата": "income"}, "status": {"Завершена": "completed"}}})
            self.assertEqual((rows[0]["receipt_type"], rows[0]["status"]), ("income", "completed"))
            path.write_text("Report title\nentity_id;receipt_id;order_id;receipt_type;amount;currency;status\nx;p1;o1;income;100;RUB;completed\n", encoding="utf-8")
            self.assertEqual(read_table(path, RECEIPT_REQUIRED, {"header_row": 2})[0]["receipt_id"], "p1")

    def test_csv_formula_injection_is_escaped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.csv"
            write_csv(path, ["name"], [{"name": "=1+1"}, {"name": "-100.00"}])
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["name"], "'=1+1")
            self.assertEqual(rows[1]["name"], "-100.00")


if __name__ == "__main__":
    unittest.main()
