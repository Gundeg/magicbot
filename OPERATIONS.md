# Operations runbook — Magic Bot

Operator-facing steps for the infrastructure improvements that the code is
*ready for* but that need a human to flip a switch. Code-only changes (async
webhook, dedup, typing indicator, token health check, pause button) are already
live once deployed — nothing to do.

---

## 0. "The bot stopped working" — run the diagnosis first

Before reading logs or guessing, open the **Render Shell** on the `magicbot`
service and run:

```bash
python scripts/diagnose.py
```

It is read-only (sends no message, writes nothing) and checks, in order:

1. **Env vars** present, and which provider customer replies actually run on.
2. **Facebook token** — reachable, *and* that it is a **Page** token, by asking
   `me` and asserting the id equals `FACEBOOK_PAGE_ID`. A User token reads the
   Page by id perfectly well, so only the `me` comparison catches the trap in §4.
3. **Page webhook subscription** — `messages` (fatal if missing: Facebook
   delivers nothing) and `message_echoes` (human-takeover mute only).
4. **The reply model, live** — a real one-line completion with the deployed
   `REPLY_MODEL` / `REPLY_MAX_TOKENS` / `GEMINI_REASONING_EFFORT`. Separates
   *retired preview model* (404) from *quota* (429), *bad key* (401) and the
   empty-reply thinking trap, and prints the exact billing page to open.
5. **Background provider** — `OPENAI_API_KEY` is required even on a Gemini
   deployment (classifier, clustering, page comments).
