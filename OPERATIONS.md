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

Two paths page you on this and they share one cooldown window, so an outage
produces **one** Telegram alert rather than one per path:
`alert_facebook_token_failure` fires from the send path on the **first** failed
customer message (and runs on the web dynos, so it survives a dead worker), and
the 6-hourly `ENABLE_TOKEN_CHECK` loop is the backstop. Both are silent no-ops
without Telegram configured — see §5.

**The one trap that turns a 5-minute fix into an hour:** the replacement **must be
a Page token, not a User token.** The Send API posts to `me/messages`, so `me` has
to resolve to the *Page*. Graph API Explorer **defaults to a User token** — copy
that by mistake and every send fails `GraphMethodException code:100 subcode:33
("Object with ID 'me' does not exist")`; the token authenticates but the bot still
looks silent.

### The procedure — a Page token that NEVER expires

**Production has run a non-expiring token since 2026-09-07.** Use this path.
Do NOT use the Debugger's *Extend Access Token* button: it mints a ~60-day
token, and that timer is what caused the 2026-09-04 outage — it fired at
22:23 on a Friday night (Ulaanbaatar) and the bot stayed down all weekend.
A Page token derived from a **long-lived User token** has no expiry at all.

All browser, no shell:

1. **Graph API Explorer** (<https://developers.facebook.com/tools/explorer/>) →
   Meta App = **MagicAI Bot** → "User or Page" = **User Token** → tick
   `pages_show_list`, `pages_messaging`, `pages_messaging_subscriptions`,
   `pages_read_engagement`, `pages_manage_metadata`, `pages_manage_posts` →
   **Generate Access Token** → approve → copy.
2. **Access Token Debugger** (<https://developers.facebook.com/tools/debug/accesstoken/>)
   → paste → **Debug** → **Extend Access Token** (needs the FB password). Copy the
   *extended* token. This is a long-lived **User** token — NOT what goes into Render.
3. Back in **Graph API Explorer**: clear the **Access Token** box, paste the
   extended User token from step 2, query `me/accounts`, **Submit**.
4. In the JSON find the entry whose `name` is `Magic Financial Group` and copy its
   **`access_token`**. That is the Page token, and it inherits the non-expiring
   property from the User token it came from.
5. **Verify in the Debugger BEFORE deploying:** Type = **Page**, Expires =
   **Never**, Profile ID = `123001937756085`. An expiry date means step 3 used the
   short-lived token — redo step 2.
6. **Render → `magicbot` service** (confirm it's `magicbot`, not another service) →
   **Environment** → replace `FACEBOOK_ACCESS_TOKEN` → **Save Changes**. Confirm a
   fresh *Environment updated* / *Deploy live* entry appears on that service's
   **Events** page — if there is none, the change went to the wrong place.
7. **Verify it took.** With shell access: `python scripts/diagnose.py` — check 2 must
   say *"Token is a valid Page token"* with the Page's name. Dashboard only: Render →
   **Logs** → search `FB token health`; the 6-hourly check logs `FB token health: OK`,
   which confirms the token AND that the worker dyno is alive. Or just message the
   Page from a non-admin account.

> **"Never expires" is not "never dies."** A non-expiring token still dies on a
> **Facebook password change** (`190`/`460` — the likeliest one, because changing a
> password feels unrelated to the bot), on the generating admin losing their Page
> role, or on the app being removed from the Page. That risk is now *unscheduled*
> rather than on a 60-day clock, so it cannot be calendared — the alerting in §5 is
> the only thing that catches it. Note down **whose** Facebook account the token was
> generated from; that account is a single point of failure.

### Fallback: the ~60-day token (emergency only)

If step 3 above will not produce a Page token (permissions missing, the account
lacks a Page role), a short-lived stopgap is: Graph API Explorer → "User or Page"
→ **Page Access Tokens → Magic Financial Group** → Debugger → *Extend Access
Token*. **This expires in ~60 days.** If you use it, put the expiry date in a
calendar the same minute, and replace it with the non-expiring token above as
soon as you can.

Full user-facing (Mongolian) walk-through:
`Facebook Page Access Token хэрхэн авах тухай дэлгэрэнгүй заавар.md` §5.

> **Replies composed during the outage are not lost.** `process_inbound_reply`
> commits the bot's message to the DB *before* handing it to Facebook, so every
> undelivered reply is visible in Admin → Мессежийн түүх. The bot will NOT
> resend them after the fix — work that history by hand for anyone who wrote
> during the outage.

---

## 5. When the alert fires and nothing happens

**What actually went wrong on 2026-09-04.** Telegram *was* configured and the
alerts *did* arrive — roughly ten of them, one every 6 hours from Friday night
to Monday. The detection worked; the response didn't. Two reasons, both fixed:

1. **The wording hedged.** The old health-check alert said the bot *"хариу
   илгээж чадахгүй **байж магадгүй**"* — "might not be able to reply." That reads
   like a warning about a possible future problem, not "the bot is down right
   now." A confirmed `190`/`463` is not a maybe. Confirmed token deaths now go
   through `alert_facebook_token_failure`, which states the outage as fact and
   names the fix; only genuinely ambiguous failures (Graph unreachable, a
   timeout) keep the soft wording, where it is honest.
2. **It repeated identically, forever.** `token_health_task` had no cooldown, so
   the same text arrived every 6 hours for three days. Both paths now share
   `FB_TOKEN_ALERT_COOLDOWN_HOURS` (6) and one state key, so an outage pages you
   once rather than ten times with a message you have already dismissed.

**The timing was the rest of it.** The token expired at **22:23 Friday,
Ulaanbaatar time** (07:23:45 PDT), i.e. the start of a weekend. No amount of
faster alerting fixes a Friday-night failure — which is why the real fix was
the non-expiring token in §4, not the alerting.

### If an alert never arrives at all

Both paths go through `send_telegram_notification`, a **silent no-op** when
`TELEGRAM_BOT_TOKEN` is unset or no chat IDs are configured. Check, on the
**worker** service (the 6-hourly health check only runs there):

- `TELEGRAM_BOT_TOKEN` is set.
- At least one chat ID is configured (Admin → Систем, or the env var).
- `WORKER_ROLE=worker` and `ENABLE_TOKEN_CHECK` is not set to false.

The send-path alert fires from the **web** dynos too, so it survives a dead
worker — but it still needs Telegram. Send yourself a test notification from the
admin panel after any change here.

### Where the alert lands matters as much as whether it fires

An outage alert that arrives in the same Telegram chat as routine handoff pings
competes with them for attention. If this recurs, route token/outage alerts to a
chat used for nothing else, so an unread message there means exactly one thing.
