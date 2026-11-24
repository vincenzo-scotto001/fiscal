// Offline tests for the trade bot: signed requests like Discord sends, with fake Discord,
// Finnhub and KV. Run from trade_bot/:  node --test
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import worker, { parseExpiry, fmtDate, usd, resultPct, cleanTicker } from "../src/worker.js";
import { COMMANDS } from "../src/commands.js";

const OWNER = "111";
console.log = () => {}; // the worker logs every step; keep test output readable
const keys = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
const publicKeyHex = Buffer.from(await crypto.subtle.exportKey("raw", keys.publicKey)).toString("hex");

function memoryKV() {
  const m = new Map();
  return {
    m,
    async get(k) { return m.has(k) ? m.get(k) : null; },
    async put(k, v) { m.set(k, String(v)); },
    async delete(k) { m.delete(k); },
    async list({ prefix = "" } = {}) {
      return { keys: [...m.keys()].filter((k) => k.startsWith(prefix)).map((name) => ({ name })), list_complete: true };
    },
  };
}

let env, calls, livePrice, msgCounter, lastResult, finnhubDelay, hangDiscord;
beforeEach(() => {
  env = {
    DISCORD_PUBLIC_KEY: publicKeyHex, DISCORD_APP_ID: "app1", DISCORD_BOT_TOKEN: "bot-token",
    FINNHUB_API_KEY: "fh", OWNER_ID: OWNER, TRADE_CHANNEL_ID: "trade-ch", INVEST_CHANNEL_ID: "invest-ch",
    TRADE_ROLE_ID: "trade-role", INVEST_ROLE_ID: "invest-role", TRADES: memoryKV(),
  };
  calls = [];
  livePrice = 190.12;
  msgCounter = 0;
  lastResult = null;
  finnhubDelay = 0;
  hangDiscord = false;
  globalThis.fetch = async (url, init = {}) => {
    const call = { url: String(url), method: init.method || "GET", body: init.body ? JSON.parse(init.body) : null };
    calls.push(call);
    if (call.url.startsWith("https://finnhub.io/")) {
      if (finnhubDelay) await new Promise((r) => setTimeout(r, finnhubDelay));
      return Response.json({ c: livePrice });
    }
    if (hangDiscord && call.url.includes("/channels/")) {
      return new Promise((_, reject) => init.signal?.addEventListener("abort", () => reject(new Error("aborted"))));
    }
    if (call.url.includes("/webhooks/")) return new Response(null, { status: 200 });
    if (call.url.endsWith("/threads")) return Response.json({ id: `thread-${msgCounter}` });
    if (call.url.endsWith("/messages")) return Response.json({ id: `msg-${++msgCounter}` });
    return new Response("not mocked", { status: 500 });
  };
});

async function send(payload, { sign = true } = {}) {
  const body = JSON.stringify(payload);
  const ts = String(Math.floor(Date.now() / 1000));
  const headers = { "X-Signature-Timestamp": ts };
  if (sign) {
    const sig = await crypto.subtle.sign({ name: "Ed25519" }, keys.privateKey, new TextEncoder().encode(ts + body));
    headers["X-Signature-Ed25519"] = Buffer.from(sig).toString("hex");
  } else {
    headers["X-Signature-Ed25519"] = "00".repeat(64);
  }
  const pending = [];
  const res = await worker.fetch(new Request("https://bot.example/", { method: "POST", headers, body }), env,
    { waitUntil: (p) => pending.push(p) });
  await Promise.all(pending);
  lastResult = { res, data: res.headers.get("Content-Type")?.includes("json") ? await res.json() : await res.text() };
  return lastResult;
}

