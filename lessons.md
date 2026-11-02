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
