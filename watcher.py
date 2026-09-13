#!/usr/bin/env python3
"""YouTube channel + X (Twitter) poster watcher, with a Gmail control plane.

Modes:
  --mode digest    Check watched channels + X accounts, summarize new uploads/posts,
                   email them as one threaded conversation with a "YouTube" section
                   and an "X Posts" section. Guarded to 8am Pacific unless --force.
  --mode commands  Read Gmail for `yt`/`x` commands (add / list / delete) and act.
  --mode feed      Diagnostic: print one channel's raw RSS feed. No email/state changes.
  --mode x-feed    Diagnostic: print one X account's raw fetched tweets. No side effects.

Secrets are read from env vars:
  ANTHROPIC_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ALLOWLIST_SENDERS, X_BEARER_TOKEN
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
X_USERS_FILE = ROOT / "x_users.json"
X_LAST_SEEN_FILE = STATE / "x_last_seen.json"
THREAD_FILE = STATE / "thread.json"
PROCESSED_FILE = STATE / "processed_emails.json"
PENDING_FILE = STATE / "pending.json"
LAST_CHECKED_FILE = STATE / "last_checked.txt"
LAST_DIGEST_FILE = STATE / "last_digest_date.txt"

UPDATES_SUBJECT = "\U0001F4FA YouTube watcher"
MAX_REFERENCES = 20      # cap the email References header (and thread.json) growth
MAX_PROCESSED = 500      # cap the processed-email dedupe ledger
PDF_RETENTION_DAYS = int(os.environ.get("PDF_RETENTION_DAYS", "30"))  # prune older summaries
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
X_API_BASE = "https://api.x.com/2"
X_TWEET_FIELDS = "created_at,conversation_id,in_reply_to_user_id,referenced_tweets,text,entities,author_id"
X_MEDIA_FIELDS = "url,preview_image_url,type,width,height"
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

## 📋 Executive Summary
A 20-40 word overview — the single big-picture point of the video.

## 📑 Contents
A bulleted list of every topic heading below, in order — titles WITH their emoji, e.g.
"- 📊 Rule 1: Track Everything". It must match the topic headings exactly.

Then go STRAIGHT into the topics — do NOT output a "Detailed Breakdown" heading. Give EACH
topic/segment of the video its OWN `##` heading, PREFIXED WITH ONE RELEVANT EMOJI, in the
order it appears, covering the ENTIRE video (one `##` per topic; never skip or merge topics).
Every `##` heading (Executive Summary, Contents, and each topic) begins with a fitting emoji.

Timestamps: the transcript is provided with [t=<seconds>s] markers showing when each part
is said. End EVERY topic `##` heading with a clickable timestamp link to where that topic
STARTS, built from the Watch URL plus the start second — e.g.
"## 📊 Rule 1: Track Everything [2:34](<watch-url>&t=154s)" (154 = the start second from the
nearest [t=...s] marker; display it as M:SS or H:MM:SS). If the source is description-only
(no [t=...s] markers), omit the timestamp links.

Under each topic heading, put the details as nested bullets. Be COMPREHENSIVE — detailed
enough that the reader gets the full value WITHOUT watching. For each topic capture:
- Each distinct point/claim AND the reasoning/evidence behind it (the "why" and "how").
- Concrete specifics: every number, statistic, price, percentage, date, and metric.
- Examples, stories, anecdotes, case studies, and analogies.
- Any framework or process — list EVERY step in order with its specifics.
- Tools, products, books, resources, links, companies, and people mentioned.
- Notable short verbatim quotes; caveats and counterpoints; takeaways as explicit steps.

Bullet nesting (IMPORTANT — get the indentation exactly right):
- Use nested bullets up to FOUR levels deep; every bullet starts with "- ".
- Indent each deeper level by EXACTLY 4 SPACES relative to its parent (NOT 2 spaces) —
  4-space indentation is REQUIRED or the nesting renders flat.
- NEVER cram a sequence into one bullet as "1. ... 2. ... 3. ...". Put each step/item on
  its OWN nested bullet line instead.

Tables: if a table communicates something better than bullets (comparisons, before/after,
plans, schedules, numeric data), use a GitHub-style Markdown table for that part instead.

Do NOT compress away specifics. For a full transcript aim for ~2,500-4,000 words total
(more if dense); never sacrifice completeness for brevity. For a description-only source,
be as complete as the source allows (no padding).

Example of a topic section (note the 4-space indents and the timestamp link):

## 📊 Rule 1: Track Everything [2:34](https://www.youtube.com/watch?v=ID&t=154s)
- Main point or claim
    - The reasoning / evidence / explanation
        - Specific number, example, quote, or sub-step
- A process — each step its OWN sub-bullet (never inline "1. 2. 3."):
    - Step 1 — ...
    - Step 2 — ...

| When useful, a table | like this |
| --- | --- |
| Calories | bodyweight × 10 |
| Protein | bodyweight × 1 g |

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


def load_x_users():
    return load_json(X_USERS_FILE, [])


def save_x_users(x_users):
    save_json(X_USERS_FILE, x_users)


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
        "  • Add an X account:    x add <x.com/twitter.com link or @handle>\n"
        "  • List X accounts:     x list\n"
        "  • Delete an X account: x delete   (then reply with the number or name)\n"
    )


# --------------------------------------------------------------------------- #
# YouTube: RSS, transcript, channel-id resolution
# --------------------------------------------------------------------------- #
def fetch_feed(channel_id):
    """Fetch a channel's RSS feed. Uses requests (with a timeout) rather than letting
    feedparser open the socket itself — feedparser.parse(url) has no timeout and can
    hang forever, which would stall the run and hold the shared concurrency lock.
    On any network error, returns an empty parse so the caller degrades gracefully."""
    try:
        resp = requests.get(RSS_URL.format(cid=channel_id), headers=UA, timeout=30)
        resp.raise_for_status()
        return feedparser.parse(resp.content)
    except Exception as exc:  # noqa: BLE001
        print(f"  feed fetch failed for {channel_id}: {exc}")
        return feedparser.parse(b"")


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


def debug_feed(only):
    """Print a watched channel's raw RSS feed (id, title, published) newest-first.
    Diagnostic only: no email, no state changes."""
    channels = load_channels()
    matches = [c for c in channels if c["channel_id"] == only or c["name"] == only]
    if not matches:
        print(f"{only!r} matched no watched channel.")
        return
    channel = matches[0]
    feed = fetch_feed(channel["channel_id"])
    print(f"Feed title: {feed_channel_name(feed)}")
    print(f"{len(feed.entries)} entries (feed order, normally newest-first):")
    for entry in feed.entries:
        print(f"  {entry_video_id(entry)}  {entry.get('published', '?')}  {entry.get('title', '(no title)')}")


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


def _supadata_segments(video_id):
    """Timestamped transcript via the Supadata API: list of (start_seconds, text)."""
    api_key = os.environ.get("SUPADATA_API_KEY")
    if not api_key:
        return None
    resp = requests.get(
        "https://api.supadata.ai/v1/youtube/transcript",
        params={"url": f"https://www.youtube.com/watch?v={video_id}"},  # segmented (no text=true)
        headers={"x-api-key": api_key},
        timeout=90,
    )
    resp.raise_for_status()
    content = resp.json().get("content")
    if not isinstance(content, list):
        return None
    segs = []
    for seg in content:
        text = (seg.get("text") or "").strip()
        if text:
            segs.append((float(seg.get("offset", 0)) / 1000.0, text))  # offset is milliseconds
    return segs or None


def _yta_segments(video_id):
    """Fallback timestamped transcript via youtube-transcript-api: (start_seconds, text)."""
    fetched = _build_transcript_api().fetch(video_id)
    segs = []
    for sn in fetched:
        text = (getattr(sn, "text", "") or "").strip()
        if text:
            segs.append((float(getattr(sn, "start", 0.0)), text))
    return segs or None


def _format_timestamped(segments, chunk_seconds=20):
    """Coalesce (start, text) segments into ~chunk_seconds lines prefixed with [t=<sec>s]."""
    lines, start, buf = [], None, []
    for seg_start, text in segments:
        if start is None:
            start = seg_start
        buf.append(text)
        if seg_start - start >= chunk_seconds:
            lines.append(f"[t={int(start)}s] " + " ".join(buf))
            start, buf = None, []
    if buf:
        lines.append(f"[t={int(start)}s] " + " ".join(buf))
    return "\n".join(lines)


def get_transcript(video_id):
    """Return a TIMESTAMPED transcript (lines prefixed with [t=<sec>s]) or None.

    Prefers Supadata (reliable, server-side — no IP bans); falls back to
    youtube-transcript-api. The caller uses the video description if both fail.
    """
    segments = None
    try:
        segments = _supadata_segments(video_id)
    except Exception as exc:  # noqa: BLE001
        print(f"  Supadata transcript failed for {video_id}: {exc}")
    if not segments:
        try:
            segments = _yta_segments(video_id)
        except Exception as exc:  # noqa: BLE001 - fall back to description
            print(f"  transcript unavailable for {video_id}: {exc}")
            return None
    return _format_timestamped(segments) if segments else None


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
# X (Twitter): API integration, thread grouping, channel-handle resolution
# --------------------------------------------------------------------------- #
def x_api_get(path, params):
    """GET one X API v2 endpoint with the app-only bearer token. Raises on HTTP error,
    including the response body — X's error bodies name the actual cause (invalid
    token, wrong access tier, suspended app, etc.), which the plain status code doesn't."""
    token = env("X_BEARER_TOKEN")
    resp = requests.get(
        f"{X_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} {resp.reason} for {resp.url}: {resp.text[:500]}")
    return resp.json()


