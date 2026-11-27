#!/usr/bin/env python3
"""Offline tests for analysis_bot.py: indicators, levels, options positioning, charts, the rules
check on the AI write-up, the Discord message, the /analysis request flow and the schedule.
Uses synthetic data (tests/analysis_fixtures.py); nothing touches the network.

Run: python tests/test_analysis_offline.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import analysis_bot as ab  # noqa: E402
import analysis_fixtures as fx  # noqa: E402

NOW = dt.datetime(2026, 11, 24, 9, 0, tzinfo=ab.ET)   # a Tuesday
PNG = b"\x89PNG\r\n\x1a\n"


def bars_from(rows):
    return ab.make_bars([(dt.date.fromisoformat(r["date"]), r["open"], r["high"], r["low"], r["close"], r["volume"])
                         for r in rows])


def use_fixtures(index_rows=None, stock_rows=None, spy_rows=None):
    """Point the data sources at synthetic data."""
    index_rows = index_rows or fx.price_rows(NOW.date() - dt.timedelta(days=1), n=1300, start=4300)
    stock_rows = stock_rows or fx.price_rows(NOW.date() - dt.timedelta(days=1), n=600, start=120, drift=0.0012,
                                             vol=0.025, volume=2.5e8, seed=3)
    spy_rows = spy_rows or fx.price_rows(NOW.date() - dt.timedelta(days=1), n=600, start=500, seed=5)
    spx_spot, nvda_spot = index_rows[-1]["close"], stock_rows[-1]["close"]
    chains = {"SPX": fx.chain(spx_spot, NOW.date()),
              "NVDA": fx.chain(nvda_spot, NOW.date(), root="NVDA", monthly_root="NVDA", step=2.5, width=0.3)}

    def history(sym):
        return bars_from({"SPX": index_rows, "NDX": index_rows, "NVDA": stock_rows, "SPY": spy_rows}[sym])

    def chain(sym):
        if sym not in chains:
            raise ab.Unavailable(f"no chain for {sym}")
        c = chains[sym]
        return c["data"]["current_price"], c["timestamp"], c["data"]["options"]

    ab.PRICE_SOURCES = [("Cboe", history)]
    ab.cboe_chain = chain
    ab.fundamentals = lambda sym, today: (ab.INDEXES.get(sym, "NVIDIA Corp"), None if ab.is_index(sym) else
                                          {"industry": "Semiconductors", "peTTM": 54.12, "nextEarnings": "2027-02-24"})
    return spx_spot, nvda_spot, chains


def test_indicators():
    flat = np.full(60, 100.0)
    assert np.allclose(ab.ema(flat, 21)[20:], 100.0)
    assert np.isnan(ab.ema(flat, 21)[19])
    assert np.allclose(ab.sma(np.arange(1, 6, dtype=float), 5)[-1], 3.0)
    rising = np.arange(1, 60, dtype=float)
    assert ab.rsi(rising)[-1] == 100.0
    m, s, h = ab.macd(flat)
    assert abs(m[-1]) < 1e-9 and abs(h[-1]) < 1e-9
    zigzag = 100 + np.sin(np.arange(200) / 3) * 5
    r = ab.rsi(zigzag)
    assert 0 < np.nanmin(r) and np.nanmax(r) < 100
    # squeeze: a long quiet stretch after a volatile one turns the squeeze on
    rng = np.random.default_rng(1)
    c = np.concatenate([100 + rng.normal(0, 3, 80).cumsum() * 0.2, 100 + rng.normal(0, 0.05, 40)])
    on, mom = ab.ttm_squeeze(c + 0.5, c - 0.5, c)
    assert on[-1], "tight range should register as a squeeze"
    state = ab.squeeze_state(on, mom)
    assert state["state"].startswith("squeeze on") and state["bars"] >= 5
    print("ok indicators")


def test_levels():
    days = fx.trading_days(dt.date(2026, 11, 20), 140)
    # up from 100 to 200, then a pullback: an up-swing
    closes = list(np.linspace(100, 200, 100)) + list(np.linspace(200, 170, 40))
    b = ab.make_bars([(d, c, c + 1, c - 1, c, 0) for d, c in zip(days, closes)])
    fib = ab.fibonacci(b)
    assert fib["direction"] == "up-swing"
    hi, lo = fib["swingHigh"]["price"], fib["swingLow"]["price"]
    assert abs(fib["retracements"]["0.618"] - (hi - 0.618 * (hi - lo))) < 0.01
    assert abs(fib["extensions"]["161.8%"] - (lo + 1.618 * (hi - lo))) < 0.01
    # falling highs make a resistance line; the line sits above every later swing high
    wave = 150 - np.arange(140) * 0.3 + np.sin(np.arange(140) / 4) * 6
    b2 = ab.make_bars([(d, c, c + 1, c - 1, c, 0) for d, c in zip(days, wave)])
    tl = ab.trendlines(b2)
    assert "resistance" in tl and tl["resistance"]["slope"] < 0
    highs, _ = ab.swings(b2.h, b2.l)
    a0, m0 = tl["resistance"]["startIndex"], tl["resistance"]["slope"]
    for j in highs:
        if j > a0:
            assert b2.h[j] <= tl["resistance"]["startValue"] + m0 * (j - a0) + 1e-6
    p = ab.pivots(110, 90, 100)
    assert p["P"] == 100 and p["R1"] == 110 and p["S1"] == 90
    vp = ab.volume_profile(ab.make_bars([(d, 100, 101 + (i % 3), 99, 100, 1e6) for i, d in enumerate(days)]))
    assert vp and vp["valueAreaLow"] <= vp["poc"] <= vp["valueAreaHigh"]
    print("ok levels")


def test_events():
    assert ab.monthly_opex(dt.date(2026, 11, 24)) == dt.date(2026, 12, 18)
    assert ab.monthly_opex(dt.date(2026, 12, 19)) == dt.date(2027, 1, 15)
    assert ab.quarter_end(dt.date(2026, 11, 24)) == dt.date(2026, 12, 31)
    assert ab.quarter_end(dt.date(2027, 1, 2)) == dt.date(2027, 3, 31)
    saved = dict(ab.fb.MARKET_HOLIDAYS)
    ab.fb.MARKET_HOLIDAYS["2027-01-15"] = "test holiday"
    try:
        assert ab.monthly_opex(dt.date(2027, 1, 2)) == dt.date(2027, 1, 14), "OpEx moves to Thursday on a holiday"
    finally:
        ab.fb.MARKET_HOLIDAYS.clear()
        ab.fb.MARKET_HOLIDAYS.update(saved)
    ev = ab.events(dt.date(2026, 11, 24), {"nextEarnings": "2027-02-24"})
    assert ev["nextMonthlyOpex"] == "2026-12-18" and ev["nextFomcDecision"] == "2026-12-09"
    assert any("Thanksgiving" in h for h in ev["marketHolidaysThisWeek"])
    print("ok events")


def test_positioning():
    spot = 5700.0
    c = fx.chain(spot, NOW.date())
    ch = ab.parse_chain("SPX", c["data"]["options"], NOW.date())
    assert len(ch["exp"]) > 1000 and set(np.unique(ch["call"])) == {False, True}
    pos = ab.positioning("SPX", spot, ch, NOW, c["timestamp"])
    d = pos["data"]
    assert d["callWall"] >= spot and d["putWall"] <= spot
    assert d["expiriesIncluded"][-1] == "2026-12-18", "through the next monthly OpEx"
    assert d["totals"]["callGammaUSD"].startswith("$") and d["totals"]["netGammaUSD"][0] in "+-"
    assert 0.5 < d["totals"]["putCallOI"] < 2
    em = d["expectedMove"]["monthlyOpex"]
    assert em["range"][0] < spot < em["range"][1] and 0 < em["movePct"] < 15
    assert d["gammaFlip"] is None or spot * 0.9 <= d["gammaFlip"] <= spot * 1.1
    assert d["maxPainMonthly"] and spot * 0.8 <= d["maxPainMonthly"] <= spot * 1.2
    assert all(row["callOI"] >= 0 for row in d["byExpiry"])
    # unknown roots (adjusted options) are ignored
    junk = [{"option": "SPXQ261218C05700000", "open_interest": 1e9, "gamma": 1}]
    assert len(ab.parse_chain("SPX", junk, NOW.date())["exp"]) == 0
    print("ok positioning")


def test_build_and_charts():
    spx_spot, _, chains = use_fixtures()
    a = ab.build("SPX", NOW, None)
    assert [n for n, _ in a["images"]] == ["chart.png", "positioning.png"]
    assert all(img.startswith(PNG) and len(img) > 20_000 for _, img in a["images"])
    raw = json.dumps(a["data"])
    assert len(raw) < 30_000, f"DATA too big for a cheap prompt ({len(raw)} chars)"
    for key in ("price", "trend", "daily", "weekly", "fibonacci", "pivots", "positioning", "events"):
        assert key in a["data"], key
    assert a["data"]["positioning"]["spot"] == round(spx_spot, 2)
    assert "relativeStrengthVsSPY" not in a["data"], "no relative strength for the index itself"
    s = ab.build("$nvda", NOW, None)
    assert s["symbol"] == "NVDA" and "relativeStrengthVsSPY" in s["data"] and "volumeProfile6m" in s["data"]
    assert s["data"]["fundamentals"]["peTTM"] == 54.12 and s["data"]["events"]["nextEarnings"] == "2027-02-24"
    # no readable chain for the index or its ETF: the post still has IMG 1
    a2 = ab.build("NDX", NOW, None)
    assert "positioning" not in a2["data"] and len(a2["images"]) == 1
    # the ETF's chain stands in for the index's, and the data says so
    chains["QQQ"] = fx.chain(500.0, NOW.date(), root="QQQ", monthly_root="QQQ", step=1.0, width=0.2)
    a3 = ab.build("NDX", NOW, None)
    pos = a3["data"]["positioning"]
    assert pos["symbol"] == "QQQ" and pos["proxyFor"] == "NDX" and "QQQ options stand in for NDX" in pos["note"]
    assert len(a3["images"]) == 2
    print("ok build + charts")


def write(**over):
    base = {"title": "S&P 500 INDEX (SPX): TECHNICALS + INSTITUTIONAL POSITIONING",
            "img1": "SPX is under the EMA 21 at 4,697 and the Fib 0.236 at 4,656 is the KEY level.",
            "img2": "The call wall sits at 4,600 and the put wall at 4,500; dealers are estimated long gamma.",
            "fundamentals": "",
            "levels": ["Reclaim 4,656 and the Fib 0.382 at 4,794 opens up.", "Lose 4,500 and the 4,433 swing low is in play."],
            "conclusion": "Into OpEx (Dec 18) the bias stays BEARISH under 4,697; the quarter-end roll is the next big date."}
    base.update(over)
    return base


def test_rules():
    assert ab.rule_problems(write()) == []
    bad = {"PT 6200 by year end": "gave a price target", "BTO SPX 6000 calls": "wrote a trade order (BTO/STO)",
           "I am buying the dip": "wrote in the first person", "You should buy here.": "told readers to trade",
           "SPX will hit 6,200.": "made a firm prediction", "@here big move": "pinged @here or @everyone",
           "This is guaranteed.": "used certainty", "Our calls are working.": "described its own positions"}
    for text, why in bad.items():
        assert why in ab.rule_problems(write(conclusion=text)), text
    assert ab.rule_problems(write(img1="IMG 1 shows a breakout; In the weekly view the trend holds.")) == []
    data = {"a": 4697.12, "b": "Fib 0.236 at 4,656.33", "c": [4600.0, 4500.0], "d": {"4433.08": 1, "x": 4794.44}}
    assert ab.unverified_numbers(write(), data) == []
    assert ab.unverified_numbers(write(levels=["Above 4,999 the 5,321 level is next."]), data) == ["4,999", "5,321"]
    assert ab.unverified_numbers(write(conclusion="On Dec 18 and in 2027 things change."), data) == []
    print("ok rules")


def test_write_up_retries():
    use_fixtures()
    a = ab.build("SPX", NOW, None)
    drafts = [write(conclusion="End of year PT 6,200."), write()]
    calls = []
    saved = ab.openai_json
    ab.openai_json = lambda instructions, text: calls.append(text) or drafts[len(calls) - 1]
    try:
        out = ab.write_up(a)
        assert len(calls) == 2 and "gave a price target" in calls[1] and out == drafts[1]
        assert "DATA (computed by the bot" in calls[0] and "IMG 2" in calls[0]
        drafts[:] = [write(conclusion="PT 6,200"), write(conclusion="PT 6,300")]
        calls.clear()
        try:
            ab.write_up(a)
            raise AssertionError("should refuse a write-up that breaks the rules twice")
        except RuntimeError as exc:
            assert "broke the rules twice" in str(exc)
    finally:
        ab.openai_json = saved
    print("ok write-up retries")


def test_openai_request():
    seen = {}

    class R:
        status_code = 200
        content = b"x"

        def json(self):
            return {"status": "completed", "model": "gpt-6.2-sol", "usage": {"input_tokens": 5000, "output_tokens": 2000},
                    "output": [{"type": "reasoning"}, {"type": "message", "content": [
                        {"type": "output_text", "text": json.dumps(write())}]}]}

    def post(url, json=None, timeout=None, headers=None):
        seen.update(url=url, body=json, headers=headers)
        return R()

    saved = ab.requests.post
    ab.requests.post = post
    os.environ["OPENAI_API_KEY"] = "sk-test"
    try:
        out = ab.openai_json(ab.INSTRUCTIONS, "hello")
    finally:
        ab.requests.post = saved
    assert out == write()
    b = seen["body"]
    assert seen["url"] == "https://api.openai.com/v1/responses" and b["model"] == "gpt-6.2-sol"
    assert b["store"] is False and b["text"]["format"]["strict"] is True and b["reasoning"]["effort"] == "medium"
    assert set(b["text"]["format"]["schema"]["required"]) == {"title", "img1", "img2", "fundamentals", "levels", "conclusion"}
    assert seen["headers"]["Authorization"] == "Bearer sk-test" and "sk-test" not in json.dumps(b)
    print("ok OpenAI request")


def test_message():
    use_fixtures()
    a = ab.build("SPX", NOW, None)
    long = write(img1="x " * 3000, levels=["level " * 80] * 8)
    payload, files = ab.message(a, long, "2 of 3 analyses left today", "Requested by <@222>")
    e1, e2 = payload["embeds"]
    assert len(e1["description"]) <= 4096 and len(e1["title"]) <= 256
    assert sum(len(json.dumps(e)) for e in payload["embeds"]) < 6000
    assert e1["image"]["url"] == "attachment://chart.png" and e2["image"]["url"] == "attachment://positioning.png"
    assert payload["attachments"] == [{"id": 0, "filename": "chart.png"}, {"id": 1, "filename": "positioning.png"}]
    assert payload["allowed_mentions"] == {"parse": []} and payload["content"] == "Requested by <@222>"
    assert re.match(r"^Data: Cboe \(delayed\) · .+ ET · AI-generated · Not financial advice · 2 of 3 analyses left today$",
                    e1["footer"]["text"]), e1["footer"]["text"]
    p2, _ = ab.message(a, write(), "Weekly outlook")
    d = p2["embeds"][0]["description"]
    assert d.startswith("**IMG 1 · Technicals:**") and "**IMG 2 · Options positioning:**" in d
    assert "**Key levels:**\n• Reclaim 4,656" in d and d.rstrip().endswith("is the next big date.")
    assert "**Fundamentals:**" not in d
    print("ok message")


class FakeKV:
    ok = True

    def __init__(self):
        self.data, self.deleted = {}, []

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value, ttl=None):
        self.data[key] = value

    def delete(self, key):
        self.deleted.append(key)
        self.data.pop(key, None)


def test_request_flow():
    use_fixtures()
    sent = []
    kv = FakeKV()
    saved = (ab.discord, ab.KV, ab.openai_json)
    ab.discord = lambda method, path, payload, files=None: sent.append((method, path, payload, files)) or {"id": "m1"}
    ab.KV = lambda: kv
    ab.openai_json = lambda i, t: write()
    os.environ.update({"TICKER": "SPX", "CHANNEL_ID": "an-ch", "MESSAGE_ID": "msg-9", "REQUESTER": "222",
                       "REFUND": "quota:an:222:2026-11-24|1", "CACHE_KEY": "an:SPX:2026-11-24",
                       "LEFT": "1 of 3 analyses left today", "DISCORD_GUILD_ID": "guild1"})
    try:
        assert ab.task_request(NOW) is True
        method, path, payload, files = sent[-1]
        assert (method, path) == ("PATCH", "/channels/an-ch/messages/msg-9")
        assert [n for n, _ in files] == ["chart.png", "positioning.png"]
        assert payload["content"] == "Requested by <@222>"
        assert json.loads(kv.data["an:SPX:2026-11-24"]) | {"at": 0} == {
            "url": "https://discord.com/channels/guild1/an-ch/msg-9", "done": True, "at": 0}
        # failure: the placeholder says so, the use is refunded and the same-day link is cleared
        sent.clear()
        kv.data = {"an:SPX:2026-11-24": "{}", "quota:an:222:2026-11-24": "2"}
        ab.openai_json = lambda i, t: (_ for _ in ()).throw(RuntimeError("OpenAI returned HTTP 500"))
        assert ab.task_request(NOW) is False
        method, path, payload, files = sent[-1]
        assert method == "PATCH" and "didn't count" in payload["embeds"][0]["description"] and payload["attachments"] == []
        assert kv.data["quota:an:222:2026-11-24"] == "1" and "an:SPX:2026-11-24" in kv.deleted
        os.environ["REFUND"] = "quota:an:222:2026-11-24|0"
        ab.task_request(NOW)
        assert "quota:an:222:2026-11-24" in kv.deleted, "a first use is refunded by deleting the counter"
    finally:
        ab.discord, ab.KV, ab.openai_json = saved
    print("ok request flow")


def test_weekly_and_auto():
    use_fixtures()
    sent = []
    kv = FakeKV()
    kv.data["collar"] = json.dumps({"call": 6020, "put": 5245, "putLow": 4580, "expiry": "2026-12-31"})
    saved = (ab.discord, ab.KV, ab.openai_json)
    prompts = []
    ab.discord = lambda method, path, payload, files=None: sent.append((method, path, payload)) or {"id": f"m{len(sent)}"}
    ab.KV = lambda: kv
    ab.openai_json = lambda i, t: prompts.append(t) or write()
    os.environ.update({"ANALYSIS_CHANNEL_ID": "an-ch", "DISCORD_GUILD_ID": "guild1"})
    monday = dt.datetime(2026, 11, 30, 8, 45, tzinfo=ab.ET)
    try:
        posted = {}
        assert ab.task_weekly(monday, posted) is True
        assert [p for _, p, _ in sent] == ["/channels/an-ch/messages"] * 2
        assert posted == {"weekly-SPX": "2026-11-30", "weekly-NDX": "2026-11-30"}
        assert '"jpmCollar": {"shortCall": 6020' in prompts[0] and "jpmCollar" not in prompts[1]
        assert "an:SPX:2026-11-30" in kv.data and "an:NDX:2026-11-30" in kv.data
        assert sent[0][2]["embeds"][0]["footer"]["text"].endswith("Weekly outlook")
        sent.clear()
        assert ab.task_weekly(monday, posted) is True and sent == [], "nothing twice in one day"
    finally:
        ab.discord, ab.KV, ab.openai_json = saved
    at = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=ab.ET)
    assert ab.auto_due(at("2026-11-30T08:37")) == ["weekly"]
    assert ab.auto_due(at("2026-11-30T08:36")) == []
    assert ab.auto_due(at("2026-11-30T12:00")) == []
    assert ab.auto_due(at("2026-12-01T09:00")) == []
    print("ok weekly + auto")


def test_schedule_replay():
    """Every Monday window (winter and summer time) gets a cron chance, like tests/test_offline.py."""
    text = open(os.path.join(ROOT, ".github", "workflows", "analysis.yml")).read()
    assert "timezone:" not in text
    crons = re.findall(r"cron:\s*'([^']+)'", text)
    assert crons, "analysis.yml needs backup cron lines"

    def matches(expr, t):
        fields = expr.split()

        def ok(field, value):
            for part in field.split(","):
                if part == "*":
                    return True
                if "-" in part:
                    lo, hi = map(int, part.split("-"))
                    if lo <= value <= hi:
                        return True
                elif int(part) == value:
                    return True
            return False
        dow = (t.weekday() + 1) % 7
        return ok(fields[0], t.minute) and ok(fields[1], t.hour) and ok(fields[2], t.day) and ok(fields[3], t.month) and ok(fields[4], dow)

    for monday in (dt.date(2027, 1, 4), dt.date(2027, 7, 5)):       # winter and summer weeks
        for offset in range(5):
            day = monday + dt.timedelta(days=offset)
            hits: dict = {}
            t = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.timezone.utc)
            for _ in range(24 * 60):
                local = t.astimezone(ab.ET)
                if any(matches(c, t) for c in crons):
                    for task in ab.auto_due(local):
                        hits.setdefault(task, []).append(local)
                t += dt.timedelta(minutes=1)
            assert len(hits.get("oi-changes", [])) >= 6, f"{day}: few open-interest chances"
            assert hits["oi-changes"][0].time() <= dt.time(10, 30), f"{day}: OI first chance too late"
            if offset == 0:
                assert len(hits["weekly"]) >= 8 and hits["weekly"][0].time() <= dt.time(9, 0), f"{day}: weekly"
            else:
                assert "weekly" not in hits, f"{day}: the outlook is Mondays only"
    print("ok schedule replay")


def test_oi_changes():
    _, _, chains = use_fixtures()
    sent = []
    saved = (ab.discord, ab.OI_DIR)
    ab.discord = lambda method, path, payload, files=None: sent.append((method, path, payload)) or {"id": "x"}
    os.environ["ANALYSIS_CHANNEL_ID"] = "an-ch"
    monday = dt.datetime(2026, 11, 30, 10, 10, tzinfo=ab.ET)
    tuesday = monday + dt.timedelta(days=1)
    with tempfile.TemporaryDirectory() as tmp:
        ab.OI_DIR = tmp
        try:
            assert ab.task_oi(monday) is True and sent == [], "the first day only saves a snapshot"
            assert sorted(os.listdir(tmp)) == ["NVDA.json", "SPX.json"]
            assert ab.task_oi(tuesday) is False and sent == [], "unchanged open interest = not updated yet, retry"

            def bump(chain, root, kind, near, delta, expiry="261218"):
                spot = chain["data"]["current_price"]
                opts = [o for o in chain["data"]["options"] if o["option"].startswith(f"{root}{expiry}{kind}")]
                o = min(opts, key=lambda o: abs(int(o["option"][-8:]) / 1000 - spot * near))
                o["open_interest"] = max(0.0, o["open_interest"] + delta)
                return o["option"]
            new_call = bump(chains["SPX"], "SPX", "C", 1.05, 12000)
            gone_put = bump(chains["SPX"], "SPX", "P", 0.97, -50_000)
            bump(chains["NVDA"], "NVDA", "C", 1.02, 3000)
            assert ab.task_oi(tuesday) is True
            method, path, payload = sent[-1]
            assert (method, path) == ("POST", "/channels/an-ch/messages")
            e = payload["embeds"][0]
            assert e["title"].startswith("📊 Open-interest changes · Tue, Dec 1") and "since Mon Nov 30" in e["description"]
            spx = next(f for f in e["fields"] if f["name"].startswith("SPX"))
            lines = spx["value"].splitlines()
            strike = int(new_call[-8:]) // 1000
            assert lines[0].startswith(f"▲ `Dec 18 {strike:,}C` +12.0K · ≈$") and "OTM" in lines[0], lines[0]
            assert any(l.startswith("▼ `Dec 18") and "P` −" in l for l in lines), lines
            assert lines[-1].startswith("Net: calls +12.0K · puts −"), lines[-1]
            assert any(f["name"].startswith("NVDA") for f in e["fields"])
            assert len(json.dumps(e)) < 6000 and all(len(f["value"]) <= 1024 for f in e["fields"])
            assert json.load(open(os.path.join(tmp, "SPX.json")))["date"] == "2026-12-01"
            snap = json.load(open(os.path.join(tmp, "SPX.json")))["oi"]
            assert snap[new_call] >= 12000 and snap.get(gone_put, 0) < 50_000, "today's open interest is the new baseline"
            sent.clear()
            assert ab.task_oi(tuesday) is True and sent == [], "nothing twice in a day"
            assert ab.task_oi(dt.datetime(2026, 12, 25, 10, 10, tzinfo=ab.ET)) is True and sent == [], "holiday"
        finally:
            ab.discord, ab.OI_DIR = saved
    print("ok open-interest changes")


def test_dry_run_saves_charts():
    use_fixtures()
    with tempfile.TemporaryDirectory() as tmp:
        ab.DRY_RUN, ab.DRY_RUN_DIR = True, tmp
        saved = ab.openai_json
        ab.openai_json = lambda i, t: write()
        try:
            os.environ["ANALYSIS_CHANNEL_ID"] = "an-ch"
            ab.task_weekly(dt.datetime(2026, 11, 30, 8, 45, tzinfo=ab.ET), {})
            assert sorted(os.listdir(tmp)) == ["chart.png", "positioning.png"]
        finally:
            ab.DRY_RUN, ab.openai_json = False, saved
    print("ok dry run")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL ANALYSIS TESTS PASSED")
