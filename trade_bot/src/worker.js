// Fiscal bot: Discord slash commands on Cloudflare Workers (free plan).
//  - Trade alerts (owner only): post trades to #trade and long-term holds to #invest. Open
//    positions live in Workers KV (binding TRADES), never in the GitHub repo. Cards show no trade
//    numbers (the owner's call); each position keeps an internal id for storage.
//  - Fiscal AI (everyone): /ask-fiscal and /bull-bear, in ai.js, and /analysis, in analysis.js
//    (the owner's /collar for the SPX analysis lives there too).
//  - Alerts (everyone): /alert price and /alert earnings, in alerts.js, checked by a cron trigger.
//
// Trade card rule: show what was bought or sold, the price in, the price out and the result in %.
// Never show position size, share count or contract count.

import {
  API, UserError, NOT_ADVICE, EPHEMERAL, discord, timedFetch, log, usd, signed, trimNum, cleanTicker,
  fmtDate, optionsOf, json, reply,
} from "./shared.js";
import { AI_COMMANDS, handleAiCommand } from "./ai.js";
import { requestAnalysis, setCollar } from "./analysis.js";
import { handleAlert, alertChoices, runAlerts } from "./alerts.js";

export { usd, cleanTicker, fmtDate };

const STYLES = { day: "Day trade", swing: "Swing", medium: "Medium-term", long: "Long-term hold" };
const COLORS = { long: 0x22c55e, short: 0xf97316, hold: 0x3b82f6, win: 0x22c55e, loss: 0xef4444, note: 0x64748b };
const REPLY_BUDGET_MS = 2200;   // answer directly if the work is done within this (Discord allows 3 s)

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("Fiscal trade bot is running.");
    const body = await request.text();
    const valid = await verifySignature(
      env.DISCORD_PUBLIC_KEY,
      request.headers.get("X-Signature-Ed25519"),
      request.headers.get("X-Signature-Timestamp"),
      body,
    );
    if (!valid) return new Response("Invalid request signature", { status: 401 });

    const interaction = JSON.parse(body);
    if (interaction.type === 1) return json({ type: 1 }); // Discord's endpoint check

    const userId = interaction.member?.user?.id || interaction.user?.id;
    const isOwner = Boolean(userId) && userId === String(env.OWNER_ID);

    if (interaction.type === 4) { // autocomplete: the owner's positions, or a member's own alerts
      const choices = interaction.data?.name === "alert"
        ? await alertChoices(env, userId, focusedValue(interaction))
        : isOwner ? await positionChoices(env, focusedValue(interaction)) : [];
      return json({ type: 8, data: { choices } });
    }
    if (interaction.type !== 2) return reply("That action isn't supported.");

    // Fiscal AI and /analysis are for every member; everything else is the owner's alone.
    const name = interaction.data?.name;
    const isAi = AI_COMMANDS.has(name);
    const forMembers = isAi || name === "analysis" || name === "alert";
    if (!forMembers && !isOwner) return reply("Only the server owner can post trades.");

    // Do the work, and answer directly if it finishes in time. Only slow commands fall back to
    // "thinking..." plus an edit afterwards, because that edit is the step that can go missing
    // on Workers (see github.com/discord/discord-api-docs/issues/7936). Work that can take
    // longer, like the AI's answer, comes back as `later` and runs after the reply.
    log("command", { name });
    const work = (name === "analysis" ? requestAnalysis(interaction, env, { isOwner })
      : name === "alert" ? handleAlert(interaction, env, { isOwner })
      : isAi ? handleAiCommand(interaction, env, { isOwner })
        : handleCommand(interaction, env).then((message) => ({ message }))
    ).catch((err) => {
      log("command failed", { name, error: String(err.message || err), expected: err instanceof UserError });
      return { message: err instanceof UserError ? err.message : `Something went wrong: ${err.message || err}` };
    });
    let timer;
    const budget = new Promise((resolve) => {
      timer = setTimeout(() => resolve(null), Number(env.REPLY_BUDGET_MS || REPLY_BUDGET_MS));
    });
    const finished = await Promise.race([work, budget]);
    clearTimeout(timer);
    if (finished) {
      log("replied directly", { name });
      if (finished.later) ctx.waitUntil(runLater(name, finished.later));
      return reply(finished.message);
    }
    log("deferred", { name });
    ctx.waitUntil(work.then(async (result) => {
      await editReply(env, interaction, result.message);
      if (result.later) await runLater(name, result.later);
    }));
    return json({ type: 5, data: { flags: EPHEMERAL } });
  },

  // Cloudflare's cron trigger (every 5 minutes; see scripts/prepare-deploy.mjs) checks the alerts.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runAlerts(env, new Date(event.scheduledTime || Date.now()))
      .catch((err) => log("alerts run failed", { error: String(err.message || err) })));
  },
};

