// /analysis TICKER (every member): a deep technical + options positioning post in #analysis.
// The post itself is built by analysis_bot.py on GitHub Actions: it draws the charts with matplotlib
// and reads Cboe's option chains (megabytes each), which is too heavy for a Worker. So the Worker
// checks the daily limit, posts a placeholder in #analysis, and starts the "Fiscal analysis"
// workflow, which fills the placeholder in a minute or two later. If that fails, the placeholder
// says so and the use is refunded.
//
// /collar (owner only): the JPM hedged-equity collar strikes that the SPX analysis factors in.
// They're public after each quarter-end roll; enter them once a quarter.

import { UserError, discord, timedFetch, log, cleanTicker, fmtDate, optionsOf } from "./shared.js";
import { caller, checkQuota, charge, refund, leftText, countModelCall, readJSON, etDay, finnhub, TWO_DAYS } from "./ai.js";

const INDEXES = new Set(["SPX", "NDX", "RUT", "VIX"]);
const COLOR = 0x338ced;
const PENDING_MS = 5 * 60 * 1000;   // a run normally takes 1-2 minutes

export async function requestAnalysis(interaction, env, { isOwner }) {
  if (!interaction.guild_id) throw new UserError("Use this inside the Fiscal server.");
  if (!env.GH_DISPATCH_TOKEN || !env.ANALYSIS_CHANNEL_ID || !env.GITHUB_REPO) {
    throw new UserError("/analysis isn't switched on yet.");
  }
  const who = caller(interaction, env, isOwner);
  const ticker = cleanTicker(optionsOf(interaction).ticker);
  const channelId = String(env.ANALYSIS_CHANNEL_ID);
  const day = etDay();
  const cacheKey = `an:${ticker}:${day}`;
  const cached = await readJSON(env, cacheKey);
  if (cached?.done) return { message: `Today's ${ticker} analysis is already up: ${cached.url}` };
  if (cached && Date.now() - cached.at < PENDING_MS) {
    return { message: `The ${ticker} analysis is being put together right now: ${cached.url}` };
  }
  const quota = await checkQuota(env, "an", who, day);
  if (!INDEXES.has(ticker)) {
    // Catch typos before spending a run. If Finnhub is down, let the run find out.
    const q = await finnhub(env, "/quote", { symbol: ticker }).catch(() => null);
    if (q && !(q.c > 0)) {
      throw new UserError(`No market data found for ${ticker}. Check the ticker (US stocks and ETFs, or SPX, NDX, RUT).`);
    }
  }
  const card = await discord(env, "POST", `/channels/${channelId}/messages`, {
    content: `Requested by <@${who.id}>`,
    embeds: [{
      title: `Analysis: ${ticker}`,
      description: "Charting it and reading the options positioning… this takes a minute or two.",
      color: COLOR,
    }],
    allowed_mentions: { parse: [] },
  });
  const url = `https://discord.com/channels/${interaction.guild_id}/${channelId}/${card.id}`;
  await env.TRADES.put(cacheKey, JSON.stringify({ url, at: Date.now(), done: false }), { expirationTtl: TWO_DAYS });
  await charge(env, quota);
  await countModelCall(env, day);
  return {
    message: `Charting ${ticker} now. It'll be in <#${channelId}> in a minute or two: ${url}`,
    later: () => startWorkflow(env, { ticker, channelId, messageId: card.id, who, quota, cacheKey, left: leftText(quota) }),
  };
}

async function startWorkflow(env, job) {
  let r = null;
  try {
    r = await timedFetch(env, `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/analysis.yml/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "fiscal-bot",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ref: "main",
        inputs: {
          task: "request", ticker: job.ticker, channel_id: job.channelId, message_id: job.messageId,
          requester: String(job.who.id), refund: job.quota.key ? `${job.quota.key}|${job.quota.used}` : "",
          cache_key: job.cacheKey, left: job.left,
        },
      }),
    });
  } catch (err) {
    log("github dispatch failed", { error: String(err.message || err) });
  }
  log("github dispatch", { status: r?.status, ticker: job.ticker });
  if (r && (r.status === 204 || r.status === 200)) return;
  if (r) log("github dispatch refused", { status: r.status, detail: (await r.text().catch(() => "")).slice(0, 200) });
  await refund(env, job.quota).catch(() => {});
  await env.TRADES.delete(job.cacheKey).catch(() => {});
  await discord(env, "PATCH", `/channels/${job.channelId}/messages/${job.messageId}`, {
    embeds: [{
      title: `Analysis: ${job.ticker}`,
      description: "Couldn't start this one right now. It didn't count toward the daily limit, so try again in a bit.",
      color: COLOR,
    }],
  }).catch(() => {});
}

// ------------------------------------------------------------------ /collar (owner only)

export async function setCollar(env, o, parseExpiry) {
  if (o.clear) {
    await env.TRADES.delete("collar");
    return "Cleared the JPM collar. The SPX analysis won't mention it until you set a new one.";
  }
  if (o.call === undefined && o.put === undefined && !o.expiry) {
    const c = await readJSON(env, "collar");
    return c
      ? `JPM collar in use: ${describe(c)}. Set the new strikes with /collar after each quarter-end roll.`
      : "No JPM collar saved. After a quarter-end roll, add it with /collar call put put_low expiry.";
  }
  if (!(o.call > 0) || !(o.put > 0) || !o.expiry) {
    throw new UserError("Give the call strike, the put strike and the expiry (put_low is optional).");
  }
  if (o.call <= o.put) throw new UserError("The call strike should be above the put strike.");
  if (o.put_low !== undefined && !(o.put_low > 0 && o.put_low < o.put)) {
    throw new UserError("put_low (the put spread's lower strike) should be below the put strike.");
  }
  const collar = { call: o.call, put: o.put, ...(o.put_low ? { putLow: o.put_low } : {}), expiry: parseExpiry(o.expiry),
    setAt: new Date().toISOString() };
  await env.TRADES.put("collar", JSON.stringify(collar)); // no expiry: stays until the next roll replaces it
  return `Saved the JPM collar: ${describe(collar)}. The SPX analysis factors it in from now on.`;
}

function describe(c) {
  const puts = c.putLow ? `long ${c.put} / short ${c.putLow} put spread` : `long ${c.put} put`;
  return `short ${c.call} call + ${puts}, expiring ${fmtDate(c.expiry)}`;
}
