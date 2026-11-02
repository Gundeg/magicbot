# MagicBot Deployment Guide

End-to-end steps to get the Facebook Messenger bot live for **Magic Financial Group**, using Render (free tier) + Facebook webhook.

> **Before anything else:** if you ever shared the access token that was hardcoded in the old `app.py` (line 30 in the original), go to Facebook → Business Settings → System Users / Access Tokens and **revoke** it. The code no longer ships any default token.

---

## 1. Prerequisites

You already have the Facebook Page. You still need:

- **Facebook Developer App** (free, ~10 min)
- **Long-lived Page Access Token** (~5 min)
- **Anthropic API key** with some prepaid credit (~3 min) — every model call runs on Claude: customer replies (Claude Opus 5.5) and the background jobs (lead classifier, FAQ clustering, post auto-comments on Claude Haiku 5.5). No other AI provider key is needed
- **GitHub account** (to push the repo to Render)
- **Render account** at https://render.com (free)

---

## 2. Create the Facebook Developer App

1. Open https://developers.facebook.com/apps → **Create app**.
2. Use case: **Other** → app type: **Business**.
3. Name it (e.g. "Magic Bot") → create.
4. From the left sidebar, click **Add Product** → enable **Messenger**.
5. Under **Messenger → Settings**:
   - Click **Add or Remove Pages** → select the Magic Financial Group page → grant.
   - Under **Access Tokens** for that Page, click **Generate Token**. Copy it — this is a *short-lived* token, ~1 hour life.

### Convert to a long-lived Page token

Short-lived tokens expire. To get a long-lived one (~60 days, then auto-renewable):

1. Open https://developers.facebook.com/tools/explorer (Graph API Explorer).
2. Top-right: select your app, then your Page in the *User or Page* dropdown.
3. Click **Generate Access Token** → grant the required permissions:
   - `pages_messaging`
   - `pages_messaging_subscriptions`
   - `pages_read_engagement`
   - `pages_manage_posts`
   - `pages_manage_metadata`
4. Copy the User Access Token shown.
5. Make this GET call in the explorer (replace placeholders):
   ```
   /oauth/access_token?grant_type=fb_exchange_token
     &client_id={APP_ID}
     &client_secret={APP_SECRET}
     &fb_exchange_token={USER_TOKEN_FROM_STEP_4}
   ```
   You get a long-lived **user** token back.
6. Call `/me/accounts?access_token={LONG_LIVED_USER_TOKEN}` — the response includes each Page's `id` and a `access_token`. The Page `access_token` here is the long-lived **Page** token. Save both:
   - `FACEBOOK_PAGE_ID` = the `id` field
   - `FACEBOOK_ACCESS_TOKEN` = the `access_token` field

The full walkthrough (in Mongolian) is in `Facebook Page Access Token хэрхэн авах тухай дэлгэрэнгүй заавар.md`.

---

## 3. Generate your secrets

In a PowerShell window:

```powershell
python -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))"
python -c "import secrets; print('VERIFY_TOKEN=' + secrets.token_hex(16))"
```

Save both values; you'll paste them into Render in step 5.

Also get your **Anthropic API key** from https://console.anthropic.com/settings/keys and add prepaid credit under **Plans & Billing** — the API won't run on a $0 balance (every reply would degrade to the canned apology and page staff on Telegram).

**Models.** Out of the box, customer replies use `claude-opus-5-5` at `REPLY_EFFORT=low` with `REPLY_MAX_TOKENS=2048`, and the background jobs use `claude-haiku-5-5`. Override with `REPLY_MODEL` / `REPLY_EFFORT` / `REPLY_MAX_TOKENS` / `BACKGROUND_MODEL` (step 5) — e.g. `REPLY_MODEL=claude-sonnet-5-5` to roughly halve reply cost. Thinking can't be switched off on Opus 5.5; `REPLY_EFFORT` is the dial, and `max_tokens` is shared between thinking and the visible reply, so keep headroom.

---

## 4. Push the repo to GitHub

```powershell
cd "C:\Users\gunju\IT Projects\MagicBot"
git init
git add .
git commit -m "Initial MagicBot deploy"
```

Then create a repo on github.com (private is fine) and push:

```powershell
git remote add origin https://github.com/YOUR_USERNAME/magicbot.git
git branch -M main
git push -u origin main
```

The `.gitignore` keeps your `.env` and the SQLite DB out of git.

---

## 5. Deploy to Render

1. Sign in at https://render.com → **New + → Web Service**.
2. **Connect a repository** → pick the magicbot repo.
3. Settings:
   - **Region**: pick the one closest to Mongolia (Singapore, usually).
   - **Branch**: `main`
   - **Runtime**: Python 3 (Render reads `runtime.txt` for the exact version)
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: leave blank — Render uses `Procfile`.
   - **Plan**: **Free** is fine to start.
