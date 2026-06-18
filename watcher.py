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

UPDATES_SUBJECT = "\U0001F4FA YouTube watcher"
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
        "markdown": {"type": "string"},
    },
    "required": ["tldr", "markdown"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You summarize YouTube videos into a detailed, skimmable brief.
Return JSON with two fields: tldr, markdown.

Base everything STRICTLY on the transcript or description provided. Never invent
details that aren't in the source. If the source is only a short description, produce a
proportionally shorter breakdown — do NOT pad or fabricate to reach a word count.

`markdown` must follow EXACTLY this structure (use the Source value provided to you):

# <Video Title>
**Channel:** <name> · **Published:** <date> · **Watch:** <url>
**Source:** <full transcript | description only>

## Executive Summary
A 20-40 word overview — the single big-picture point of the video.

## Detailed Breakdown
A thorough OUTLINE of everything in the video, in order. Full sentences are NOT
required — fragments and nested bullets are ideal. Cover every topic, argument,
explanation, example, and number, and lay out the COMPLETE process / steps whenever the
video has one (sets, reps, amounts, settings, sequence — leave nothing out). When the
source is a full transcript, aim for ~1000-2000 words here; when it's only a
description, be as detailed as the source genuinely supports.

Use nested bullets UP TO FOUR LEVELS DEEP where the content has that structure.
Indent each deeper level by exactly 2 spaces and start every bullet with "- ", e.g.:

- <Topic / segment>
  - <main point>
    - <sub-point / supporting detail>
      - <finer detail, number, example, or sub-step>
- <next Topic / segment>
  - (for any routine / recipe / how-to, list every step in order with specifics)
(continue through the ENTIRE video — do not skip sections; nest down to 4 levels when it adds clarity)

Emphasis (use SPARINGLY — restraint matters, do not over-bold):
- **Bold** only a FEW of the most important items — the single key number, name, or
  takeaway in a section, not every term. At most one or two bolds per section, and many
  sections need none. Over-bolding makes the page noisy and is worse than no bolding.
- Optionally use a Markdown blockquote (a line starting with "> ") to call out the one
  biggest insight of the whole video — at most once or twice in the ENTIRE summary.

Rules:
- `tldr` = the Executive Summary text (20-40 words, plain text, no markdown headers).
- Put ALL detail (including the full process/steps) in `markdown` — the email body only
  shows the TL;DR, so nothing important should live outside the markdown.
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
    return (
        "\n\n───────────────\n"
        "Reply in this thread to control me:\n"
        "  • Add a channel:    add <YouTube channel or video link>\n"
        "  • List channels:    list\n"
        "  • Delete a channel: delete   (then reply with the number or name)\n"
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


def _build_transcript_api():
    """Build a YouTubeTranscriptApi, routed through Webshare residential proxies
    when WEBSHARE_PROXY_USERNAME / WEBSHARE_PROXY_PASSWORD are set."""
    from youtube_transcript_api import YouTubeTranscriptApi

    proxy_user = os.environ.get("WEBSHARE_PROXY_USERNAME")
    proxy_pass = os.environ.get("WEBSHARE_PROXY_PASSWORD")
    if proxy_user and proxy_pass:
        from youtube_transcript_api.proxies import WebshareProxyConfig

        return YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(
                proxy_username=proxy_user, proxy_password=proxy_pass
            )
        )
    return YouTubeTranscriptApi()


def get_transcript(video_id):
    """Return the transcript text, or None if unavailable.

    Uses a Webshare residential proxy when configured (required to fetch transcripts
    from cloud IPs such as GitHub Actions runners); otherwise tries a direct request.
    """
    try:
        fetched = _build_transcript_api().fetch(video_id)
        return " ".join(snippet.text for snippet in fetched)
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
        max_tokens=16000,
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
def resend_send(subject, text_body, headers, reply_to, attachment=None):
    """Send one email via the Resend HTTP API (https://resend.com)."""
    import base64

    api_key = env("RESEND_API_KEY")
    from_addr = os.environ.get("RESEND_FROM") or "YouTube Watcher <onboarding@resend.dev>"
    recipient = os.environ.get("RECIPIENT") or env("GMAIL_ADDRESS")
    payload = {
        "from": from_addr,
        "to": [recipient],
        "reply_to": reply_to,
        "subject": subject,
        "text": text_body,
        "headers": headers,
    }
    if attachment is not None:
        payload["attachments"] = [
            {
                "filename": attachment.name,
                "content": base64.b64encode(attachment.read_bytes()).decode(),
            }
        ]
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def send_threaded(body, attachment=None, dry=False):
    """Send a message into the single canonical conversation thread, via Resend.

    Sends from a distinct address (so it arrives as normal inbox mail, not 'from me'),
    with Reply-To set to your address so your in-thread replies route back to the
    Gmail inbox the command listener reads. Tagged X-YT-Watcher to avoid re-ingestion.
    All bot mail uses one constant subject so Gmail keeps it in a single conversation.
    """
    recipient = os.environ.get("RECIPIENT") or env("GMAIL_ADDRESS")
    thread = load_json(THREAD_FILE, {})
    base_subject = thread.get("base_subject", UPDATES_SUBJECT)
    last_id = thread.get("last_message_id")
    refs = thread.get("references", [])
    new_id = make_msgid()

    headers = {"X-YT-Watcher": "bot", "Message-ID": new_id}
    if last_id:
        headers["In-Reply-To"] = last_id
        headers["References"] = " ".join(refs)

    if dry:
        print("---- DRY-RUN THREADED EMAIL (Resend) ----")
        print("Subject:", base_subject)
        print(body)
        return

    resend_send(base_subject, body + footer(), headers, reply_to=recipient, attachment=attachment)
    thread.setdefault("base_subject", base_subject)
    thread.setdefault("root_message_id", new_id)
    thread["last_message_id"] = new_id
    thread["references"] = refs + [new_id]
    save_json(THREAD_FILE, thread)


# --------------------------------------------------------------------------- #
# Summary file
# --------------------------------------------------------------------------- #
_PDF_CSS = """
@page { size: letter; margin: 1.4cm 1.5cm 1.5cm 1.5cm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 12.5px; line-height: 1.5; color: #2b2b33; }
.hdr { background-color: #111827; padding: 16px 18px; margin-bottom: 16px; }
.title { color: #ffffff; font-size: 22px; font-weight: bold; }
.meta { color: #9ca3af; font-size: 10.5px; margin-top: 7px; }
.exec { background-color: #eef4ff; border-left: 5px solid #2563eb; padding: 11px 14px; margin: 4px 0 16px 0; font-size: 13.5px; color: #15233b; }
h2 { color: #1d4ed8; font-size: 16px; margin: 20px 0 8px 0; border-bottom: 2px solid #bfdbfe; padding-bottom: 4px; }
h3 { color: #111827; font-size: 13.5px; margin: 13px 0 4px 0; }
p { margin: 5px 0; }
ul { margin: 4px 0 9px 0; padding-left: 18px; }
li { margin: 3px 0; }
strong { color: #b45309; font-weight: bold; }
em { color: #6d28d9; font-style: italic; }
blockquote { background-color: #f0f7ff; border-left: 4px solid #2563eb; margin: 10px 0; padding: 8px 13px; color: #1e3a5f; }
"""


def write_summary_pdf(channel_name, video_id, title, markdown_text, published_date):
    """Render the markdown summary into a styled, readable PDF and return its path."""
    import re as _re
    from html import escape
    import markdown as md_lib
    from xhtml2pdf import pisa

    directory = SUMMARIES / slugify(channel_name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{published_date}-{video_id}-{slugify(title)}.pdf"

    # Drop emoji (the built-in PDF fonts can't render them and they leave gaps).
    emoji = _re.compile(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
        "\U0001F1E6-\U0001F1FF\U00002190-\U000021FF\U0000FE00-\U0000FE0F]"
    )
    lines = emoji.sub("", markdown_text).splitlines()

    # Pull the title + meta lines (everything before the first "## ") into a styled
    # header; render the rest of the markdown as the body.
    doc_title, meta_parts, body_start = "", [], len(lines)
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            body_start = i
            break
        s = ln.strip()
        if s.startswith("# ") and not doc_title:
            doc_title = s[2:].strip()
        elif s:
            meta_parts.append(s.replace("**", ""))
    body_md = "\n".join(lines[body_start:])

    header = f'<div class="hdr"><div class="title">{escape(doc_title or title)}</div>'
    if meta_parts:
        header += f'<div class="meta">{escape(" ".join(meta_parts))}</div>'
    header += "</div>"

    body_html = md_lib.markdown(body_md, extensions=["extra", "sane_lists"])
    html = (
        "<html><head><meta charset='utf-8'><style>" + _PDF_CSS + "</style></head>"
        "<body>" + header + body_html + "</body></html>"
    )
    with open(path, "wb") as fh:
        status = pisa.CreatePDF(src=html, dest=fh, encoding="utf-8")
    if status.err:
        raise RuntimeError(f"PDF generation failed for {video_id}")
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
    path = write_summary_pdf(channel_name, video_id, title, result["markdown"], published_date)

    body = (
        f'\U0001F3AC {channel_name} — "{title}"\n\n'
        f"▶  {url}\n"
        f"\U0001F5D3  Published {published_human}\n\n"
        f"TL;DR\n{result['tldr']}\n\n"
        f"Full summary — executive summary + detailed breakdown — is in the attached PDF:\n{path.name}\n"
    )

    send_threaded(body, attachment=path, dry=dry)
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


def header_refs(message):
    """All Message-IDs this message references (In-Reply-To + References headers)."""
    ids = set()
    for header in ("In-Reply-To", "References"):
        ids.update(re.findall(r"<[^>]+>", message.get(header, "")))
    return ids


def extract_reply_text(body):
    """The top, un-quoted portion of a reply — strips Gmail quote, attribution, footer."""
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if re.match(r"On .+wrote:$", stripped):
            break
        if stripped.startswith("─") or stripped.startswith("___") or stripped.startswith("----"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


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


def handle_command(command_text, dry=False):
    text = command_text.strip()
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
            reply = "Reply with the number or name to delete (or 'list' to see them)."
    else:
        reply = "I didn't catch that. Reply with:\n  add <link>   |   list   |   delete"

    send_threaded(reply, dry=dry)


def run_commands(dry=False):
    addr = env("GMAIL_ADDRESS")
    password = env("GMAIL_APP_PASSWORD")
    allow = {a.strip().lower() for a in env("ALLOWLIST_SENDERS").split(",") if a.strip()}
    processed = set(load_json(PROCESSED_FILE, []))

    # Message-IDs in our one canonical thread — a reply referencing any of them is a
    # command, so the user can just reply in the thread (chat-style) with no `yt` subject.
    thread = load_json(THREAD_FILE, {})
    base_subject = thread.get("base_subject", UPDATES_SUBJECT).strip().lower()
    thread_ids = set(thread.get("references", []))
    for key in ("root_message_id", "last_message_id"):
        if thread.get(key):
            thread_ids.add(thread[key])

    mailbox = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mailbox.login(addr, password)
    mailbox.select("INBOX")

    # Recent mail from allowlisted senders (read or unread); dedupe via processed_emails.json.
    since = (datetime.datetime.now(PACIFIC) - datetime.timedelta(days=3)).strftime("%d-%b-%Y")
    msg_nums, seen_nums = [], set()
    for sender_addr in allow:
        _, data = mailbox.search(None, "SINCE", since, "FROM", sender_addr)
        msg_nums.extend(data[0].split())

    for num in msg_nums:
        if num in seen_nums:
            continue
        seen_nums.add(num)
        _, fetched = mailbox.fetch(num, "(BODY.PEEK[])")
        message = email.message_from_bytes(fetched[0][1], policy=policy.default)
        if message.get("X-YT-Watcher"):  # our own outgoing mail — never a command
            continue
        msgid = message.get("Message-ID", "")
        if msgid and msgid in processed:
            continue
        sender = parseaddr(message.get("From", ""))[1].lower()
        if sender not in allow:
            continue

        nsubj = normalize_subject(message.get("Subject", ""))
        body = get_text_body(message)
        if nsubj.lower().startswith("yt"):                   # a fresh "yt ..." email
            command_text = f"{nsubj}\n{body}"
        elif nsubj.strip().lower() == base_subject or (thread_ids & header_refs(message)):
            command_text = extract_reply_text(body)          # a reply inside our thread
        else:
            continue
        if not command_text.strip():
            continue

        print(f"command from {sender}: {command_text.strip().splitlines()[0][:60]!r}")
        handle_command(command_text, dry=dry)

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
