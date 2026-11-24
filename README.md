# Fiscal bot: automated earnings and macro posts

Posts to two Discord channels on a schedule, for free (GitHub Actions + Discord webhooks + free API keys).

| When (New York time) | Channel | Post |
|---|---|---|
| Sunday 6:07 PM | #macro | 🗓️ Macro Week Ahead: CPI, jobs, GDP, Fed meetings, holidays |
| Sunday 6:07 PM | #earnings | 📅 Earnings Week Ahead: biggest reporters each day, before open / after close |
| Weekdays 7:41 AM | #macro | ☕ Morning Brief: last close for S&P/Nasdaq/Dow/VIX/yields, plus today's data |
| Weekdays 7:41 AM | #earnings | 📊 Earnings Today: EPS and revenue estimates for today's reporters |
| Weekdays 8:07 PM | #earnings | 🧾 Earnings Results: beats and misses vs. estimates |

GitHub often starts scheduled runs late and skips many of them, so each post gets a chance every 15 minutes inside its window.
The bot only posts inside the right window (morning brief 7 AM–noon, results after 8 PM, week ahead Sunday after 6 PM) and remembers what it already posted, so nothing doubles.
Nothing posts on weekends or market holidays, and quiet days are skipped instead of posting an empty message.

---

## Setup (about 20 minutes)

### 1. Make #earnings and #macro read-only, with threads for discussion
For each channel: **hover over the channel → ⚙️ Edit Channel → Permissions → Advanced permissions → @everyone**, then set:

- ❌ **Send Messages**: members can't post in the feed
- ✅ **Create Public Threads**: members can start a discussion on a post
- ✅ **Send Messages in Threads**: and reply inside it
- ✅ **Add Reactions**

Save. Members discuss a post by hovering over it and clicking **Create Thread**. You (the owner) can still post normally.
Do the same for #trade so only you post trades and members discuss each one in its own thread.

### 2. Create a webhook in each channel
1. Hover over **#earnings → ⚙️ Edit Channel → Integrations → Webhooks → New Webhook**.
2. Click the new webhook and name it `Fiscal Earnings`. Check that the channel is **#earnings**, then click **Copy Webhook URL**.
3. Paste it somewhere temporary. Repeat for **#macro** (name it `Fiscal Macro`).

> Treat webhook URLs like passwords: anyone who has one can post in that channel. If one leaks, delete the webhook and make a new one.

### 3. Get two free API keys
- **Finnhub** (earnings): sign up at https://finnhub.io/register. Your API key is on the dashboard.
- **FRED** (economic calendar and market closes): make an account at https://fredaccount.stlouisfed.org, then go to **API Keys → Request API Key**.

### 4. Put the code on GitHub
1. On GitHub: **New repository** → name it `fiscal-bot` → set it to **Private** → **Create repository**.
2. Click **uploading an existing file** and drag in `fiscal_bot.py`, `requirements.txt` and `README.md`. Click **Commit changes**.
3. Click **Add file → Create new file**. In the name box type `.github/workflows/fiscal.yml` (the slashes create the folders). Paste in the contents of `fiscal.yml` and click **Commit changes**.

### 5. Add your keys as secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**. Add these four, with the names exactly as written:

| Name | Value |
|---|---|
| `FINNHUB_API_KEY` | your Finnhub key |
| `FRED_API_KEY` | your FRED key |
| `DISCORD_WEBHOOK_EARNINGS` | the #earnings webhook URL |
| `DISCORD_WEBHOOK_MACRO` | the #macro webhook URL |

### 6. Test it
Go to the **Actions** tab, pick **Fiscal feeds** on the left, click **Run workflow**, choose a task and click **Run workflow** again.

1. `test-post`: a ✅ "Fiscal is connected" message should show up in both channels.
2. `check`: posts nothing. Open the run and the **Run Fiscal bot** step to see what the data sources return. Send that log to Claude so the release names can be fine-tuned.
3. `earnings-today` or `macro-morning`: a real post. Tick **dry run** first if you'd rather preview it in the log.

From then on it runs by itself. If a run fails, GitHub emails you.

---

