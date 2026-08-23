# Staging manifest — curated public copy of the trading-bot project

Prepared for a new PUBLIC repo under the siko15006-bot GitHub account. Every
file below was copied read-only from the live machine/Oracle server; nothing
original was modified or deleted. This manifest lists what's in, what was
redacted, and what was deliberately left out.

## Included: `docs/` (4 files)
`AGENTS.md`, `CLAUDE.md`, `PROJECT_GUARDRAILS.md`, `CURRENT_TASK.md` — the
Claude/Codex workflow rules and architecture reference. Source:
`C:\TradingBot\{AGENTS,CLAUDE,PROJECT_GUARDRAILS,CURRENT_TASK}.md`.
Reproduced by hand (not `cp`) with the Oracle server's IP address replaced by
"the Oracle cloud host" and absolute personal Windows paths trimmed to
relative paths where it didn't lose information. No secrets found in the
originals.

## Included: `ai_orchestrator/` (12 files)
Source: `C:\TradingBot\cloned_repo\ai_orchestrator\`. Copied: `README.md`,
`orchestrator.py`, `auto_worker.py`, `status.py`, `config.json`, and all 7
`research/**/*.md` reports. **Not** copied: `task_queue.json` (run history,
no lasting value), `worker_status.json`/`run_phase3.log` (ephemeral state),
`__pycache__/`, `test_workspace/`, `test_queue_patch_gate.json` (disposable
test scratch). No secrets found — `config.json`'s only "sensitive-looking"
content is local Windows absolute paths (`C:\Users\ahmed\...`), which are
machine paths, not credentials, and were left as-is.

## Included: `windows_bots/` (56 files)
Source: `C:\TradingBot\Bot_Active\*.py`, every `.py` file except the two
`*_BACKUP*` duplicates (`status_dashboard_BACKUP_before_mobile_ui.py`,
`participation_pilot_btc_range_STATIC_BACKUP_2.0x.py`). Not copied: `*.log`,
`*.db`, `*.json` state files, `.session` files, `__pycache__/`.

**Redactions made (literal secret values replaced with `<REDACTED_...>`
placeholders, code structure otherwise untouched):**
- MT5 login numbers for EA/EM/BA (3 distinct account numbers) — every
  occurrence, across 17 files.
- MT5 account passwords for EA/EM/BA (3 distinct passwords) — every
  occurrence, across the same 17 files.
- Oracle server IP address — 6 files.
- ntfy.sh push-notification topic (an unguessable topic name functions as a
  bearer token on ntfy.sh) — 2 files.
- Telegram API_ID / API_HASH (Telethon app credentials) — 3 files
  (`fx_signal_exec.py`, `tg_relogin_step1.py`, `tg_relogin_step2.py`).
- Ahmed's personal phone number — 2 files (`tg_relogin_step1.py`,
  `tg_relogin_step2.py`).

No Bybit/ccxt API keys were found on the Windows side (BAA trading happens
only through the Oracle server).

## Included: `oracle_bots/` (53 files)
Source: `/home/ubuntu/trading-bot/` on the Oracle server, pulled read-only
via SSH `cat` (nothing on the server was touched). Included the current
version of every active `.py` module and every `start_*.sh` /
`watchdog_alert.sh` launcher. Excluded: every `.bak*`/`.backup*`/`_backup*`/
`.pre_diag`/`_DEAD*` historical duplicate (~40+ files — `tg_signal_bot.py`
alone had 13), the six `crypto_bot_v3x`/`_backup` variants (kept only the
current `crypto_bot.py`), `scan_groups2.py` (superseded by `scan_groups3.py`),
the five one-off `patch_*.py` scripts (already applied, no ongoing value),
`.session`/`.session_DEAD*` files (live Telegram auth credentials —
excluded entirely, not even redacted, see below), `signals_all.json*`,
`research_data/`, `backups/`, `staging_tgfix/`, and three stray empty/junk
directories (`cp`, `echo`, `;`).

Most Oracle scripts already source Bybit/Telegram credentials from
environment variables (`os.environ`/`os.getenv`) — nothing to redact there.

**Redactions made:**
- `spot_bot.py` — hardcoded Bybit spot API key + secret (this one script,
  unlike every other Oracle bot, had them as literals instead of env vars).
- `webhook_server.py`, `tg_relogin_step1.py`, `tg_relogin_step2.py` — Oracle
  IP / Telegram API_ID / API_HASH / phone number (same values as the Windows
  side, since it's the same Telegram account).
- `ntfy_alert.py` — same ntfy.sh topic as the Windows copy.

**Excluded entirely, not even redacted (pure credential material):**
- `tg_session.session` and `tg_session_DEAD_20260704.session` — live
  Telethon session files; possessing one grants full access to the Telegram
  account, no code value to preserve.

## Verification
Ran a final full-tree grep across every staged file for: the 3 MT5
login/password pairs, the Oracle IP, the Telegram API_ID/API_HASH/phone, the
ntfy topic, the Bybit spot key/secret, and generic patterns
(`password=`, `apiKey:`, `secret:`, PEM/OpenSSH private-key headers). Zero
unredacted matches remain as of this manifest.

**Nothing here needs a second look before pushing** — every finding above
was either redacted or excluded, and the final grep pass confirmed it.