// Background work after the reply. Cloudflare stops it 30 seconds after the reply is sent.
async function runLater(name, later) {
  try {
    await later();
  } catch (err) {
    log("background work failed", { name, error: String(err.message || err) });
  }
}

// ------------------------------------------------------------------ commands

export async function handleCommand(interaction, env) {
  const o = optionsOf(interaction);
  switch (interaction.data.name) {
    case "buy":
      return openPosition(env, { kind: "stock", side: "long", ticker: o.ticker, style: o.style, price: o.price });
    case "short":
      return openPosition(env, { kind: "stock", side: "short", ticker: o.ticker, style: o.style, price: o.price });
    case "option":
      return openPosition(env, {
        kind: "option", side: "long", ticker: o.ticker, style: o.style, price: o.price,
        option: { type: o.type, strike: o.strike, expiry: parseExpiry(o.expiry) },
      });
    case "trim":
      return trimPosition(env, o.position, o.price);
    case "close":
      return closePosition(env, o.position, o.price);
    case "positions":
      return listPositions(env);
    case "collar":
      return setCollar(env, o, parseExpiry);
    default:
      throw new UserError("Unknown command.");
  }
}

async function openPosition(env, p) {
  const ticker = cleanTicker(p.ticker);
  const isHold = p.kind === "stock" && p.side === "long" && p.style === "long";
  if (!isHold && !["day", "swing", "medium"].includes(p.style)) throw new UserError("Pick a trade style.");

  let entry = p.price;
  let underlying = null;
  if (p.kind === "option") {
    if (!(entry > 0)) throw new UserError("Add the option's price (price per share of the contract).");
    underlying = await quote(env, ticker).catch(() => null);
  } else if (!(entry > 0)) {
    entry = await quote(env, ticker);
  }

  const book = isHold ? "hold" : "trade";
  const number = await nextNumber(env, book);
  const pos = {
    id: `${isHold ? "H" : "T"}${number}`,
    number, book, kind: p.kind, side: p.side, ticker, style: p.style, entry,
    option: p.option || null, openedAt: new Date().toISOString(), trims: [],
  };
  const channelId = isHold ? env.INVEST_CHANNEL_ID : env.TRADE_CHANNEL_ID;
  const roleId = isHold ? env.INVEST_ROLE_ID : env.TRADE_ROLE_ID;

  const message = await discord(env, "POST", `/channels/${channelId}/messages`,
    withPing(roleId, openEmbed(pos, underlying)));
  pos.channelId = channelId;
  pos.messageId = message.id;
  try {
    const thread = await discord(env, "POST", `/channels/${channelId}/messages/${message.id}/threads`,
      { name: threadName(pos), auto_archive_duration: 10080 });
    pos.threadId = thread.id;
  } catch (err) {
    pos.threadId = null; // the alert is still posted; discussion just won't have its own thread
  }
  await env.TRADES.put(`pos:${pos.id}`, JSON.stringify(pos));
  return `Posted: ${openTitle(pos)} at ${usd(entry)} in <#${channelId}>.`;
}

async function trimPosition(env, id, price) {
  const pos = await getPosition(env, id);
  const exit = await exitPriceFor(env, pos, price);
  const pct = resultPct(pos, exit);
  const verb = pos.side === "short" ? "Covered part of" : "Trimmed";
  const embed = {
    title: `${verb} ${contractName(pos)}`,
    description: `${verb === "Trimmed" ? "Trimmed" : "Covered part"} at **${usd(exit)}** · ${signed(pct)} from ${usd(pos.entry)}`,
    color: COLORS.note,
    footer: { text: `Still open · ${NOT_ADVICE}` },
    timestamp: new Date().toISOString(),
  };
  await discord(env, "POST", `/channels/${pos.threadId || pos.channelId}/messages`,
    { embeds: [embed], allowed_mentions: { parse: [] } });
  pos.trims.push({ price: exit, at: new Date().toISOString() });
  await env.TRADES.put(`pos:${pos.id}`, JSON.stringify(pos));
  return `Posted: ${embed.title} at ${usd(exit)} (${signed(pct)}). The position stays open.`;
}

