// /alert (every member): price alerts ("tell me when NVDA goes above 200") and earnings alerts
// (a reminder before a company reports, then its results when they're out), sent by DM. If a
// member's DMs are closed, the bot pings them in the channel where they set the alert instead.
//
// A Cloudflare cron trigger calls runAlerts() every 5 minutes (see scripts/prepare-deploy.mjs):
//  - price checks while the US market is open (Finnhub's market status covers holidays)
//  - earnings reminders once a day after the close, for reports on the next trading day
//  - earnings results before the open and after the close
// Every alert lives in one KV key ("alerts"), so a run is one read and at most one write.

import { UserError, discord, log, usd, signed, cleanTicker, fmtDate } from "./shared.js";
import { caller, finnhub, etDay } from "./ai.js";

const KEY = "alerts";
const LIMIT = { member: 5, unlimited: 25 };
const MAX_TICKERS_PER_RUN = 25;  // the free plan allows 50 outside requests per run
const MAX_NOTICES_PER_RUN = 10;  // each DM is up to 2 requests; the rest go out on the next run
const EXPIRE_DAYS = 90;          // price alerts that never trigger are dropped after this
const ET = "America/New_York";
const DM_TIP = "If your DMs from server members are off, I'll ping you in this channel instead.";

export async function handleAlert(interaction, env, { isOwner }) {
  if (!interaction.guild_id) throw new UserError("Use this inside the Fiscal server.");
  const who = caller(interaction, env, isOwner);
  const sub = interaction.data.options?.[0] || {};
  const o = Object.fromEntries((sub.options || []).map((x) => [x.name, x.value]));
  switch (sub.name) {
    case "price": return { message: await addPrice(env, who, interaction.channel_id, o) };
    case "earnings": return { message: await addEarnings(env, who, interaction.channel_id, o) };
    case "list": return { message: await listAlerts(env, who) };
    case "remove": return { message: await removeAlert(env, who, o.alert) };
    default: throw new UserError("Pick price, earnings, list or remove.");
  }
}

async function addPrice(env, who, channel, o) {
  const ticker = cleanTicker(o.ticker);
  const target = Number(o.price);
  if (!(target > 0)) throw new UserError("Add the price to watch for, like 200.");
  const dir = o.when === "below" ? "below" : "above";
  const q = await liveQuote(env, ticker);
  if (dir === "above" ? q.c >= target : q.c <= target) {
    return `${ticker} is already ${dir} ${usd(target)} (now ${usd(q.c)}). Pick a level it hasn't reached yet.`;
  }
  const state = await load(env);
  checkLimit(state, who);
  state.items.push({ id: `a${state.next++}`, user: who.id, kind: "price", ticker, dir, price: target, ref: q.c,
    at: new Date().toISOString(), channel });
  await save(env, state);
  return `Alert set: **${ticker}** ${dir} ${usd(target)} (now ${usd(q.c)}). I'll DM you when it crosses during market hours. ${DM_TIP}`;
}

async function addEarnings(env, who, channel, o) {
  const ticker = cleanTicker(o.ticker);
  const state = await load(env);
  if (state.items.some((a) => a.user === who.id && a.kind === "earnings" && a.ticker === ticker)) {
    return `You already have an earnings alert for ${ticker}.`;
  }
  await liveQuote(env, ticker);
  checkLimit(state, who);
  const today = etDay();
  const cal = await finnhub(env, "/calendar/earnings", { symbol: ticker, from: today, to: shiftDay(today, 120) }).catch(() => null);
  const next = (cal?.earningsCalendar || []).filter((e) => e.date >= today).sort((a, b) => a.date.localeCompare(b.date))[0];
  state.items.push({ id: `a${state.next++}`, user: who.id, kind: "earnings", ticker, at: new Date().toISOString(), channel });
  await save(env, state);
  const when = next ? `: next report ${fmtDate(next.date)}${timing(next.hour)}` : ". No report date is out yet";
  return `Earnings alert set for **${ticker}**${when}. I'll DM you the day before it reports and again when the results are out, every quarter until you remove it. ${DM_TIP}`;
}

async function listAlerts(env, who) {
  const mine = (await load(env)).items.filter((a) => a.user === who.id);
  if (!mine.length) return "You have no alerts. Add one with /alert price or /alert earnings.";
  const max = who.unlimited ? LIMIT.unlimited : LIMIT.member;
  return [`Your alerts (${mine.length} of ${max}):`, ...mine.map((a) => `• ${describe(a)}`)].join("\n").slice(0, 1900);
}

async function removeAlert(env, who, id) {
  const state = await load(env);
  const a = state.items.find((x) => x.id === String(id || "").trim() && x.user === who.id);
  if (!a) throw new UserError("Pick one of your alerts from the list.");
  state.items = state.items.filter((x) => x !== a);
  await save(env, state);
  return `Removed: ${describe(a)}.`;
}