## Customizing (all at the top of `fiscal_bot.py`)
- `WATCHLIST`: tickers that always appear (marked ⭐), e.g. `["PLTR", "HOOD"]`.
- `MIN_REVENUE_ESTIMATE`: the size cutoff for which companies count as notable (default $1B quarterly revenue).
- `KEY_RELEASES`: which economic reports appear in #macro.
- `FOMC_DECISIONS`, `MARKET_HOLIDAYS`: update these once a year from federalreserve.gov and nyse.com.
- Post times: edit the `cron` lines in `.github/workflows/fiscal.yml`. They're in **UTC** (GitHub ignored the `timezone:` setting on this repo), and each post has a chance every 15 minutes, covering both daylight-saving offsets. Also update `AUTO_WINDOWS` in `fiscal_bot.py`. `tests/test_offline.py` replays the schedule to check it.

---

# Media feeds: videos, education, AI and crypto

A second workflow (**Fiscal media feeds**, `feeds_bot.py`) posts from free sources listed in `sources.yml`:

| Channel | What gets posted |
|---|---|
| #videos | New episodes from podcasts with political and policy guests, plus Fed press conferences and speeches |
| #education | New videos, articles and research, from beginner explainers to advanced valuation |
| #ai | New AI podcast episodes and videos, plus a daily **AI Headlines** digest |
| #crypto | New crypto videos, a daily **Crypto Headlines** digest, and a daily **Crypto Board** with a chart of the biggest coins |

It checks every 15 minutes (GitHub skips many of these) and stays quiet from 11 PM to 6 AM New York time. Each run posts at most 4 new items per channel; anything left over goes out on the next run.
The digests and the crypto board go out on the first run after 8 AM. YouTube links play right inside Discord, and Shorts are skipped.

### Setup
1. Make #videos, #education, #ai and #crypto read-only with threads, the same way as step 1 above.
2. Create a webhook in each of the four channels (⚙️ Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL).
3. Add them as repository secrets, with the names exactly as written: `DISCORD_WEBHOOK_VIDEOS`, `DISCORD_WEBHOOK_EDUCATION`, `DISCORD_WEBHOOK_AI`, `DISCORD_WEBHOOK_CRYPTO`.
   A channel without a secret is simply skipped, so you can add them one at a time.
4. Optional: the crypto board uses CoinGecko's free keyless API. If it ever gets rate-limited, get a free Demo key at coingecko.com/en/api and add it as `COINGECKO_API_KEY`.
5. Go to **Actions → Fiscal media feeds → Run workflow** and run `check-feeds`. It tests every source and posts nothing. Then run `test-post`.

### Adding or removing sources
Edit `sources.yml`. For a YouTube channel, paste its @handle (e.g. `youtube: "@allin"`) or its channel ID.
For a podcast or website, paste its RSS feed URL. Add `digest: true` to put a source in the daily headlines instead of posting each item.
Run `check-feeds` afterwards to confirm the new source works.

---

## Scheduling: the Fiscal ticker
GitHub's own scheduler skips most scheduled runs on this repo (about 3% ran on Nov 22–23). So a workflow called **Fiscal ticker** keeps things running instead:
- Each tick asks **Fiscal feeds** and **Fiscal media feeds** to post whatever is due (`task: auto`), then queues the next tick.
- The queued tick waits **30 minutes** in the `ticker-30m` environment before running, so posts land within about 30 minutes of their target time.
- The bots remember what they've posted, so nothing doubles. That includes the older 15-minute schedules, which stay in place as extra chances.

**One-time setup:** Settings → Environments → **New environment**, named `ticker-30m`. Tick **Wait timer**, set it to `30`, and save. (Wait timers are free only on public repos.)

**Start or restart the loop:** Actions → **Fiscal ticker** → Run workflow, with "wait" unticked. The loop restarts itself if GitHub's scheduler ever fires the backup schedule, but if the Actions tab shows no Fiscal ticker run in the last hour, start it by hand.

**Keep the repo public.** On a private repo the wait timer is ignored. The ticker notices that and stops itself. Private would also need about 4,000+ paid Actions minutes a month.

---

# Trade alerts: slash commands for #trade and #invest

You post trades from Discord, on your phone or desktop, with slash commands. The bot posts a clean card in **#trade** or **#invest** and pings the alert role. It never shows position size, share count or contract count, only what you bought or sold, the prices and the result.

| Command | What it posts |
|---|---|
| `/buy ticker style [price]` | "Bought NVDA · Swing" in #trade. With style **Long-term hold**, it posts "New long-term hold" in #invest instead. Leave the price blank to use the live price. |
| `/short ticker style [price]` | "Shorted TSLA · Swing" in #trade. Closing it posts "Covered", and a falling price counts as a gain. |
| `/option ticker type strike expiry price style` | "Bought TSLA $250 call · Day trade", with the expiry, the contract price and the stock's live price. |
| `/trim position [price]` | "Trimmed NVDA" in the trade's thread only, with no ping. The position stays open. |
| `/close position [price]` | "Sold NVDA ▲ +10.4%" in the channel with a ping, and in the thread. |
| `/positions` | Your open positions with live moves. Only you can see the reply. |

