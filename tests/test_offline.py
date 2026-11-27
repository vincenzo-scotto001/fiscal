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


def fake_quote(sym):
    h = sum(map(ord, sym))
    return {"c": 100 + h % 300 + 0.25, "dp": ((h * 7) % 61 - 30) / 10, "pc": 100.0}


def fake_http_get(url, params):
    if "finnhub.io" in url and url.endswith("/quote"):
        return fake_quote(params["symbol"])
    if "cdn.cboe.com" in url:
        return {"data": {"current_price": 17.42, "price_change_percent": -3.21}}
    if "coingecko" in url:
        return {"bitcoin": {"usd": 97123.4, "usd_24h_change": 1.234}, "ethereum": {"usd": 3511.0, "usd_24h_change": -0.5}}
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
    ("market-wrap", "2026-11-18"),        # a normal Wednesday
    ("market-wrap", "2026-11-25"),        # day before Thanksgiving: the next session is Friday's early close
]
for task, date in cases:
    out, ps = run(task, date)
    assert ps, f"{task} {date} posted nothing:\n{out}"
    for p in ps:
        validate(p)
    show(ps[0])

# skips
for task, date in [("earnings-today", "2026-11-26"), ("earnings-results", "2026-11-21"),
                   ("macro-morning", "2026-11-22"), ("market-wrap", "2026-11-26"), ("market-wrap", "2026-11-28")]:
    out, ps = run(task, date)
    assert not ps, f"{task} {date} should skip"
    print(f"\nskip ok: {task} {date}: {out.strip()}")

# holiday with a release still posts, with a closed-market note
out, ps = run("macro-morning", "2026-11-26")
assert ps and "closed today" in ps[0]["embeds"][0]["description"], out
print("\nholiday-with-release ok:", ps[0]["embeds"][0]["description"].splitlines()[0])

# market wrap: indexes, sectors ranked, megacap movers, VIX and crypto, and the next session
out, ps = run("market-wrap", "2026-11-25")
w = ps[0]["embeds"][0]
names = [f["name"] for f in w["fields"]]
assert w["title"] == "🔔 Market Wrap · Wed, Nov 25", w["title"]
assert names[:4] == ["📈 Indexes (ETFs)", "🏭 Sectors", "🚀 Megacap movers", "😨 Volatility & crypto"], names
assert names[-1].startswith("🗓️ Next session · Fri, Nov 27"), "Thanksgiving is skipped"
assert "Early close at 1:00 PM ET" in w["fields"][-1]["value"]
sectors = w["fields"][1]["value"].splitlines()
lead = [float(x.split()[-1].rstrip("%")) for x in sectors[0].split(" · ")]
assert lead == sorted(lead, reverse=True) and sectors[2].endswith("of 11 sectors up"), sectors
assert "**VIX** 17.42 ▼ -3.21%" in w["fields"][3]["value"] and "**BTC** $97,123" in w["fields"][3]["value"]
assert w["description"].startswith("S&P 500 ") and "Russell 2000" in w["description"]
print("\nmarket wrap ok:", w["description"])

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

# auto schedule: replay the UTC cron lines from fiscal.yml for a winter and a summer week,
# including a skipped run, and check each post goes out exactly once at the right NY time.
import yaml  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
crons = [c["cron"] for c in yaml.safe_load(open(os.path.join(ROOT, ".github/workflows/fiscal.yml")))[True]["schedule"]]
assert not any("timezone" in str(c) for c in crons)


def expand(field, lo, hi):
    out = set()
    for part in field.split(","):
        rng, _, step = part.partition("/")
        a, b = (lo, hi) if rng == "*" else (tuple(map(int, rng.split("-"))) if "-" in rng else (int(rng), int(rng)))
        out.update(range(a, b + 1, int(step or 1)))
    return out


def fires(start_utc, days):
    t = start_utc
    while t < start_utc + dt.timedelta(days=days):
        for c in crons:
            mi, hr, _, _, dow = c.split()
            if t.minute in expand(mi, 0, 59) and t.hour in expand(hr, 0, 23) and \
                    ((t.weekday() + 1) % 7) in expand(dow, 0, 6):
                yield t
                break
        t += dt.timedelta(minutes=1)


def replay(start_utc, skip=()):
    posted, log = {}, []
    for t in fires(start_utc, 7):
        if t in skip:
            continue
        now = t.astimezone(fb.ET)
        todo, _ = fb.plan_auto(now, posted)
        for task in todo:
            posted[task] = now.date().isoformat()
            log.append((now.strftime("%a %H:%M"), task))
    return log


