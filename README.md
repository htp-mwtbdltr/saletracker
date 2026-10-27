# Price Tracker

A self-hosted price and listing tracker for **Micro Center** and **Amazon**. It has a web GUI and a Discord bot.

- Track **search / category result pages** and get a Discord message when new listings show up (or old ones come back).
- Track **individual products** with full price history. Amazon tracking covers Amazon's own price, the lowest new price from any seller, and **used offers by condition**. The data comes from Amazon's offer list itself, not from a third-party site like camelcamelcamel.
- Set **price alerts** ("used ≤ $130", "drops 10%", "new all-time low") in the GUI or from Discord.

Everything runs in a single container: a FastAPI web GUI, the scheduler and scrapers, a discord.py bot, and a SQLite database.

---

## ⚠️ Secrets and state NOT in this repo

This folder holds everything needed to rebuild the container. Some values were **left out on purpose** and must be supplied when you recreate it:

| What | Where it goes | Notes |
|---|---|---|
| **Discord bot token** | `.env` → `DISCORD_TOKEN=` | Not committed. Get it from <https://discord.com/developers/applications> → your app → **Bot** → **Reset Token**. Without it the app still runs, but notifications only go out if a webhook is set. |
| **Web GUI password** (optional) | `.env` → `WEB_PASSWORD=` | Leave empty for no auth (LAN only). |
| **Discord webhook URL** (optional) | GUI → Settings, or `.env` → `DISCORD_WEBHOOK_URL=` | Fallback delivery path when the bot is offline. Webhook URLs contain a secret token. |
| **Notification channel ID** | GUI → Settings, `/setchannel` in Discord, or `.env` → `DISCORD_CHANNEL_ID=` | Not secret, but specific to your server. |
| **LAN URL of the GUI** | `.env` → `WEB_URL=` or GUI → Settings | Used for "Open in web GUI" links in Discord. The original deployment used `http://192.168.1.216:8295`. |
| **The `data/` directory** | `./data` (bind-mounted to `/data`) | The SQLite DB (`tracker.db`) holds all trackers, alerts, price history, and the settings entered in the GUI (channel ID, webhook URL, mention, store ID…). Plus an image cache. **Back this up** if you want to keep history; it is gitignored. |

`.env` itself is gitignored. `.env.example` is the template.

> The previous version of this bot (`bot_manager`) had the Discord token **hardcoded in the Python source**. This version reads it only from `.env`. If that old token was ever committed or shared anywhere, reset it in the Developer Portal.

---

## Quick start (recreating from scratch)

```bash
git clone http://git.proxub/billy/pricetracker.git
cd pricetracker

cp .env.example .env
nano .env                       # set DISCORD_TOKEN (and optionally WEB_PASSWORD, WEB_URL)

docker compose up -d --build
docker compose logs -f          # should show "logged in as <bot>" and the first checks
```

Open the GUI at **http://&lt;host&gt;:8295**, then:

1. **Settings** → pick the **default notification channel** (or run `/setchannel` in Discord) → **Send test notification**.
2. **Trackers → ＋ Add tracker** → paste a URL → **Preview** → set an optional alert → **Start tracking**.

On a brand-new database, the app adds one tracker automatically: the original Micro Center "Open Box 1440p/4K monitors" search. Delete it if you don't want it.

To restore an old deployment, copy its `data/` folder in before starting.

### Discord bot setup (new bot / new server)

1. <https://discord.com/developers/applications> → **New Application** → **Bot** → **Reset Token** → put it in `.env`.
   No privileged intents are needed (the bot uses slash commands only).
2. Invite it with the `bot` and `applications.commands` scopes and these permissions:
   View Channels, Send Messages, Embed Links, **Attach Files** (product images are attached to notifications), Read Message History (for `/count`).

   ```
   https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot+applications.commands&permissions=117760
   ```

   Once the bot is online, the Settings page in the GUI shows this link pre-filled.
3. Slash commands are synced to each server when the bot starts or joins, so they show up right away.

---

## Configuration

### `.env` (read at startup)

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | *(empty)* | Bot token. Empty = no bot. |
| `WEB_PASSWORD` | *(empty)* | If set, the GUI/API require HTTP basic auth (any username). |
| `DISCORD_CHANNEL_ID` | *(empty)* | First-run default channel. |
| `DISCORD_WEBHOOK_URL` | *(empty)* | First-run webhook. |
| `MC_STORE_ID` | `141` | First-run Micro Center store (141 = Columbus). |
| `FLARESOLVERR_URL` | `http://host.docker.internal:8191` | First-run FlareSolverr fallback. |
| `WEB_URL` | *(empty)* | First-run public GUI URL. |
| `DATA_DIR` | `/data` | Set in the Dockerfile. |
| `WEB_PORT` | `8000` | Port inside the container (compose maps host `8295` to it). |

The `DISCORD_CHANNEL_ID` through `WEB_URL` values are only **initial defaults**. After the first start they live in the database and are edited in **GUI → Settings**; changing them in `.env` later has no effect.

### GUI → Settings (stored in the DB)

Default channel, @mention added to each notification, webhook URL, failure notifications on/off, default check interval (15 min), Micro Center store ID, FlareSolverr URL, and the public GUI URL.

