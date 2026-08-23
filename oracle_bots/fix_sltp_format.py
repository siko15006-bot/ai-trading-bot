"""
Fix SL/TP format bug in both bots.
Bybit v5 ignores dict format {'triggerPrice':...,'price':...} silently.
Correct format: 'stopLoss': str(price), 'slTriggerBy': 'MarkPrice'
"""
import re

def fix_sltp(code):
    # Pattern 1: {'triggerPrice': str(X), 'price': str(X)}
    # Replace stopLoss dict with plain string
    code = re.sub(
        r"'stopLoss':\s*\{'triggerPrice':\s*str\(([^)]+)\),\s*'price':\s*str\([^)]+\)\}",
        r"'stopLoss': str(round(float(\1), 4)), 'slTriggerBy': 'MarkPrice'",
        code
    )
    code = re.sub(
        r"'takeProfit':\s*\{'triggerPrice':\s*str\(([^)]+)\),\s*'price':\s*str\([^)]+\)\}",
        r"'takeProfit': str(round(float(\1), 4)), 'tpTriggerBy': 'MarkPrice'",
        code
    )
    # Pattern 2: {'triggerPrice': X, 'price': X} (no str())
    code = re.sub(
        r"'stopLoss':\s*\{'triggerPrice':\s*([^,}]+),\s*'price':\s*[^}]+\}",
        r"'stopLoss': str(round(float(\1), 4)), 'slTriggerBy': 'MarkPrice'",
        code
    )
    code = re.sub(
        r"'takeProfit':\s*\{'triggerPrice':\s*([^,}]+),\s*'price':\s*[^}]+\}",
        r"'takeProfit': str(round(float(\1), 4)), 'tpTriggerBy': 'MarkPrice'",
        code
    )
    return code

for fname, ver_old, ver_new in [
    ('crypto_bot.py', 'Crypto Bot v3.14', 'Crypto Bot v3.15'),
    ('scalp_bot.py',  'Scalp Bot v1.2',   'Scalp Bot v1.3'),
]:
    path = f'/home/ubuntu/trading-bot/{fname}'
    with open(path, 'r') as f:
        code = f.read()

    # Count bad patterns before
    bad_before = code.count("'triggerPrice'")

    code = code.replace(ver_old, ver_new)
    code = fix_sltp(code)

    bad_after = code.count("'triggerPrice'")

    with open(path, 'w') as f:
        f.write(code)

    print(f'{fname}: fixed {bad_before - bad_after} SL/TP dict(s), {bad_after} remaining → {ver_new}')

print('Done — SL/TP format fixed in both bots')