6. **Traffic** — how long ago the last INBOUND and last OUTBOUND message was.
   This is the fastest split there is: an inbound row from minutes ago with no
   outbound after it means delivery is fine and the break is in the reply/send
   path; no inbound for days means Facebook is not delivering at all (check 3,
   the app's Live/Dev mode, the Callback URL).

It ends with a verdict listing every failure, most likely cause first. Secrets
are masked, so the output is safe to paste into a chat.

Only when it comes back all-green is the problem per-conversation (a mute,
staff takeover, or handoff) rather than global — check that one user in
Admin → Мессежийн түүх.

---

## 1. Keep the service warm (stops cold-start retries)

**Why:** when the Render service spins down after idle time, the first customer
message triggers a slow cold-start boot. Facebook times out and retries the
delivery several times — which is what caused the phantom "Та маш олон мессеж"
rate-limit on first messages. A warm service never cold-starts.

**Do this:** point an uptime pinger at the new health endpoint every ~10 min.

- URL to ping: `https://<your-render-url>/health` (returns `{"status":"ok"}`, no auth, no DB).
- Free options: <https://cron-job.org> or <https://uptimerobot.com> → add an HTTP(s)
  monitor, interval 5–10 min, that URL. Done.
- Alternative: a Render Cron Job running `curl -fsS https://<url>/health` on `*/10 * * * *`.

If your Render plan is already always-on (doesn't sleep), this is optional — but
harmless, and still useful as an uptime alarm.

---

## 2. Migrate SQLite → Render Postgres (optional, bigger durability win)

**Why:** SQLite under 2 gunicorn workers is the source of the `database is locked`
class of bugs (the WAL/`busy_timeout` pragmas in `app.py` are a workaround).
Postgres removes that entirely and gives real concurrency. The code is already
Postgres-ready: the URI scheme is normalised, `psycopg2-binary` is in
`requirements.txt`, and the SQLite-only PRAGMA is guarded.

> ⚠️ This moves **live customer data**. Do it in a low-traffic window and verify
> row counts before deleting anything. The current SQLite file is safe on the
> persistent disk — keep it as the rollback.

**Steps:**

1. **Provision** a Render PostgreSQL instance (same region as the web service).
2. Copy its **Internal Database URL** (looks like `postgres://user:pass@host/db`).
3. **Migrate the data** (one-time). Easiest reliable path is `pgloader`:
   ```bash
   # from a machine that can reach both files/DB:
   pgloader ./magic_bot.db  postgresql://user:pass@host/db
   ```
   Or, pure-Python (no extra tools), run once with BOTH URLs available:
   ```python
   # scripts/sqlite_to_postgres.py  (run: python scripts/sqlite_to_postgres.py)
   import os
   from sqlalchemy import create_engine, MetaData, insert
   SRC = os.environ['SQLITE_URL']      # e.g. sqlite:////var/data/magic_bot.db
   DST = os.environ['POSTGRES_URL']    # postgresql://...
   src, dst = create_engine(SRC), create_engine(DST)
   md = MetaData(); md.reflect(bind=src)
   md.create_all(bind=dst)             # create tables on Postgres
   with src.connect() as s, dst.begin() as d:
       for table in md.sorted_tables:  # FK-safe order
           rows = [dict(r._mapping) for r in s.execute(table.select())]
           if rows:
               d.execute(insert(table), rows)
           print(f"{table.name}: {len(rows)} rows")
   ```
   Then verify: row counts per table match the SQLite source.
4. Set `SQLALCHEMY_DATABASE_URI` = the Internal Database URL on **both** the web
   and worker services, and redeploy. `init_db()` / `ensure_schema()` run on
   boot and are idempotent on Postgres too.
5. Watch the deploy logs go green and smoke-test (log in, send a test message).
6. Keep the SQLite file for a few days as rollback before removing the disk.

After cutover, the `app.py` SQLite PRAGMA block simply no-ops (it's guarded to
`sqlite3.Connection`), so it can stay.

---

## 3. Tunable env vars added in this batch

| Var | Default | Effect |
|---|---|---|
| `REPLY_MAX_TOKENS` | 500 | Max length of a bot reply. Lower = faster + cheaper. |
| `ENABLE_TOKEN_CHECK` | true | Worker pings the FB token every N hours, alerts on expiry. |
| `TOKEN_CHECK_INTERVAL_HOURS` | 6 | How often the token health check runs. |
| `FB_TOKEN_ALERT_COOLDOWN_HOURS` | 6 | Min gap between "Page token is dead" Telegram alerts fired from the send path. |
| `HUMAN_TAKEOVER_MUTE_MINUTES` | 30 | Auto-mute window when a human replies in the inbox. |
| `BACKGROUND_WORKERS` | 4 | Thread pool size for async reply generation per process. |

---

## 4. Renewing the Facebook Page token (when the bot goes silent)

**Symptom:** every customer stops getting replies, and gets *nothing at all* —
not even the Mongolian apology text. (The apology arriving means the opposite:
the Send API is fine and the reply provider is broken. A *single* silent user is
a third thing — a mute/handoff, see the admin manual.) Render logs
(`r=1h`, query `Send API`) show `Send API FAILED ... OAuthException code:190`
(`subcode:463` = token expired, `subcode:460` = FB account password changed /
session invalidated).

**The give-away in the logs** is that everything *except* the send works:
`POST /webhook ... 200` (Facebook is delivering), a `200 OK` from the reply
provider (the bot composed a good answer), then `Send API FAILED status=401`.
The bot is thinking correctly and failing at the last inch.

Two things page you on this: `alert_facebook_token_failure` fires from the send
path on the **first** failed message (cooldown `FB_TOKEN_ALERT_COOLDOWN_HOURS`),
and the 6-hourly `ENABLE_TOKEN_CHECK` loop is the backstop. Both are silent
no-ops without Telegram configured — see §5.

> **Replies composed during the outage are not lost.** `process_inbound_reply`
> commits the bot's message to the DB *before* handing it to Facebook, so every
> undelivered reply is visible in Admin → Мессежийн түүх. The bot will NOT
> resend them after the fix — work that history by hand for anyone who wrote
> during the outage.

**The one trap that turns a 5-minute fix into an hour:** the replacement **must be
a Page token, not a User token.** The Send API posts to `me/messages`, so `me` has
to resolve to the *Page*. Graph API Explorer **defaults to a User token** — copy
that by mistake and every send fails `GraphMethodException code:100 subcode:33
("Object with ID 'me' does not exist")`; the token authenticates but the bot still
looks silent.

**Steps:**

1. **Graph API Explorer** (<https://developers.facebook.com/tools/explorer/>) →
   Meta App = **MagicAI Bot** → set the **"User or Page"** dropdown to
   **Page Access Tokens → Magic Financial Group** (NOT the default User Token).
2. Extend it to long-lived in the **Access Token Debugger** → *Extend Access Token*
   (needs the FB account password; ~60-day token). For a non-expiring token, use
   `GET /me/accounts` with a long-lived user token instead.
3. **Render → `magicbot` service** (confirm it's `magicbot`, not another service) →
   **Environment** → replace `FACEBOOK_ACCESS_TOKEN` → **Save, rebuild, and deploy**.
   Confirm a fresh entry appears on that service's **Events** page.
4. **Verify** in the `magicbot` **Shell** (read-only, exposes nothing):
   ```bash
   python scripts/diagnose.py
   ```
   Check 2 must say *"Token is a valid Page token"* with the Page's name. If it
   says **"This is a USER token, not a PAGE token"** you copied the User token
   again — redo step 1. (The raw equivalent, if you want just the one call:
   `python -c "import os,requests;print(requests.get('https://graph.facebook.com/v18.0/me',headers={'Authorization':'Bearer '+os.environ['FACEBOOK_ACCESS_TOKEN']}).text)"`
   → correct is `{"name":"Magic Financial Group","id":"123001937756085"}`; a
   person's name means it is still a User token.)

Full user-facing (Mongolian) walk-through:
`Facebook Page Access Token хэрхэн авах тухай дэлгэрэнгүй заавар.md` §5.

### Better: get a token that never expires (do this once, stop doing §4 forever)

"Extend Access Token" in the Debugger buys ~60 days and then this outage repeats
— it did on 2026-09-04, three days before anyone noticed. A Page token derived
from a **long-lived User token** has no expiry at all. All browser, ~3 minutes
more than the steps above:

1. **Graph API Explorer** → app **MagicAI Bot** → "User or Page" = **User Token**
   → tick `pages_show_list`, `pages_messaging`, `pages_messaging_subscriptions`,
   `pages_read_engagement`, `pages_manage_metadata`, `pages_manage_posts` →
   **Generate Access Token** → copy.
2. **Access Token Debugger** → paste → **Debug** → **Extend Access Token**
   (needs the FB password). Copy the extended token — this is a long-lived
   **User** token, NOT what goes into Render.
3. Back in **Graph API Explorer**: paste that extended User token into the
   **Access Token** box, query `me/accounts`, **Submit**.
4. In the JSON, find the entry whose `name` is `Magic Financial Group` and copy
   its **`access_token`**. That is the Page token, and it inherits the
   non-expiring property.
5. **Verify in the Debugger before deploying:** Type = **Page**, Expires =
   **Never**, Profile ID = `123001937756085`. An expiry date means step 3 used
   the short-lived token — redo step 2.
6. Deploy it as `FACEBOOK_ACCESS_TOKEN` per step 3 above, and confirm the Events
   entry per step 3's warning.

---

## 5. If a token dies and nobody gets paged

Both alert paths go through `send_telegram_notification`, which is a **silent
no-op** when `TELEGRAM_BOT_TOKEN` is unset or no chat IDs are configured — so a
misconfigured Telegram turns every alert in this system into a log line nobody
reads. On 2026-09-04 that is exactly what happened: the token died on a Friday
and the outage ran until Monday.

Check, on the **worker** service (the 6-hourly health check only runs there):

- `TELEGRAM_BOT_TOKEN` is set.
- At least one chat ID is configured (Admin → Систем, or the env var).
- `WORKER_ROLE=worker` and `ENABLE_TOKEN_CHECK` is not set to false.

The send-path alert (`alert_facebook_token_failure`) fires from the **web**
dynos too, so it survives a dead worker — but it still needs Telegram. Send
yourself a test notification from the admin panel after any change here.