const command = (name, options = {}, user = OWNER) => ({
  type: 2, token: "tok", application_id: "app1", member: { user: { id: user } },
  data: { name, options: Object.entries(options).map(([n, value]) => ({ name: n, value })) },
});
const posts = () => calls.filter((c) => c.url.startsWith("https://discord.com/api/v10/channels/") && c.url.endsWith("/messages"));
// The reply you see: sent directly (type 4) when the work is quick, otherwise by editing "thinking...".
const lastReply = () => (lastResult?.data?.type === 4 ? lastResult.data.data.content
  : calls.filter((c) => c.url.includes("/webhooks/")).at(-1)?.body.content);

function checkEmbed(e) {
  assert.ok(e.title.length <= 256 && (e.description || "").length <= 4096);
  const text = JSON.stringify(e).toLowerCase();
  for (const word of ["shares", "contracts", "size", "quantity", "qty"]) {
    assert.ok(!text.includes(word), `card must never mention ${word}: ${text}`);
  }
}

test("health check and signature checks", async () => {
  const res = await worker.fetch(new Request("https://bot.example/"), env, { waitUntil() {} });
  assert.equal(await res.text(), "Fiscal trade bot is running.");
  assert.equal((await send({ type: 1 }, { sign: false })).res.status, 401);
  assert.deepEqual((await send({ type: 1 })).data, { type: 1 });
});

test("only the owner can post", async () => {
  const { data } = await send(command("buy", { ticker: "NVDA", style: "swing" }, "999"));
  assert.equal(data.type, 4);
  assert.match(data.data.content, /Only the server owner/);
  assert.equal(posts().length, 0);
});

