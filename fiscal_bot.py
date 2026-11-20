#!/usr/bin/env python3
"""
Fiscal bot - posts earnings and macro updates into Discord channels via webhooks.

Tasks
  auto              picks the right tasks from the current New York time (used by the schedule)
  earnings-week     Sunday preview of the biggest companies reporting next week
  earnings-today    morning lineup: who reports before the open / after the close
  earnings-results  evening recap: beats and misses vs. estimates
  macro-week        Sunday preview of key US economic releases and Fed events
  macro-morning     weekday morning brief: last close + today's releases
  check             prints what the data sources return (posts nothing)
  test-post         sends a "connected" message to each webhook to confirm setup

Options
  --dry-run         print the Discord message instead of posting it
  --date YYYY-MM-DD pretend today is this date (handy for testing)

Environment variables (set as GitHub secrets)
  FINNHUB_API_KEY, FRED_API_KEY, DISCORD_WEBHOOK_EARNINGS, DISCORD_WEBHOOK_MACRO
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")

# ============================ SETTINGS YOU CAN EDIT ============================

# Tickers that always show up in earnings posts (marked with a star), even small ones.
WATCHLIST: list[str] = []  # e.g. ["PLTR", "HOOD", "SOFI", "RKLB"]

# Companies whose quarterly revenue estimate is below this are left out, to keep posts short.
MIN_REVENUE_ESTIMATE = 1_000_000_000  # $1B

# Most companies listed per session (before open / after close) in each post.
MAX_PER_SESSION_WEEK = 12
MAX_PER_SESSION_DAY = 15

# Key US economic releases, matched against FRED's release names (lower case).
# A plain pattern must match the START of the name; a pattern beginning with * can appear anywhere.
# (pattern, label shown in Discord, usual release time ET, show in the Sunday preview)
KEY_RELEASES = [
    ("employment situation", "Jobs Report (Nonfarm Payrolls)", "8:30 AM", True),
    ("consumer price index", "CPI Inflation", "8:30 AM", True),
    ("producer price index", "PPI Inflation", "8:30 AM", True),
    ("personal income and outlays", "PCE Inflation & Spending", "8:30 AM", True),
    ("gross domestic product", "GDP", "8:30 AM", True),
    ("advance monthly sales for retail", "Retail Sales", "8:30 AM", True),
    ("job openings and labor turnover", "JOLTS Job Openings", "10:00 AM", True),
    ("*surveys of consumers", "UMich Consumer Sentiment", "10:00 AM", True),
    ("new residential construction", "Housing Starts", "8:30 AM", True),
    ("*industrial production and capacity utilization", "Industrial Production", "9:15 AM", False),
    ("*durable goods", "Durable Goods Orders", "8:30 AM", True),
    ("manufacturers' shipments, inventories", "Factory Orders", "10:00 AM", False),
    ("unemployment insurance weekly claims", "Weekly Jobless Claims", "8:30 AM", True),
]

# FOMC rate-decision days (statement 2:00 PM ET, press conference 2:30 PM ET).
# Source: federalreserve.gov/monetarypolicy/fomccalendars.htm - update once a year.
FOMC_DECISIONS = {
    "2026-09-16": "with projections & dot plot",
    "2026-10-28": "",
    "2026-12-09": "with projections & dot plot",
    "2027-01-27": "",
    "2027-03-17": "",
    "2027-04-28": "",
    "2027-06-09": "",
    "2027-07-28": "",
    "2027-09-15": "",
    "2027-11-17": "",
}
FOMC_OTHER = {"2027-10-27": "FOMC strategy meeting (no rate decision)"}
FOMC_MINUTES_LAG_DAYS = 21  # minutes come out three weeks after each decision, 2:00 PM ET

# NYSE full-day closures and early closes (source: NYSE holiday calendar).
MARKET_HOLIDAYS = {
    "2026-11-26": "Thanksgiving",
    "2026-12-25": "Christmas",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King Jr. Day",
    "2027-02-15": "Presidents' Day",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving",
    "2027-12-24": "Christmas (observed)",
}
EARLY_CLOSES = {"2026-11-27": "1:00 PM", "2026-12-24": "1:00 PM", "2027-11-26": "1:00 PM"}

# Market snapshot in the morning brief (FRED series id, label, kind).
SNAPSHOT = [
    ("SP500", "S&P 500", "index"),
    ("NASDAQCOM", "Nasdaq", "index"),
    ("DJIA", "Dow", "index"),
    ("VIXCLS", "VIX", "points"),
    ("DGS2", "2Y Treasury", "yield"),
    ("DGS10", "10Y Treasury", "yield"),
]

COLORS = {"earnings": 0x3B82F6, "results": 0x22C55E, "macro": 0xF59E0B, "brief": 0x8B5CF6}
NOT_ADVICE = "Not financial advice"

# ===============================================================================

DRY_RUN = False


# ---------------------------------------------------------------- helpers

def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing {name}. Add it under GitHub > Settings > Secrets and variables > Actions.")
    return value


def http_get(url: str, params: dict) -> dict:
    """GET JSON with retries. Error messages never include the URL (it can contain keys)."""
    host = url.split("/")[2]
    last = ""
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=25)
        except requests.RequestException as exc:
            last = type(exc).__name__
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            time.sleep(2 ** attempt * 2)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"{host} returned HTTP {r.status_code}: {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"{host} failed after retries ({last})")


def iso(d: dt.date) -> str:
    return d.isoformat()


def day_label(d: dt.date) -> str:
    return f"{d:%a}, {d:%b} {d.day}"


def week_label(start: dt.date, end: dt.date) -> str:
    if start.month == end.month:
        return f"{start:%b} {start.day}-{end.day}"
    return f"{start:%b} {start.day} - {end:%b} {end.day}"


def next_monday(today: dt.date) -> dt.date:
    return today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)


def is_weekend(d: dt.date) -> bool:
    return d.weekday() >= 5


def money(x: float | None) -> str:
    if x is None:
        return "n/a"
    sign, a = ("-" if x < 0 else ""), abs(x)
    if a >= 1e12:
        return f"{sign}${a / 1e12:.2f}T"
    if a >= 1e10:
        return f"{sign}${a / 1e9:.1f}B"
    if a >= 1e9:
        return f"{sign}${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.0f}M"
    return f"{sign}${a:,.0f}"


def eps(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"-${abs(x):.2f}" if x < 0 else f"${x:.2f}"


def time_sort_key(t: str) -> int:
    """'8:30 AM' -> minutes after midnight, for ordering events within a day."""
    try:
        clock, ampm = t.split()
        h, m = (int(p) for p in clock.split(":"))
        return (h % 12 + (12 if ampm.upper() == "PM" else 0)) * 60 + m
    except ValueError:
        return 24 * 60


def lines_to_fields(title: str, lines: list[str], limit: int = 1024) -> list[dict]:
    """Pack lines into Discord embed fields, each under the 1024-character limit."""
    fields, chunk = [], []
    for line in lines:
        line = line[:limit]
        if chunk and len("\n".join(chunk + [line])) > limit:
            fields.append(chunk)
            chunk = []
        chunk.append(line)
    if chunk:
        fields.append(chunk)
    return [
        {"name": title if i == 0 else f"{title} (cont.)", "value": "\n".join(c), "inline": False}
        for i, c in enumerate(fields)
    ]


def embed_size(e: dict) -> int:
    n = len(e.get("title", "")) + len(e.get("description", "")) + len(e.get("footer", {}).get("text", ""))
    return n + sum(len(f["name"]) + len(f["value"]) for f in e.get("fields", []))


def fit_embed(e: dict) -> dict:
    """Keep within Discord limits: 25 fields, 6000 characters in total."""
    e["fields"] = e.get("fields", [])[:25]
    trimmed = False
    while embed_size(e) > 5800 and e["fields"]:
        e["fields"].pop()
        trimmed = True
    if trimmed:
        e["description"] = (e.get("description", "") + "\n*(List shortened to fit Discord.)*").strip()
    return e


def make_embed(title: str, description: str, color: int, fields: list[dict], footer: str) -> dict:
    e = fit_embed({
        "title": title[:256],
        "description": description[:4000],
        "color": color,
        "fields": fields,
        "footer": {"text": footer},
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    for key in ("description", "fields"):  # Discord dislikes empty values
        if not e[key]:
            del e[key]
    return e


def post(webhook_env: str, username: str, embed: dict) -> None:
    payload = {"username": username, "embeds": [embed], "allowed_mentions": {"parse": []}}
    if DRY_RUN:
        print(f"--- DRY RUN: would post to {webhook_env} ---")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    url = require_env(webhook_env)
    for attempt in range(4):
        r = requests.post(url, params={"wait": "true"}, json=payload, timeout=25)
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except ValueError:
                wait = 2
            time.sleep(min(wait, 30))
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Discord rejected the post (HTTP {r.status_code}): {r.text[:300]}")
        print(f"Posted '{embed['title']}' via {webhook_env}")
        return
    raise RuntimeError("Discord kept failing after retries")


# ---------------------------------------------------------------- data: Finnhub

SESSIONS = [
    ("bmo", "🌅 Before the open", "🌅 **Before open:**"),
    ("amc", "🌙 After the close", "🌙 **After close:**"),
    ("dmh", "🕐 During market hours", "🕐 **During market:**"),
    ("", "❔ Time not announced", "❔ **Time TBA:**"),
]


def finnhub_earnings(day: dt.date) -> list[dict]:
    data = http_get(
        "https://finnhub.io/api/v1/calendar/earnings",
        {"from": iso(day), "to": iso(day), "token": require_env("FINNHUB_API_KEY")},
    )
    return [r for r in (data.get("earningsCalendar") or []) if r.get("date") == iso(day)]


def notable(rows: list[dict]) -> list[dict]:
    """Watchlist names first, then the rest by revenue estimate, biggest first."""
    wl = {t.upper() for t in WATCHLIST}
    seen, out = set(), []
    for r in rows:
        sym = (r.get("symbol") or "").upper()
        if not sym or sym in seen:
            continue
        if sym in wl or (r.get("revenueEstimate") or 0) >= MIN_REVENUE_ESTIMATE:
            seen.add(sym)
            out.append(r)
    out.sort(key=lambda r: (r["symbol"].upper() not in wl, -(r.get("revenueEstimate") or 0)))
    return out


def session_of(r: dict) -> str:
    h = (r.get("hour") or "").lower()
    return h if h in ("bmo", "amc", "dmh") else ""


def ticker(r: dict) -> str:
    sym = r["symbol"].upper()
    return f"⭐{sym}" if sym in {t.upper() for t in WATCHLIST} else sym


def by_session(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {key: [] for key, _, _ in SESSIONS}
    for r in rows:
        groups[session_of(r)].append(r)
    return groups


def surprise_pct(actual: float | None, est: float | None) -> float | None:
    if actual is None or not est:
        return None
    return (actual - est) / abs(est) * 100


def verdict(actual: float | None, est: float | None, is_eps: bool) -> str | None:
    if actual is None or est is None:
        return None
    if is_eps:
        a, e = round(actual, 2), round(est, 2)
        return "beat" if a > e else "miss" if a < e else "inline"
    pct = surprise_pct(actual, est) or 0
    return "beat" if pct > 0.05 else "miss" if pct < -0.05 else "inline"


def result_line(r: dict) -> str:
    ea, ee = r.get("epsActual"), r.get("epsEstimate")
    ra, re_ = r.get("revenueActual"), r.get("revenueEstimate")
    v = verdict(ea, ee, True) or verdict(ra, re_, False)
    icon = {"beat": "🟢", "miss": "🔴", "inline": "⚪"}.get(v or "", "⚫")
    parts = [f"{icon} **{ticker(r)}**"]
    if ea is not None:
        s = surprise_pct(ea, ee)
        parts.append(f"EPS {eps(ea)} vs {eps(ee)} est" + (f" ({s:+.1f}%)" if s is not None else "")
                     if ee is not None else f"EPS {eps(ea)}")
    if ra is not None:
        s = surprise_pct(ra, re_)
        parts.append(f"Rev {money(ra)} vs {money(re_)}" + (f" ({s:+.1f}%)" if s is not None else "")
                     if re_ is not None else f"Rev {money(ra)}")
    return " · ".join(parts)


def preview_line(r: dict) -> str:
    bits = [f"**{ticker(r)}**"]
    if r.get("epsEstimate") is not None:
        bits.append(f"EPS est {eps(r['epsEstimate'])}")
    if r.get("revenueEstimate"):
        bits.append(f"Rev est {money(r['revenueEstimate'])}")
    return " · ".join(bits)


# ---------------------------------------------------------------- data: FRED + Fed calendar

def fred_get(path: str, **params) -> dict:
    params.update(api_key=require_env("FRED_API_KEY"), file_type="json")
    return http_get(f"https://api.stlouisfed.org/fred/{path}", params)


def fred_release_dates(start: dt.date, end: dt.date, today: dt.date) -> list[dict]:
    """All FRED release dates between start and end, including scheduled future ones."""
    out, offset = [], 0
    while True:
        data = fred_get(
            "releases/dates",
            realtime_start=iso(min(start, today)),
            include_release_dates_with_no_data="true",
            order_by="release_date",
            sort_order="asc",
            limit=1000,
            offset=offset,
        )
        batch = data.get("release_dates") or []
        out.extend(batch)
        if len(batch) < 1000 or (batch and batch[-1].get("date", "") > iso(end)):
            break
        offset += 1000
    return [r for r in out if iso(start) <= r.get("date", "") <= iso(end)]


def match_release(name: str):
    n = name.lower().strip()
    if " by " in n:  # skip regional splits like "GDP by State"
        return None
    for pattern, label, when, weekly in KEY_RELEASES:
        hit = pattern[1:] in n if pattern.startswith("*") else n.startswith(pattern)
        if hit:
            return label, when, weekly
    return None


def fed_events(d: dt.date) -> list[tuple[str, str, bool]]:
    events = []
    key = iso(d)
    if key in FOMC_DECISIONS:
        note = FOMC_DECISIONS[key]
        events.append(("2:00 PM", "🏛️ **FOMC rate decision**" + (f" ({note})" if note else ""), True))
        events.append(("2:30 PM", "🎙️ Fed Chair press conference", True))
    if key in FOMC_OTHER:
        events.append(("All day", f"🏛️ {FOMC_OTHER[key]}", True))
    for decision in FOMC_DECISIONS:
        if dt.date.fromisoformat(decision) + dt.timedelta(days=FOMC_MINUTES_LAG_DAYS) == d:
            events.append(("2:00 PM", "📝 FOMC minutes", True))
    return events


def macro_events(start: dt.date, end: dt.date, today: dt.date) -> dict[str, list[tuple[str, str, bool]]]:
    """{date: [(time, label, show_in_weekly), ...]} for key releases and Fed events."""
    events: dict[str, list[tuple[str, str, bool]]] = {}
    seen = set()
    for r in fred_release_dates(start, end, today):
        m = match_release(r.get("release_name", ""))
        if not m:
            continue
        label, when, weekly = m
        if (r["date"], label) in seen:
            continue
        seen.add((r["date"], label))
        events.setdefault(r["date"], []).append((when, label, weekly))
    d = start
    while d <= end:
        for ev in fed_events(d):
            events.setdefault(iso(d), []).append(ev)
        d += dt.timedelta(days=1)
    for k in events:
        events[k].sort(key=lambda e: time_sort_key(e[0]))
    return events


def fred_latest(series_id: str, asof: dt.date):
    data = fred_get(
        "series/observations",
        series_id=series_id,
        sort_order="desc",
        limit=15,
        observation_end=iso(asof),
    )
    obs = []
    for o in data.get("observations") or []:
        try:
            obs.append((o["date"], float(o["value"])))
        except (KeyError, ValueError):
            continue  # FRED uses "." for missing days
    if len(obs) < 2:
        return None
    return obs[0][0], obs[0][1], obs[1][1]


# ---------------------------------------------------------------- tasks: earnings

def earnings_week(today: dt.date) -> None:
    monday = next_monday(today)
    days = [monday + dt.timedelta(days=i) for i in range(5)]
    fields, total = [], 0
    for d in days:
        if iso(d) in MARKET_HOLIDAYS:
            fields.append({"name": day_label(d), "value": f"🚫 Market closed ({MARKET_HOLIDAYS[iso(d)]})", "inline": False})
            continue
        rows = notable(finnhub_earnings(d))
        total += len(rows)
        lines = []
        if iso(d) in EARLY_CLOSES:
            lines.append(f"⏰ Early close at {EARLY_CLOSES[iso(d)]} ET")
        groups = by_session(rows)
        for key, _, short in SESSIONS:
            group = groups[key]
            if not group:
                continue
            names = ", ".join(ticker(r) for r in group[:MAX_PER_SESSION_WEEK])
            extra = len(group) - MAX_PER_SESSION_WEEK
            lines.append(f"{short} {names}" + (f" +{extra} more" if extra > 0 else ""))
        if not rows:
            lines.append("No major reporters")
        fields.extend(lines_to_fields(day_label(d), lines))
    if total == 0:
        print("earnings-week: no notable reporters next week, nothing posted")
        return
    embed = make_embed(
        f"📅 Earnings Week Ahead · {week_label(days[0], days[-1])}",
        f"**{total}** notable companies report next week, biggest first by expected revenue.\n"
        "Estimates drop here each morning; beats and misses each evening.",
        COLORS["earnings"], fields, f"Data: Finnhub · {NOT_ADVICE}",
    )
    post("DISCORD_WEBHOOK_EARNINGS", "Fiscal Earnings", embed)


def earnings_today(today: dt.date) -> None:
    if is_weekend(today) or iso(today) in MARKET_HOLIDAYS:
        print("earnings-today: market closed, nothing posted")
        return
    rows = notable(finnhub_earnings(today))
    if not rows:
        print("earnings-today: no notable reporters today, nothing posted")
        return
    fields = []
    for key, title, _ in SESSIONS:
        group = by_session(rows)[key]
        if not group:
            continue
        lines = [preview_line(r) for r in group[:MAX_PER_SESSION_DAY]]
        extra = group[MAX_PER_SESSION_DAY:]
        if extra:
            lines.append(f"+{len(extra)} more: " + ", ".join(ticker(r) for r in extra[:20]))
        fields.extend(lines_to_fields(title, lines))
    desc = f"**{len(rows)}** notable companies report today. Results recap tonight around 8 PM ET."
    if iso(today) in EARLY_CLOSES:
        desc += f"\n⏰ Early close today at {EARLY_CLOSES[iso(today)]} ET."
    embed = make_embed(f"📊 Earnings Today · {day_label(today)}", desc, COLORS["earnings"], fields,
                       f"Data: Finnhub · {NOT_ADVICE}")
    post("DISCORD_WEBHOOK_EARNINGS", "Fiscal Earnings", embed)


def earnings_results(today: dt.date) -> None:
    if is_weekend(today) or iso(today) in MARKET_HOLIDAYS:
        print("earnings-results: market closed, nothing posted")
        return
    rows = notable(finnhub_earnings(today))
    reported = [r for r in rows if r.get("epsActual") is not None or r.get("revenueActual") is not None]
    pending = [r for r in rows if r not in reported]
    if not reported:
        print("earnings-results: no results available yet, nothing posted")
        return
    tally = {"beat": 0, "miss": 0, "inline": 0}
    for r in reported:
        v = verdict(r.get("epsActual"), r.get("epsEstimate"), True)
        if v:
            tally[v] += 1
    fields = []
    for key, title, _ in SESSIONS:
        group = [r for r in by_session(reported)[key]]
        if group:
            fields.extend(lines_to_fields(title, [result_line(r) for r in group[:MAX_PER_SESSION_DAY]]))
    if pending:
        fields.append({"name": "⏳ Not out yet", "value": ", ".join(ticker(r) for r in pending[:40])[:1024], "inline": False})
    desc = (f"EPS vs. estimates: **{tally['beat']} beat** · **{tally['miss']} missed** · {tally['inline']} in line\n"
            "🟢 beat · 🔴 miss · ⚪ in line (based on EPS)")
    embed = make_embed(f"🧾 Earnings Results · {day_label(today)}", desc, COLORS["results"], fields,
                       f"Data: Finnhub · {NOT_ADVICE}")
    post("DISCORD_WEBHOOK_EARNINGS", "Fiscal Earnings", embed)


# ---------------------------------------------------------------- tasks: macro

def macro_week(today: dt.date) -> None:
    monday = next_monday(today)
    friday = monday + dt.timedelta(days=4)
    events = macro_events(monday, friday, today)
    fields, count = [], 0
    for i in range(5):
        d = monday + dt.timedelta(days=i)
        lines = []
        if iso(d) in MARKET_HOLIDAYS:
            lines.append(f"🚫 Market closed ({MARKET_HOLIDAYS[iso(d)]})")
        if iso(d) in EARLY_CLOSES:
            lines.append(f"⏰ Early close at {EARLY_CLOSES[iso(d)]} ET")
        day_events = [e for e in events.get(iso(d), []) if e[2]]
        count += len(day_events)
        lines += [f"`{when:>8}` {label}" for when, label, _ in day_events]
        if not lines:
            lines.append("No major releases")
        fields.extend(lines_to_fields(day_label(d), lines))
    embed = make_embed(
        f"🗓️ Macro Week Ahead · {week_label(monday, friday)}",
        f"**{count}** key US data releases and Fed events next week. Times are Eastern.",
        COLORS["macro"], fields, f"Data: FRED release calendar & Federal Reserve · {NOT_ADVICE}",
    )
    post("DISCORD_WEBHOOK_MACRO", "Fiscal Macro", embed)


def snapshot_lines(today: dt.date) -> tuple[list[str], str | None]:
    results, dates = [], []
    for sid, label, kind in SNAPSHOT:
        try:
            got = fred_latest(sid, today - dt.timedelta(days=1))
        except RuntimeError as exc:
            print(f"snapshot: {sid} failed: {exc}")
            got = None
        if got:
            results.append((label, kind, *got))
            dates.append(got[0])
    if not results:
        return [], None
    common = max(set(dates), key=dates.count)
    lines = []
    for label, kind, d, now, prev in results:
        arrow = "▲" if now > prev else "▼" if now < prev else "•"
        if kind == "index":
            pct = (now - prev) / prev * 100 if prev else 0
            text = f"**{label}** {now:,.2f} {arrow} {pct:+.2f}%"
        elif kind == "yield":
            text = f"**{label}** {now:.2f}% {arrow} {(now - prev) * 100:+.0f} bp"
        else:
            text = f"**{label}** {now:.2f} {arrow} {now - prev:+.2f}"
        if d != common:
            text += f" *(as of {dt.date.fromisoformat(d):%b} {dt.date.fromisoformat(d).day})*"
        lines.append(text)
    return lines, common


def macro_morning(today: dt.date) -> None:
    if is_weekend(today):
        print("macro-morning: weekend, nothing posted")
        return
    friday = today + dt.timedelta(days=4 - today.weekday())
    events = macro_events(today, friday, today)
    todays = events.get(iso(today), [])
    holiday = MARKET_HOLIDAYS.get(iso(today))
    if holiday and not todays:
        print("macro-morning: market holiday with no releases, nothing posted")
        return
    fields = []
    snap, asof = snapshot_lines(today)
    if snap:
        fields.extend(lines_to_fields("📈 Markets at last close", snap))
    today_lines = [f"`{when:>8}` {label}" for when, label, _ in todays] or ["No major US data today."]
    fields.extend(lines_to_fields("🗓️ On deck today (ET)", today_lines))
    later = []
    d = today + dt.timedelta(days=1)
    while d <= friday:
        for when, label, weekly in events.get(iso(d), []):
            if weekly:
                later.append(f"{d:%a} `{when:>8}` {label}")
        d += dt.timedelta(days=1)
    if later:
        fields.extend(lines_to_fields("🔭 Later this week", later))
    notes = []
    if holiday:
        notes.append(f"🚫 US stock market closed today ({holiday}).")
    if iso(today) in EARLY_CLOSES:
        notes.append(f"⏰ Early close today at {EARLY_CLOSES[iso(today)]} ET.")
    if asof:
        notes.append(f"Closing levels from {day_label(dt.date.fromisoformat(asof))}.")
    embed = make_embed(f"☕ Morning Brief · {day_label(today)}", "\n".join(notes), COLORS["brief"], fields,
                       f"Data: FRED (St. Louis Fed) & Federal Reserve · {NOT_ADVICE}")
    post("DISCORD_WEBHOOK_MACRO", "Fiscal Macro", embed)


# ---------------------------------------------------------------- tasks: utilities

def check(today: dt.date) -> None:
    print(f"== Check run for {today} ==")
    try:
        end = today + dt.timedelta(days=14)
        rows = fred_release_dates(today, end, today)
        names = sorted({r.get("release_name", "") for r in rows})
        print(f"\nFRED: {len(rows)} release dates in the next 14 days, {len(names)} distinct releases.")
        print("Matched as key releases:")
        for r in rows:
            m = match_release(r.get("release_name", ""))
            if m:
                print(f"  {r['date']}  {m[0]:<32} <- {r['release_name']}")
        print("All release names in the window (to tune KEY_RELEASES):")
        for n in names:
            print(f"  - {n}")
    except RuntimeError as exc:
        print(f"FRED check failed: {exc}")
    try:
        print("\nFinnhub earnings, next 5 weekdays:")
        d, shown = today, 0
        while shown < 5:
            if not is_weekend(d):
                raw = finnhub_earnings(d)
                print(f"  {d}: {len(raw)} companies, {len(notable(raw))} notable")
                shown += 1
            d += dt.timedelta(days=1)
    except RuntimeError as exc:
        print(f"Finnhub check failed: {exc}")


def test_post(today: dt.date) -> None:
    for env, name in (("DISCORD_WEBHOOK_EARNINGS", "Fiscal Earnings"), ("DISCORD_WEBHOOK_MACRO", "Fiscal Macro")):
        if not os.environ.get(env) and not DRY_RUN:
            print(f"test-post: {env} not set, skipped")
            continue
        embed = make_embed("✅ Fiscal is connected", f"This channel will receive automated **{name}** posts.",
                           COLORS["results"], [], NOT_ADVICE)
        post(env, name, embed)


TASKS = {
    "earnings-week": earnings_week,
    "earnings-today": earnings_today,
    "earnings-results": earnings_results,
    "macro-week": macro_week,
    "macro-morning": macro_morning,
    "check": check,
    "test-post": test_post,
}


def auto_tasks(now: dt.datetime) -> list[str]:
    """What the schedule should run at this New York time (tolerates GitHub's start delays)."""
    wd = now.weekday()
    if wd == 6:
        return ["macro-week", "earnings-week"]
    if wd <= 4 and now.hour < 12:
        return ["macro-morning", "earnings-today"]
    if wd <= 4 and now.hour >= 17:
        return ["earnings-results"]
    return []


def main() -> int:
    global DRY_RUN
    parser = argparse.ArgumentParser(description="Fiscal Discord feeds")
    parser.add_argument("tasks", nargs="+", choices=["auto", *TASKS])
    parser.add_argument("--dry-run", action="store_true", help="print instead of posting")
    parser.add_argument("--date", help="pretend today is YYYY-MM-DD (or YYYY-MM-DDTHH:MM)")
    args = parser.parse_args()
    DRY_RUN = args.dry_run or os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true")

    now = dt.datetime.now(ET)
    if args.date:
        parsed = dt.datetime.fromisoformat(args.date)
        now = parsed.replace(tzinfo=ET) if "T" in args.date else now.replace(
            year=parsed.year, month=parsed.month, day=parsed.day)

    tasks = []
    for t in args.tasks:
        tasks.extend(auto_tasks(now) if t == "auto" else [t])
    if not tasks:
        print(f"Nothing scheduled for {now:%a %H:%M} ET")
        return 0

    failures = 0
    for t in tasks:
        print(f"\n>>> {t} ({now:%a %b %d %H:%M} ET){' [dry run]' if DRY_RUN else ''}")
        try:
            TASKS[t](now.date())
        except Exception as exc:  # keep going so one bad source doesn't block the others
            failures += 1
            print(f"!!! {t} failed: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