async function closePosition(env, id, price) {
  const pos = await getPosition(env, id);
  const exit = await exitPriceFor(env, pos, price);
  const underlying = pos.kind === "option" ? await quote(env, pos.ticker).catch(() => null) : null;
  const pct = resultPct(pos, exit);
  const inVerb = pos.side === "short" ? "Shorted" : "Bought";
  const outVerb = pos.side === "short" ? "Covered" : "Sold";
  const lines = [];
  if (pos.option) lines.push(`Expires ${fmtDate(pos.option.expiry)}`);
  lines.push(`${inVerb} ${usd(pos.entry)} → ${outVerb} **${usd(exit)}**` +
    (underlying ? ` · ${pos.ticker} at ${usd(underlying)}` : ""));
  const embed = {
    title: `${closeTitle(pos)} ${pct >= 0 ? "▲" : "▼"} ${signed(pct)}`,
    description: lines.join("\n"),
    color: pct >= 0 ? COLORS.win : COLORS.loss,
    footer: { text: NOT_ADVICE },
    timestamp: new Date().toISOString(),
  };
  const roleId = pos.book === "hold" ? env.INVEST_ROLE_ID : env.TRADE_ROLE_ID;
  await discord(env, "POST", `/channels/${pos.channelId}/messages`, withPing(roleId, embed));
  if (pos.threadId) {
    await discord(env, "POST", `/channels/${pos.threadId}/messages`,
      { embeds: [embed], allowed_mentions: { parse: [] } }).catch(() => null);
  }
  await env.TRADES.put(`closed:${pos.id}`, JSON.stringify({ ...pos, exit, pct, closedAt: new Date().toISOString() }));
  await env.TRADES.delete(`pos:${pos.id}`);
  return `Posted: ${closeTitle(pos)} at ${usd(exit)} (${signed(pct)}).`;
}

async function listPositions(env) {
  const open = await openPositions(env);
  if (!open.length) return "You have no open positions.";
  const lines = [];
  for (const pos of open) {
    let now = "";
    if (pos.kind === "stock") {
      const live = await quote(env, pos.ticker).catch(() => null);
      if (live) now = ` · now ${usd(live)} (${signed(resultPct(pos, live))})`;
    }
    lines.push(`• ${openTitle(pos)} · in at ${usd(pos.entry)} on ${shortDay(pos.openedAt)}${now}`);
  }
  return lines.join("\n").slice(0, 1900);
}

// ------------------------------------------------------------------ positions (KV)

async function nextNumber(env, book) {
  const key = `next:${book}`;
  const n = parseInt((await env.TRADES.get(key)) || "1", 10);
  await env.TRADES.put(key, String(n + 1));
  return n;
}

async function openPositions(env) {
  const out = [];
  let cursor;
  do {
    const page = await env.TRADES.list({ prefix: "pos:", cursor });
    for (const k of page.keys) {
      const raw = await env.TRADES.get(k.name);
      if (raw) out.push(JSON.parse(raw));
    }
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return out.sort((a, b) => a.openedAt.localeCompare(b.openedAt));
}

async function getPosition(env, id) {
  const raw = id ? await env.TRADES.get(`pos:${String(id).trim()}`) : null;
  if (!raw) throw new UserError("Pick an open position from the list.");
  return JSON.parse(raw);
}

async function positionChoices(env, typed) {
  const needle = (typed || "").toLowerCase();
  return (await openPositions(env))
    .map((pos) => ({ name: `${openTitle(pos)} · in at ${usd(pos.entry)} · ${shortDay(pos.openedAt)}`.slice(0, 100), value: pos.id }))
    .filter((c) => !needle || c.name.toLowerCase().includes(needle))
    .slice(0, 25);
}

async function exitPriceFor(env, pos, price) {
  if (pos.kind === "option") {
    if (price === undefined || price === null || !(price >= 0)) {
      throw new UserError("Add the option's price (price per share of the contract, 0 if it expired worthless).");
    }
    return price;
  }
  return price > 0 ? price : quote(env, pos.ticker);
}

// ------------------------------------------------------------------ card text

export function contractName(pos) {
  return pos.kind === "option" ? `${pos.ticker} $${trimNum(pos.option.strike)} ${pos.option.type}` : pos.ticker;
}

export function openTitle(pos) {
  if (pos.book === "hold") return `New long-term hold: ${pos.ticker}`;
  return `${pos.side === "short" ? "Shorted" : "Bought"} ${contractName(pos)} · ${STYLES[pos.style]}`;
}

function closeTitle(pos) {
  if (pos.book === "hold") return `Sold long-term hold: ${pos.ticker}`;
  return `${pos.side === "short" ? "Covered" : "Sold"} ${contractName(pos)}`;
}

function openEmbed(pos, underlying) {
  const lines = [];
  if (pos.option) lines.push(`Expires ${fmtDate(pos.option.expiry)}`);
  lines.push(`${pos.side === "short" ? "Shorted" : "Bought"} at **${usd(pos.entry)}**` +
    (underlying ? ` · ${pos.ticker} at ${usd(underlying)}` : ""));
  const bearish = pos.side === "short" || pos.option?.type === "put";
  return {
    title: openTitle(pos),
    description: lines.join("\n"),
    color: pos.book === "hold" ? COLORS.hold : bearish ? COLORS.short : COLORS.long,
    footer: { text: NOT_ADVICE },
    timestamp: pos.openedAt,
  };
}

function threadName(pos) {
  const what = pos.kind === "option"
    ? `${pos.ticker} $${trimNum(pos.option.strike)}${pos.option.type === "call" ? "C" : "P"}`
    : `${pos.ticker} ${pos.book === "hold" ? "hold" : pos.side}`;
  return `${what} · ${shortDay(pos.openedAt)}`;
}

// Trades are told apart by the day they were opened ("Nov 24"); the internal id stays in storage only.
function shortDay(iso) {
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "America/New_York" });
}

