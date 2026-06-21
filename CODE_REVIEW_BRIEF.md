# Code Review Brief — `youtube-watcher`

**You are an independent code reviewer.** Another AI assistant built this project across a long, iterative session. Your job is to **audit all of it for correctness and completeness** — find holes, bugs, untested paths, race conditions, security issues, and any requested feature that is missing or broken. **Do not assume the code is correct because it reads cleanly — actually run it and verify behavior.**

Be skeptical and concrete. For every claim of "this works," point to evidence (a test you ran, a code path you traced). Report findings with severity (blocker / bug / risk / nit), the file:line, and a suggested fix.

---

## 1. What the project is

A hands-off automation that watches a list of YouTube channels and, on each new upload, uses **Claude (Sonnet 4.6)** to write a detailed summary, renders it to a **styled PDF**, and emails it to the user. It runs on **GitHub Actions** (free, public repo). The user controls it by **email commands**. It is currently deployed at `github.com/kkamalvanshi/youtube-watcher` and runs live.

**Main file:** `watcher.py` (single module). Entry point: `python watcher.py --mode digest|commands [--force] [--no-email]`.

---

## 2. How to set up to test

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium      # PDF rendering uses headless Chromium
cp .env.example .env                        # fill in real values (ask the user)
set -a; source .env; set +a
```

Env vars (all consumed in `watcher.py`): `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `RESEND_FROM`, `RECIPIENT`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `ALLOWLIST_SENDERS`, `SUPADATA_API_KEY`, `WEBSHARE_PROXY_USERNAME`, `WEBSHARE_PROXY_PASSWORD`.

Safe ways to run without spamming the user:
- `python watcher.py --mode digest --force --no-email` — runs the pipeline, prints the email instead of sending (still makes real Claude/transcript calls).
- Unit-test the pure functions directly (see §5).
- Trigger cloud runs via `gh workflow run digest.yml` / `commands.yml` and read logs with `gh run view <id> --log`.

---

## 3. Features to verify (REQUESTED by the user)

Tick each only after you've confirmed it in code AND, where possible, by running it.

- [ ] Detects new uploads for a configured channel set via YouTube RSS (`feedparser`), no API key/quota.
- [ ] Summarizes each new video with **Claude `claude-sonnet-4-6`**.
- [ ] Sends the video **link** in the email.
- [ ] Runs in the **cloud** (GitHub Actions), works with the user's laptop off.
- [ ] Polls/【runs **daily at 8:00 AM Pacific**】 (DST-aware) and catches each new upload.
- [ ] **Multi-channel**: a config list (`channels.json`); easy to add channels.
- [ ] **Email commands** (subject starting `yt`, OR a reply in the conversation):
  - [ ] `add` + a YouTube link → resolves `@handle` / `/c/` / `/user/` / `/channel/` / video URL to a `UC…` id, adds it, seeds last-seen to current latest (only future uploads summarized), replies to confirm.
  - [ ] `list` → numbered list of channels.
  - [ ] `delete` → numbered list, then reply with a **number or name**; also one-shot `delete <number|name>`.
- [ ] Delivery is **email to `kkamalvanshi@gmail.com`** via **Resend** (a distinct sender, so it lands in the inbox — not self-sent "from me").
- [ ] User's command **replies route back** to the Gmail inbox the listener reads (Reply-To).
- [ ] Summary delivered as a **PDF attachment** (not pasted into the body); email body = title + link + TL;DR only.
- [ ] Every email ends with a **command cheatsheet** footer.
- [ ] Summary structure: **Executive Summary (20–40 words)**, a **Contents/table-of-contents** section, then one `##` heading **per topic** (no "Detailed Breakdown" heading), each topic comprehensive.
- [ ] Each topic heading has a **relevant emoji** and a **clickable timestamp link** into the video (`&t=<seconds>s`) at that topic's start.
- [ ] Bullets nest up to **4 levels** and actually render **indented** (4-space markdown indentation).
- [ ] **Tables** used when they communicate better than bullets.
- [ ] **Bolding is sparse** (not every term highlighted).
- [ ] **Fonts**: smallest ~12px, everything proportionally larger.
- [ ] Transcripts fetched via **Supadata** (primary).
- [ ] **All PDFs in ONE flat folder** `summaries/` (no per-channel subfolders), channel as filename prefix.
- [ ] If **multiple videos upload the same day**, they go in **ONE email** (all PDFs attached), **subject includes the date**.
- [ ] "Downloadable video" = the **YouTube link** (attaching the file is infeasible — Gmail 25 MB cap; confirm this is handled, not silently broken).

## 4. Features/decisions ADDED (not explicitly requested) — verify they're sound

