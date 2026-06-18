#!/usr/bin/env python3
"""YouTube channel watcher + Gmail control plane.

Modes:
  --mode digest    Check watched channels, summarize new uploads, email them
                   (one threaded conversation). Guarded to 8am Pacific unless --force.
  --mode commands  Read Gmail for `yt` commands (add via link / list / delete) and act.

Secrets are read from env vars:
  ANTHROPIC_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ALLOWLIST_SENDERS
"""

import argparse
import datetime
import email
import imaplib
import json
import os
import re
import smtplib
import time
from email import policy
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import feedparser
import requests

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
SUMMARIES = ROOT / "summaries"
CHANNELS_FILE = ROOT / "channels.json"
LAST_SEEN_FILE = STATE / "last_seen.json"
THREAD_FILE = STATE / "thread.json"
PROCESSED_FILE = STATE / "processed_emails.json"
PENDING_FILE = STATE / "pending.json"
LAST_CHECKED_FILE = STATE / "last_checked.txt"

UPDATES_SUBJECT = "\U0001F4FA YouTube watcher — new video summaries"
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
MODEL = "claude-sonnet-4-6"
PACIFIC = ZoneInfo("America/Los_Angeles")
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "tldr": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
        "markdown": {"type": "string"},
    },
    "required": ["tldr", "steps", "markdown"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You summarize YouTube videos into a tight, skimmable one-page brief.
Return JSON with three fields: tldr, steps, markdown.

`markdown` must follow EXACTLY this structure (use the Source value provided to you):

# <Video Title>
**Channel:** <name> · **Published:** <date> · **Watch:** <url>
**Source:** <full transcript | description only>

## TL;DR
2-3 sentences: what it's about and its single main message.

## What it covers
- 5-8 ordered bullets through the main segments / arguments.

## Steps / Process   (INCLUDE THIS SECTION ONLY if the video gives a routine, recipe, workout, or how-to)
- Every step / exercise / instruction in order, one line each — complete but terse
  (e.g. "Incline DB press — 4x8-10, 90s rest", "Step 3: deglaze with 1/2 cup stock").
  Do NOT drop any step.

## Key takeaways
- 3-5 concrete, actionable insights.

## Worth noting
- Up to 3 standout quotes / stats (omit this section entirely if none).

## Verdict
One sentence — who should watch the full video, or whether to skip.

Rules:
- Keep the narrative sections within one page (~400-500 words total).
- The Steps / Process section, WHEN PRESENT, must be COMPLETE (every step), even if it
  pushes the doc past one page. Omit it entirely if the video is not instructional.
- `tldr` = the 1-3 sentence hook as plain text (no markdown headers).
- `steps` = the same list of steps as a plain array of short strings, in order;
  use an empty array [] if the video has no routine / recipe / how-to.
"""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def env(name, required=True, default=None):
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"Missing required env var: {name}")
    return value


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return default
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load_channels():
    return load_json(CHANNELS_FILE, [])


def save_channels(channels):
    save_json(CHANNELS_FILE, channels)


def slugify(text):
    text = re.sub(r"[^\w\s-]", "", text or "").strip().lower()
    return re.sub(r"[\s_-]+", "-", text)[:60] or "video"


def footer():
    addr = env("GMAIL_ADDRESS")
    return (
        "\n\n───────────────\n"
        f'Commands — email {addr}, subject starting "yt":\n'
        "  • Add:    yt add <YouTube channel or video link>\n"
        "  • List:   yt list\n"
        "  • Delete: yt delete   (then reply with the number or name)\n"
    )


# --------------------------------------------------------------------------- #
# YouTube: RSS, transcript, channel-id resolution
# --------------------------------------------------------------------------- #
def fetch_feed(channel_id):
    return feedparser.parse(RSS_URL.format(cid=channel_id))


def feed_channel_name(feed):
    return getattr(feed.feed, "title", None) or "Unknown channel"


def entry_video_id(entry):
    vid = entry.get("yt_videoid")
    if vid:
        return vid
    match = re.search(r"[?&]v=([\w-]{11})", entry.get("link", ""))
    return match.group(1) if match else None


def entry_description(entry):
    return entry.get("summary") or entry.get("media_description") or ""


def get_transcript(video_id):
    """Return the transcript text, or None if unavailable (captions off / IP blocked)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        try:  # youtube-transcript-api >= 1.0 (instance API)
            fetched = YouTubeTranscriptApi().fetch(video_id)
            return " ".join(snippet.text for snippet in fetched)
        except AttributeError:  # older classmethod API
            data = YouTubeTranscriptApi.get_transcript(video_id)
            return " ".join(chunk["text"] for chunk in data)
    except Exception as exc:  # noqa: BLE001 - any failure -> fall back to description
        print(f"  transcript unavailable for {video_id}: {exc}")
        return None


def resolve_channel_id(url):
    """Resolve any YouTube URL (channel / @handle / /c/ / /user/ / video) to a UC… id."""
    match = re.search(r"/channel/(UC[\w-]{20,})", url)
    if match:
        return match.group(1)
    try:
        html = requests.get(url, headers=UA, timeout=20).text
    except Exception as exc:  # noqa: BLE001
        print(f"  could not fetch {url}: {exc}")
        return None
    for pattern in (
        r'"channelId":"(UC[\w-]{20,})"',
        r'"externalId":"(UC[\w-]{20,})"',
        r'<link[^>]+rel="canonical"[^>]+href="https://www\.youtube\.com/channel/(UC[\w-]{20,})"',
    ):
        found = re.search(pattern, html)
        if found:
            return found.group(1)
    return None


# --------------------------------------------------------------------------- #
# Claude summarization
# --------------------------------------------------------------------------- #
def summarize(title, channel, url, published, source, content):
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    label = "TRANSCRIPT" if source.startswith("full") else "DESCRIPTION"
    user = (
        f"Title: {title}\n"
        f"Channel: {channel}\n"
        f"URL: {url}\n"
        f"Published: {published}\n"
        f"Summary source: {source}\n\n"
        f"--- {label} ---\n{content}"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to summarize this video.")
    text = next(block.text for block in resp.content if block.type == "text")
    return json.loads(text)


# --------------------------------------------------------------------------- #
# Email out (SMTP)
# --------------------------------------------------------------------------- #
def smtp_send(msg):
    addr = env("GMAIL_ADDRESS")
    password = env("GMAIL_APP_PASSWORD")
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(addr, password)
        server.send_message(msg)


def send_update_email(channel_name, title, url, published, tldr, steps, md_path, dry=False):
    addr = env("GMAIL_ADDRESS")
    thread = load_json(THREAD_FILE, {})
    last_id = thread.get("last_message_id")
    refs = thread.get("references", [])

    msg = EmailMessage()
    msg["From"] = addr
    msg["To"] = addr
    if last_id:
        msg["Subject"] = "Re: " + UPDATES_SUBJECT
        msg["In-Reply-To"] = last_id
        msg["References"] = " ".join(refs)
    else:
        msg["Subject"] = UPDATES_SUBJECT
    new_id = make_msgid()
    msg["Message-ID"] = new_id

    body = (
        f'\U0001F3AC {channel_name} — "{title}"\n\n'
        f"▶  {url}\n"
        f"\U0001F5D3  Published {published}\n\n"
        f"TL;DR\n{tldr}\n"
    )
    if steps:
        body += "\nSteps / process:\n"
        body += "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1)) + "\n"
    body += f"\nFull summary attached: {md_path.name}\n"
    body += footer()
    msg.set_content(body)
    msg.add_attachment(
        md_path.read_bytes(), maintype="text", subtype="markdown", filename=md_path.name
    )

    if dry:
        print("---- DRY-RUN UPDATE EMAIL ----")
        print("Subject:", msg["Subject"])
        print(body)
        return

    smtp_send(msg)
    thread.setdefault("base_subject", UPDATES_SUBJECT)
    thread.setdefault("root_message_id", new_id)
    thread["last_message_id"] = new_id
    thread["references"] = refs + [new_id]
    save_json(THREAD_FILE, thread)


def send_reply(to_addr, orig_subject, orig_msgid, body, dry=False):
    addr = env("GMAIL_ADDRESS")
    subject = orig_subject or "yt"
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject

    msg = EmailMessage()
    msg["From"] = addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    if orig_msgid:
        msg["In-Reply-To"] = orig_msgid
        msg["References"] = orig_msgid
    msg.set_content(body + footer())

    if dry:
        print("---- DRY-RUN REPLY ----")
        print("Subject:", subject)
        print(body)
        return
    smtp_send(msg)


# --------------------------------------------------------------------------- #
# Summary file
# --------------------------------------------------------------------------- #
def write_summary_md(channel_name, video_id, title, markdown, published_date):
    directory = SUMMARIES / slugify(channel_name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{published_date}-{video_id}-{slugify(title)}.md"
    path.write_text(markdown.rstrip() + "\n")
    return path


# --------------------------------------------------------------------------- #
# Digest mode
# --------------------------------------------------------------------------- #
def process_new_video(channel_name, entry, dry=False):
    video_id = entry_video_id(entry)
    title = entry.get("title", "Untitled")
    url = entry.get("link") or f"https://www.youtube.com/watch?v={video_id}"

    published_dt = None
    if entry.get("published_parsed"):
        published_dt = datetime.datetime(*entry.published_parsed[:6])
    published_date = published_dt.strftime("%Y-%m-%d") if published_dt else "unknown"
    published_human = (
        published_dt.strftime("%b %d, %Y") if published_dt else entry.get("published", "unknown")
    )

    transcript = get_transcript(video_id)
    if transcript:
        source, content = "full transcript", transcript
    else:
        source = "description only (transcript unavailable)"
        content = entry_description(entry) or "(no description available)"

    print(f"  summarizing '{title}' ({source})")
    result = summarize(title, channel_name, url, published_human, source, content)
    path = write_summary_md(channel_name, video_id, title, result["markdown"], published_date)
    send_update_email(
        channel_name, title, url, published_human, result["tldr"], result.get("steps", []), path, dry=dry
    )
    print(f"  wrote {path} and emailed it")


def run_digest(force=False, dry=False):
    if not force:
        now_pt = datetime.datetime.now(PACIFIC)
        if now_pt.hour != 8:
            print(f"Not 8am Pacific (currently {now_pt:%H:%M} PT) — exiting.")
            return

    channels = load_channels()
    last_seen = load_json(LAST_SEEN_FILE, {})

    for channel in channels:
        cid = channel["channel_id"]
        name = channel.get("name", cid)
        print(f"Checking {name} ({cid})")
        feed = fetch_feed(cid)
        entries = list(feed.entries)
        if not entries:
            print("  no entries in feed")
            continue

        seen = last_seen.get(cid)
        if seen is None:
            new_entries = entries[:1]  # first time: newest only, skip the back-catalog
        else:
            new_entries = []
            for entry in entries:  # feed is newest-first
                if entry_video_id(entry) == seen:
                    break
                new_entries.append(entry)

        # Process oldest -> newest so the email thread reads chronologically.
        for entry in reversed(new_entries):
            process_new_video(name, entry, dry=dry)

        last_seen[cid] = entry_video_id(entries[0])
        time.sleep(1)  # be gentle to YouTube from a single runner IP

    if not dry:
        save_json(LAST_SEEN_FILE, last_seen)
        LAST_CHECKED_FILE.write_text(datetime.datetime.now(PACIFIC).isoformat() + "\n")


# --------------------------------------------------------------------------- #
# Command mode (Gmail inbox)
# --------------------------------------------------------------------------- #
YT_URL_RE = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/\S+", re.I)


def normalize_subject(subject):
    text = subject or ""
    while True:
        match = re.match(r"\s*(re|fwd|fw)\s*:\s*", text, re.I)
        if not match:
            break
        text = text[match.end():]
    return text.strip()


def get_text_body(message):
    if message.is_multipart():
        for part in message.walk():
            disp = str(part.get("Content-Disposition", ""))
            if part.get_content_type() == "text/plain" and "attachment" not in disp:
                return part.get_content()
        for part in message.walk():
            if part.get_content_type() == "text/html":
                return re.sub(r"<[^>]+>", " ", part.get_content())
        return ""
    return message.get_content()


def load_pending():
    pending = load_json(PENDING_FILE, {})
    if pending.get("action") == "delete":
        try:
            created = datetime.datetime.fromisoformat(pending["created"])
            if (datetime.datetime.now(PACIFIC) - created).total_seconds() > 86400:
                return {}
        except Exception:  # noqa: BLE001
            return {}
    return pending


def clear_pending():
    save_json(PENDING_FILE, {})


def resolve_target(text, channels, pending):
    """Map a user reply (a number or a channel name) to a channel_id."""
    stripped = re.sub(r"\b(yt|delete|remove|stop)\b", " ", text, flags=re.I).strip()
    num = re.search(r"\b(\d{1,3})\b", stripped)
    if num:
        idx = num.group(1)
        if pending.get("action") == "delete" and idx in pending.get("map", {}):
            return pending["map"][idx]
        n = int(idx)
        if 1 <= n <= len(channels):
            return channels[n - 1]["channel_id"]
    name = stripped.strip().strip('"').strip()
    if name:
        for channel in channels:
            if channel["name"].lower() == name.lower():
                return channel["channel_id"]
        for channel in channels:
            if name.lower() in channel["name"].lower():
                return channel["channel_id"]
    return None


def cmd_add(url, channels):
    cid = resolve_channel_id(url)
    if not cid:
        return f"Couldn't find a YouTube channel from that link:\n{url}\nTry the channel page URL or a video link."
    existing = next((c for c in channels if c["channel_id"] == cid), None)
    if existing:
        return f"Already watching {existing['name']}."

    feed = fetch_feed(cid)
    name = feed_channel_name(feed)
    latest = feed.entries[0] if feed.entries else None
    latest_id = entry_video_id(latest) if latest else None
    latest_title = latest.get("title", "(none)") if latest else "(none)"

    channels.append(
        {"channel_id": cid, "name": name, "added_at": datetime.date.today().isoformat()}
    )
    save_channels(channels)

    last_seen = load_json(LAST_SEEN_FILE, {})
    if latest_id:
        last_seen[cid] = latest_id  # seed: only future uploads get summarized
        save_json(LAST_SEEN_FILE, last_seen)

    return (
        f"Added {name}. You'll get a summary for every new upload from now on.\n"
        f"Channel: https://www.youtube.com/channel/{cid}\n"
        f'Latest existing video (not summarized): "{latest_title}"\n'
        f'Now watching {len(channels)} channels. Reply "yt list" to see them.'
    )


def cmd_list(channels):
    if not channels:
        return "You're not watching any channels yet. Email a YouTube link with subject 'yt add ...' to start."
    lines = [f"You're watching {len(channels)} channels:"]
    lines += [f"{i}. {c['name']}" for i, c in enumerate(channels, 1)]
    lines.append('\nReply "yt delete <number or name>" to remove one.')
    return "\n".join(lines)


def cmd_delete_prompt(channels):
    if not channels:
        return "You're not watching any channels."
    mapping = {str(i): c["channel_id"] for i, c in enumerate(channels, 1)}
    save_json(
        PENDING_FILE,
        {"action": "delete", "map": mapping, "created": datetime.datetime.now(PACIFIC).isoformat()},
    )
    lines = ["Which channel should I delete? Reply with the number or the name:"]
    lines += [f"{i}. {c['name']}" for i, c in enumerate(channels, 1)]
    return "\n".join(lines)


def do_delete(cid, channels):
    name = next((c["name"] for c in channels if c["channel_id"] == cid), cid)
    remaining = [c for c in channels if c["channel_id"] != cid]
    save_channels(remaining)
    last_seen = load_json(LAST_SEEN_FILE, {})
    last_seen.pop(cid, None)
    save_json(LAST_SEEN_FILE, last_seen)
    clear_pending()
    return f"\U0001F5D1 Removed {name} — now watching {len(remaining)} channels."


def handle_command(nsubj, body, sender, orig_subject, orig_msgid, dry=False):
    text = f"{nsubj}\n{body}".strip()
    low = text.lower()
    channels = load_channels()
    pending = load_pending()

    url_match = YT_URL_RE.search(text)
    if url_match:
        reply = cmd_add(url_match.group(0).strip(), channels)
    elif re.search(r"\b(list|channels|show)\b", low):
        reply = cmd_list(channels)
    elif re.search(r"\b(delete|remove|stop)\b", low) or pending.get("action") == "delete":
        cid = resolve_target(text, channels, pending)
        if cid:
            reply = do_delete(cid, channels)
        elif re.search(r"\b(delete|remove|stop)\b", low):
            reply = cmd_delete_prompt(channels)
        else:
            reply = "Reply with the number or name to delete (or 'yt list' to see them)."
    else:
        reply = (
            "Sorry, I didn't recognize that. Use:\n"
            "  yt add <link>   |   yt list   |   yt delete"
        )

    send_reply(sender, orig_subject, orig_msgid, reply, dry=dry)


def run_commands(dry=False):
    addr = env("GMAIL_ADDRESS")
    password = env("GMAIL_APP_PASSWORD")
    allow = {a.strip().lower() for a in env("ALLOWLIST_SENDERS").split(",") if a.strip()}
    processed = set(load_json(PROCESSED_FILE, []))

    mailbox = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mailbox.login(addr, password)
    mailbox.select("INBOX")
    _, data = mailbox.search(None, "UNSEEN")
    msg_nums = data[0].split()

    for num in msg_nums:
        _, fetched = mailbox.fetch(num, "(BODY.PEEK[])")
        message = email.message_from_bytes(fetched[0][1], policy=policy.default)
        msgid = message.get("Message-ID", "")
        if msgid and msgid in processed:
            continue
        sender = parseaddr(message.get("From", ""))[1].lower()
        subject = message.get("Subject", "")
        nsubj = normalize_subject(subject)
        if sender not in allow or not nsubj.lower().startswith("yt"):
            continue

        body = get_text_body(message)
        print(f"command from {sender}: {nsubj!r}")
        handle_command(nsubj, body, sender, subject, msgid, dry=dry)

        if not dry:
            mailbox.store(num, "+FLAGS", "\\Seen")
        if msgid:
            processed.add(msgid)

    mailbox.logout()
    if not dry:
        save_json(PROCESSED_FILE, sorted(processed))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["digest", "commands"], required=True)
    parser.add_argument("--force", action="store_true", help="ignore the 8am Pacific guard (digest)")
    parser.add_argument("--no-email", action="store_true", help="print emails instead of sending")
    args = parser.parse_args()

    if args.mode == "digest":
        run_digest(force=args.force, dry=args.no_email)
    else:
        run_commands(dry=args.no_email)


if __name__ == "__main__":
    main()
