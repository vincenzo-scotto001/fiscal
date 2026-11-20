# Fiscal bot: automated earnings and macro posts

Posts to two Discord channels on a schedule, for free (GitHub Actions + Discord webhooks + free API keys).

| When (New York time) | Channel | Post |
|---|---|---|
| Sunday 6:07 PM | #macro | 🗓️ Macro Week Ahead: CPI, jobs, GDP, Fed meetings, holidays |
| Sunday 6:07 PM | #earnings | 📅 Earnings Week Ahead: biggest reporters each day, before open / after close |
| Weekdays 7:41 AM | #macro | ☕ Morning Brief: last close for S&P/Nasdaq/Dow/VIX/yields, plus today's data |
| Weekdays 7:41 AM | #earnings | 📊 Earnings Today: EPS and revenue estimates for today's reporters |
| Weekdays 8:07 PM | #earnings | 🧾 Earnings Results: beats and misses vs. estimates |

GitHub can start scheduled runs 5–30 minutes late when it's busy. That's normal.
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
- Post times: edit the `cron` lines in `.github/workflows/fiscal.yml`.

## Cost
$0. Each run takes about a minute of GitHub Actions time, roughly 75 minutes a month. Private repos get 2,000 free minutes a month.
Finnhub's free key is meant for personal, non-commercial use. Revisit that before Fiscal adds a paid tier.
