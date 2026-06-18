# YouTube Channel Watcher (Gmail-controlled)

Watches YouTube channels and, on every new upload, uses Claude
(`claude-sonnet-4-6`) to write a one-page summary, saves it as a `.md`, and emails
it to you with the `.md` **attached** — all in one threaded Gmail conversation.
You add/list/delete channels by emailing the same Gmail address.

Runs free on **GitHub Actions** (public repo). Starting channel: **Brandon Carter**.

## How it works

- **`digest.yml`** — daily at **8 AM Pacific** (cron fires 15:00 + 16:00 UTC; the
  script proceeds only when it's 08:00 in `America/Los_Angeles`). Checks each
  channel's RSS feed, summarizes anything newer than the last-seen video, emails
  it as a reply in the running thread, and commits the summary + state.
- **`commands.yml`** — every ~15 min. Reads your `yt` command emails over IMAP and
  replies.

## Commands (email `kkamalvanshi@gmail.com`, subject starting `yt`)

| Send | Action |
|---|---|
| `yt add` + a YouTube link (channel / `@handle` / `/c/` / `/user/` / video URL) | Start watching that channel's future uploads. |
| `yt list` | List the channels being watched. |
| `yt delete` | Get a numbered list; reply with a number or name to remove one. |
| `yt delete <number or name>` | Remove in one step. |

Only emails from `ALLOWLIST_SENDERS` are acted on.

## One-time setup

1. Create a personal **Anthropic API key** at https://console.anthropic.com .
2. On `kkamalvanshi@gmail.com`: enable **2-Step Verification**, generate a 16-char
   **App Password** (Google → Security → App passwords), and **enable IMAP**
   (Gmail → Settings → Forwarding and POP/IMAP → Enable IMAP).
3. Create a **public** GitHub repo and push this project.
4. Add repo **secrets** (Settings → Secrets and variables → Actions):
   - `ANTHROPIC_API_KEY`
   - `GMAIL_ADDRESS` = `kkamalvanshi@gmail.com`
   - `GMAIL_APP_PASSWORD` = the 16-char app password
   - `ALLOWLIST_SENDERS` = `kkamalvanshi@gmail.com`
5. (Optional) Enable the two workflows under the repo's **Actions** tab.

The first daily run summarizes each channel's **current latest** video once (a
nice confirmation it works), then stays quiet until new uploads. To skip even
that, pre-fill `state/last_seen.json` with `{ "<channel_id>": "<latest video id>" }`.

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

- `channels.json` — watched channels.
- `state/last_seen.json` — last video id seen per channel.
- `state/thread.json` — Gmail thread chain for the updates conversation.
- `state/processed_emails.json` — handled command message-ids (dedupe).
- `state/pending.json` — in-flight `yt delete` selection.

## Notes / caveats

- **Transcripts**: brand-new uploads may lack captions, and YouTube sometimes
  blocks shared cloud IPs — in either case the summary falls back to the video
  **description**, clearly labeled.
- **60-day schedule rule**: GitHub disables scheduled workflows after 60 days of
  no repo activity. The daily commit normally keeps it active; for a hard
  guarantee, push the daily commit with a personal access token (so it's
  attributed to you).
