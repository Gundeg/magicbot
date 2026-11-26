# lessons.md — corrections from Gundeg, turned into rules

Checked at the start of every session on this project (per global CLAUDE.md).

## 2026-11-03 — Never show Russian text to Gundeg

**What happened:** while fixing the bot replying in the customer's language, I
printed Russian test inputs/outputs in chat. Gundeg: "орос хэл огт бүү ашигла.
надтай англиар эсвэл монголоо ярина."

**Rule:** Communicate with Gundeg in Mongolian (or English) only. Never paste,
quote or display Russian text in messages — not even as a test sample or a
bot reply excerpt. If a test needs non-Mongolian input, describe it
("орос хэлээр асуусан хувилбар") instead of showing the text, and keep the
sample inside the script file.

**Related rule for the bot itself:** the bot must always answer in Mongolian
Cyrillic regardless of the customer's language — enforced in code
(`LANGUAGE_QUALITY_RULE`, cached stable half), not in the persona.

## 2026-11-03 — Don't over-generalise a correction

**What happened:** from "орос хэл огт бүү ашигла" I shipped a code rule that
forces the bot to answer in Mongolian whatever language the customer writes.
Gundeg: the instruction was about *my* messages and the bot's *default*
language — a customer writing in a foreign language may be answered in that
language. They chose to leave the strict rule in place for now (no foreign
customers yet).

**Rule:** When a correction arrives, restate the exact scope I'm about to
apply it to before changing product behaviour — especially customer-facing
behaviour — instead of extending it to the widest plausible reading. Product
behaviour changes need the user's scope, not my inference.

**Current state to remember (2026-11-03 entry):** `LANGUAGE_QUALITY_RULE` still says "always
Mongolian". If foreign-language customers appear, relax it to "Mongolian by
default; answer in the customer's language when they write in one" — a
one-line prompt change + the two eval scenarios.

## 2026-11-25 — A model switch needs a cost projection against the old bill

**What happened:** the 2026-11-02 move to Claude put every reply on Opus 5.5 for
Mongolian quality, with no monthly cost estimate. The bill went from $30-40 a
month (OpenAI/Gemini) to ~$100 in under a month, and the Console spend limit
stopped the bot twice (11-12, 11-25). Gundeg: "арай ч дээ". Measured on
2026-11-25: the cached prompt had grown to ~26.6K tokens (CLAUDE.md still said
14K), 60% of replies missed the 5-minute cache, and those cache WRITES were
~80% of the cost (~$0.10 a reply, ~60 replies a day).

**Rule:** before recommending or shipping a model, provider, effort or prompt-size
change, project the monthly cost from real prod volume (replies/day x tokens per
reply, cache writes included at their write price) and show it to Gundeg next to
the previous monthly bill, BEFORE merging. Re-measure the prompt size and the
cache hit rate from the `reply usage` log whenever the admin data grows — never
quote a token count from docs.

## 2026-11-16 — Drive the Chrome window Gundeg is actually looking at

**What happened:** two Chrome browsers were connected to the extension; I opened
the Render tab in the default one ("Browser 1") and asked Gundeg to sign in
there. Gundeg: "you didn't open chrome." — the tab was in a window they
couldn't see, and the first diagnosis round was wasted.

**Rule:** before the first browser action of a session, call
`list_connected_browsers`. If more than one is connected, run
`switch_browser` (Gundeg clicks Connect in the window in front of them)
BEFORE navigating — never assume the default is the visible one. Tell
Gundeg to have Chrome in front with the extension panel open, because the
connect prompt times out after 2 minutes.
