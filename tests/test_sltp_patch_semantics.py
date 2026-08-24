"""Regression checks for MT5 modify PATCH semantics.

Uses stdlib unittest/mocks only. It never connects to MT5 and never sends a
real order.
"""
import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MCP_SITE = Path.home() / "AppData/Local/Python/pythoncore-3.14-64/Lib/site-packages"
if MCP_SITE.exists():
    sys.path.insert(0, str(MCP_SITE))

mp = importlib.import_module("metatrader_client.order.modify_position")
mpo = importlib.import_module("metatrader_client.order.modify_pending_order")


class Rows:
    def __init__(self, rows):
        self._rows = rows
        self.index = SimpleNamespace(size=len(rows))
        self.shape = (len(rows),)
        self.iloc = self

    def __getitem__(self, idx):
        return self._rows[idx]


def rows(*items):
    return Rows(list(items))


class SltpPatchSemanticsTest(unittest.TestCase):
    def test_modify_position_sl_only_preserves_tp(self):
        sent = {}
        reads = iter([
            rows({"stop_loss": 1.0, "take_profit": 2.0}),
            rows({"stop_loss": 1.1, "take_profit": 2.0}),
        ])
        with patch.object(mp, "get_positions_by_id", lambda *_: next(reads)), \
             patch.object(mp, "send_order", lambda _c, **kw: sent.update(kw) or {"success": True, "data": None}):
            result = mp.modify_position(None, 123, stop_loss=1.1)

        self.assertFalse(result["error"])
        self.assertEqual(sent["stop_loss"], 1.1)
        self.assertEqual(sent["take_profit"], 2.0)
        self.assertEqual(result["data"]["read_back"], {"stop_loss": 1.1, "take_profit": 2.0})

    def test_modify_position_tp_only_preserves_sl(self):
        sent = {}
        reads = iter([
            rows({"stop_loss": 1.0, "take_profit": 2.0}),
            rows({"stop_loss": 1.0, "take_profit": 2.2}),
        ])
        with patch.object(mp, "get_positions_by_id", lambda *_: next(reads)), \
             patch.object(mp, "send_order", lambda _c, **kw: sent.update(kw) or {"success": True, "data": None}):
            result = mp.modify_position(None, 123, take_profit=2.2)

        self.assertFalse(result["error"])
        self.assertEqual(sent["stop_loss"], 1.0)
        self.assertEqual(sent["take_profit"], 2.2)

    def test_modify_pending_both_and_explicit_remove(self):
        sent = {}
        reads = iter([
            rows({"open": 10.0, "stop_loss": 9.0, "take_profit": 12.0}),
            rows({"open": 10.5, "stop_loss": 0.0, "take_profit": 13.0}),
        ])
        with patch.object(mpo, "get_pending_orders", lambda *_args, **_kw: next(reads)), \
             patch.object(mpo, "send_order", lambda _c, **kw: sent.update(kw) or {"success": True, "data": None}):
            result = mpo.modify_pending_order(None, id=456, price=10.5, take_profit=13.0, remove_stop_loss=True)

        self.assertFalse(result["error"])
        self.assertEqual(sent["price"], 10.5)
        self.assertEqual(sent["stop_loss"], 0.0)
        self.assertEqual(sent["take_profit"], 13.0)

    def test_zero_without_explicit_remove_fails_closed(self):
        with patch.object(mp, "get_positions_by_id", lambda *_: rows({"stop_loss": 1.0, "take_profit": 2.0})):
            result = mp.modify_position(None, 123, stop_loss=0)

        self.assertTrue(result["error"])
        self.assertIn("requires explicit", result["message"])

    def test_partial_broker_response_fails_closed(self):
        reads = iter([
            rows({"stop_loss": 1.0, "take_profit": 2.0}),
            rows({"stop_loss": 1.1, "take_profit": 0.0}),
        ])
        with patch.object(mp, "get_positions_by_id", lambda *_: next(reads)), \
             patch.object(mp, "send_order", lambda _c, **_kw: {"success": True, "data": None}):
            result = mp.modify_position(None, 123, stop_loss=1.1)

        self.assertTrue(result["error"])
        self.assertTrue(result["critical"])
        self.assertIn("read-back mismatch", result["message"])

    def test_failed_broker_response_fails_closed(self):
        with patch.object(mp, "get_positions_by_id", lambda *_: rows({"stop_loss": 1.0, "take_profit": 2.0})), \
             patch.object(mp, "send_order", lambda _c, **_kw: {"success": False, "message": "broker rejected", "data": None}):
            result = mp.modify_position(None, 123, take_profit=2.2)

        self.assertEqual(result, {"error": True, "message": "broker rejected", "data": None})


if __name__ == "__main__":
    unittest.main()
