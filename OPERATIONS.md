# Operations runbook — Magic Bot

Operator-facing steps for the infrastructure improvements that the code is
*ready for* but that need a human to flip a switch. Code-only changes (async
webhook, dedup, typing indicator, token health check, pause button) are already
live once deployed — nothing to do.

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
| `HUMAN_TAKEOVER_MUTE_MINUTES` | 30 | Auto-mute window when a human replies in the inbox. |
| `BACKGROUND_WORKERS` | 4 | Thread pool size for async reply generation per process. |

---

## 4. Renewing the Facebook Page token (when the bot goes silent)

**Symptom:** every customer stops getting replies (a *single* silent user is a
different thing — that's a mute/handoff, see the admin manual). Render logs
(`r=1h`, query `Send API`) show `Send API FAILED ... OAuthException code:190`
(`subcode:463` = token expired, `subcode:460` = FB account password changed /
session invalidated). The token health check (`ENABLE_TOKEN_CHECK`) Telegram-alerts
on this if Telegram is configured.

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
   python -c "import os,requests;print(requests.get('https://graph.facebook.com/v18.0/me',params={'access_token':os.environ['FACEBOOK_ACCESS_TOKEN']}).text)"
   ```
   Correct = `{"name":"Magic Financial Group","id":"123001937756085"}`. A person's
   name = you copied a User token again — redo step 1.

Full user-facing (Mongolian) walk-through:
`Facebook Page Access Token хэрхэн авах тухай дэлгэрэнгүй заавар.md` §5.