export async function alertChoices(env, userId, typed) {
  const needle = String(typed || "").toLowerCase();
  return (await load(env)).items
    .filter((a) => a.user === userId)
    .map((a) => ({ name: describe(a).replace(/\*\*/g, "").slice(0, 100), value: a.id }))
    .filter((c) => !needle || c.name.toLowerCase().includes(needle))
    .slice(0, 25);
}

function describe(a) {
  return a.kind === "price"
    ? `**${a.ticker}** ${a.dir} ${usd(a.price)} (set ${shortDay(a.at)} at ${usd(a.ref)})`
    : `**${a.ticker}** earnings`;
}

function checkLimit(state, who) {
  const mine = state.items.filter((a) => a.user === who.id).length;
  const max = who.unlimited ? LIMIT.unlimited : LIMIT.member;
  if (mine >= max) {
    throw new UserError(`You have ${mine} alerts, the most allowed${who.unlimited ? "" : ` (Premium members get ${LIMIT.unlimited})`}. Remove one with /alert remove.`);
  }
}

async function liveQuote(env, ticker) {
  const q = await finnhub(env, "/quote", { symbol: ticker }).catch(() => null);
  if (!q) throw new UserError("Couldn't reach the market data right now. Try again in a bit.");
  if (!(q.c > 0)) throw new UserError(`No market data found for ${ticker}. Check the ticker (US stocks and ETFs).`);
  return q;
}

// ------------------------------------------------------------------ the cron run

export async function runAlerts(env, now = new Date()) {
  const state = await load(env);
  if (!state.items.length) return { sent: 0 };
  const clock = etClock(now);
  if (clock.weekday === "Sat" || clock.weekday === "Sun") return { sent: 0 };
  const day = etDay(now);
  const t = clock.minutes;
  const run = { sent: 0, changed: false, now };
  const cutoff = now.getTime() - EXPIRE_DAYS * 86400e3;
  const before = state.items.length;
  state.items = state.items.filter((a) => a.kind !== "price" || Date.parse(a.at) >= cutoff);
  run.changed = state.items.length !== before;

  if (t >= 9 * 60 + 30 && t < 16 * 60 + 5) await checkPrices(env, state, run);

  state.jobs = state.jobs || {};
  const due = [];
  if (t >= 9 * 60 && t < 9 * 60 + 30 && state.jobs.resultsAm !== day) due.push("resultsAm");
  if (t >= 16 * 60 + 5 && state.jobs.remind !== day) due.push("remind");
  if (t >= 16 * 60 + 45 && state.jobs.resultsPm !== day) due.push("resultsPm");
  if (due.length && state.items.some((a) => a.kind === "earnings")) {
    const finished = await checkEarnings(env, state, run, day, due.includes("remind"),
      due.includes("resultsAm") || due.includes("resultsPm"));
    if (finished) for (const job of due) state.jobs[job] = day;
    run.changed = true;
  } else if (due.length) {
    for (const job of due) state.jobs[job] = day;
    run.changed = true;
  }
  if (run.changed) await save(env, state);
  log("alerts run", { day, minutes: t, sent: run.sent, alerts: state.items.length });
  return { sent: run.sent };
}

async function checkPrices(env, state, run) {
  const watching = state.items.filter((a) => a.kind === "price");
  if (!watching.length) return;
  const status = await finnhub(env, "/stock/market-status", { exchange: "US" }).catch(() => null);
  if (status && status.isOpen === false) return; // a holiday or an early close
  const tickers = [...new Set(watching.map((a) => a.ticker))];
  const start = (state.rot || 0) % tickers.length;
  const pick = [...tickers.slice(start), ...tickers.slice(0, start)].slice(0, MAX_TICKERS_PER_RUN);
  if (tickers.length > MAX_TICKERS_PER_RUN) {
    state.rot = start + pick.length; // take turns, and remember whose turn it is
    run.changed = true;
  }
  const quotes = Object.fromEntries(await Promise.all(
    pick.map(async (t) => [t, await finnhub(env, "/quote", { symbol: t }).catch(() => null)])));
  for (const a of watching) {
    const q = quotes[a.ticker];
    if (!(q?.c > 0) || !(a.dir === "above" ? q.c >= a.price : q.c <= a.price)) continue;
    if (run.sent >= MAX_NOTICES_PER_RUN) break;
    const move = Number.isFinite(q.dp) ? ` (${signed(q.dp)} today)` : "";
    await notify(env, state, a, `🔔 **${a.ticker}** is ${a.dir} ${usd(a.price)}: now ${usd(q.c)}${move}. ` +
      `You set this alert on ${shortDay(a.at)}, when it was ${usd(a.ref)}. Not financial advice.`);
    run.sent++;
    state.items = state.items.filter((x) => x !== a);
    run.changed = true;
  }
}

