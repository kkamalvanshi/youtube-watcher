# YouTube Channel + X Poster Watcher (Gmail-controlled)

Watches YouTube channels and X (Twitter) accounts. On every new video, Claude
(`claude-sonnet-4-6`) writes a detailed summary (a 20–40 word executive summary
plus a full outline breakdown) and it's rendered to a **PDF**. On every new X
post/thread, the verbatim tweet text and images are rendered to a PDF too (no
AI rewriting of the content itself), with a short Claude-written TL;DR at the
top of the PDF and in the email. Everything lands in **one daily email** with
two sections — "📺 YouTube" and "🐦 X Posts" — each PDF attached, all in one
threaded Gmail conversation. You add/list/delete channels and X accounts by
emailing the same Gmail address.

Runs free on **GitHub Actions** (public repo). Starting channel: **Brandon Carter**.

## How it works

- **`digest.yml`** — daily at **8 AM Pacific** (cron fires 15:00 + 16:00 UTC; the
  script proceeds only when it's 08:00 in `America/Los_Angeles`). Checks each
  channel's RSS feed and each watched X account's timeline, summarizes anything
  new, emails it as a reply in the running thread, and commits the summaries +
  state.
- **`commands.yml`** — every ~15 min. Reads your `yt`/`x` command emails over
  IMAP and replies.

## Commands (email `your-gmail@example.com`, subject starting `yt` or `x`)

| Send | Action |
|---|---|
| `yt add` + a YouTube link (channel / `@handle` / `/c/` / `/user/` / video URL) | Start watching that channel's future uploads. |
| `yt list` | List the channels being watched. |
| `yt delete` | Get a numbered list; reply with a number or name to remove one. |
| `yt delete <number or name>` | Remove in one step. |
| `x add` + an x.com/twitter.com link or `@handle` | Start watching that account's future posts. |
| `x list` | List the X accounts being watched. |
| `x delete` | Get a numbered list; reply with a number or name to remove one. |
| `x delete <number or name>` | Remove in one step. |

Only emails from `ALLOWLIST_SENDERS` are acted on.

## One-time setup

1. Create a personal **Anthropic API key** at https://console.anthropic.com .
2. On `your-gmail@example.com`: enable **2-Step Verification**, generate a 16-char
   **App Password** (Google → Security → App passwords), and **enable IMAP**
   (Gmail → Settings → Forwarding and POP/IMAP → Enable IMAP).
3. Create a **public** GitHub repo and push this project.
4. Add repo **secrets** (Settings → Secrets and variables → Actions):
   - `ANTHROPIC_API_KEY`
   - `RESEND_API_KEY` = your Resend API key (sending, so mail arrives from a distinct sender)
   - `RESEND_FROM` = `YouTube Watcher <onboarding@resend.dev>`
   - `RECIPIENT` = `your-gmail@example.com` (where summaries are sent)
   - `GMAIL_ADDRESS` = `your-gmail@example.com` (inbox the command listener reads via IMAP)
   - `GMAIL_APP_PASSWORD` = the 16-char app password
   - `ALLOWLIST_SENDERS` = `your-gmail@example.com`
   - `SUPADATA_API_KEY` = Supadata API key — primary transcript source (reliable, no proxy)
   - `WEBSHARE_PROXY_USERNAME` = Webshare residential proxy username (optional transcript fallback)
   - `WEBSHARE_PROXY_PASSWORD` = Webshare residential proxy password (optional transcript fallback)
   - `X_BEARER_TOKEN` = X API v2 App-only Bearer token (read access) — only needed to
     watch X accounts; leave `x_users.json` empty to skip this entirely.
5. (Optional) Enable the two workflows under the repo's **Actions** tab.

The first daily run summarizes each channel's/account's **current latest**
video/post once (a nice confirmation it works), then stays quiet until new
uploads. To skip even that, pre-fill `state/last_seen.json` with
`{ "<channel_id>": "<latest video id>" }` and/or `state/x_last_seen.json` with
`{ "<user_id>": "<latest tweet id>" }`.

## Local testing

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your values
set -a; source .env; set +a

python watcher.py --mode digest --force --no-email   # print, don't send
python watcher.py --mode digest --force              # actually email
python watcher.py --mode commands                    # process inbox commands
```

- `--force` ignores the 8 AM Pacific guard (always used by manual `workflow_dispatch`).
- `--no-email` prints the email instead of sending it.

## State (committed to the repo)

- `channels.json` — watched YouTube channels.
- `x_users.json` — watched X accounts (`user_id`, `handle`, `added_at`).
- `state/last_seen.json` — last video id seen per channel.
- `state/x_last_seen.json` — last tweet id seen per X account.
- `state/thread.json` — Gmail thread chain for the updates conversation.
- `state/processed_emails.json` — handled command message-ids (dedupe).
- `state/pending.json` — in-flight `yt delete`/`x delete` selection.

## Notes / caveats

- **Transcripts**: brand-new uploads may lack captions, and YouTube sometimes
  blocks shared cloud IPs — in either case the summary falls back to the video
  **description**, clearly labeled.
- **X threads spanning days**: if a self-thread gets a new reply after the
  original tweets already went out in an earlier digest, the watcher backfills
  the full thread (via X's recent-search endpoint, a 7-day window) so that
  day's PDF is complete — it overwrites the same PDF filename rather than
  creating a duplicate.
- **60-day schedule rule**: GitHub disables scheduled workflows after 60 days of
  no repo activity. The daily commit normally keeps it active; for a hard
  guarantee, push the daily commit with a personal access token (so it's
  attributed to you).