4. Click **Advanced → Add Environment Variable** and add every key from `.env.example`:

   | Key | Value |
   |---|---|
   | `SECRET_KEY` | (from step 3) |
   | `ANTHROPIC_API_KEY` | sk-ant-... (every model call: customer replies + background jobs) |
   | `REPLY_MODEL` | *(optional)* default `claude-opus-5-5`; `claude-sonnet-5-5` to cut cost |
   | `REPLY_EFFORT` | *(optional)* `low` (default, fast chat) / `medium` / `high` — reasoning depth |
   | `REPLY_MAX_TOKENS` | *(optional)* default `2048`; thinking + reply share it, keep headroom |
   | `BACKGROUND_MODEL` | *(optional)* default `claude-haiku-5-5` for classifier / clustering / comments |
   | `FACEBOOK_PAGE_ID` | (from step 2) |
   | `FACEBOOK_ACCESS_TOKEN` | long-lived Page token from step 2 |
   | `VERIFY_TOKEN` | (from step 3) |
   | `INITIAL_ADMIN_PASSWORD` | strong password — you'll log in as `admin` with this |
   | `ADMIN_EMAIL` | your admin email (optional) |
   | `ENABLE_POLLING` | `false` (turn on later if you want post auto-comments) |
5. Click **Create Web Service**. First build takes ~3-5 min.
6. When it goes green, copy the public URL (e.g. `https://magicbot-xyz.onrender.com`).

### SQLite is ephemeral on Render Free

Render's free tier wipes the disk on every redeploy, so leads and conversation logs will reset. For real use:

- Cheap option: add a **Persistent Disk** ($1/mo) and mount it at `/var/data`, then change `SQLALCHEMY_DATABASE_URI` in `app.py:24` to `sqlite:////var/data/magic_bot.db`.
- Better option: provision a Render PostgreSQL database (free tier exists) and set `SQLALCHEMY_DATABASE_URI` to its connection string via env var.

---

## 6. Wire the Messenger webhook

1. Back in the Facebook Developer App → **Messenger → Settings → Webhooks → Add Callback URL**.
2. Fill in:
   - **Callback URL**: `https://magicbot-xyz.onrender.com/webhook`
   - **Verify Token**: the `VERIFY_TOKEN` value from step 3
3. Click **Verify and Save**. Facebook hits `GET /webhook`; on success this turns green.
4. In the **Webhook Fields** for that Page, subscribe to:
   - `messages`
   - `messaging_postbacks`
5. Double-check **Add or Remove Pages** still shows your Page connected.

---

## 7. Verify end-to-end

1. **Render Logs tab** — should show `Default admin user created with username 'admin'.` and no Python tracebacks.
2. **Send a DM** to the Magic Financial Group Page from your personal Facebook (Page must be in development mode → only admins/testers/devs can DM until you submit for App Review).
   - Try: `Сайн уу, сургалтын мэдээлэл өгөөч`
   - Expect a Mongolian AI reply within ~5 sec.
3. **Admin dashboard** — visit `https://magicbot-xyz.onrender.com/login`, log in as `admin` / `INITIAL_ADMIN_PASSWORD`.
   - **Logs** page shows the message you just sent and the bot reply.
   - Send `99112233` to the bot → reload **Leads** page → user appears as a lead.

### Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Render build fails on import | Missing env var (`SECRET_KEY` or `FACEBOOK_ACCESS_TOKEN`) | Add it under *Environment*, redeploy |
| Webhook verification fails (red ❌) | `VERIFY_TOKEN` mismatch | Make sure Render env var and Facebook field are identical, no whitespace |
| DM gets no reply | Page not subscribed to `messages`, or Page is in dev mode and your account isn't a tester | Add yourself as a tester at *App Roles → Testers*; recheck webhook subscription |
| Reply says "Уучлаарай, түр зуурын саатал..." | Claude API 400 `credit balance is too low` or 401 — no prepaid credit or a bad key | Top up at console.anthropic.com → Plans & Billing, or fix `ANTHROPIC_API_KEY`. The Telegram alert fires on exactly these account-level errors (not on transient 429/529). |
| Same apology, logs show `Empty reply from … stop_reason=max_tokens` | `REPLY_MAX_TOKENS` too tight — thinking and the reply share the budget | Raise `REPLY_MAX_TOKENS` (default 2048) or lower `REPLY_EFFORT` |
| 500 on `/webhook` | Most often `db.session` outside app context — confirm latest code is deployed | Check Render *Events* tab for the deploy commit hash |

---

## 8. Going public (later)

Until App Review is approved, only admins/developers/testers of the Facebook app can DM the Page and get bot replies. To open it to the public:

1. App Dashboard → **App Review → Permissions and Features**.
2. Request advanced access for `pages_messaging` (and the other permissions you're using).
3. Facebook requires a screencast showing the bot's flow and a privacy policy URL.

That review typically takes 1-5 business days.
