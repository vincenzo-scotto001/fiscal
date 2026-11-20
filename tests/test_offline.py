"""Offline test: fake Finnhub/FRED responses, run every task in dry-run, check Discord limits.

Run from the repo root:  python tests/test_offline.py   (needs only `requests` installed)
"""
import datetime as dt
import io
import json
import os
import random
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fiscal_bot as fb  # noqa: E402

os.environ.update(FINNHUB_API_KEY="x", FRED_API_KEY="y")
random.seed(7)

BIG = [  # symbol, hour, eps est, rev est
    ("NVDA", "amc", 1.25, 55.2e9), ("TGT", "bmo", 1.70, 25.9e9), ("LOW", "bmo", 2.95, 20.8e9),
    ("TJX", "bmo", 1.26, 15.1e9), ("PANW", "amc", 0.89, 2.5e9), ("SNOW", "amc", 0.31, 1.18e9),
    ("WSM", "bmo", 1.80, 1.85e9), ("ZM", "amc", 1.43, 1.2e9), ("BBY", "bmo", 1.30, 9.6e9),
    ("DELL", "amc", 2.45, 27.0e9), ("DE", "bmo", 3.90, 9.4e9), ("ADI", "bmo", 2.20, 3.0e9),
]
NEG = ("RIVN", "amc", -0.65, 1.5e9)


def finnhub_rows(day, many=False):
    rows = []
    for sym, hour, e, r in BIG + [NEG]:
        rows.append({"date": day, "symbol": sym, "hour": hour, "epsEstimate": e, "revenueEstimate": r,
                     "epsActual": None, "revenueActual": None, "quarter": 3, "year": 2026})
    for i in range(60):  # small caps that should be filtered out
        rows.append({"date": day, "symbol": f"SM{i}", "hour": "", "epsEstimate": 0.01,
                     "revenueEstimate": 5e7, "epsActual": None, "revenueActual": None})
    rows.append({"date": day, "symbol": "NVDA", "hour": "amc", "epsEstimate": 1.25, "revenueEstimate": 55.2e9})  # dup
    rows.append({"date": day, "symbol": "NOREV", "hour": "bmo", "epsEstimate": 0.5, "revenueEstimate": None})
    if many:  # stress test: 200 big reporters in one day
        for i in range(200):
            rows.append({"date": day, "symbol": f"BIG{i}", "hour": random.choice(["bmo", "amc", "dmh", ""]),
                         "epsEstimate": 1.0, "revenueEstimate": 2e9 + i * 1e7,
                         "epsActual": 1.1 if i % 2 else None, "revenueActual": 2.1e9 if i % 2 else None})
    # give some actuals
    for r in rows:
        if r["symbol"] in ("TGT", "LOW", "TJX", "NVDA", "RIVN", "BBY"):
            r["epsActual"] = round(r["epsEstimate"] * random.choice([1.05, 0.93, 1.0]), 2)
            r["revenueActual"] = r["revenueEstimate"] * random.choice([1.01, 0.99])
    return rows


FRED_NAMES = [
    ("Consumer Price Index", "2026-12-10"), ("Employment Situation", "2026-12-04"),
    ("Unemployment Insurance Weekly Claims Report", None), ("Gross Domestic Product", "2026-11-25"),
    ("Gross Domestic Product by State", "2026-11-25"), ("Personal Income and Outlays", "2026-11-25"),
    ("Manufacturer's Shipments, Inventories, and Orders (M3) Survey", "2026-11-25"),
    ("Manufacturer's Shipments, Inventories, and Orders (M3) Survey", "2026-12-03"),
    ("ADP National Employment Report", "2026-12-02"), ("New Residential Sales", "2026-11-24"),
    ("State Unemployment Insurance Weekly Claims Report", "2026-11-25"),
    ("Gross Domestic Product by County", "2026-11-25"), ("Personal Income by County", "2026-11-25"),
    ("Consumer Price Index Excluding Energy Commodities", "2026-12-10"), ("Existing Home Sales", "2026-11-23"),
    ("Surveys of Consumers", "2026-11-25"), ("New Residential Construction", "2026-11-19"),
    ("G.17 Industrial Production and Capacity Utilization", "2026-11-17"),
    ("H.15 Selected Interest Rates", None), ("Producer Price Index", "2026-11-24"),
    ("Job Openings and Labor Turnover Survey", "2026-12-08"),
    ("Advance Monthly Sales for Retail and Food Services", "2026-11-18"),
]


def fred_release_rows(start):
    rows = []
    s = dt.date.fromisoformat(start)
    for i in range(40):
        d = s + dt.timedelta(days=i)
        for name, fixed in FRED_NAMES:
            if fixed == d.isoformat() or (fixed is None and name.startswith("H.15") and d.weekday() < 5) or \
                    (fixed is None and name.startswith("Unemployment") and d.weekday() == 3):
                rows.append({"release_id": hash(name) % 500, "release_name": name, "date": d.isoformat()})
    return rows


SERIES = {"SP500": 6800.0, "NASDAQCOM": 22900.0, "DJIA": 47000.0, "VIXCLS": 17.2, "DGS2": 3.55, "DGS10": 4.10}