// Returns false if it ran out of room this run, so the job runs again next time.
async function checkEarnings(env, state, run, day, remind, results) {
  const watching = state.items.filter((a) => a.kind === "earnings");
  const tickers = [...new Set(watching.map((a) => a.ticker))].slice(0, MAX_TICKERS_PER_RUN);
  const nextDay = nextTradingDay(day);
  const rows = Object.fromEntries(await Promise.all(tickers.map(async (t) => {
    const cal = await finnhub(env, "/calendar/earnings", { symbol: t, from: shiftDay(day, -3), to: nextDay }).catch(() => null);
    return [t, (cal?.earningsCalendar || []).filter((e) => e.symbol === t || !e.symbol)];
  })));
  for (const a of watching) {
    for (const e of rows[a.ticker] || []) {
      if (run.sent >= MAX_NOTICES_PER_RUN) return false;
      if (remind && e.date === nextDay && a.lastReminder !== e.date) {
        const when = nextDay === shiftDay(day, 1) ? "tomorrow" : `on ${fmtDate(e.date)}`;
        const est = [Number.isFinite(e.epsEstimate) ? `EPS ${eps(e.epsEstimate)}` : "",
          Number.isFinite(e.revenueEstimate) ? `revenue ${bigUsd(e.revenueEstimate)}` : ""].filter(Boolean).join(", ");
        await notify(env, state, a, `📅 **${a.ticker}** reports earnings ${when}${timing(e.hour)}.` +
          `${est ? ` Estimates: ${est}.` : ""} Not financial advice.`);
        a.lastReminder = e.date;
        run.sent++;
      }
      if (results && e.date <= day && e.date >= a.at.slice(0, 10) && Number.isFinite(e.epsActual) && a.lastResult !== e.date) {
        await notify(env, state, a, resultText(a.ticker, e));
        a.lastResult = e.date;
        run.sent++;
      }
    }
  }
  return true;
}

function resultText(ticker, e) {
  let eps_ = `EPS ${eps(e.epsActual)}`;
  if (Number.isFinite(e.epsEstimate)) {
    const diff = e.epsActual - e.epsEstimate;
    const verdict = Math.abs(diff) < 0.005 ? "in line" : diff > 0 ? "beat" : "miss";
    const pct = e.epsEstimate !== 0 ? ` by ${Math.abs(diff / e.epsEstimate * 100).toFixed(1)}%` : "";
    eps_ += ` vs ${eps(e.epsEstimate)} expected (${verdict}${verdict === "in line" ? "" : pct})`;
  }
  const rev = Number.isFinite(e.revenueActual)
    ? ` Revenue ${bigUsd(e.revenueActual)}${Number.isFinite(e.revenueEstimate) ? ` vs ${bigUsd(e.revenueEstimate)} expected` : ""}.`
    : "";
  return `📊 **${ticker}** reported (${fmtDate(e.date)}${timing(e.hour)}): ${eps_}.${rev} Not financial advice.`;
}

async function notify(env, state, a, text) {
  state.dm = state.dm || {};
  try {
    let channel = state.dm[a.user];
    if (!channel) {
      channel = (await discord(env, "POST", "/users/@me/channels", { recipient_id: a.user })).id;
      state.dm[a.user] = channel;
    }
    await discord(env, "POST", `/channels/${channel}/messages`, { content: text, allowed_mentions: { parse: [] } });
    return "dm";
  } catch (err) {
    delete state.dm[a.user];
    log("alert dm failed", { user: a.user, error: String(err.message || err) });
  }
  if (!a.channel) return null;
  await discord(env, "POST", `/channels/${a.channel}/messages`,
    { content: `<@${a.user}> ${text}`, allowed_mentions: { users: [String(a.user)] } })
    .catch((err) => log("alert ping failed", { user: a.user, error: String(err.message || err) }));
  return "channel";
}

// ------------------------------------------------------------------ storage and small helpers

async function load(env) {
  try {
    const s = JSON.parse((await env.TRADES.get(KEY)) || "null");
    if (s && Array.isArray(s.items)) return s;
  } catch { /* start fresh */ }
  return { next: 1, items: [] };
}

async function save(env, state) {
  await env.TRADES.put(KEY, JSON.stringify(state));
}

function etClock(now) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: ET, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(now).map((p) => [p.type, p.value]));
  return { weekday: parts.weekday, minutes: Number(parts.hour) * 60 + Number(parts.minute) };
}

function shiftDay(day, days) {
  const d = new Date(`${day}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function nextTradingDay(day) {
  let d = shiftDay(day, 1);
  while ([0, 6].includes(new Date(`${d}T12:00:00Z`).getUTCDay())) d = shiftDay(d, 1);
  return d;
}

function shortDay(iso) {
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: ET });
}

function timing(hour) {
  return hour === "bmo" ? " before the open" : hour === "amc" ? " after the close" : "";
}

function eps(x) {
  return `${x < 0 ? "-" : ""}$${Math.abs(x).toFixed(2)}`;
}

function bigUsd(x) {
  const a = Math.abs(x);
  for (const [div, unit] of [[1e12, "T"], [1e9, "B"], [1e6, "M"]]) if (a >= div) return `$${(x / div).toFixed(2)}${unit}`;
  return usd(x);
}
