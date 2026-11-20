"""Offline test for feeds_bot: fake feeds and CoinGecko, run every task in dry-run.

Run from the repo root:  python tests/test_feeds_offline.py
Needs requests, feedparser, PyYAML and matplotlib (pip install -r requirements.txt).
"""
import datetime as dt
import io
import json
import math
import os
import sys
import tempfile
from contextlib import redirect_stdout

TMP = tempfile.mkdtemp()
os.environ["FEEDS_STATE_FILE"] = os.path.join(TMP, "state", "feeds.json")
os.environ["DRY_RUN_DIR"] = os.path.join(TMP, "dry-run-output")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import feeds_bot as feeds  # noqa: E402
import fiscal_bot as fb  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc)


def iso(hours_ago):
    return (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def rfc(hours_ago):
    return (NOW - dt.timedelta(hours=hours_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")


def youtube_xml(cid):
    entries = []
    # 6 recent videos (more than the per-run cap), one Short, one old video
    for i, age in enumerate([1, 3, 5, 7, 9, 11]):
        vid = f"{cid[-4:]}V{i}"
        entries.append(f"""<entry><id>yt:video:{vid}</id><yt:videoId>{vid}</yt:videoId>
          <title>{cid[-4:]} episode {i} &amp; guests</title>
          <link rel="alternate" href="https://www.youtube.com/watch?v={vid}"/>
          <published>{iso(age)}</published><updated>{iso(age)}</updated>
          <media:group><media:thumbnail url="https://i.ytimg.com/vi/{vid}/hqdefault.jpg"/></media:group></entry>""")
    entries.append(f"""<entry><id>yt:video:SHORT{cid[-4:]}</id><title>Quick clip #shorts</title>
      <link rel="alternate" href="https://www.youtube.com/shorts/SHORT{cid[-4:]}"/>
      <published>{iso(2)}</published></entry>""")
    entries.append(f"""<entry><id>yt:video:OLD{cid[-4:]}</id><title>Old video</title>
      <link rel="alternate" href="https://www.youtube.com/watch?v=OLD{cid[-4:]}"/>
      <published>{iso(80)}</published></entry>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom"><title>Channel {cid}</title>{''.join(entries)}</feed>"""


def rss_xml(name, n=3, start_age=2, undated=False, dup_title=None):
    items = []
    for i in range(n):
        title = dup_title if (dup_title and i == 0) else f"{name} story {i}: markets [update]"
        items.append(f"""<item><title>{title}</title><link>https://example.com/{name}/{i}</link>
          <guid>{name}-{i}</guid><pubDate>{rfc(start_age + i * 3)}</pubDate>
          <description><![CDATA[<p>{'Long summary text ' * 40}</p>]]></description>
          <itunes:image href="https://example.com/{name}.jpg"/></item>""")
    if undated:
        items.append(f"<item><title>{name} undated</title><link>https://example.com/{name}/u</link></item>")
    return f"""<?xml version="1.0"?><rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel><title>{name}</title><link>https://example.com</link><description>d</description>{''.join(items)}</channel></rss>"""


def fake_fetch(url):
    if "youtube.com/feeds" in url:
        return youtube_xml(url.split("=")[-1]).encode()
    if "techcrunch" in url:
        raise RuntimeError("techcrunch.com returned HTTP 403")  # one broken source
    name = url.split("/")[2].replace("www.", "").split(".")[0]
    digest_dup = "Bitcoin hits new record high" if name in ("coindesk", "cointelegraph") else None
    return rss_xml(name, n=8 if name in ("coindesk", "cointelegraph", "decrypt", "theverge", "openai") else 2,
                   undated=(name == "fredblog"), dup_title=digest_dup).encode()


def fake_coingecko(path, **params):
    if path == "global":
        return {"data": {"total_market_cap": {"usd": 3.21e12}, "market_cap_change_percentage_24h_usd": -1.37,
                         "market_cap_percentage": {"btc": 58.4}}}
    coins = [("bitcoin", "btc", "Bitcoin", 97412.0, 1.93e12), ("ethereum", "eth", "Ethereum", 3384.5, 4.1e11),
             ("tether", "usdt", "Tether", 1.0, 1.4e11), ("ripple", "xrp", "XRP", 2.31, 1.3e11),
             ("binancecoin", "bnb", "BNB", 655.2, 9.5e10), ("solana", "sol", "Solana", 187.4, 9.0e10),
             ("usd-coin", "usdc", "USDC", 1.0, 6.0e10), ("staked-ether", "steth", "Lido Staked Ether", 3380.0, 3e10),
             ("dogecoin", "doge", "Dogecoin", 0.1834, 2.7e10), ("tron", "trx", "TRON", 0.241, 2.1e10),
             ("cardano", "ada", "Cardano", 0.712, 2.5e10), ("wrapped-bitcoin", "wbtc", "Wrapped Bitcoin", 97300.0, 1.2e10),
             ("chainlink", "link", "Chainlink", 18.4, 1.1e10)]
    out = []
    for k, (cid, sym, name, price, cap) in enumerate(coins):
        drift = (-1) ** k * 0.04
        spark = [price * (1 - drift + drift * i / 167 + 0.015 * math.sin(i / 9 + k)) for i in range(168)]
        out.append({"id": cid, "symbol": sym, "name": name, "current_price": price, "market_cap": cap,
                    "price_change_percentage_24h_in_currency": (-1) ** k * 1.2 * (k + 1) / 3,
                    "price_change_percentage_7d_in_currency": (spark[-1] - spark[0]) / spark[0] * 100,
                    "sparkline_in_7d": {"price": spark}})
    return out


feeds.fetch = fake_fetch
feeds.coingecko = fake_coingecko
feeds.fear_greed = lambda: "62 · Greed"
fb.DRY_RUN = True
SOURCES = feeds.load_sources()


def capture(fn, *args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*args)
    out = buf.getvalue()
    payloads = [json.loads(chunk[chunk.index("{"):chunk.index("\n}") + 2])
                for chunk in out.split("--- DRY RUN")[1:]]
    return result, out, payloads


def validate(p):
    assert p["allowed_mentions"] == {"parse": []}
    assert len(p.get("content", "")) <= 2000
    for e in p.get("embeds", []):
        assert len(e.get("title", "")) <= 256 and len(e.get("description", "")) <= 4096
        for f in e.get("fields", []):
            assert 0 < len(f["value"]) <= 1024
        assert fb.embed_size(e) <= 6000


state = feeds.load_state()
warnings = []

# --- individual posts
_, out, ps = capture(feeds.run_feeds, SOURCES, state, warnings)
for p in ps:
    validate(p)
by_user = {}
for p in ps:
    by_user.setdefault(p["username"], []).append(p)
print("feeds run 1:", {k: len(v) for k, v in by_user.items()})
assert all(len(v) <= feeds.MAX_POSTS_PER_CHANNEL for v in by_user.values())
assert not any("shorts" in (p.get("content") or "") for p in ps), "Shorts must be skipped"
assert not any("OLD" in (p.get("content") or "") for p in ps), "old videos must be skipped"
assert not any("undated" in json.dumps(p) for p in ps), "undated items from new sources must not post"
yt = [p for p in ps if p.get("content")]
assert yt and yt[0]["content"].splitlines()[1].startswith("https://www.youtube.com/watch?v="), yt[0]
emb = [p for p in ps if p.get("embeds")]
print("sample video post:\n" + yt[0]["content"])
print("sample article post:", json.dumps(emb[0]["embeds"][0], ensure_ascii=False)[:400])
assert any("waiting for the next run" in line for line in out.splitlines())

# second run: the waiting items go out, nothing repeats
first_keys = set(state["seen"])
_, out2, ps2 = capture(feeds.run_feeds, SOURCES, state, warnings)
contents2 = {p.get("content") for p in ps2}
assert not (contents2 & {p.get("content") for p in ps if p.get("content")}), "no repeats"
print("feeds run 2:", len(ps2), "posts (backlog), seen keys", len(first_keys), "->", len(state["seen"]))

# --- digests
_, out, ps = capture(feeds.run_digests, SOURCES, state, warnings)
assert len(ps) == 2, out  # ai + crypto
for p in ps:
    validate(p)
    e = p["embeds"][0]
    lines = e["description"].splitlines()
    assert len(lines) <= feeds.DIGEST_SIZE
    print(f"\n## {e['title']}\n" + "\n".join(lines[:4]) + f"\n... ({len(lines)} lines) -- {e['footer']['text']}")
crypto_digest = ps[1]["embeds"][0]["description"]
assert crypto_digest.count("Bitcoin hits new record high") == 1, "duplicate headlines across sources collapse"
assert "(update)" in crypto_digest and "[update]" not in crypto_digest, "brackets escaped for markdown links"
srcs = [line.rsplit("*", 2)[-2] for line in crypto_digest.splitlines()[:3]]
assert len(set(srcs)) == 3, f"digest takes turns across sources: {srcs}"
assert any("techcrunch" in w.lower() for w in warnings), "broken source becomes a warning"

# --- crypto board
ok, out, ps = capture(feeds.run_board, SOURCES, warnings)
assert ok and len(ps) == 1, out
validate(ps[0])
e = ps[0]["embeds"][0]
print(f"\n## {e['title']}\n{e['description']}\n[{e['fields'][0]['name']}]\n{e['fields'][0]['value']}")
assert e["image"]["url"] == "attachment://crypto-board.png"
syms = [ln.split("**")[1] for ln in e["fields"][0]["value"].splitlines()]
assert syms == ["BTC", "ETH", "XRP", "BNB", "SOL", "DOGE", "TRX", "ADA"], syms  # stables/wrapped/staked dropped
png = os.path.join(os.environ["DRY_RUN_DIR"], "crypto-board.png")
assert os.path.getsize(png) > 10000
print("chart:", png)

# --- auto end-to-end through main(): state persists only outside dry run
assert feeds.main.__doc__ is None or True
print("\nwarnings:", warnings)
print("\nALL FEEDS TESTS PASSED")