- [ ] Transcript fallback chain: **Supadata → youtube-transcript-api (Webshare proxy / direct) → video description**. Timestamped transcript (`[t=Ns]` markers) feeds the timestamp links.
- [ ] **Extended thinking is disabled** for summarization (adaptive thinking could consume the whole `max_tokens` on long transcripts → truncated/empty output). `max_tokens=32000`, streaming.
- [ ] **Chromium (Playwright)** renders the PDF (for color emoji + full CSS); replaced `xhtml2pdf`.
- [ ] **Resilient digest**: per-video try/except; `last_seen` advances only past videos that were actually summarized+sent; failures retry next run.
- [ ] **`X-YT-Watcher` header** on all outgoing mail + skip-on-intake, so the bot never processes its own emails as commands (no reply loop).
- [ ] Command intake searches **recent mail (read or unread)** and dedupes on Message-ID (`processed_emails.json`) — not UNSEEN-only.
- [ ] **Digest guard** (recently fixed): runs once/day on the first fire **≥ 8 AM Pacific**, tracked in `state/last_digest_date.txt` (GitHub delays scheduled runs 1–2h, so an exact `hour == 8` check skipped every run — verify the fix is correct and can't double-send or skip a day).
- [ ] **Allowlist** (`ALLOWLIST_SENDERS`) — only the user's address can issue commands.
- [ ] Workflow **concurrency group** shared by both workflows; state committed back to the repo each run.

---

## 5. Suggested verification steps

1. **Static**: `python -m py_compile watcher.py`; read `watcher.py` end-to-end; check imports, unused code, error handling.
2. **Pure functions** (no network/secrets): test `slugify`, `normalize_subject`, `extract_reply_text`, `header_refs`, `resolve_target`, `entry_video_id`, `_format_timestamped`, `YT_URL_RE`. Confirm `resolve_channel_id` on a `/channel/UC…` URL (no network) and on an `@handle` (network).
3. **Markdown→PDF**: render a sample with 4-level nested bullets, a table, emoji headers, and a timestamp link; confirm Chromium produces a valid multi-page PDF and bullets are visually indented.
4. **Transcript**: confirm `get_transcript` returns `[t=Ns]`-prefixed text via Supadata and that the max timestamp is plausible for the video length (ms→s conversion).
5. **Summarize**: run `summarize()` on a real transcript; confirm `{tldr, markdown}`, no truncation (`stop_reason` handling), comprehensive output, sparse bolding, emoji + timestamp headers.
6. **Digest end-to-end**: `--mode digest --force --no-email` (prints batched email) and a real `gh workflow run digest.yml` (read the log: did it summarize, batch into one dated email, advance state?).
7. **Commands**: send a test `yt list` and an in-thread `list` reply; run `--mode commands`; confirm correct reply, dedupe, no loop.

---

## 6. Holes to hunt for specifically

- **Email size**: a busy day batches many ~300–400 KB Chromium PDFs into one email. Check Resend/Gmail attachment limits (~25–40 MB) — is there a cap or failure if a day has many uploads?
- **Digest guard correctness**: across DST, GitHub delays, and two daily cron fires sharing one concurrency group — can it skip a day or send twice? Does `last_digest_date.txt` get committed/pushed reliably before the second fire reads it?
- **State race conditions**: `digest.yml` and `commands.yml` both commit/push state. They share a concurrency group — confirm that actually serializes them and that `git pull --rebase --autostash` + push can't lose or corrupt `last_seen.json` / `processed_emails.json` / `thread.json`.
- **`last_seen` advancement**: on partial failure mid-batch, are already-sent videos prevented from re-sending, and are failed ones retried (not skipped)?
- **Command parsing**: in-thread reply detection (subject-prefix match vs `yt` subject), quoted-text stripping in `extract_reply_text`, `delete` by number vs name, stale `pending.json` (expiry), and that the bot never treats its own mail as a command.
- **Transcript edge cases**: no captions (premieres/live), Supadata async/large-video responses, proxy down — does it degrade to description cleanly without crashing the run?
- **Timestamp links**: are the `&t=Ns` seconds accurate to each topic's start? What if the source is description-only (links should be omitted)?
- **Cost/limits**: `max_tokens=32000` + very long transcripts (input tokens). Any unbounded growth (`processed_emails.json`, committed PDFs bloating the repo)?
- **Security**: secrets only via env/GitHub Secrets; `.env` gitignored; allowlist enforced; no secret in committed files or logs.
- **Silent failures**: anywhere an exception is swallowed and a video/command is dropped without retry or notice.

---

## 7. Known / intentional decisions (do NOT flag as bugs)

- **iMessage is not used** — Apple has no send/receive API from a cloud server; email via Resend was chosen deliberately.
- **The video file is not attached** — Gmail's 25 MB cap; the email includes the YouTube link instead.
- **Extended thinking is intentionally off** for summaries (reliability; see §4).
- **Dated subject ⇒ each day is its own Gmail conversation** (not one infinite thread) — intentional, per the user's "date in the subject" request. Command replies still register via subject-prefix match.
- **Public repo** — chosen for free unlimited Actions; summaries (PDFs) are committed as the storage folder.
- **Webshare proxy is a fallback only** now that Supadata is primary; it may be disabled.

---

## 8. What to deliver

A written report:
1. **Verdict**: does the system fulfill every item in §3 and §4? List anything missing or broken.
2. **Findings** by severity (blocker / bug / risk / nit), each with `file:line`, evidence (what you ran/traced), and a fix.
3. **Holes from §6** you confirmed or ruled out.
4. **Anything the user asked for that you can't find implemented.**

Run the code. Prefer evidence over inspection.