def resolve_x_handle(url_or_handle):
    """Resolve an x.com/twitter.com URL or a bare @handle to (user_id, handle). Returns
    (None, None) if nothing could be resolved."""
    match = re.search(r"(?:x\.com|twitter\.com)/(\w{1,15})", url_or_handle, re.I)
    handle = match.group(1) if match else url_or_handle.strip().lstrip("@")
    handle = handle.strip()
    if not handle or handle.lower() in ("i", "home", "search"):  # common non-profile paths
        return None, None
    try:
        data = x_api_get(f"/users/by/username/{handle}", {})
    except Exception as exc:  # noqa: BLE001
        print(f"  could not resolve X handle {handle!r}: {exc}")
        return None, None
    user = data.get("data")
    if not user:
        return None, None
    return user["id"], user["username"]


def fetch_x_user_tweets(user_id, since_id=None, max_results=100):
    """Fetch a user's own recent tweets (media/entities included), newest activity within
    the window. Returns a list of tweet dicts merged with any attached media."""
    params = {
        "max_results": max_results,
        "exclude": "retweets",
        "tweet.fields": X_TWEET_FIELDS,
        "expansions": "attachments.media_keys",
        "media.fields": X_MEDIA_FIELDS,
    }
    if since_id:
        params["since_id"] = since_id
    try:
        data = x_api_get(f"/users/{user_id}/tweets", params)
    except Exception as exc:  # noqa: BLE001
        print(f"  X tweets fetch failed for {user_id}: {exc}")
        return []
    return _attach_media(data)


def _attach_media(data):
    """Merge expansions.media into each tweet as tweet['media'] (a list of media dicts)."""
    media_by_key = {m["media_key"]: m for m in data.get("includes", {}).get("media", [])}
    tweets = data.get("data") or []
    for tweet in tweets:
        keys = tweet.get("attachments", {}).get("media_keys", [])
        tweet["media"] = [media_by_key[k] for k in keys if k in media_by_key]
    return tweets


def filter_self_thread_tweets(tweets, user_id):
    """Keep a tweet if it's not a reply, or is a reply to the same account (a self-thread
    continuation). Drop replies to anyone else."""
    return [t for t in tweets if not t.get("in_reply_to_user_id") or t["in_reply_to_user_id"] == user_id]


def group_by_conversation(tweets):
    """Group tweets by conversation_id, each group sorted oldest->newest by tweet id."""
    groups = {}
    for tweet in tweets:
        groups.setdefault(tweet["conversation_id"], []).append(tweet)
    for group in groups.values():
        group.sort(key=lambda t: int(t["id"]))
    return groups


def backfill_thread(conversation_id, user_id, handle):
    """Fetch the full conversation (this author's tweets only) via recent search, for a
    thread that continues past this run's since_id window. Covers only the last 7 days
    (X's standard recent-search window); returns [] if nothing more is found."""
    params = {
        "query": f"conversation_id:{conversation_id} from:{handle}",
        "max_results": 100,
        "tweet.fields": X_TWEET_FIELDS,
        "expansions": "attachments.media_keys",
        "media.fields": X_MEDIA_FIELDS,
    }
    try:
        data = x_api_get("/tweets/search/recent", params)
    except Exception as exc:  # noqa: BLE001
        print(f"  backfill search failed for conversation {conversation_id}: {exc}")
        return []
    return _attach_media(data)


