# Execution Safety SL/TP Patch 2026-08-24

Scope: modify-position / modify-pending safety only. No new orders, no
strategy/risk/threshold/lot/SL/TP policy changes.

## Root Cause

- `control_api.py` sent modify requests without read-back verification.
- MCP `modify_pending_order` passed only supplied fields through to the generic
  order sender; omitted fields were not forced to broker-truth values.
- MCP `modify_position` preserved omitted fields before sending, but also had
  no read-back verification and allowed ambiguous zero values.
- In both surfaces, `0` could be interpreted as a protective-level removal
  without an explicit removal intent.

## Fix

- Omitted `stop_loss` preserves existing SL.
- Omitted `take_profit` preserves existing TP.
- `stop_loss=0` fails closed unless `remove_stop_loss=true`.
- `take_profit=0` fails closed unless `remove_take_profit=true`.
- After successful broker response, both paths read back SL/TP and fail closed
  with `critical=true` on mismatch.

## Patched Runtime Files

- `C:\TradingBot\Bot_Active\control_api.py`
- `C:\Users\ahmed\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\metatrader_client\order\modify_position.py`
- `C:\Users\ahmed\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\metatrader_client\order\modify_pending_order.py`
- `C:\Users\ahmed\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\metatrader_client\client_order.py`
- `C:\Users\ahmed\AppData\Local\Python\pythoncore-3.14-64\Lib\site-packages\metatrader_mcp\server.py`

## Backups

- `C:\TradingBot\Bot_Active\Backups\control_api_sltp_patch_20260824_232220.py`
- `C:\TradingBot\Bot_Active\Backups\mcp_modify_position_sltp_patch_20260824_232220.py`
- `C:\TradingBot\Bot_Active\Backups\mcp_modify_pending_order_sltp_patch_20260824_232220.py`

## Validation

- `py_compile` passed for patched runtime/control/MCP files.
- `python C:\TradingBot\Bot_Active\control_api.py --selfcheck` passed.
- `python tests\test_sltp_patch_semantics.py` passed: 6 tests.
- `C:\Users\ahmed\AppData\Local\Python\pythoncore-3.14-64\python.exe tests\test_sltp_patch_semantics.py` passed: 6 tests.

## Read-Only Current Protection

- EA: positions `0`, pending orders `2`, all pending orders have SL and TP.
- BA: positions `0`, pending orders `1`, all pending orders have SL and TP.

## Deploy

- `control_api` restarted from `control_api.bat`.
- Old execution MCP processes were stopped so stale modify semantics cannot
  remain active in already-running MCP servers.
- Read-only EM MCP processes were not stopped.
- Next fresh MCP launch loads the patched package signatures with
  `remove_stop_loss` and `remove_take_profit`.

## Rollback

1. Stop `control_api.py`.
2. Restore `control_api.py` from the backup above.
3. Restore MCP package files from the backups above.
4. Restart `control_api.bat`.
5. Start a fresh Claude/Codex MCP session.