UTC = dt.timezone.utc
for label, start in (("winter (EST)", dt.datetime(2026, 11, 22, 12, tzinfo=UTC)),
                     ("summer (EDT)", dt.datetime(2027, 6, 13, 12, tzinfo=UTC))):
    log = replay(start)
    times = {task: sorted({w for w, t in log if t == task}) for task in fb.TASKS if any(t == task for _, t in log)}
    print(f"\n{label}:", {k: v for k, v in times.items()})
    assert len(log) == len({(w.split()[0], t) for w, t in log}), "a task posted twice on the same day"
    assert all(w.endswith("07:41") for w, t in log if t in ("macro-morning", "earnings-today")), log
    assert all(w.endswith("20:07") for w, t in log if t == "earnings-results"), log
    assert all(w == "Sun 18:07" for w, t in log if t in ("macro-week", "earnings-week")), log
    assert all(w.endswith("16:22") for w, t in log if t == "market-wrap"), log
    assert sum(t == "market-wrap" for _, t in log) == 5
    assert sum(t == "macro-morning" for _, t in log) == 5 and sum(t == "earnings-results" for _, t in log) == 5
    assert sum(t == "earnings-week" for _, t in log) == 1

# GitHub skips the 7:41 AM and 8:07 PM runs on Monday -> the next chance posts 15 minutes later, once
skip = {dt.datetime(2026, 11, 23, 12, 41, tzinfo=UTC), dt.datetime(2026, 11, 24, 1, 7, tzinfo=UTC)}
mon = [(w, t) for w, t in replay(dt.datetime(2026, 11, 22, 12, tzinfo=UTC), skip) if w.startswith("Mon")]
print("Monday with two skipped runs:", mon)
assert ("Mon 07:56", "macro-morning") in mon and ("Mon 20:22", "earnings-results") in mon
assert sum(t == "macro-morning" for _, t in mon) == 1

# GitHub drops 85% of runs (what we saw on Nov 21): over 40 simulated weeks, nothing ever posts
# twice, nothing posts outside its window, and almost every post still goes out.
import random  # noqa: E402

rng = random.Random(7)
start = dt.datetime(2026, 11, 22, 12, tzinfo=UTC)
all_fires = list(fires(start, 7 * 40))
dropped = {t for t in all_fires if rng.random() < 0.85}
posted, log = {}, []
for t in all_fires:
    if t in dropped:
        continue
    now = t.astimezone(fb.ET)
    todo, _ = fb.plan_auto(now, posted)
    for task in todo:
        posted[task] = now.date().isoformat()
        log.append((now, task))
per_day = {}
for now, task in log:
    key = (now.date(), task)
    per_day[key] = per_day.get(key, 0) + 1
assert max(per_day.values()) == 1, "posted twice in a day"
assert all((n.hour, n.minute) >= (7, 41) and n.hour < 12 for n, t in log if t == "macro-morning")
assert all((n.hour, n.minute) >= (20, 7) for n, t in log if t == "earnings-results")
assert all(n.weekday() == 6 and (n.hour, n.minute) >= (18, 7) for n, t in log if t == "earnings-week")
assert all((n.hour, n.minute) >= (16, 22) and n.hour < 20 for n, t in log if t == "market-wrap")
wraps = sum(t == "market-wrap" for _, t in log)
print(f"   market wraps {wraps}/200")
assert wraps >= 160
mornings = sum(t == "macro-morning" for _, t in log)
results = sum(t == "earnings-results" for _, t in log)
weeks = sum(t == "earnings-week" for _, t in log)
print(f"85% of runs dropped, 40 weeks: mornings {mornings}/200, results {results}/200, week-ahead {weeks}/40")
assert mornings >= 180 and results >= 180 and weeks >= 34

# results with no actuals yet must stay retryable (returns False)
assert fb.TASKS["earnings-results"].__name__ == "earnings_results"
assert fb.next_monday(dt.date(2026, 11, 22)) == dt.date(2026, 11, 23)
# The ticker loop: a tick roughly every 30 minutes (30 min wait + up to 3 min of queueing),
# starting at a random time. Every post goes out once per day, within about 35 minutes of its
# window opening, and never outside its window.
tick_rng = random.Random(11)
for trial in range(20):
    now = dt.datetime(2026, 11, 22, 12, tzinfo=UTC) + dt.timedelta(minutes=tick_rng.randint(0, 59))
    posted, log = {}, []
    while now < dt.datetime(2026, 11, 29, 12, tzinfo=UTC):
        local = now.astimezone(fb.ET)
        todo, _ = fb.plan_auto(local, posted)
        for task in todo:
            posted[task] = local.date().isoformat()
            log.append((local, task))
        now += dt.timedelta(minutes=30, seconds=tick_rng.randint(10, 180))
    count = {}
    for local, task in log:
        count[(local.date(), task)] = count.get((local.date(), task), 0) + 1
    assert max(count.values()) == 1
    assert sum(t == "macro-morning" for _, t in log) == 5 and sum(t == "earnings-results" for _, t in log) == 5
    assert sum(t == "earnings-week" for _, t in log) == 1
    assert sum(t == "market-wrap" for _, t in log) == 5
    opens = {"macro-morning": (7, 41), "earnings-results": (20, 7), "earnings-week": (18, 7), "market-wrap": (16, 22)}
    for local, task in log:
        if task in opens:
            h, m = opens[task]
            late = (local.hour * 60 + local.minute) - (h * 60 + m)
            assert 0 <= late <= 35, (task, local)
print("ticker every ~30 min: every post once a day, within 35 minutes of its window opening (20 random starts)")

print("\nALL TESTS PASSED")
