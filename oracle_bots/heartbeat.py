"""heartbeat.py -- shared functional-heartbeat writer for Oracle bots.
Mirrors the Windows-side Bot_Activeheartbeat.py (2026-08-06
rollout after the ema_adx_bot 8.5h silent-hang incident). Write ONLY after a
real, successfully-completed unit of work -- never on process start alone.
"""
import json
import os
from datetime import datetime, timezone

BOT_DIR = os.path.dirname(os.path.abspath(__file__))


def write_heartbeat(name, **extra):
    path = os.path.join(BOT_DIR, f'heartbeat_{name}.json')
    try:
        payload = {'last_success_ts': datetime.now(timezone.utc).isoformat()}
        payload.update(extra)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        pass


def read_heartbeat(name):
    path = os.path.join(BOT_DIR, f'heartbeat_{name}.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def classify(name, healthy_max_age_sec, degraded_max_age_sec, process_alive=True):
    data = read_heartbeat(name)
    if data is None:
        return 'Critical', f'{name}: no heartbeat file'
    last = datetime.fromisoformat(data['last_success_ts'])
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if not process_alive:
        return 'Critical', f'{name}: process not running (last success {age:.0f}s ago)'
    if age <= healthy_max_age_sec:
        return 'Healthy', f'{name}: last success {age:.0f}s ago'
    if age <= degraded_max_age_sec:
        return 'Degraded', f'{name}: last success {age:.0f}s ago'
    return 'Critical', f'{name}: last success {age:.0f}s ago -- stale'