test("buy with live price posts to #trade, pings, opens a thread, saves the position", async () => {
  const { data } = await send(command("buy", { ticker: "$nvda", style: "swing" }));
  assert.equal(data.type, 4, "quick commands answer directly, with no 'thinking...' step");
  assert.equal(data.data.flags, 64);
  assert.ok(!calls.some((c) => c.url.includes("/webhooks/")), "no edit needed");
  const [p] = posts();
  assert.ok(p.url.includes("/channels/trade-ch/"));
  assert.equal(p.body.content, "<@&trade-role>");
  assert.deepEqual(p.body.allowed_mentions, { roles: ["trade-role"] });
  const e = p.body.embeds[0];
  checkEmbed(e);
  assert.equal(e.title, "Bought NVDA · Swing");
  assert.equal(e.description, "Bought at **$190.12**");
  assert.match(e.footer.text, /^Trade #1 · Not financial advice$/);
  assert.ok(calls.some((c) => c.url.endsWith("/messages/msg-1/threads") && c.body.name === "NVDA long · #1"));
  const saved = JSON.parse(env.TRADES.m.get("pos:T1"));
  assert.equal(saved.entry, 190.12);
  assert.equal(saved.threadId, "thread-1");
  assert.match(lastReply(), /^Posted: Bought NVDA · Swing at \$190\.12/);
});

test("long-term hold goes to #invest with its own numbering and role", async () => {
  await send(command("buy", { ticker: "AAPL", style: "long", price: 228.5 }));
  const [p] = posts();
  assert.ok(p.url.includes("/channels/invest-ch/"));
  assert.equal(p.body.content, "<@&invest-role>");
  assert.equal(p.body.embeds[0].title, "New long-term hold: AAPL");
  assert.equal(p.body.embeds[0].description, "Bought at **$228.50**");
  assert.match(p.body.embeds[0].footer.text, /^Hold #1/);
  assert.ok(env.TRADES.m.has("pos:H1"));
});

test("option open and close show the contract, its price and the stock price", async () => {
  livePrice = 246.8;
  await send(command("option", { ticker: "TSLA", type: "call", strike: 250, expiry: "2026-12-04", price: 3.1, style: "day" }));
  let e = posts()[0].body.embeds[0];
  checkEmbed(e);
  assert.equal(e.title, "Bought TSLA $250 call · Day trade");
  assert.equal(e.description, "Expires Fri, Dec 4\nBought at **$3.10** · TSLA at $246.80");
  livePrice = 249.9;
  await send(command("close", { position: "T1", price: 4.65 }));
  e = posts()[1].body.embeds[0];
  checkEmbed(e);
  assert.equal(e.title, "Sold TSLA $250 call ▲ +50.0%");
  assert.equal(e.description, "Expires Fri, Dec 4\nBought $3.10 → Sold **$4.65** · TSLA at $249.90");
  assert.equal(posts()[1].body.content, "<@&trade-role>");
  assert.ok(posts()[2].url.includes("/channels/thread-1/"), "close is also posted in the thread");
  assert.equal(posts()[2].body.content, undefined, "no ping inside the thread");
  assert.ok(!env.TRADES.m.has("pos:T1") && env.TRADES.m.has("closed:T1"));
});

test("an option close needs a price", async () => {
  await send(command("option", { ticker: "TSLA", type: "put", strike: 240, expiry: "12/4", price: 2, style: "swing" }));
  assert.equal(posts()[0].body.embeds[0].title, "Bought TSLA $240 put · Swing");
  await send(command("close", { position: "T1" }));
  assert.match(lastReply(), /Add the option's price/);
  assert.equal(posts().length, 1);
});

test("short and cover: a falling price is a gain", async () => {
  await send(command("short", { ticker: "TSLA", style: "swing", price: 251.2 }));
  assert.equal(posts()[0].body.embeds[0].title, "Shorted TSLA · Swing");
  assert.equal(posts()[0].body.embeds[0].description, "Shorted at **$251.20**");
  await send(command("close", { position: "T1", price: 243.4 }));
  const e = posts()[1].body.embeds[0];
  assert.equal(e.title, "Covered TSLA ▲ +3.1%");
  assert.equal(e.description, "Shorted $251.20 → Covered **$243.40**");
  assert.equal(e.color, 0x22c55e);
});

test("losing close is red with a minus sign", async () => {
  await send(command("buy", { ticker: "AAPL", style: "long", price: 228.5 }));
  await send(command("close", { position: "H1", price: 218.9 }));
  const e = posts()[1].body.embeds[0];
  assert.equal(e.title, "Sold long-term hold: AAPL ▼ −4.2%");
  assert.equal(e.color, 0xef4444);
});

test("trim posts only in the thread, with no ping, and keeps the position open", async () => {
  await send(command("buy", { ticker: "NVDA", style: "swing", price: 182.4 }));
  await send(command("trim", { position: "T1", price: 195.1 }));
  const t = posts()[1];
  assert.ok(t.url.includes("/channels/thread-1/"));
  assert.equal(t.body.content, undefined);
  assert.equal(t.body.embeds[0].title, "Trimmed NVDA");
  assert.match(t.body.embeds[0].description, /Trimmed at \*\*\$195\.10\*\* · \+7\.0% from \$182\.40/);
  checkEmbed(t.body.embeds[0]);
  assert.equal(JSON.parse(env.TRADES.m.get("pos:T1")).trims.length, 1);
});

test("autocomplete lists open positions for the owner only, filtered by what's typed", async () => {
  await send(command("buy", { ticker: "NVDA", style: "swing", price: 182.4 }));
  await send(command("short", { ticker: "TSLA", style: "day", price: 251.2 }));
  const auto = (value, user = OWNER) => ({
    type: 4, member: { user: { id: user } },
    data: { name: "close", options: [{ name: "position", value, focused: true }] },
  });
  let { data } = await send(auto(""));
  assert.equal(data.type, 8);
  assert.deepEqual(data.data.choices.map((c) => c.value), ["T1", "T2"]);
  assert.equal(data.data.choices[1].name, "#2 Shorted TSLA · Day trade · in at $251.20");
  ({ data } = await send(auto("tsla")));
  assert.deepEqual(data.data.choices.map((c) => c.value), ["T2"]);
  ({ data } = await send(auto("", "999")));
  assert.deepEqual(data.data.choices, []);
});

test("positions lists open trades with live moves for stocks", async () => {
  await send(command("buy", { ticker: "NVDA", style: "swing", price: 182.4 }));
  livePrice = 200;
  await send(command("positions"));
  assert.match(lastReply(), /\*\*Trade #1\*\* Bought NVDA · Swing · in at \$182\.40 · now \$200\.00 \(\+9\.6%\)/);
});

test("unknown position and bad ticker give friendly errors", async () => {
  await send(command("close", { position: "T99", price: 10 }));
  assert.match(lastReply(), /Pick an open position/);
  await send(command("buy", { ticker: "not a ticker", style: "swing" }));
  assert.match(lastReply(), /doesn't look like a ticker/);
  livePrice = 0;
  await send(command("buy", { ticker: "ZZZZ", style: "swing" }));
  assert.match(lastReply(), /No live price found for ZZZZ/);
  assert.equal(posts().length, 0);
});

test("helpers: expiry parsing, dates, prices, results", () => {
  const today = new Date("2026-11-23T15:00:00Z");
  assert.equal(parseExpiry("2026-12-04", today), "2026-12-04");
  assert.equal(parseExpiry("12/4", today), "2026-12-04");
  assert.equal(parseExpiry("1/15", today), "2027-01-15");
  assert.equal(parseExpiry("1/15/28", today), "2028-01-15");
  assert.equal(parseExpiry("Dec 18", today), "2026-12-18");
  assert.equal(parseExpiry("January 15, 2027", today), "2027-01-15");
  assert.throws(() => parseExpiry("2/30", today), /isn't a real date/);
  assert.throws(() => parseExpiry("next friday", today), /Couldn't read/);
  assert.equal(fmtDate("2026-12-04", today), "Fri, Dec 4");
  assert.equal(fmtDate("2027-01-15", today), "Fri, Jan 15, 2027");
  assert.equal(usd(3.1), "$3.10");
  assert.equal(usd(0.45), "$0.45");
  assert.equal(usd(0.1834), "$0.1834");
  assert.equal(usd(97412), "$97,412.00");
  assert.equal(Math.round(resultPct({ entry: 100, side: "short" }, 90) * 10) / 10, 10);
  assert.equal(cleanTicker("brk.b"), "BRK.B");
});

test("command definitions follow Discord's rules", () => {
  for (const c of COMMANDS) {
    assert.match(c.name, /^[a-z]{1,32}$/);
    assert.ok(c.description.length <= 100, c.name);
    assert.equal(c.default_member_permissions, "0");
    let seenOptional = false;
    for (const o of c.options || []) {
      assert.ok(o.description.length <= 100, `${c.name}.${o.name}`);
      if (!o.required) seenOptional = true;
      else assert.ok(!seenOptional, `${c.name}: required options must come before optional ones`);
    }
  }
});

test("slow commands fall back to 'thinking...' and then edit the reply", async () => {
  env.REPLY_BUDGET_MS = "5";
  finnhubDelay = 40;
  const { data } = await send(command("buy", { ticker: "NVDA", style: "swing" }));
  assert.deepEqual(data, { type: 5, data: { flags: 64 } });
  const edit = calls.filter((c) => c.url.includes("/webhooks/")).at(-1);
  assert.equal(edit.method, "PATCH");
  assert.ok(edit.url.endsWith("/webhooks/app1/tok/messages/@original"));
  assert.match(edit.body.content, /^Posted: Bought NVDA · Swing/);
});

test("a stuck Discord request becomes a clear error instead of endless 'thinking...'", async () => {
  env.REPLY_BUDGET_MS = "5";
  env.FETCH_TIMEOUT_MS = "30";
  hangDiscord = true;
  await send(command("buy", { ticker: "NVDA", style: "swing", price: 100 }));
  assert.match(lastReply(), /Discord didn't answer in time/);
  assert.ok(!env.TRADES.m.has("pos:T1"), "nothing saved when the post didn't go through");
});

test("/positions answers directly", async () => {
  const { data } = await send(command("positions"));
  assert.equal(data.type, 4);
  assert.equal(data.data.content, "You have no open positions.");
});