### Per-tracker settings

Name, URL, check interval, notification channel override, and:

- **Search trackers:** notify on new items, on items returning (after being missing for N hours), or on any price drop; notify for items found on the first check (off by default, so adding a search doesn't flood you); "only notify new items under $X"; keyword include/exclude filters; max result pages; Micro Center store ID / in-stock-only; skip Amazon sponsored results.
- **Product trackers:** back-in-stock notifications; include shipping in Amazon prices.

---

## What gets tracked

| URL type | Example | Price types |
|---|---|---|
| Micro Center search / category | `microcenter.com/search/search_results.aspx?...` | New, Open Box, Clearance, plus stock per item |
| Micro Center product | `microcenter.com/product/698654/...` | New, Open Box (lowest), Clearance, store stock count |
| Amazon product | `amazon.com/dp/B0916BP6J5`, `amzn.to/...` | Amazon (new), Lowest new, Lowest used, Used – Like New / Very Good / Good / Acceptable, Renewed |
| Amazon search | `amazon.com/s?k=...` | Price |

Every price type also has a virtual **"Lowest of any"** option for alerts.

Amazon product prices come from Amazon's **all-offers listing** (the "Other sellers / New & Used" panel). That's how the tool sees the real current used price on the listing. The full offer table (condition, price, shipping, seller) is shown on the tracker page.

## Alerts

| Type | Fires when |
|---|---|
| **At or below $X** | Price ≤ X. Fires once, fires again if it drops further, and re-arms only after the price is seen above X again. Offers that briefly disappear don't re-trigger it. |
| **Drops by ≥ N%** | Price fell at least N% since the previous check. |
| **New all-time low** | Price is below every previously recorded price for that type. |

Alerts can apply to a whole tracker (on a search, that means every item) or to a single item inside a search (item **Details** → "Alert for just this item").

## Discord commands

| Command | |
|---|---|
| `/track url [below] [price_type] [name]` | Add a tracker, optionally with an "at or below" alert |
| `/list` | All trackers with current prices and status |
| `/info tracker` | Prices, all-time lows, alerts, last error |
| `/check tracker` | Check now |
| `/pause` / `/resume` / `/remove tracker` | |
| `/interval tracker minutes` | Change check frequency |
| `/alert add tracker [value] [price_type] [kind] [note]` | Add an alert |
| `/alert list [tracker]` / `/alert remove alert_id` | |
| `/setchannel [channel] [tracker]` | Default channel, or a per-tracker override |
| `/status` | Overview and recent notifications |
| `/count [days]` | Count messages in the channel (kept from the old bot) |

Tracker arguments autocomplete by name or `#id`.

---

## How it works

```
tracker/
  main.py        entry point: runs uvicorn (web), the scheduler and the Discord bot in one asyncio loop
  config.py      env vars + default settings
  db.py          SQLite schema and helpers (trackers, items, price_history, alerts, alert_state, events, settings)
  fetch.py       HTTP via curl_cffi browser impersonation, profile rotation, Amazon interstitial handling, FlareSolverr fallback
  sites/
    microcenter.py   search + product parsers
    amazon.py        product (all-offers listing) + search parsers
  monitor.py     scheduler, per-check ingest, new/returning item detection, alert evaluation
  notify.py      Discord bot / webhook delivery (embeds, image attachments), event log
  service.py     shared operations used by both the web API and the bot
  bot.py         slash commands
  web.py         JSON API + image proxy + static GUI
  images.py      product image fetch + disk cache (Micro Center's CDN blocks direct hotlinks)
  static/        single-page GUI (vanilla JS + Chart.js from cdnjs)
```

- **Scheduling:** every 20 s the scheduler starts any tracker that is due. Only one request per site runs at a time, with a 2–6 s random pause between them. A failing tracker backs off (2×, 4×, up to 8× its interval). After 3 failures in a row it sends one ⚠️ notification, and a ✅ when it recovers.
- **History:** a price is written when it changes, and at least every 6 h otherwise. When an item or offer disappears, a gap is recorded.
- **Anti-bot:**
  - Micro Center is behind Cloudflare and currently accepts Safari TLS fingerprints. If that stops working, requests go through FlareSolverr (set its URL in Settings).
  - Amazon sometimes shows a "Continue shopping" interstitial or an Akamai `bm-verify` proof-of-work page. Both are cleared automatically. A real image captcha can't be cleared, so the tracker backs off.
  - Keep Amazon intervals at 15 min or more.
- **API docs:** `http://<host>:8295/api/docs` (FastAPI/Swagger).

## Maintenance

```bash
docker compose logs -f                       # live logs
docker compose up -d --build                 # apply code changes
cp data/tracker.db tracker-backup.db         # backup (or use sqlite3 .backup while running)
```

**If a site changes its page layout**, the tracker shows `ScrapeError` / "No products found" in the GUI. The selectors are in `tracker/sites/*.py`.

**If you see `Blocked by site anti-bot protection`:** for Micro Center, make sure the FlareSolverr URL is set and that container is running. For Amazon, wait. The tracker backs off by itself.