function withPing(roleId, embed) {
  return {
    content: roleId ? `<@&${roleId}>` : "",
    embeds: [embed],
    allowed_mentions: { roles: roleId ? [String(roleId)] : [] },
  };
}

export function resultPct(pos, exit) {
  const change = ((exit - pos.entry) / pos.entry) * 100;
  return pos.side === "short" ? -change : change;
}

// ------------------------------------------------------------------ parsing

const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

export function parseExpiry(raw, today = new Date()) {
  const s = String(raw || "").trim().toLowerCase().replace(/,/g, " ").replace(/\s+/g, " ");
  let y, m, d, m1;
  if ((m1 = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/))) [y, m, d] = [+m1[1], +m1[2], +m1[3]];
  else if ((m1 = s.match(/^(\d{1,2})\/(\d{1,2})(?:\/(\d{2}|\d{4}))?$/))) {
    [m, d] = [+m1[1], +m1[2]];
    if (m1[3]) y = m1[3].length === 2 ? 2000 + +m1[3] : +m1[3];
  } else if ((m1 = s.match(/^([a-z]{3})[a-z]* (\d{1,2})(?: (\d{4}))?$/)) && MONTHS.includes(m1[1])) {
    [m, d] = [MONTHS.indexOf(m1[1]) + 1, +m1[2]];
    if (m1[3]) y = +m1[3];
  } else {
    throw new UserError(`Couldn't read the expiry "${raw}". Try 12/4 or 2026-12-04.`);
  }
  const base = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  if (!y) {
    y = base.getUTCFullYear();
    if (Date.UTC(y, m - 1, d) < base.getTime()) y += 1; // no year given: the next one coming up
  }
  const date = new Date(Date.UTC(y, m - 1, d));
  if (date.getUTCMonth() !== m - 1 || date.getUTCDate() !== d) throw new UserError(`"${raw}" isn't a real date.`);
  return date.toISOString().slice(0, 10);
}

function focusedValue(interaction) {
  const find = (opts) => {
    for (const o of opts || []) {
      if (o.focused) return o.value;
      const inner = find(o.options); // subcommands nest their options
      if (inner !== undefined) return inner;
    }
    return undefined;
  };
  return find(interaction.data?.options) || "";
}

// ------------------------------------------------------------------ outside services

async function quote(env, ticker) {
  let r;
  try {
    r = await timedFetch(env, `https://finnhub.io/api/v1/quote?symbol=${encodeURIComponent(ticker)}&token=${env.FINNHUB_API_KEY}`);
  } catch (err) {
    log("finnhub failed", { ticker, error: String(err.message || err) });
    throw new UserError(`Couldn't get a live price for ${ticker} right now. Add the price yourself.`);
  }
  if (!r.ok) {
    log("finnhub failed", { ticker, status: r.status });
    throw new UserError(`Couldn't get a live price for ${ticker} right now. Add the price yourself.`);
  }
  const data = await r.json();
  if (!(data.c > 0)) throw new UserError(`No live price found for ${ticker}. Check the ticker, or add the price yourself.`);
  return data.c;
}

async function editReply(env, interaction, content) {
  const appId = interaction.application_id || env.DISCORD_APP_ID;
  try {
    const r = await timedFetch(env, `${API}/webhooks/${appId}/${interaction.token}/messages/@original`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: String(content).slice(0, 2000), allowed_mentions: { parse: [] } }),
    });
    log("edited reply", { status: r.status, ...(r.ok ? {} : { body: (await r.text()).slice(0, 200) }) });
  } catch (err) {
    log("edit reply failed", { error: String(err.message || err) });
  }
}

export async function verifySignature(publicKeyHex, signatureHex, timestamp, body) {
  if (!publicKeyHex || !signatureHex || !timestamp) return false;
  try {
    const key = await crypto.subtle.importKey("raw", hexToBytes(publicKeyHex), { name: "Ed25519" }, false, ["verify"]);
    return await crypto.subtle.verify({ name: "Ed25519" }, key, hexToBytes(signatureHex),
      new TextEncoder().encode(timestamp + body));
  } catch {
    return false;
  }
}

function hexToBytes(hex) {
  const clean = String(hex).trim();
  if (!/^([0-9a-f]{2})+$/i.test(clean)) throw new Error("bad hex");
  return new Uint8Array(clean.match(/../g).map((b) => parseInt(b, 16)));
}
