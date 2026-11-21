# Fiscal bot: automated earnings and macro posts

Posts to two Discord channels on a schedule, for free (GitHub Actions + Discord webhooks + free API keys).

| When (New York time) | Channel | Post |
|---|---|---|
| Sunday 6:07 PM | #macro | 🗓️ Macro Week Ahead: CPI, jobs, GDP, Fed meetings, holidays |
| Sunday 6:07 PM | #earnings | 📅 Earnings Week Ahead: biggest reporters each day, before open / after close |
| Weekdays 7:41 AM | #macro | ☕ Morning Brief: last close for S&P/Nasdaq/Dow/VIX/yields, plus today's data |
| Weekdays 7:41 AM | #earnings | 📊 Earnings Today: EPS and revenue estimates for today's reporters |
| Weekdays 8:07 PM | #earnings | 🧾 Earnings Results: beats and misses vs. estimates |

GitHub often starts scheduled runs late (from about 35 minutes to 2½ hours) and sometimes skips them, so each post gets a few chances.
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
- Post times: edit the `cron` lines in `.github/workflows/fiscal.yml`. They're in **UTC** (GitHub ignored the `timezone:` setting on this repo), and each post has one line per daylight-saving offset plus a backup. Also update `AUTO_WINDOWS` in `fiscal_bot.py`. `tests/test_offline.py` replays the schedule to check it.

---

# Media feeds: videos, education, AI and crypto

A second workflow (**Fiscal media feeds**, `feeds_bot.py`) posts from free sources listed in `sources.yml`:

| Channel | What gets posted |
|---|---|
| #videos | New episodes from podcasts with political and policy guests, plus Fed press conferences and speeches |
| #education | New videos, articles and research, from beginner explainers to advanced valuation |
| #ai | New AI podcast episodes and videos, plus a daily **AI Headlines** digest |
| #crypto | New crypto videos, a daily **Crypto Headlines** digest, and a daily **Crypto Board** with a chart of the biggest coins |

It runs every hour (at :23 UTC, often late) and stays quiet from 11 PM to 6 AM New York time. Each run posts at most 4 new items per channel; anything left over goes out on the next run.
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

## Making changes
Work on a branch and open a pull request instead of pushing to `main`.
Before opening it, run `python tests/test_offline.py` and `python tests/test_feeds_offline.py`. They run every post type against sample data.

## Cost
$0. The repo is public, and GitHub Actions is free for public repos. If it ever goes private again: the earnings/macro workflow uses about 150 minutes a month and the media feeds about 720, within the 2,000 free minutes private repos get.
Finnhub's free key and CoinGecko's free tier are meant for personal, non-commercial use. Revisit both before Fiscal adds a paid tier.
