#!/usr/bin/env python3
"""
Fiscal media feeds - posts new videos, podcast episodes and articles, a daily headline
digest, and a daily crypto board into Discord. Sources are listed in sources.yml.

Tasks
  auto          what the schedule runs: new items every run, plus the daily digests and the
                crypto board on the first run after 8 AM New York time
  feeds         post new videos / episodes / articles only
  digests       post today's headline digests now
  crypto-board  post the crypto board now
  check-feeds   test every source and print what it returns (posts nothing)
  test-post     send a "connected" message to each media channel

Options
  --dry-run     print messages instead of posting (and don't remember anything as posted)

Environment variables (GitHub secrets)
  DISCORD_WEBHOOK_VIDEOS, DISCORD_WEBHOOK_EDUCATION, DISCORD_WEBHOOK_AI, DISCORD_WEBHOOK_CRYPTO
  COINGECKO_API_KEY (optional; the free keyless CoinGecko API is used without it)
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time

import feedparser
import requests
import yaml

import fiscal_bot as fb

ET = fb.ET
ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(ROOT, "sources.yml")
STATE_FILE = os.environ.get("FEEDS_STATE_FILE", os.path.join(ROOT, ".state", "feeds.json"))

# ============================ SETTINGS YOU CAN EDIT ============================
MAX_AGE_HOURS = 36           # never post anything older than this
MAX_POSTS_PER_CHANNEL = 4    # per run, so a busy day can't flood a channel (the rest wait a run)
DIGEST_SIZE = 10             # headlines per daily digest
DIGEST_HOUR = 8              # digests + crypto board go out on the first run after 8 AM ET
BOARD_COINS = 8              # coins on the crypto board
# ===============================================================================

UA = "Mozilla/5.0 (compatible; FiscalFeedBot/1.0; +https://github.com)"
SNIPPET_CHARS = 280
COLORS = {"videos": 0xEF4444, "education": 0x14B8A6, "ai": 0x6366F1, "crypto": 0xF59E0B}
KIND_WORDS = {"youtube": "new video", "podcast": "new episode", "rss": "new post"}

# Stablecoins, wrapped and staked tokens are left off the crypto board.
BOARD_SKIP = {
    "usdt", "usdc", "usde", "dai", "usds", "fdusd", "tusd", "pyusd", "usd1", "usdtb", "bsc-usd",
    "buidl", "susde", "rlusd", "usdf", "usyc", "bfusd", "steth", "wsteth", "wbtc", "weth", "weeth",
    "wbeth", "cbbtc", "reth", "jitosol", "lbtc", "solvbtc", "bnsol", "meth", "ezeth", "rseth",
}


# ---------------------------------------------------------------- state

def load_state() -> dict:
    try:
        with open(STATE_FILE) as fh:
            state = json.load(fh)
    except (FileNotFoundError, ValueError):
        state = {}
    state.setdefault("seen", {})          # item key -> date first posted
    state.setdefault("known_sources", [])
    state.setdefault("youtube_ids", {})   # @handle -> channel ID
    state.setdefault("daily", {})         # "digest" / "board" -> last date posted
    return state


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    if fb.DRY_RUN:
        return
    cutoff = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- fetching

def load_sources() -> dict:
    with open(SOURCES_FILE) as fh:
        return yaml.safe_load(fh) or {}


def fetch(url: str) -> bytes:
    host = url.split("/")[2]
    last = ""
    for attempt in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en"},
                             cookies={"CONSENT": "YES+1"}, timeout=25)
        except requests.RequestException as exc:
            last = type(exc).__name__
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {r.status_code}"
            time.sleep(2 ** attempt * 2)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"{host} returned HTTP {r.status_code}")
        return r.content
    raise RuntimeError(f"{host} failed ({last})")


def resolve_handle(handle: str, state: dict) -> str:
    if handle in state["youtube_ids"]:
        return state["youtube_ids"][handle]
    page = fetch(f"https://www.youtube.com/{handle}").decode("utf-8", "replace")
    m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page)
         or re.search(r'"externalId":"(UC[\w-]{22})"', page)
         or re.search(r'"channelId":"(UC[\w-]{22})"', page))
    if not m:
        raise RuntimeError(f"couldn't find the channel ID for {handle}")
    state["youtube_ids"][handle] = m.group(1)
    return m.group(1)


def source_url(src: dict, state: dict) -> tuple[str, str]:
    if src.get("youtube"):
        cid = str(src["youtube"]).strip()
        if cid.startswith("@"):
            cid = resolve_handle(cid, state)
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", "youtube"
    for kind in ("podcast", "rss"):
        if src.get(kind):
            return str(src[kind]).strip(), kind
    raise RuntimeError("needs a youtube, podcast or rss line")


def clean_text(raw: str, limit: int = SNIPPET_CHARS) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    return text


def image_of(entry, feed) -> str | None:
    for key in ("media_thumbnail", "media_content"):
        for media in entry.get(key) or []:
            if media.get("url") and (key == "media_thumbnail" or "image" in media.get("type", "image")):
                return media["url"]
    if entry.get("image", {}).get("href"):
        return entry["image"]["href"]
    return (feed.feed.get("image") or {}).get("href")


def read_source(src: dict, state: dict) -> tuple[str, str, list[dict], int]:
    """Returns (feed url, kind, items, shorts skipped)."""
    url, kind = source_url(src, state)
    feed = feedparser.parse(fetch(url))
    if not feed.entries and feed.bozo:
        raise RuntimeError(f"not a readable feed ({type(feed.bozo_exception).__name__})")
    include = [w.lower() for w in src.get("include") or []]
    exclude = [w.lower() for w in src.get("exclude") or []]
    items, shorts = [], 0
    for e in feed.entries:
        title = clean_text(e.get("title", ""), 256)
        link = e.get("link", "")
        low = title.lower()
        if kind == "youtube" and ("/shorts/" in link or "#shorts" in low):
            shorts += 1
            continue
        if include and not any(w in low for w in include):
            continue
        if exclude and any(w in low for w in exclude):
            continue
        ts = e.get("published_parsed") or e.get("updated_parsed")
        items.append({
            "key": f"{url}|{e.get('id') or link}",
            "title": title or "(untitled)",
            "link": link,
            "published": dt.datetime.fromtimestamp(calendar.timegm(ts), dt.timezone.utc) if ts else None,
            "summary": clean_text(e.get("summary", "")),
            "image": image_of(e, feed),
            "source": src.get("name", url),
            "kind": kind,
        })
    return url, kind, items, shorts


def gather(cfg: dict, state: dict, digest: bool, warnings: list[str]) -> list[dict]:
    items = []
    for src in cfg.get("sources") or []:
        if bool(src.get("digest")) != digest:
            continue
        try:
            url, _, got, _ = read_source(src, state)
        except Exception as exc:
            warnings.append(f"{src.get('name', '?')}: {exc}")
            continue
        first_time = url not in state["known_sources"]
        for it in got:
            it["first_time"] = first_time
        if first_time:
            state["known_sources"].append(url)
        items.extend(got)
    return items


def fresh(items: list[dict], state: dict, max_age_hours: float) -> list[dict]:
    now = dt.datetime.now(dt.timezone.utc)
    today = dt.date.today().isoformat()
    out = []
    for it in items:
        if it["key"] in state["seen"]:
            continue
        if it["published"] is None:
            if it["first_time"]:  # undated item from a brand-new source: remember it, don't post
                state["seen"][it["key"]] = today
                continue
        elif now - it["published"] > dt.timedelta(hours=max_age_hours):
            continue
        out.append(it)
    return out


# ---------------------------------------------------------------- posting

def channels(sources: dict) -> list[tuple[str, dict]]:
    return [(name, cfg) for name, cfg in sources.items() if isinstance(cfg, dict) and cfg.get("webhook")]


def webhook_ready(cfg: dict) -> bool:
    return fb.DRY_RUN or bool(os.environ.get(cfg["webhook"], "").strip())


def post_item(name: str, cfg: dict, it: dict) -> None:
    icon = cfg.get("icon", "")
    header = f"{icon} **{it['source']}** · {KIND_WORDS[it['kind']]}".strip()
    if it["kind"] == "youtube":  # a bare YouTube link unfurls into a playable video in Discord
        fb.post(cfg["webhook"], cfg.get("username", "Fiscal"), content=f"{header}\n{it['link']}")
        return
    embed = {
        "author": {"name": f"{it['source']} · {KIND_WORDS[it['kind']]}"},
        "title": it["title"][:256],
        "url": it["link"],
        "color": COLORS.get(name, 0x64748B),
    }
    if it["summary"]:
        embed["description"] = it["summary"]
    if it["image"]:
        embed["thumbnail"] = {"url": it["image"]}
    if it["published"]:
        embed["timestamp"] = it["published"].isoformat()
    fb.post(cfg["webhook"], cfg.get("username", "Fiscal"), embed=embed)


def run_feeds(sources: dict, state: dict, warnings: list[str]) -> None:
    for name, cfg in channels(sources):
        if not webhook_ready(cfg):
            print(f"{name}: {cfg['webhook']} not set, skipped")
            continue
        new = fresh(gather(cfg, state, False, warnings), state, MAX_AGE_HOURS)
        oldest_first = lambda it: it["published"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc)  # noqa: E731
        queues: dict[str, list[dict]] = {}
        for it in sorted(new, key=oldest_first):
            queues.setdefault(it["source"], []).append(it)
        batch: list[dict] = []
        while len(batch) < MAX_POSTS_PER_CHANNEL and any(queues.values()):  # take turns across sources
            for q in queues.values():
                if q and len(batch) < MAX_POSTS_PER_CHANNEL:
                    batch.append(q.pop(0))
        batch.sort(key=oldest_first)
        for it in batch:
            post_item(name, cfg, it)
            state["seen"][it["key"]] = dt.date.today().isoformat()
            time.sleep(1)
        waiting = len(new) - len(batch)
        print(f"{name}: posted {len(batch)} new item(s)" + (f", {waiting} waiting for the next run" if waiting else ""))


def headline(it: dict) -> str:
    title = it["title"].replace("[", "(").replace("]", ")")
    return f"• [{title}]({it['link']}) · *{it['source']}*"


def run_digests(sources: dict, state: dict, warnings: list[str]) -> bool:
    today = dt.datetime.now(ET).date()
    for name, cfg in channels(sources):
        if not any(s.get("digest") for s in cfg.get("sources") or []):
            continue
        if not webhook_ready(cfg):
            print(f"{name}: {cfg['webhook']} not set, digest skipped")
            continue
        items = fresh(gather(cfg, state, True, warnings), state, 24)
        by_source: dict[str, list[dict]] = {}
        seen_titles = set()
        for it in sorted(items, key=lambda i: i["published"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                         reverse=True):
            norm = re.sub(r"[^a-z0-9 ]", "", it["title"].lower())[:60]
            if norm in seen_titles:
                continue
            seen_titles.add(norm)
            by_source.setdefault(it["source"], []).append(it)
        picked = []
        while len(picked) < DIGEST_SIZE and any(by_source.values()):  # take turns across sources
            for lst in by_source.values():
                if lst and len(picked) < DIGEST_SIZE:
                    picked.append(lst.pop(0))
        if not picked:
            print(f"{name}: no new headlines for the digest")
            continue
        lines, total = [], 0
        for it in picked:
            line = headline(it)
            if total + len(line) + 1 > 3900:
                break
            lines.append(line)
            total += len(line) + 1
        used = sorted({it["source"] for it in picked[:len(lines)]})
        embed = {
            "title": f"{cfg.get('icon', '')} {cfg.get('label', name.title())} Headlines · {fb.day_label(today)}".strip(),
            "description": "\n".join(lines),
            "color": COLORS.get(name, 0x64748B),
            "footer": {"text": f"From {', '.join(used)} · {fb.NOT_ADVICE}"},
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        fb.post(cfg["webhook"], cfg.get("username", "Fiscal"), embed=embed)  # errors fail the run
        for it in picked[:len(lines)]:
            state["seen"][it["key"]] = dt.date.today().isoformat()
    return True


# ---------------------------------------------------------------- crypto board

def coingecko(path: str, **params) -> dict | list:
    headers = {"User-Agent": UA, "Accept": "application/json"}
    key = os.environ.get("COINGECKO_API_KEY", "").strip()
    if key:
        headers["x-cg-demo-api-key"] = key
    for attempt in range(3):
        r = requests.get(f"https://api.coingecko.com/api/v3/{path}", params=params, headers=headers, timeout=25)
        if r.status_code == 429:  # keyless limit is ~5 calls/min
            time.sleep(20)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"CoinGecko returned HTTP {r.status_code}: {r.text[:150]}")
        return r.json()
    raise RuntimeError("CoinGecko kept rate-limiting")


def board_coins() -> list[dict]:
    rows = coingecko("coins/markets", vs_currency="usd", order="market_cap_desc", per_page=30, page=1,
                     sparkline="true", price_change_percentage="24h,7d")
    out = []
    for c in rows:
        sym, coin_name = (c.get("symbol") or "").lower(), (c.get("name") or "").lower()
        price = c.get("current_price") or 0
        if sym in BOARD_SKIP or any(w in coin_name for w in ("wrapped", "staked", "bridged")):
            continue
        if 0.97 <= price <= 1.03 and "usd" in sym + coin_name:  # stablecoin
            continue
        if not (c.get("sparkline_in_7d") or {}).get("price"):
            continue
        out.append(c)
        if len(out) == BOARD_COINS:
            break
    return out


def usd(x: float) -> str:
    if x >= 1000:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:,.2f}"
    return f"${x:.4f}"


def change_7d(c: dict) -> float:
    v = c.get("price_change_percentage_7d_in_currency")
    if v is None:
        p = c["sparkline_in_7d"]["price"]
        v = (p[-1] - p[0]) / p[0] * 100 if p[0] else 0.0
    return v


def render_board(coins: list[dict]) -> bytes:
    """Small multiples: one 7-day price line per coin. Green = up over 7 days, red = down;
    the ▲/▼ label repeats the direction so color is never the only cue."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    surface, ink, ink2, grid = "#1a1a19", "#f0efec", "#c3c2b7", "#383835"
    up, down = "#0ca30c", "#d03b3b"
    cols = 4
    rows = (len(coins) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(12, 2.9 * rows), dpi=110, squeeze=False)
    fig.patch.set_facecolor(surface)
    compact = FuncFormatter(lambda v, _: f"{v / 1000:,.0f}k" if v >= 10000 else (f"{v:,.0f}" if v >= 100 else f"{v:,.2f}" if v >= 1 else f"{v:.3f}"))
    for ax, c in zip(axes.flat, coins):
        prices = c["sparkline_in_7d"]["price"]
        ch = change_7d(c)
        color = up if ch >= 0 else down
        ax.set_facecolor(surface)
        ax.plot(range(len(prices)), prices, color=color, linewidth=2, solid_capstyle="round")
        ax.margins(x=0, y=0.08)
        ax.set_xticks([])
        ax.yaxis.set_major_formatter(compact)
        ax.yaxis.set_major_locator(plt.MaxNLocator(3))
        ax.tick_params(axis="y", colors=ink2, labelsize=8, length=0)
        ax.grid(axis="y", color=grid, linewidth=0.6)
        for side in ax.spines.values():
            side.set_visible(False)
        ax.set_title(f"{c['symbol'].upper()}  {usd(c['current_price'])}", color=ink, loc="left",
                     fontsize=12, fontweight="bold", pad=8)
        ax.text(1.0, 1.035, f"{'▲' if ch >= 0 else '▼'} {ch:+.1f}% 7d", transform=ax.transAxes,
                ha="right", va="bottom", color=ink2, fontsize=10)
    for ax in list(axes.flat)[len(coins):]:
        ax.set_visible(False)
    fig.text(0.01, 0.005, "7-day price, USD · hourly · Data: CoinGecko", color=ink2, fontsize=8)
    fig.tight_layout(rect=(0, 0.03, 1, 1), h_pad=2.2, w_pad=1.6)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=surface)
    plt.close(fig)
    return buf.getvalue()