def complete_thread_groups(groups, user_id, handle):
    """For each conversation group whose oldest tweet is itself a reply (meaning earlier
    tweets fell outside this run's fetch window), backfill via recent search and merge.
    Falls back to the group as-fetched if backfill finds nothing more (e.g. the true root
    is older than the 7-day search window)."""
    completed = {}
    for conversation_id, group in groups.items():
        if group[0].get("in_reply_to_user_id"):
            backfilled = backfill_thread(conversation_id, user_id, handle)
            if backfilled:
                by_id = {t["id"]: t for t in group}
                for tweet in backfilled:
                    by_id[tweet["id"]] = tweet
                group = sorted(by_id.values(), key=lambda t: int(t["id"]))
        completed[conversation_id] = group
    return completed


def expand_tweet_text(tweet):
    """Tweet text with t.co links swapped for their expanded_url."""
    text = tweet.get("text", "")
    for url in tweet.get("entities", {}).get("urls", []):
        short, expanded = url.get("url"), url.get("expanded_url")
        if short and expanded:
            text = text.replace(short, expanded)
    return text


def debug_x_feed(only):
    """Print one X account's raw fetched tweets (diagnostic, no state changes). Looks up
    `only` in x_users.json first; if it isn't watched yet, resolves it live via the X API
    so this works standalone (e.g. to sanity-check a handle/token before ever adding it)."""
    x_users = load_x_users()
    matches = [u for u in x_users if u["user_id"] == only or u["handle"] == only]
    if matches:
        user_id, handle = matches[0]["user_id"], matches[0]["handle"]
    else:
        print(f"{only!r} isn't watched yet — resolving it live via the X API...")
        user_id, handle = resolve_x_handle(only)
        if not user_id:
            print(f"Could not resolve {only!r} to an X account.")
            return
    tweets = fetch_x_user_tweets(user_id)
    print(f"@{handle}: {len(tweets)} tweets fetched (no since_id — full recent window):")
    for tweet in tweets:
        print(f"  {tweet['id']}  {tweet.get('created_at', '?')}  "
              f"in_reply_to={tweet.get('in_reply_to_user_id')}  "
              f"conversation_id={tweet.get('conversation_id')}  {tweet.get('text', '')[:60]!r}")


# --------------------------------------------------------------------------- #
# Claude summarization
# --------------------------------------------------------------------------- #
def summarize(title, channel, url, published, source, content):
    client = anthropic.Anthropic(max_retries=4)  # reads ANTHROPIC_API_KEY; retry transient errors
    label = "TRANSCRIPT" if source.startswith("full") else "DESCRIPTION"
    user = (
        f"Title: {title}\n"
        f"Channel: {channel}\n"
        f"URL: {url}\n"
        f"Published: {published}\n"
        f"Summary source: {source}\n\n"
        f"--- {label} ---\n{content}"
    )
    with client.messages.stream(
        model=MODEL,
        max_tokens=32000,
        thinking={"type": "disabled"},  # summarization is structured extraction; adaptive
        system=SYSTEM_PROMPT,            # thinking could eat the whole budget on long transcripts
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to summarize this video.")
    text = next((block.text for block in resp.content if block.type == "text"), None)
    if not text:
        raise RuntimeError(f"empty summary response (stop_reason={resp.stop_reason})")
    return json.loads(text)


X_POST_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"tldr": {"type": "string"}},
    "required": ["tldr"],
    "additionalProperties": False,
}

X_POST_SYSTEM_PROMPT = """You summarize a day's new X (Twitter) posts from one account into
a single 1-2 sentence TL;DR. Base it STRICTLY on the text provided — never invent details.
Cover the overall theme(s) across everything posted, not just the first post. Return JSON
with one field, tldr (plain text, no markdown, roughly 20-40 words)."""