def fake_http_get(url, params):
    if "finnhub.io" in url:
        return {"earningsCalendar": finnhub_rows(params["from"], many=STRESS)}
    if url.endswith("releases/dates"):
        rows = fred_release_rows(params["realtime_start"])
        off = params.get("offset", 0)
        return {"release_dates": rows[off:off + params["limit"]]}
    if url.endswith("series/observations"):
        end = dt.date.fromisoformat(params["observation_end"])
        obs, d, base = [], end, SERIES[params["series_id"]]
        while len(obs) < 15:
            if d.weekday() < 5:
                val = "." if (params["series_id"] == "DGS10" and len(obs) == 0) else f"{base + len(obs) * 3.1:.2f}"
                obs.append({"date": d.isoformat(), "value": val})
            d -= dt.timedelta(days=1)
        return {"observations": obs}
    raise AssertionError(url)


fb.http_get = fake_http_get
fb.DRY_RUN = True
STRESS = False


def run(task, date):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fb.TASKS[task](dt.date.fromisoformat(date))
    out = buf.getvalue()
    payloads = []
    for chunk in out.split("--- DRY RUN")[1:]:
        payloads.append(json.loads(chunk[chunk.index("{"):]))
    return out, payloads


def validate(p):
    assert p["allowed_mentions"] == {"parse": []}
    for e in p["embeds"]:
        assert len(e["title"]) <= 256
        assert len(e.get("description", "")) <= 4096
        assert len(e.get("fields", [])) <= 25
        for f in e.get("fields", []):
            assert 0 < len(f["name"]) <= 256, f["name"]
            assert 0 < len(f["value"]) <= 1024, (f["name"], len(f["value"]))
        assert fb.embed_size(e) <= 6000, fb.embed_size(e)


def show(p):
    e = p["embeds"][0]
    print(f"\n## {e['title']}   [{fb.embed_size(e)} chars, {len(e.get('fields', []))} fields]")
    if e.get("description"):
        print(e["description"])
    for f in e.get("fields", []):
        print(f"[{f['name']}]\n{f['value']}")
    print(f"-- {e['footer']['text']}")


cases = [
    ("earnings-week", "2026-11-22"),      # Sunday before Thanksgiving week
    ("macro-week", "2026-11-22"),
    ("earnings-today", "2026-11-18"),
    ("earnings-results", "2026-11-18"),
    ("macro-morning", "2026-11-18"),
    ("macro-morning", "2026-11-23"),      # Monday
    ("macro-week", "2026-12-06"),         # FOMC week
]
for task, date in cases:
    out, ps = run(task, date)
    assert ps, f"{task} {date} posted nothing:\n{out}"
    for p in ps:
        validate(p)
    show(ps[0])

# skips
for task, date in [("earnings-today", "2026-11-26"), ("earnings-results", "2026-11-21"),
                   ("macro-morning", "2026-11-22")]:
    out, ps = run(task, date)
    assert not ps, f"{task} {date} should skip"
    print(f"\nskip ok: {task} {date}: {out.strip()}")

# holiday with a release still posts, with a closed-market note
out, ps = run("macro-morning", "2026-11-26")
assert ps and "closed today" in ps[0]["embeds"][0]["description"], out
print("\nholiday-with-release ok:", ps[0]["embeds"][0]["description"].splitlines()[0])

# FOMC minutes 3 weeks after Oct 28 decision -> Nov 18
assert any("minutes" in e[1] for e in fb.fed_events(dt.date(2026, 11, 18)))
assert any("rate decision" in e[1] for e in fb.fed_events(dt.date(2026, 12, 9)))

# stress test
STRESS = True
for task in ("earnings-today", "earnings-results", "earnings-week"):
    out, ps = run(task, "2026-11-18" if task != "earnings-week" else "2026-11-22")
    for p in ps:
        validate(p)
    e = ps[0]["embeds"][0]
    print(f"\nstress ok: {task}: {fb.embed_size(e)} chars, {len(e.get('fields', []))} fields")
STRESS = False

# auto schedule mapping
ET = fb.ET
assert fb.auto_tasks(dt.datetime(2026, 11, 22, 18, 7, tzinfo=ET)) == ["macro-week", "earnings-week"]
assert fb.auto_tasks(dt.datetime(2026, 11, 23, 7, 41, tzinfo=ET)) == ["macro-morning", "earnings-today"]
assert fb.auto_tasks(dt.datetime(2026, 11, 23, 8, 40, tzinfo=ET)) == ["macro-morning", "earnings-today"]
assert fb.auto_tasks(dt.datetime(2026, 11, 23, 20, 7, tzinfo=ET)) == ["earnings-results"]
assert fb.auto_tasks(dt.datetime(2026, 11, 21, 9, 0, tzinfo=ET)) == []
assert fb.next_monday(dt.date(2026, 11, 22)) == dt.date(2026, 11, 23)
assert fb.next_monday(dt.date(2026, 11, 23)) == dt.date(2026, 11, 30)
m = fb.match_release
assert m("Manufacturer's Shipments, Inventories, and Orders (M3) Survey", "2026-11-25")[0] == "Durable Goods Orders"
assert m("Manufacturer's Shipments, Inventories, and Orders (M3) Survey", "2026-12-03")[0] == "Factory Orders"
assert m("ADP National Employment Report", "2026-12-02")[0] == "ADP Private Payrolls"
assert m("State Unemployment Insurance Weekly Claims Report") is None
assert m("Gross Domestic Product by County") is None and m("Personal Income by County") is None
assert m("Debt to Gross Domestic Product Ratios") is None
assert m("Consumer Price Index Excluding Energy Commodities") is None
assert m("Research Consumer Price Index") is None
assert m("Consumer Price Index", "2026-12-10")[0] == "CPI Inflation"
assert m("Manufacturing Business Outlook Survey")[0] == "Philly Fed Manufacturing"
print("\nALL TESTS PASSED")