def fear_greed() -> str | None:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", headers={"User-Agent": UA}, timeout=15)
        d = r.json()["data"][0]
        return f"{d['value']} · {d['value_classification']}"
    except Exception:
        return None


def run_board(sources: dict, warnings: list[str]) -> bool:
    for name, cfg in channels(sources):
        if not cfg.get("market_board"):
            continue
        if not webhook_ready(cfg):
            print(f"{name}: {cfg['webhook']} not set, crypto board skipped")
            return True
        try:
            coins = board_coins()
            glob = coingecko("global").get("data", {})
        except Exception as exc:  # data problem: warn and try again on the next run
            warnings.append(f"crypto board: {exc} (will retry next run)")
            return False
        fng = fear_greed()
        desc = []
        total = (glob.get("total_market_cap") or {}).get("usd")
        if total:
            chg = glob.get("market_cap_change_percentage_24h_usd") or 0
            desc.append(f"**Total market cap** {fb.money(total)} {'▲' if chg >= 0 else '▼'} {chg:+.2f}% (24h)")
        dom = (glob.get("market_cap_percentage") or {}).get("btc")
        if dom:
            desc.append(f"**Bitcoin dominance** {dom:.1f}%")
        if fng:
            desc.append(f"**Fear & Greed Index** {fng}")
        lines = []
        for c in coins:
            d24 = c.get("price_change_percentage_24h_in_currency", c.get("price_change_percentage_24h")) or 0
            lines.append(f"**{c['symbol'].upper()}** {usd(c['current_price'])} · 24h {d24:+.1f}% · "
                         f"7d {change_7d(c):+.1f}% · cap {fb.money(c.get('market_cap'))}")
        embed = {
            "title": f"🪙 Crypto Board · {fb.day_label(dt.datetime.now(ET).date())}",
            "description": "\n".join(desc),
            "color": COLORS["crypto"],
            "fields": fb.lines_to_fields("Biggest coins by market cap", lines),
            "footer": {"text": "Data: CoinGecko · Fear & Greed: alternative.me · " + fb.NOT_ADVICE},
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        files = None
        try:
            files = [("crypto-board.png", render_board(coins))]
            embed["image"] = {"url": "attachment://crypto-board.png"}
        except Exception as exc:
            warnings.append(f"crypto chart: {exc} (posted without the chart)")
        fb.post(cfg["webhook"], cfg.get("username", "Fiscal"), embed=fb.fit_embed(embed), files=files)
    return True


# ---------------------------------------------------------------- utilities

def check_feeds(sources: dict, state: dict) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    bad = 0
    for name, cfg in channels(sources):
        print(f"\n== #{name} ({cfg['webhook']}: {'set' if os.environ.get(cfg['webhook']) else 'NOT SET'})")
        for src in cfg.get("sources") or []:
            tag = "digest" if src.get("digest") else "post"
            try:
                url, kind, items, shorts = read_source(src, state)
            except Exception as exc:
                bad += 1
                print(f"  FAIL  {src.get('name', '?')} [{tag}]: {exc}")
                continue
            dated = [i for i in items if i["published"]]
            newest = max(dated, key=lambda i: i["published"]) if dated else (items[0] if items else None)
            recent = sum(1 for i in dated if now - i["published"] <= dt.timedelta(days=7))
            note = f", {shorts} Shorts skipped" if shorts else ""
            line = f"  OK    {src.get('name')} [{kind}, {tag}]: {len(items)} items, {recent} in the last 7 days{note}"
            if newest:
                when = newest["published"].astimezone(ET).strftime("%b %d") if newest["published"] else "undated"
                line += f"\n        newest ({when}): {newest['title'][:90]}"
            if str(src.get("youtube", "")).startswith("@"):
                line += f"\n        {src['youtube']} -> {state['youtube_ids'].get(src['youtube'])}"
            print(line)
    print("\n== Crypto board data")
    try:
        coins = board_coins()
        print("  OK    CoinGecko: " + ", ".join(f"{c['symbol'].upper()} {usd(c['current_price'])}" for c in coins))
    except Exception as exc:
        bad += 1
        print(f"  FAIL  CoinGecko: {exc}")
    fng = fear_greed()
    print(f"  OK    Fear & Greed: {fng}" if fng else "  FAIL  Fear & Greed: no response")
    print(f"\n{bad} problem(s) found." if bad else "\nAll sources OK.")


def test_post(sources: dict) -> None:
    for name, cfg in channels(sources):
        if not webhook_ready(cfg):
            print(f"{name}: {cfg['webhook']} not set, skipped")
            continue
        embed = {"title": "✅ Fiscal is connected",
                 "description": f"This channel will receive automated **{cfg.get('label', name)}** posts.",
                 "color": 0x22C55E, "footer": {"text": fb.NOT_ADVICE}}
        fb.post(cfg["webhook"], cfg.get("username", "Fiscal"), embed=embed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fiscal media feeds")
    parser.add_argument("task", choices=["auto", "feeds", "digests", "crypto-board", "check-feeds", "test-post"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    fb.DRY_RUN = args.dry_run or os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true")

    sources, state, warnings = load_sources(), load_state(), []
    now = dt.datetime.now(ET)
    today = now.date().isoformat()
    failed = False
    print(f">>> {args.task} ({now:%a %b %d %H:%M} ET){' [dry run]' if fb.DRY_RUN else ''}")
    try:
        if args.task == "check-feeds":
            check_feeds(sources, state)
        elif args.task == "test-post":
            test_post(sources)
        else:
            if args.task in ("auto", "feeds"):
                run_feeds(sources, state, warnings)
            daily_due = args.task == "auto" and now.hour >= DIGEST_HOUR
            if args.task == "digests" or (daily_due and state["daily"].get("digest") != today):
                if run_digests(sources, state, warnings):
                    state["daily"]["digest"] = today
            if args.task == "crypto-board" or (daily_due and state["daily"].get("board") != today):
                if run_board(sources, warnings):
                    state["daily"]["board"] = today
                elif args.task == "crypto-board":
                    failed = True
    except Exception as exc:
        failed = True
        print(f"!!! {exc}")
    finally:
        save_state(state)
    for w in warnings:  # shows as a yellow note on the GitHub run page; a single bad feed doesn't fail the run
        print(f"::warning::{w}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
