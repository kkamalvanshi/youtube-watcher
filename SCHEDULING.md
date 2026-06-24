# Scheduling

How the two workflows are triggered, and why.

## TL;DR

GitHub Actions `schedule:` (cron) events are **best-effort and heavily
throttled** — in practice the `*/15` command poll fired only every 1–4 hours,
and the "8 AM" digest landed 1–3 hours late. `workflow_dispatch` /
`repository_dispatch` events, by contrast, run on time.

So the **primary trigger is an external scheduler ([cron-job.org](https://cron-job.org))**
that calls each workflow's `workflow_dispatch` REST endpoint on schedule. The
GitHub `schedule:` cron lines remain as a **fallback** if the external trigger
ever fails. A once-per-day guard makes the digest safe to trigger from both.

```
cron-job.org ──POST workflow_dispatch──▶ GitHub Actions ──▶ watcher.py
   (on-time clock)                         (free compute,
                                            Chromium/PDF)
GitHub schedule: (delayed) ────────────────▲ fallback only
```

## The two cron-job.org jobs

Both use **method `POST`**, timezone **`America/Los_Angeles`**, body
`{"ref":"main"}`, and these headers:

| Header | Value |
|---|---|
| `Accept` | `application/vnd.github+json` |
| `Authorization` | `Bearer <FINE_GRAINED_PAT>`  ← the `Bearer ` prefix is required |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `User-Agent` | `yt-watcher-cron` |

| Job | Schedule | URL |
|---|---|---|
| **YT digest** | daily **08:00** | `https://api.github.com/repos/kkamalvanshi/youtube-watcher/actions/workflows/digest.yml/dispatches` |
| **YT commands** | every **5 min** (`*/5`) | `https://api.github.com/repos/kkamalvanshi/youtube-watcher/actions/workflows/commands.yml/dispatches` |

A successful dispatch returns HTTP **204 No Content**.

## GitHub token

A **fine-grained personal access token** scoped to this repo only:

- **Repository access:** `youtube-watcher` only.
- **Permissions → Actions: Read and write** (the only scope needed to dispatch).
- Stored **only in cron-job.org** — never committed. Worst case if leaked:
  someone can trigger these workflows on this one repo.

Rotate it from GitHub → Settings → Developer settings → Fine-grained tokens →
Regenerate, then paste the new value (with `Bearer `) into both cron jobs.

## Why there's no double-send

`digest.yml` runs `watcher.py --mode digest` (the **guarded** path) for both the
external trigger and the `schedule:` fallback. The guard in `run_digest()`:

1. exits if it's before 08:00 America/Los_Angeles;
2. exits if `state/last_digest_date.txt` already equals today;
3. otherwise sends one batched email and writes today's date to that marker.

So whichever trigger fires first after 8 AM sends the single daily email; every
later fire that day (including the delayed GitHub fallback) sees the marker and
skips. The command listener dedupes separately on email message-id
(`state/processed_emails.json`), so overlapping triggers never double-reply.

**Manual force:** to send immediately regardless of the guard (testing), run the
digest from the GitHub UI — **Actions → Daily digest → Run workflow → `force: true`**.
Only this explicit input bypasses the guard; the external trigger and schedule
never do.

## DST

GitHub cron is UTC with no DST handling (hence the two fallback lines at 15:00
and 16:00 UTC covering PDT/PST). The cron-job.org job uses the
`America/Los_Angeles` timezone directly, so 08:00 stays 08:00 local across DST
automatically.

## Verifying

```sh
gh run list --workflow=digest.yml   --limit 5
gh run list --workflow=commands.yml --limit 5
```

On-time runs show trigger **`workflow_dispatch`**; the throttled `schedule`
fallback runs (when they appear) are expected to log "already ran today" / find
no unprocessed mail and exit quickly.
