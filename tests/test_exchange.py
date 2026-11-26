"""核对业务方交换材料的格式和来源引用。"""
from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]


def source(name):
    return json.loads((ROOT / "inputs" / (name + ".json")).read_text(encoding="utf-8"))


class Exchanges(unittest.TestCase):
    def test_分送行和出库回执按实际目的地连接(self):
        order, shipping, requests = source("purchase"), source("shipping"), source("requests")
        lines = {row["id"]: row for row in order["lines"]}
        self.assertTrue(all(row["line"] in lines for row in shipping))
        self.assertNotEqual(order["billing_district"], lines["L02"]["district"])
        self.assertEqual(shipping[0]["instruction"], shipping[1]["instruction"])
        self.assertEqual({shipping[0]["status"], shipping[1]["status"]}, {"unknown", "not_picked"})
        self.assertEqual(requests["transfer"]["line"], shipping[1]["line"])
        self.assertEqual({item["shipment"] for item in requests["claims"]}, {shipping[2]["event"]})
        self.assertEqual(len({item["by"] for item in requests["claims"]}), 2)