def summarize_x_posts(handle, raw_text_blocks):
    """One short, text-only Claude call: raw tweet text for everything an account posted
    since last check -> a 1-2 sentence TL;DR, reused as both the email line and the PDF's
    exec-summary callout. Deliberately NOT the video SUMMARY_SCHEMA/SYSTEM_PROMPT, which
    are shaped for a markdown exec-summary+outline report."""
    client = anthropic.Anthropic(max_retries=4)
    user = f"@{handle}'s new posts today:\n\n" + "\n\n---\n\n".join(raw_text_blocks)
    with client.messages.stream(
        model=MODEL,
        max_tokens=1024,
        thinking={"type": "disabled"},
        system=X_POST_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": X_POST_SUMMARY_SCHEMA}},
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to summarize these posts.")
    text = next((block.text for block in resp.content if block.type == "text"), None)
    if not text:
        raise RuntimeError(f"empty summary response (stop_reason={resp.stop_reason})")
    return json.loads(text)["tldr"]


# --------------------------------------------------------------------------- #
# Email out (SMTP)
# --------------------------------------------------------------------------- #
def resend_send(subject, text_body, headers, reply_to, attachments=None):
    """Send one email via the Resend HTTP API (https://resend.com). `attachments` is a
    list of file paths, all attached to the single message."""
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
    if attachments:
        payload["attachments"] = [
            {"filename": a.name, "content": base64.b64encode(a.read_bytes()).decode()}
            for a in attachments
        ]
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def send_threaded(body, attachments=None, subject=None, dry=False):
    """Send an email via Resend into the YouTube-watcher conversation.

    From a distinct address (so it lands in the inbox as normal mail, not 'from me'),
    Reply-To set to your address so replies route to the Gmail inbox the listener reads,
    tagged X-YT-Watcher to avoid re-ingestion. `subject` overrides the base subject (used
    for the dated daily digest); `attachments` is a list of file paths.
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
        print("---- DRY-RUN EMAIL (Resend) ----")
        print("Subject:", subject or base_subject)
        print(body)
        return

    resend_send(subject or base_subject, body + footer(), headers,
                reply_to=recipient, attachments=attachments)
    thread.setdefault("base_subject", base_subject)
    thread.setdefault("root_message_id", new_id)
    thread["last_message_id"] = new_id
    # Keep the thread root + most recent IDs only. Dated subjects already split each day
    # into its own conversation, so older IDs add nothing but unbounded header growth.
    new_refs = refs + [new_id]
    if len(new_refs) > MAX_REFERENCES:
        new_refs = new_refs[:1] + new_refs[-(MAX_REFERENCES - 1):]
    thread["references"] = new_refs
    save_json(THREAD_FILE, thread)


# --------------------------------------------------------------------------- #
# Summary file
# --------------------------------------------------------------------------- #
_PDF_CSS = """
body { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 14px; line-height: 1.55; color: #2b2b33; }
.hdr { background-color: #111827; padding: 16px 18px; margin-bottom: 16px; }
.title { color: #ffffff; font-size: 25px; font-weight: bold; }
.meta { color: #9ca3af; font-size: 12px; margin-top: 7px; }
.exec { background-color: #eef4ff; border-left: 5px solid #2563eb; padding: 11px 14px; margin: 4px 0 16px 0; font-size: 15.5px; color: #15233b; }
h2 { color: #1d4ed8; font-size: 18px; margin: 20px 0 8px 0; border-bottom: 2px solid #bfdbfe; padding-bottom: 4px; }
h3 { color: #334155; font-size: 16px; margin: 16px 0 5px 0; }
p { margin: 5px 0; }
ul { margin: 4px 0 9px 0; padding-left: 18px; }
li { margin: 3px 0; }
strong { color: #b45309; font-weight: bold; }
em { color: #6d28d9; font-style: italic; }
blockquote { background-color: #f0f7ff; border-left: 4px solid #2563eb; margin: 10px 0; padding: 8px 13px; color: #1e3a5f; }
table { border-collapse: collapse; margin: 9px 0; font-size: 13.5px; }
th, td { border: 1px solid #cbd5e1; padding: 5px 9px; text-align: left; vertical-align: top; }
th { background-color: #eef4ff; color: #15233b; }
a { color: #2563eb; }
"""


def prune_old_pdfs(retention_days=PDF_RETENTION_DAYS):
    """Delete committed PDFs whose published date is older than retention_days so the
    repo (and every CI checkout) stays small. last_seen is untouched, so pruned videos
    are never re-summarized — only their stored PDF is dropped. Filenames embed the
    published date as `...-YYYY-MM-DD-...`; git doesn't preserve mtimes, so we read the
    date from the name rather than the filesystem."""
    if retention_days <= 0 or not SUMMARIES.exists():
        return
    cutoff = datetime.datetime.now(PACIFIC).date() - datetime.timedelta(days=retention_days)
    removed = 0
    for pdf in SUMMARIES.glob("*.pdf"):
        match = re.search(r"(\d{4}-\d{2}-\d{2})", pdf.name)
        if not match:
            continue  # e.g. an "unknown"-dated file — keep it
        try:
            pub_date = datetime.date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if pub_date < cutoff:
            pdf.unlink()
            removed += 1
    if removed:
        print(f"pruned {removed} PDF(s) older than {retention_days} days")


def write_summary_pdf(channel_name, video_id, title, markdown_text, published_date):
    """Render the markdown summary into a styled PDF via headless Chromium.

    Chromium renders color emoji and full CSS. All PDFs go into ONE flat folder
    (summaries/, no per-channel subfolders), with the channel as a filename prefix.
    """
    from html import escape
    import markdown as md_lib
    from playwright.sync_api import sync_playwright

    SUMMARIES.mkdir(parents=True, exist_ok=True)
    path = SUMMARIES / f"{slugify(channel_name)}-{published_date}-{video_id}-{slugify(title)}.pdf"

    # Pull the title + meta lines (everything before the first "## ") into a styled header;
    # render the rest of the markdown as the body.
    lines = markdown_text.splitlines()
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
        "<!doctype html><html><head><meta charset='utf-8'><style>" + _PDF_CSS + "</style></head>"
        "<body>" + header + body_html + "</body></html>"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(
            path=str(path),
            format="Letter",
            print_background=True,
            margin={"top": "0.6in", "bottom": "0.6in", "left": "0.6in", "right": "0.6in"},
        )
        browser.close()
    return path


_TWEET_PDF_CSS = """
body { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 14px; line-height: 1.5; color: #2b2b33; }
.thread-hdr { background-color: #111827; padding: 16px 18px; margin-bottom: 16px; }
.thread-hdr .name { color: #ffffff; font-size: 20px; font-weight: bold; }
.thread-hdr .handle { color: #9ca3af; font-size: 14px; }
.thread-hdr .meta { color: #9ca3af; font-size: 12px; margin-top: 7px; }
.thread-hdr a { color: #93c5fd; }
.exec { background-color: #eef4ff; border-left: 5px solid #2563eb; padding: 11px 14px; margin: 4px 0 16px 0; font-size: 15.5px; color: #15233b; }
.exec strong { display: block; margin-bottom: 4px; }
.tweet-card { border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px 15px; margin: 0 0 12px 0; }
.tweet-meta { color: #94a3b8; font-size: 12px; margin-bottom: 6px; }
.tweet-text { white-space: pre-wrap; font-size: 15px; }
.tweet-text a { color: #2563eb; }
.tweet-img { max-width: 100%; margin-top: 10px; border-radius: 6px; }
.thread-ftr { color: #94a3b8; font-size: 12px; margin-top: 8px; }
"""


def write_tweet_pdf(handle, conversation_id, tweets, published_date, exec_summary):
    """Render one X thread/post into a PDF: a header, an exec-summary callout (the same
    text as the account's email TL;DR), then each tweet as its own verbatim card
    (oldest-first) with images embedded as base64 data URIs.
    """
    import base64
    from html import escape
    from playwright.sync_api import sync_playwright

    SUMMARIES.mkdir(parents=True, exist_ok=True)
    first_text = expand_tweet_text(tweets[0]) if tweets else ""
    path = SUMMARIES / (
        f"{slugify('x-' + handle)}-{published_date}-{conversation_id}-{slugify(first_text[:40])}.pdf"
    )

    thread_url = f"https://x.com/{handle}/status/{tweets[0]['id']}" if tweets else f"https://x.com/{handle}"
    label = "Thread" if len(tweets) > 1 else "Post"
    header = (
        f'<div class="thread-hdr"><div class="name">@{escape(handle)}</div>'
        f'<div class="meta">{label} · {escape(published_date)} · '
        f'<a href="{escape(thread_url)}">View on X</a></div></div>'
    )
    exec_html = f'<div class="exec"><strong>\U0001F4CB Executive Summary</strong>{escape(exec_summary)}</div>'

    cards = []
    for tweet in tweets:
        created = tweet.get("created_at", "")
        try:
            when = datetime.datetime.fromisoformat(created.replace("Z", "+00:00")).strftime("%b %d, %Y · %-I:%M %p")
        except ValueError:
            when = created
        text_html = escape(expand_tweet_text(tweet)).replace("\n", "<br>")
        images_html = ""
        for media in tweet.get("media", []):
            media_url = media.get("url") or media.get("preview_image_url")
            if not media_url:
                continue
            try:
                img_bytes = requests.get(media_url, headers=UA, timeout=20).content
                b64 = base64.b64encode(img_bytes).decode()
                content_type = "image/png" if media_url.lower().endswith("png") else "image/jpeg"
                images_html += f'<img class="tweet-img" src="data:{content_type};base64,{b64}" alt="attached image">'
            except Exception as exc:  # noqa: BLE001
                print(f"  could not embed image for tweet {tweet['id']}: {exc}")
        cards.append(
            f'<div class="tweet-card"><div class="tweet-meta">{escape(when)}</div>'
            f'<div class="tweet-text">{text_html}</div>{images_html}</div>'
        )

    footer_html = f'<div class="thread-ftr">{len(tweets)} post{"s" if len(tweets) != 1 else ""} · fetched {datetime.date.today().isoformat()}</div>'
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><style>" + _TWEET_PDF_CSS + "</style></head>"
        "<body><div class='thread-doc'>" + header + exec_html + "".join(cards) + footer_html + "</div></body></html>"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(
            path=str(path),
            format="Letter",
            print_background=True,
            margin={"top": "0.6in", "bottom": "0.6in", "left": "0.6in", "right": "0.6in"},
        )
        browser.close()
    return path


# --------------------------------------------------------------------------- #
# Digest mode
# --------------------------------------------------------------------------- #
def summarize_video(channel_name, entry):
    """Summarize one video and render its PDF; return a dict. Does NOT send email."""
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
    return {
        "channel": channel_name, "title": title, "url": url,
        "published": published_human, "tldr": result["tldr"], "pdf": path,
    }


def send_digest_email(video_items, x_account_items, dry=False):
    """Send ONE email covering all new videos and X posts for the day, as two headed
    sections — each present only if it has content. video_items: {channel, title, url,
    published, tldr, pdf}. x_account_items: {handle, tldr, threads: [{title, url, pdf}]}."""
    date_str = datetime.datetime.now(PACIFIC).strftime("%B %-d, %Y")
    video_count = len(video_items)
    post_count = sum(len(acct["threads"]) for acct in x_account_items)
    parts = []
    if video_count:
        parts.append(f"{video_count} video{'s' if video_count != 1 else ''}")
    if post_count:
        parts.append(f"{post_count} X post{'s' if post_count != 1 else ''}")
    head = f"{' and '.join(parts)} — {date_str}\n\n"

    sections = []
    attachments = []

    if video_items:
        blocks = []
        for it in video_items:
            blocks.append(
                f'\U0001F3AC {it["channel"]} — "{it["title"]}"\n'
                f'▶  {it["url"]}\n'
                f'TL;DR  {it["tldr"]}\n'
                f'\U0001F4C4 Full summary attached: {it["pdf"].name}'
            )
            attachments.append(it["pdf"])
        sections.append("\U0001F4FA YouTube\n──────────\n" + "\n\n".join(blocks))

    if x_account_items:
        blocks = []
        for acct in x_account_items:
            lines = [f'@{acct["handle"]}', f'TL;DR  {acct["tldr"]}']
            for thread in acct["threads"]:
                lines.append(f'  • {thread["title"]} — {thread["url"]}')
                lines.append(f'    \U0001F4C4 {thread["pdf"].name}')
                attachments.append(thread["pdf"])
            blocks.append("\n".join(lines))
        sections.append("\U0001F426 X Posts\n──────────\n" + "\n\n".join(blocks))

    body = head + "\n\n".join(sections) + "\n"
    subject = f"{UPDATES_SUBJECT} — {date_str}"
    send_threaded(body, attachments=attachments, subject=subject, dry=dry)


def send_no_new_content_email(channel_count, x_user_count, dry=False):
    """Send a short 'nothing new today' note so the watcher checks in every day, even on
    quiet days. Same dated subject as the digest, so it lands in today's conversation."""
    date_str = datetime.datetime.now(PACIFIC).strftime("%B %-d, %Y")
    if channel_count or x_user_count:
        parts = []
        if channel_count:
            parts.append(f"{channel_count} channel{'s' if channel_count != 1 else ''}")
        if x_user_count:
            parts.append(f"{x_user_count} X account{'s' if x_user_count != 1 else ''}")
        line = (
            f"All quiet today — none of the {' and '.join(parts)} you're watching "
            "have posted since the last check."
        )
    else:
        line = "You're not watching any channels or X accounts yet — nothing to check."
    body = (
        f"\U0001F4ED No new content — {date_str}\n\n"
        f"{line}\n\n"
        "I'll keep watching and send a full summary the moment something drops.\n"
    )
    subject = f"{UPDATES_SUBJECT} — {date_str}"
    send_threaded(body, subject=subject, dry=dry)


def process_new_video(channel_name, entry, dry=False):
    """Summarize + email a single video (a one-item dated digest)."""
    item = summarize_video(channel_name, entry)
    send_digest_email([item], [], dry=dry)
    print(f"  wrote {item['pdf']} and emailed it")


def extract_video_id(ref):
    match = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", ref)
    if match:
        return match.group(1)
    return ref if re.fullmatch(r"[\w-]{11}", ref) else None


def run_single_video(only, video_ref, dry=False):
    """Summarize + email one specific video from a watched channel's feed. A one-off
    resend/backfill: does NOT touch last_seen, so it never affects normal digest
    tracking of what's already been sent for that channel."""
    channels = load_channels()
    matches = [c for c in channels if c["channel_id"] == only or c["name"] == only]
    if not matches:
        print(f"--only {only!r} matched no watched channel — exiting.")
        return
    channel = matches[0]
    video_id = extract_video_id(video_ref)
    if not video_id:
        print(f"Could not parse a video id from {video_ref!r}")
        return
    feed = fetch_feed(channel["channel_id"])
    entry = next((e for e in feed.entries if entry_video_id(e) == video_id), None)
    if not entry:
        print(f"Video {video_id} not found in {channel['name']}'s current feed "
              "(only the last ~15 uploads are listed).")
        return
    process_new_video(channel["name"], entry, dry=dry)


def _thread_item(handle, conversation_id, group, tldr):
    """Build one x_account_items[...]["threads"] entry: render the PDF and describe it."""
    published_dt = None
    try:
        published_dt = datetime.datetime.fromisoformat(group[0]["created_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        pass
    published_date = published_dt.strftime("%Y-%m-%d") if published_dt else "unknown"
    pdf = write_tweet_pdf(handle, conversation_id, group, published_date, tldr)
    title = f"Thread ({len(group)} posts)" if len(group) > 1 else expand_tweet_text(group[0])[:60]
    url = f"https://x.com/{handle}/status/{group[0]['id']}"
    return {"title": title, "url": url, "pdf": pdf}


def process_x_account(user, since_id):
    """Fetch, group, backfill, summarize, and render PDFs for one account's new posts
    since `since_id`. `since_id=None` means first encounter: only the single newest
    thread is processed (mirrors YouTube's back-catalog-skipping first-run rule) rather
    than emailing the whole visible history. Returns (x_account_item or None if nothing
    new, newest tweet id seen or None)."""
    user_id, handle = user["user_id"], user["handle"]
    tweets = filter_self_thread_tweets(fetch_x_user_tweets(user_id, since_id=since_id), user_id)
    if not tweets:
        return None, None
    newest_id = str(max(int(t["id"]) for t in tweets))
    groups = group_by_conversation(tweets)
    if since_id is None:
        newest_conv = max(groups, key=lambda cid: max(int(t["id"]) for t in groups[cid]))
        groups = {newest_conv: groups[newest_conv]}
        tweets = groups[newest_conv]
    groups = complete_thread_groups(groups, user_id, handle)

    tldr = summarize_x_posts(handle, [expand_tweet_text(t) for t in tweets])
    threads = [_thread_item(handle, conversation_id, group, tldr) for conversation_id, group in groups.items()]
    return {"handle": handle, "tldr": tldr, "threads": threads}, newest_id


def process_new_x_thread(handle, conversation_id, tweets, dry=False):
    """Summarize + email one specific X thread/post (a one-item dated digest)."""
    tldr = summarize_x_posts(handle, [expand_tweet_text(t) for t in tweets])
    account_item = {"handle": handle, "tldr": tldr, "threads": [_thread_item(handle, conversation_id, tweets, tldr)]}
    send_digest_email([], [account_item], dry=dry)
    print(f"  wrote {account_item['threads'][0]['pdf']} and emailed it")


def extract_tweet_id(ref):
    match = re.search(r"status/(\d+)", ref)
    if match:
        return match.group(1)
    return ref if re.fullmatch(r"\d+", ref) else None


def run_single_tweet(only, tweet_ref, dry=False):
    """Summarize + email one specific tweet/thread from a watched X account's feed. A
    one-off resend/backfill: does NOT touch x_last_seen."""
    x_users = load_x_users()
    matches = [u for u in x_users if u["user_id"] == only or u["handle"] == only]
    if not matches:
        print(f"--only {only!r} matched no watched X account — exiting.")
        return
    user = matches[0]
    tweet_id = extract_tweet_id(tweet_ref)
    if not tweet_id:
        print(f"Could not parse a tweet id from {tweet_ref!r}")
        return
    tweets = filter_self_thread_tweets(fetch_x_user_tweets(user["user_id"]), user["user_id"])
    entry = next((t for t in tweets if t["id"] == tweet_id), None)
    if not entry:
        print(f"Tweet {tweet_id} not found in @{user['handle']}'s current feed "
              "(only recent tweets are listed).")
        return
    groups = complete_thread_groups(group_by_conversation(tweets), user["user_id"], user["handle"])
    group = groups[entry["conversation_id"]]
    process_new_x_thread(user["handle"], entry["conversation_id"], group, dry=dry)


def run_digest(force=False, dry=False, only=None):
    now_pt = datetime.datetime.now(PACIFIC)
    today = now_pt.date().isoformat()
    if not force:
        # Run once per day on the first cron fire AT/AFTER 8am Pacific. GitHub delays
        # scheduled runs (often 1-2h), so an exact "hour == 8" check would skip every
        # delayed run; instead gate on >= 8am plus a "ran today" marker.
        if now_pt.hour < 8:
            print(f"Before 8am Pacific (currently {now_pt:%H:%M} PT) — exiting.")
            return
        if LAST_DIGEST_FILE.exists() and LAST_DIGEST_FILE.read_text().strip() == today:
            print(f"Digest already ran today ({today}) — exiting.")
            return

    channels = load_channels()
    x_users = load_x_users()
    if only:
        channels = [c for c in channels if c["channel_id"] == only or c["name"] == only]
        x_users = [u for u in x_users if u["user_id"] == only or u["handle"] == only]
        if not channels and not x_users:
            print(f"--only {only!r} matched no watched channel or X account — exiting.")
            return

    last_seen = load_json(LAST_SEEN_FILE, {})
    items = []        # all new videos across channels, summarized
    advance = {}      # channel_id -> newest video id to mark seen (only fully summarized)
    feeds_read = 0    # channels whose RSS we actually managed to read this run

    for channel in channels:
        cid = channel["channel_id"]
        name = channel.get("name", cid)
        print(f"Checking {name} ({cid})")
        feed = fetch_feed(cid)
        entries = list(feed.entries)
        if not entries:
            print("  no entries in feed")
            continue
        feeds_read += 1

        seen = last_seen.get(cid)
        if seen is None:
            new_entries = entries[:1]  # first time: newest only, skip the back-catalog
        else:
            new_entries = []
            for entry in entries:  # feed is newest-first
                if entry_video_id(entry) == seen:
                    break
                new_entries.append(entry)

        # Oldest -> newest; collect summaries. A channel's last-seen advances only to
        # its last successfully-summarized video (after the email sends), so a transient
        # failure retries next run instead of skipping.
        for entry in reversed(new_entries):
            try:
                items.append(summarize_video(name, entry))
                advance[cid] = entry_video_id(entry)
            except Exception as exc:  # noqa: BLE001
                print(f"  failed on {entry_video_id(entry)}: {exc} — will retry next run")
                break
        time.sleep(1)  # be gentle to YouTube from a single runner IP

    x_last_seen = load_json(X_LAST_SEEN_FILE, {})
    x_account_items = []   # one entry per account with new posts (see process_x_account)
    x_advance = {}         # user_id -> newest tweet id to mark seen (only fully summarized)
    x_read = 0             # accounts whose timeline we actually managed to read this run

    for user in x_users:
        handle = user["handle"]
        print(f"Checking @{handle} ({user['user_id']})")
        try:
            item, newest_id = process_x_account(user, x_last_seen.get(user["user_id"]))
            x_read += 1
            if item:
                x_account_items.append(item)
            if newest_id:
                x_advance[user["user_id"]] = newest_id
        except Exception as exc:  # noqa: BLE001
            print(f"  failed on @{handle}: {exc} — will retry next run")
        time.sleep(1)

    # Did we actually assess the world? True per platform if we read >=1 feed/account, or
    # there was nothing to watch on it. False means every fetch on that platform failed
    # (network/API down) — don't claim "all quiet" and don't mark today done, so a later
    # fire retries instead of skipping a day.
    checked = (feeds_read > 0 or not channels) and (x_read > 0 or not x_users)

    if items or x_account_items:
        send_digest_email(items, x_account_items, dry=dry)  # ONE email for the whole day's batch
        total = len(items) + sum(len(a["threads"]) for a in x_account_items)
        print(f"sent {total} item(s) in one digest email")
    elif checked:
        print("No new content — sending the daily 'all quiet' note.")
        send_no_new_content_email(len(channels), len(x_users), dry=dry)
    else:
        print("Could not read all watched feeds/accounts — not sending a note; "
              "leaving today unmarked so the next fire retries.")

    if not dry and checked:
        for cid, vid in advance.items():
            last_seen[cid] = vid
        save_json(LAST_SEEN_FILE, last_seen)
        for uid, tid in x_advance.items():
            x_last_seen[uid] = tid
        save_json(X_LAST_SEEN_FILE, x_last_seen)
        LAST_DIGEST_FILE.write_text(today + "\n")  # mark today done so later fires skip
        LAST_CHECKED_FILE.write_text(now_pt.isoformat() + "\n")
        prune_old_pdfs()  # keep the committed summaries/ folder bounded


# --------------------------------------------------------------------------- #
# Command mode (Gmail inbox)
# --------------------------------------------------------------------------- #
YT_URL_RE = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/\S+", re.I)
X_URL_RE = re.compile(r"https?://(?:www\.)?(?:x\.com|twitter\.com)/\S+", re.I)


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


def resolve_target(text, items, pending, id_key="channel_id", name_key="name"):
    """Map a user reply (a number or a name) to an item's id, generic over any
    list of {id_key, name_key}-shaped records (YouTube channels or X users)."""
    stripped = re.sub(r"\b(yt|x|delete|remove|stop)\b", " ", text, flags=re.I).strip()
    num = re.search(r"\b(\d{1,3})\b", stripped)
    if num:
        idx = num.group(1)
        if pending.get("action") == "delete" and idx in pending.get("map", {}):
            return pending["map"][idx]
        n = int(idx)
        if 1 <= n <= len(items):
            return items[n - 1][id_key]
    name = stripped.strip().strip('"').strip()
    if name:
        for item in items:
            if item[name_key].lower() == name.lower():
                return item[id_key]
        for item in items:
            if name.lower() in item[name_key].lower():
                return item[id_key]
    return None


def cmd_add(url, channels, dry=False):
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
    if not dry:
        save_channels(channels)

    last_seen = load_json(LAST_SEEN_FILE, {})
    if latest_id:
        last_seen[cid] = latest_id  # seed: only future uploads get summarized
        if not dry:
            save_json(LAST_SEEN_FILE, last_seen)

    return (
        f"Added {name}. You'll get a summary for every new upload from now on.\n"
        f"Channel: https://www.youtube.com/channel/{cid}\n"
        f'Latest existing video (not summarized): "{latest_title}"\n'
        f'Now watching {len(channels)} channels. Reply "yt list" to see them.'
    )


def cmd_x_add(handle_or_url, x_users, dry=False):
    user_id, handle = resolve_x_handle(handle_or_url)
    if not user_id:
        return (f"Couldn't find an X account from that link/handle:\n{handle_or_url}\n"
                "Try the profile URL (https://x.com/handle) or just @handle.")
    existing = next((u for u in x_users if u["user_id"] == user_id), None)
    if existing:
        return f"Already watching @{existing['handle']}."

    latest = filter_self_thread_tweets(fetch_x_user_tweets(user_id, max_results=5), user_id)
    latest_id = str(max(int(t["id"]) for t in latest)) if latest else None
    latest_text = expand_tweet_text(latest[-1])[:80] if latest else "(none)"

    x_users.append(
        {"user_id": user_id, "handle": handle, "added_at": datetime.date.today().isoformat()}
    )
    if not dry:
        save_x_users(x_users)

    x_last_seen = load_json(X_LAST_SEEN_FILE, {})
    if latest_id:
        x_last_seen[user_id] = latest_id  # seed: only future posts get summarized
        if not dry:
            save_json(X_LAST_SEEN_FILE, x_last_seen)

    return (
        f"Added @{handle}. You'll get a summary for every new post from now on.\n"
        f"Profile: https://x.com/{handle}\n"
        f'Latest existing post (not summarized): "{latest_text}"\n'
        f'Now watching {len(x_users)} X account{"s" if len(x_users) != 1 else ""}. Reply "x list" to see them.'
    )


def cmd_list(items, id_key="channel_id", name_key="name", noun="channel", cmd_prefix="yt"):
    if not items:
        return f"You're not watching any {noun}s yet."
    lines = [f"You're watching {len(items)} {noun}{'s' if len(items) != 1 else ''}:"]
    lines += [f"{i}. {it[name_key]}" for i, it in enumerate(items, 1)]
    lines.append(f'\nReply "{cmd_prefix} delete <number or name>" to remove one.')
    return "\n".join(lines)


def cmd_delete_prompt(items, id_key="channel_id", name_key="name", noun="channel",
                       platform="yt", dry=False):
    if not items:
        return f"You're not watching any {noun}s."
    mapping = {str(i): it[id_key] for i, it in enumerate(items, 1)}
    if not dry:
        save_json(
            PENDING_FILE,
            {"action": "delete", "platform": platform, "map": mapping,
             "created": datetime.datetime.now(PACIFIC).isoformat()},
        )
    lines = ["Which one should I delete? Reply with the number or the name:"]
    lines += [f"{i}. {it[name_key]}" for i, it in enumerate(items, 1)]
    return "\n".join(lines)


def do_delete(item_id, items, id_key, name_key, noun, save_fn, state_file, dry=False):
    name = next((it[name_key] for it in items if it[id_key] == item_id), item_id)
    remaining = [it for it in items if it[id_key] != item_id]
    if not dry:
        save_fn(remaining)
        last_seen = load_json(state_file, {})
        last_seen.pop(item_id, None)
        save_json(state_file, last_seen)
        clear_pending()
    return f"\U0001F5D1 Removed {name} — now watching {len(remaining)} {noun}{'s' if len(remaining) != 1 else ''}."


def handle_command(command_text, dry=False):
    text = command_text.strip()
    low = text.lower()
    channels = load_channels()
    x_users = load_x_users()
    pending = load_pending()
    pending_delete = pending.get("action") == "delete"
    pending_platform = pending.get("platform", "yt")

    # X-specific detection must run BEFORE the generic (YouTube) keyword checks below,
    # since "delete"/"list" also appear inside "x delete"/"x list" text — an explicit
    # "x ..." prefix always means X; a bare "list"/"delete" keeps meaning YouTube.
    wants_add_x = X_URL_RE.search(text)
    x_add_arg = wants_add_x.group(0).strip() if wants_add_x else None
    if not x_add_arg:
        bare_handle = re.search(r"\bx\s+add\s+(@?[A-Za-z0-9_]{1,15})\b", text, re.I)
        if bare_handle:
            x_add_arg = bare_handle.group(1)
    wants_delete_x = re.search(r"\bx\s+(delete|remove|stop)\b", low)
    wants_list_x = re.search(r"\bx\s+(list|accounts?)\b", low)

    wants_add = YT_URL_RE.search(text)
    wants_delete = re.search(r"\b(delete|remove|stop)\b", low)
    # 'show' is intentionally NOT a list synonym: it collides with channel names
    # (e.g. "The Saamir Show"), which would misroute a delete-by-name to `list`.
    wants_list = re.search(r"\b(list|channels)\b", low)

    if x_add_arg:
        reply = cmd_x_add(x_add_arg, x_users, dry=dry)
    elif wants_add:
        reply = cmd_add(wants_add.group(0).strip(), channels, dry=dry)
    elif pending_delete and not (wants_list or wants_list_x):
        # We previously asked which channel/account to delete; this reply is the target
        # (a number or a name). Resolve it BEFORE the generic `list` fallback so a
        # name isn't mistaken for a command. (`list`/`x list` still let you peek.)
        # pending.platform (set when the prompt was issued) picks which list applies.
        if pending_platform == "x":
            items, id_key, name_key, noun = x_users, "user_id", "handle", "X account"
            save_fn, state_file, list_cmd = save_x_users, X_LAST_SEEN_FILE, "x list"
        else:
            items, id_key, name_key, noun = channels, "channel_id", "name", "channel"
            save_fn, state_file, list_cmd = save_channels, LAST_SEEN_FILE, "list"
        target_id = resolve_target(text, items, pending, id_key=id_key, name_key=name_key)
        if target_id:
            reply = do_delete(target_id, items, id_key, name_key, noun, save_fn, state_file, dry=dry)
        else:
            reply = f"Reply with the number or name to delete (or '{list_cmd}' to see them)."
    elif wants_delete_x:
        uid = resolve_target(text, x_users, pending, id_key="user_id", name_key="handle")
        reply = (
            do_delete(uid, x_users, "user_id", "handle", "X account", save_x_users, X_LAST_SEEN_FILE, dry=dry)
            if uid else
            cmd_delete_prompt(x_users, "user_id", "handle", "X account", platform="x", dry=dry)
        )
    elif wants_list_x:
        reply = cmd_list(x_users, "user_id", "handle", "X account", cmd_prefix="x")
    elif wants_delete:
        cid = resolve_target(text, channels, pending)
        reply = (
            do_delete(cid, channels, "channel_id", "name", "channel", save_channels, LAST_SEEN_FILE, dry=dry)
            if cid else
            cmd_delete_prompt(channels, "channel_id", "name", "channel", platform="yt", dry=dry)
        )
    elif wants_list:
        reply = cmd_list(channels, "channel_id", "name", "channel", cmd_prefix="yt")
    else:
        reply = (
            "I didn't catch that. Reply with:\n"
            "  add <link>   |   list   |   delete\n"
            "  x add <link or @handle>   |   x list   |   x delete"
        )

    send_threaded(reply, dry=dry)


def run_commands(dry=False):
    addr = env("GMAIL_ADDRESS")
    password = env("GMAIL_APP_PASSWORD")
    allow = {a.strip().lower() for a in env("ALLOWLIST_SENDERS").split(",") if a.strip()}
    processed_log = load_json(PROCESSED_FILE, [])   # ordered: oldest first, newest last
    processed = set(processed_log)

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
        if re.match(r"^(yt|x)\b", nsubj, re.I):               # a fresh "yt ..."/"x ..." email
            command_text = f"{nsubj}\n{body}"
        elif nsubj.strip().lower().startswith(base_subject) or (thread_ids & header_refs(message)):
            command_text = extract_reply_text(body)          # a reply inside our thread
        else:
            continue
        if not command_text.strip():
            continue

        print(f"command from {sender}: {command_text.strip().splitlines()[0][:60]!r}")
        try:
            handle_command(command_text, dry=dry)
        except Exception as exc:  # noqa: BLE001 - one bad command must not abort the batch
            print(f"  command failed: {exc} — leaving unmarked to retry next run")
            continue  # don't mark Seen/processed, so it retries next run

        if not dry:
            mailbox.store(num, "+FLAGS", "\\Seen")
        if msgid and msgid not in processed:
            processed.add(msgid)
            processed_log.append(msgid)

    mailbox.logout()
    if not dry:
        # Bound the dedupe ledger; intake only searches the last 3 days, so pruned
        # (older) IDs can't reappear in a future search.
        save_json(PROCESSED_FILE, processed_log[-MAX_PROCESSED:])


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["digest", "commands", "feed", "x-feed"], required=True)
    parser.add_argument("--force", action="store_true", help="ignore the 8am Pacific guard (digest)")
    parser.add_argument("--no-email", action="store_true", help="print emails instead of sending")
    parser.add_argument("--only", help="digest/feed/x-feed: limit to one watched channel or X account (id/handle/name)")
    parser.add_argument("--video", help="digest: summarize + email one specific video (id or URL) "
                                         "from the --only channel; does not advance last_seen")
    parser.add_argument("--post", help="digest: summarize + email one specific tweet/thread (id or URL) "
                                        "from the --only X account; does not advance x_last_seen")
    args = parser.parse_args()

    if args.mode == "digest":
        if args.video:
            if not args.only:
                parser.error("--video requires --only <channel id or name>")
            run_single_video(args.only, args.video, dry=args.no_email)
        elif args.post:
            if not args.only:
                parser.error("--post requires --only <X account id or handle>")
            run_single_tweet(args.only, args.post, dry=args.no_email)
        else:
            run_digest(force=args.force, dry=args.no_email, only=args.only)
    elif args.mode == "feed":
        if not args.only:
            parser.error("--mode feed requires --only <channel id or name>")
        debug_feed(args.only)
    elif args.mode == "x-feed":
        if not args.only:
            parser.error("--mode x-feed requires --only <X account id or handle>")
        debug_x_feed(args.only)
    else:
        run_commands(dry=args.no_email)


if __name__ == "__main__":
    main()