`position` is a pick-list of your open trades. The commands only appear for you, and the bot refuses anyone else. Each trade gets a number and its own discussion thread.
The bot runs on **Cloudflare Workers' free plan**, because GitHub can't receive Discord commands in real time. Open positions are stored in Cloudflare, never in this repo. Merging changes under `trade_bot/` redeploys it automatically.

### One-time setup (about 20 minutes)
**1. Create the Discord bot** at discord.com/developers/applications → **New Application** (e.g. "Fiscal Trades").
- **General Information:** copy the **Application ID** and the **Public Key**.
- **Bot:** click **Reset Token** and copy the token. Leave all the "Privileged Gateway Intents" off.
- **Installation** (or OAuth2 → URL Generator): choose scopes `bot` and `applications.commands`, and bot permissions **View Channels, Send Messages, Embed Links, Create Public Threads, Send Messages in Threads, Mention Everyone** (needed to ping the alert roles). Open the link and add the bot to Fiscal.

**2. Let it post in the read-only channels.** For #trade and #invest: ⚙️ Edit Channel → Permissions → add the bot's role → allow **Send Messages, Embed Links, Create Public Threads, Send Messages in Threads**.

**3. Copy the IDs.** In Discord, go to User Settings → Advanced and turn on **Developer Mode**. Then right-click and choose **Copy ID** on:
- the server icon (server ID)
- your own name (your user ID)
- #trade and #invest
- the **Trade Alerts** and **Invest Alerts** roles (create them under Server Settings → Roles if you haven't)

**4. Create a free Cloudflare account** at dash.cloudflare.com.
- Open **Workers & Pages** once. The first time, it asks you to pick a `workers.dev` subdomain.
- Copy your **Account ID** from the Workers & Pages overview page.
- Go to **My Profile → API Tokens → Create Token**, use the **Edit Cloudflare Workers** template, select your account, then create it and copy the token.

**5. Add them to GitHub.** Settings → Secrets and variables → Actions:

| Tab | Name | Value |
|---|---|---|
| Secrets | `CLOUDFLARE_API_TOKEN` | the Cloudflare token |
| Secrets | `CLOUDFLARE_ACCOUNT_ID` | your Cloudflare Account ID |
| Secrets | `DISCORD_BOT_TOKEN` | the bot token |
| Variables | `DISCORD_APP_ID` | Application ID |
| Variables | `DISCORD_PUBLIC_KEY` | Public Key |
| Variables | `DISCORD_GUILD_ID` | server ID |
| Variables | `DISCORD_OWNER_ID` | your user ID |
| Variables | `TRADE_CHANNEL_ID` / `INVEST_CHANNEL_ID` | the two channel IDs |
| Variables | `TRADE_ROLE_ID` / `INVEST_ROLE_ID` | the two role IDs |

(`FINNHUB_API_KEY` is already set from step 1 of this README.)

**6. Deploy.** Go to Actions → **Deploy trade bot** → Run workflow. The "Deploy to Cloudflare" step prints the bot's address, like `https://fiscal-trade-bot.<your-subdomain>.workers.dev`.

**7. Connect Discord to it.** Back in the Developer Portal → General Information → **Interactions Endpoint URL**: paste the address and click **Save Changes**. Discord checks it on the spot.

**8. Try it.** Type `/positions` in any channel. You should get a private "You have no open positions." reply. Your next `/buy` posts for real.

---

## Making changes
Work on a branch and open a pull request instead of pushing to `main`.
Before opening it, run `python tests/test_offline.py`, `python tests/test_feeds_offline.py`, and `node --test` inside `trade_bot/`. They run every post type and command against sample data.

## Cost
$0, because the repo is public and GitHub Actions is free for public repos. **Keep it public:** the 15-minute checks add up to about 4,000 runs a month, well over the 2,000 free minutes a private repo gets. If it ever goes private, change the media feeds back to hourly (`23 * * * *`) and give each fiscal post just one or two chances.
Finnhub's free key and CoinGecko's free tier are meant for personal, non-commercial use. Revisit both before Fiscal adds a paid tier.
