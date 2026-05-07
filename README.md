# 🏠 CROUS Logement Alert Bot

Telegram bot that checks `trouverunlogement.lescrous.fr` every **5 minutes** and sends you an instant alert whenever a room becomes available in **Île-de-France** — Paris, Saclay, Orsay, Palaiseau, and more.

**100% free** — runs on GitHub Actions, no server needed.

---

## 📦 What's in this repo

```
crous-bot/
├── check_crous.py              # Main scraper + Telegram notifier
├── state.json                  # Tracks already-seen room IDs (auto-updated)
├── requirements.txt
└── .github/
    └── workflows/
        └── crous_alert.yml     # GitHub Actions: runs every 5 minutes
```

---

## 🚀 Setup (10 minutes, one-time)

### Step 1 — Get your Telegram Chat ID

1. Open Telegram, search for **@userinfobot**
2. Send `/start`
3. It replies with your Chat ID — looks like `123456789`

### Step 2 — Create a GitHub repository

1. Go to [github.com](https://github.com) → **New repository**
2. Name it `crous-alert` (or anything)
3. Set it to **Private** (recommended — keeps your token safe)
4. Click **Create repository**

### Step 3 — Upload the files

Upload all files from this folder into your repo. You can drag-and-drop them on the GitHub web interface, or use git:

```bash
git init
git remote add origin https://github.com/YOUR_USERNAME/crous-alert.git
git add .
git commit -m "init"
git push -u origin main
```

### Step 4 — Add your secrets

In your GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these two secrets:

| Name | Value |
|------|-------|
| `TELEGRAM_TOKEN` | Your bot token from @BotFather (e.g. `7123456789:AAF...`) |
| `CHAT_ID` | Your chat ID from @userinfobot (e.g. `123456789`) |

### Step 5 — Enable Actions

Go to the **Actions** tab in your repo. If GitHub asks you to enable workflows, click **Enable**.

### Step 6 — Test it manually

Go to **Actions** → **CROUS Logement Alert** → **Run workflow** → **Run workflow**

Check your Telegram — if rooms are currently available, you'll get alerts instantly!

---

## 🗺️ Zones monitored

| Zone | Covers |
|------|--------|
| Paris intramuros | All arrondissements |
| Saclay / Gif / Orsay | Plateau de Saclay, Paris-Saclay campus |
| Palaiseau / Massy / Verrières | École Polytechnique area |
| Versailles / Saint-Quentin | UVSQ area |
| Créteil / Val-de-Marne | UPEC area |
| Saint-Denis / Villetaneuse | Paris 13 nord, Sorbonne Paris Nord |
| Nanterre / La Défense | Paris Nanterre |
| Évry / Courcouronnes | Université d'Évry |

---

## ➕ Adding or removing zones

Edit `check_crous.py` → the `ZONES` list at the top.

To get the bounds for a new city:
1. Go to `trouverunlogement.lescrous.fr`
2. Search for a city and zoom in
3. The URL will contain `?bounds=lon1_lat1_lon2_lat2`
4. Copy those values into a new zone entry

---

## 📅 How often does it run?

Every **5 minutes** — that's the minimum interval GitHub Actions allows on a free account. GitHub guarantees best-effort scheduling (may be 1-2 min late during busy periods, but still very fast).

---

## 🔕 How to pause alerts

Go to **Actions** → **CROUS Logement Alert** → click the `...` menu → **Disable workflow**

Re-enable the same way when you want alerts again.

---

## 🧠 How the state works

`state.json` stores the IDs of all rooms you've already been notified about. After each run, the bot commits the updated file back to your repo. This is how it "remembers" between runs without any database.

If you want to **reset** (get re-alerted for all current rooms), just replace `state.json` with:
```json
{"seen_ids": [], "updated_at": "2025-01-01T00:00:00"}
```

---

## ⚠️ Notes

- GitHub Actions free tier gives **2,000 minutes/month** — running every 5 min uses ~8,640 min/month. To stay free, the job must finish in under **14 seconds** on average (which it does — typical run is 5-10s). If you're worried, set the cron to `*/10 * * * *` (every 10 min) to use half the minutes.
- The bot scrapes the public CROUS website. If CROUS changes their API structure, the script may need a small update.
