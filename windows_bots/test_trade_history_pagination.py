"""
test_trade_history_pagination.py -- HTTP-level tests against the LIVE
dashboard process on :5001, real MT5-backed data. Not a unit-test-with-
mocks suite: status_dashboard.py starts a real background thread at
import time (line ~682), so importing it directly would spin up a second
polling loop against MT5/Oracle -- these tests instead hit the already-
running process over HTTP, same as the curl-based verification already
done for this feature, exercising the exact code path a real browser hits.

Requires the dashboard to be running on http://localhost:5001 (it already
is, per the standing operational routine). Run standalone:
    python test_trade_history_pagination.py
"""
import json
import urllib.request
import urllib.parse

BASE = "http://localhost:5001"


def _get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.status, json.loads(resp.read())


def _get_html(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_trades_page_http_200():
    status, _ = _get_html("/trades")
    assert status == 200


def test_page_1():
    status, d = _get("/api/trades", {"view": "all", "page": 1})
    assert status == 200
    assert d["page"] == 1
    assert d["showing_from"] == 1
    assert len(d["trades"]) == d["per_page"] or len(d["trades"]) == d["filtered_total"]


def test_page_2():
    status, d = _get("/api/trades", {"view": "all", "page": 2})
    assert status == 200
    assert d["page"] == 2
    assert d["showing_from"] == d["per_page"] + 1


def test_last_page():
    _, first = _get("/api/trades", {"view": "all", "page": 1})
    total_pages = first["total_pages"]
    status, last = _get("/api/trades", {"view": "all", "page": total_pages})
    assert status == 200
    assert last["page"] == total_pages
    assert last["showing_to"] == last["filtered_total"]
    # newest-first default -> the LAST page holds the OLDEST trades
    assert last["trades"], "last page must not be empty when filtered_total > 0"


def test_sort_oldest_first():
    status, d = _get("/api/trades", {"view": "all", "sort": "asc", "page": 1})
    assert status == 200
    assert d["sort"] == "asc"
    times = [t["exit_time"] for t in d["trades"]]
    assert times == sorted(times), "asc sort must be non-decreasing by exit_time"


def test_sort_newest_first():
    status, d = _get("/api/trades", {"view": "all", "sort": "desc", "page": 1})
    assert status == 200
    assert d["sort"] == "desc"
    times = [t["exit_time"] for t in d["trades"]]
    assert times == sorted(times, reverse=True), "desc sort must be non-increasing by exit_time"


def test_page_size_50():
    status, d = _get("/api/trades", {"view": "all", "per_page": 50, "page": 1})
    assert status == 200
    assert d["per_page"] == 50
    assert len(d["trades"]) <= 50


def test_page_size_500():
    status, d = _get("/api/trades", {"view": "all", "per_page": 500, "page": 1})
    assert status == 200
    assert d["per_page"] == 500
    assert len(d["trades"]) <= 500


def test_total_count():
    status, d = _get("/api/trades", {"view": "all"})
    assert status == 200
    assert d["grand_total"] > 0
    # grand_total is unfiltered -- must be >= what "all" (no filters) shows
    assert d["filtered_total"] == d["grand_total"]


def test_filtered_count():
    status, d = _get("/api/trades", {"view": "all", "account": "EA"})
    assert status == 200
    _, unfiltered = _get("/api/trades", {"view": "all"})
    assert d["filtered_total"] <= unfiltered["grand_total"]
    assert all(t["account"] == "EA" for t in d["trades"])


def test_filter_and_pagination():
    status, p1 = _get("/api/trades", {"view": "all", "account": "EA", "strategy": "London Breakout", "page": 1})
    assert status == 200
    assert all(t["account"] == "EA" and t["strategy"] == "London Breakout" for t in p1["trades"])
    if p1["total_pages"] > 1:
        _, p2 = _get("/api/trades", {"view": "all", "account": "EA", "strategy": "London Breakout", "page": 2})
        ids1 = {t["position_id"] for t in p1["trades"]}
        ids2 = {t["position_id"] for t in p2["trades"]}
        assert ids1.isdisjoint(ids2), "page 1 and page 2 must not overlap"


def test_empty_filter_no_crash():
    status, d = _get("/api/trades", {"view": "all", "symbol": "NONEXISTENT_SYMBOL_XYZ"})
    assert status == 200
    assert d["filtered_total"] == 0
    assert d["trades"] == []
    assert d["total_pages"] == 1


def test_historical_date_range():
    status, d = _get("/api/trades", {"view": "all", "date_from": "2026-02-10", "date_to": "2026-02-11"})
    assert status == 200
    assert d["date_from"] == "2026-02-10"
    assert d["date_to"] == "2026-02-11"
    for t in d["trades"]:
        assert t["exit_time"] >= "2026-02-10" and t["exit_time"] < "2026-02-12"


def test_no_duplicate_position_ids_across_pages():
    seen = set()
    _, first = _get("/api/trades", {"view": "all", "per_page": 100, "page": 1})
    pages_to_check = min(3, first["total_pages"])
    for p in range(1, pages_to_check + 1):
        _, d = _get("/api/trades", {"view": "all", "per_page": 100, "page": p})
        ids = [t["position_id"] for t in d["trades"]]
        assert not (seen & set(ids)), f"duplicate position_id across pages at page {p}"
        seen.update(ids)


def test_invalid_page():
    status, d = _get("/api/trades", {"view": "all", "page": 999999})
    assert status == 200
    assert d["page"] == d["total_pages"], "out-of-range page must clamp to last page, not 500"

    status2, d2 = _get("/api/trades", {"view": "all", "page": -5})
    assert status2 == 200
    assert d2["page"] == 1, "negative page must clamp to 1, not 500"

    status3, d3 = _get("/api/trades", {"view": "all", "page": "abc"})
    assert status3 == 200
    assert d3["page"] == 1, "non-numeric page must fall back to default, not 500"


def test_invalid_page_size():
    status, d = _get("/api/trades", {"view": "all", "per_page": 13})
    assert status == 200
    assert d["per_page"] == 100, "out-of-whitelist per_page must fall back to the default, not 500"

    status2, d2 = _get("/api/trades", {"view": "all", "per_page": -1})
    assert status2 == 200
    assert d2["per_page"] == 100


if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items()
                    if name.startswith("test_") and callable(fn))
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"PASS {name}")
        except Exception as e:
            failed.append(name)
            print(f"FAIL {name}: {e}")
    print(f"\n{passed}/{len(tests)} PASSED")
    if failed:
        print("FAILED:", ", ".join(failed))
        raise SystemExit(1)
