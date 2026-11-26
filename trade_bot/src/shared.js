// Helpers shared by the trade commands (worker.js) and Fiscal AI (ai.js).

export const API = "https://discord.com/api/v10";
export const NOT_ADVICE = "Not financial advice";
export const EPHEMERAL = 64;
export const FETCH_TIMEOUT_MS = 8000; // give up on a stuck Discord or Finnhub request after this

// An error whose message is meant for the person who ran the command.
export class UserError extends Error {}

// ------------------------------------------------------------------ Discord (bot token)

export async function discord(env, method, path, body) {
  for (let attempt = 0; attempt < 3; attempt++) {
    let r;
    try {
      r = await timedFetch(env, `${API}${path}`, {
        method,
        headers: { Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`, "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (err) {
      log("discord failed", { method, path: safePath(path), error: String(err.message || err) });
      throw new UserError("Discord didn't answer in time. Check the channel before trying again, in case it posted anyway.");
    }
    log("discord", { method, path: safePath(path), status: r.status });
    if (r.status === 429) {
      const wait = Number((await r.json().catch(() => ({}))).retry_after || 1);
      await new Promise((res) => setTimeout(res, Math.min(wait, 5) * 1000));
      continue;
    }
    if (r.status === 403) {
      throw new UserError("The bot isn't allowed to post there. Give the Fiscal bot Send Messages, Embed Links and Create Public Threads in that channel.");
    }
    if (!r.ok) throw new Error(`Discord returned HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
    return r.status === 204 ? {} : r.json();
  }
  throw new Error("Discord kept rate-limiting");
}

// ------------------------------------------------------------------ network and logs

export async function timedFetch(env, url, init = {}, timeoutMs) {
  const controller = new AbortController();
  const limit = Number(timeoutMs || env.FETCH_TIMEOUT_MS || FETCH_TIMEOUT_MS);
  const timer = setTimeout(() => controller.abort(new Error("timed out")), limit);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Logs show up in Cloudflare under Workers & Pages > fiscal-trade-bot > Logs. Never log tokens or keys.
export function log(event, details = {}) {
  console.log(JSON.stringify({ event, ...details }));
}

export function safePath(path) {
  return path.replace(/\/webhooks\/[^/]+\/[^/]+/, "/webhooks/<app>/<token>");
}

// ------------------------------------------------------------------ formatting and parsing

export function usd(x) {
  const digits = x >= 1 ? 2 : Math.min(4, Math.max(2, (String(x).split(".")[1] || "").length));
  return `$${Number(x).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function signed(pct) {
  const r = Math.round(pct * 10) / 10;
  return `${r > 0 ? "+" : r < 0 ? "−" : ""}${Math.abs(r).toFixed(1)}%`;
}

export function trimNum(x) {
  return Number.isInteger(x) ? String(x) : String(Number(x.toFixed(2)));
}

export function cleanTicker(raw) {
  const t = String(raw || "").trim().replace(/^\$/, "").toUpperCase();
  if (!/^[A-Z][A-Z0-9.\-]{0,9}$/.test(t)) throw new UserError(`"${raw}" doesn't look like a ticker.`);
  return t;
}

export function fmtDate(iso, today = new Date()) {
  const date = new Date(`${iso}T12:00:00Z`);
  const opts = { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" };
  if (date.getUTCFullYear() !== today.getUTCFullYear()) opts.year = "numeric";
  return date.toLocaleDateString("en-US", opts);
}

export function optionsOf(interaction) {
  const out = {};
  for (const o of interaction.data?.options || []) out[o.name] = o.value;
  return out;
}

// ------------------------------------------------------------------ interaction responses

export function json(data) {
  return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
}

export function reply(content) {
  return json({ type: 4, data: { content, flags: EPHEMERAL, allowed_mentions: { parse: [] } } });
}
