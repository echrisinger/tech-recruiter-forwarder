# Recruiter Email Forwarder — Design

- **Date:** 2026-04-29
- **Status:** Draft, pending user review
- **Owner:** evanc

## Purpose

Automatically forward tech-recruiter outreach from my personal Gmail to a configurable recipient (default: my girlfriend), so she can pick up promising leads without me triaging them.

## Non-goals

- Replying to recruiters or scheduling calls.
- Generating summaries of recruiter pitches (forward fidelity matters more than digest brevity).
- Working across multiple Gmail accounts. Personal Gmail only; work email is excluded.
- A web UI, dashboard, or any remote/server deployment. Runs only on my MacBook.

## Architecture

```
launchd (every 15 min)
  └─> recruiter-forwarder (Python CLI)
        ├─ Gmail: list `newer_than:1h in:inbox -label:AutoForwarded`
        ├─ for each message:
        │    ├─ fetch headers + first ~500 chars of plaintext body
        │    ├─ Claude Haiku 4.5 via Anthropic API (or custom gateway):
        │    │     "is this a tech recruiter outreach?" (tool-use → structured)
        │    └─ if yes:
        │          ├─ Gmail.users.messages.send (RFC 5322 forward to recipient)
        │          └─ Gmail.users.messages.modify (add label AutoForwarded)
        └─ exit
```

**Idempotency model:** the Gmail label `AutoForwarded` is the single source of truth for "already processed." There is no local state file. The lookback window (1h) intentionally exceeds the cron cadence (15min) so a missed run self-heals on the next sweep without re-forwarding (the label query excludes already-processed mail).

**Order of operations:** forward → label. If the forward succeeds and the label call fails, the next sweep will re-forward. We accept that low-probability double-send in exchange for never silently dropping a message (recruiter mail is low-stakes; missing one is worse than duplicating one).

## Components

### `config.py`
Loads `~/.config/recruiter-forwarder/config.toml`. Schema:

```toml
recipient_emails = ["girlfriend@example.com"]   # list, so multi-recipient is trivial
lookback_hours = 1
gmail_label = "AutoForwarded"
dry_run = false
forward_subject_prefix = ""                      # e.g. "[recruiter] "; default empty
```

Secrets are NOT in TOML. They live in `~/.config/recruiter-forwarder/secrets.env`. Either of:

```
# Direct Anthropic API (default)
ANTHROPIC_API_KEY=sk-ant-...
```

or, for a custom Anthropic-compatible gateway:

```
ANTHROPIC_BASE_URL=https://your-gateway.example.com/
ANTHROPIC_AUTH_TOKEN=<bearer-token>
```

The script loads `secrets.env` itself at startup with a small parser; the plist stays clean of secrets. (We considered putting them in the plist's `EnvironmentVariables` block, but that puts secrets into a launchctl-readable file outside our config dir.)

Gmail OAuth tokens live at `~/.config/recruiter-forwarder/gmail_token.json` (cached after first run).

### `gmail.py`
Thin wrapper over `google-api-python-client`. OAuth via the installed-app flow on first run; refresh handled by the SDK. Methods:

- `list_unforwarded(since: datetime, label: str) -> list[MessageMeta]` — runs the Gmail search query.
- `get_message(id: str) -> ParsedMessage` — fetches full message, parses From/Subject/Date headers and decodes the plaintext body (falls back to stripping HTML if no plaintext part).
- `forward(message: ParsedMessage, to: list[str]) -> None` — builds an RFC 5322 forward (preserves original headers and body, prepends standard "----- Forwarded message -----" block, sets Subject to `Fwd: <original>`), base64-encodes, sends via `users.messages.send`.
- `ensure_label(name: str) -> str` — finds or creates the label, returns its ID.
- `add_label(message_id: str, label_id: str) -> None` — `users.messages.modify` with `addLabelIds`.

### `classifier.py`
Single Claude call per message:

```python
# SDK reads ANTHROPIC_API_KEY by default; if ANTHROPIC_BASE_URL is set
# we route through the gateway with bearer-token auth instead.
client = Anthropic()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=200,
    system=[{
        "type": "text",
        "text": RUBRIC_AND_FEW_SHOT,
        "cache_control": {"type": "ephemeral"},
    }],
    tools=[RECORD_JUDGMENT_TOOL],
    tool_choice={"type": "tool", "name": "record_judgment"},
    messages=[{"role": "user", "content": format_email(msg)}],
)
```

Tool schema:

```python
RECORD_JUDGMENT_TOOL = {
    "name": "record_judgment",
    "description": "Record whether the email is a tech recruiter outreach.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_recruiter": {"type": "boolean"},
            "reason": {"type": "string", "description": "One short sentence."},
        },
        "required": ["is_recruiter", "reason"],
    },
}
```

Input to the model: `From:` header, `Subject:`, and the first ~500 chars of plaintext body. The system prompt is cached (PromptCaching ephemeral) so repeat sweeps in the 5-minute TTL window pay only the cache-read cost.

The rubric must explicitly distinguish:
- **Recruiter outreach (true):** unsolicited contact about a job opportunity from an external recruiter, in-house TA, or staffing agency. Tech-flavored roles preferred but not required (we'd rather over-forward than miss).
- **Not recruiter (false):** newsletters, job-board digests (LinkedIn weekly summary, Indeed alerts), recruiter follow-ups already in an existing thread the user initiated, sales/marketing for recruiting tools, GitHub job-board notifications.

### `main.py`
Glue. For each unforwarded message in the lookback window:

1. `get_message` → `ParsedMessage`
2. `classifier.judge(msg)` → `RecruiterJudgment`
3. If `is_recruiter`:
   - If `dry_run`: log `would forward {id} {subject!r} to {recipients}: {reason}`.
   - Else: `gmail.forward(msg, recipients)` then `gmail.add_label(id, label_id)`.
4. If not `is_recruiter`: log at debug, do nothing (no label).

Per-message try/except so one bad message doesn't poison the run. All exceptions are logged; the script always exits 0 unless setup-level failures (missing config, bad creds) occurred — those exit non-zero so launchd logs visibly fail.

Logs go to `~/Library/Logs/recruiter-forwarder/forwarder.log` (rotated by size — keep last 5MB).

### `scripts/install-launchd.sh`
Idempotent installer:
1. Resolves the project path (where the script is run from).
2. Renders `com.evanc.recruiter-forwarder.plist.template` with that path.
3. Copies to `~/Library/LaunchAgents/com.evanc.recruiter-forwarder.plist`.
4. `launchctl bootout` (if loaded) then `launchctl bootstrap gui/$(id -u)`.

The plist runs `uv run --project ~/Projects/recruiter-forwarder recruiter-forwarder` with `StartInterval=900` (the launchd primitive for periodic execution) and `RunAtLoad=true`. StandardOut/StandardError redirected to the log path.

## Configuration & secrets

| Item | Location | Format |
|---|---|---|
| Recipient(s), cadence, label, dry-run | `~/.config/recruiter-forwarder/config.toml` | TOML |
| LLM gateway URL + token | `~/.config/recruiter-forwarder/secrets.env` | `KEY=VALUE` lines |
| Gmail OAuth refresh token | `~/.config/recruiter-forwarder/gmail_token.json` | JSON, written by SDK |
| Gmail OAuth client credentials | `~/.config/recruiter-forwarder/gmail_credentials.json` | JSON, downloaded once from GCP console |

Mode bits on `secrets.env` and `gmail_token.json` should be `600`.

## Validation strategy (in lieu of unit tests)

1. **First-run dry mode:** install with `dry_run = true`, let it run for ~48h, inspect the log to confirm the classifier's verdicts on real inbox traffic. Tune the rubric if needed.
2. **Targeted re-runs:** when adjusting the rubric, manually invoke `recruiter-forwarder --since-id <message-id>` to reclassify a specific known message and verify.
3. **Live cutover:** flip `dry_run = false`. The first real sweep will only act on mail in the past hour, so any incorrect judgment surface area is small.
4. **Ongoing:** the `AutoForwarded` label in Gmail is auditable — periodically inspect what got forwarded and confirm no false positives.

We will _not_ build a pytest suite for this project. Personal-script scope; dry-run is sufficient QA.

## Error handling

| Failure | Behavior |
|---|---|
| Gmail auth refresh fails | Log + exit non-zero. Launchd logs the failure; user re-runs OAuth flow manually. |
| Anthropic / gateway 5xx | Log + skip the message (no label → retried on next sweep). |
| Anthropic / gateway 4xx (auth) | Log + skip the message. (Will appear as repeated errors in the log until the token is rotated; visibility through the log is sufficient signal for a personal script.) |
| Forward send fails | Log + skip the message (no label → retried). |
| Label add fails after successful forward | Log warning. Next sweep may re-forward (acceptable). |
| Malformed message body (no plaintext, no HTML) | Log + skip. |
| Rate limited (429) | Log + skip; relies on the next sweep for retry. The 15-min cadence + 1h lookback is the de-facto backoff. |

The Anthropic and Google SDKs both perform their own internal retries on transient 5xx/429 (with backoff and jitter), so additional retry logic in our code would be redundant for the same scenarios.

## Project layout

```
~/Projects/recruiter-forwarder/
├── pyproject.toml                              # uv project, deps: anthropic, google-api-python-client, google-auth-oauthlib, tomli
├── README.md                                   # OAuth setup + install steps
├── config.example.toml
├── src/recruiter_forwarder/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── gmail.py
│   └── classifier.py
├── scripts/
│   ├── install-launchd.sh
│   └── com.evanc.recruiter-forwarder.plist.template
└── docs/
    └── superpowers/specs/
        └── 2026-04-29-recruiter-email-forwarder-design.md   # this file
```

`pyproject.toml` exposes a `recruiter-forwarder` console script. The launchd plist invokes it via `uv run --project ~/Projects/recruiter-forwarder recruiter-forwarder`.

## Open issues / things to revisit

- **Plaintext extraction quality.** Some recruiter emails are HTML-only with heavy inline styling. The MVP falls back to a naive `BeautifulSoup` strip; we'll see if that meaningfully degrades classification.
- **Non-UTF-8 bodies.** `_decode_body` decodes parts as UTF-8 with `errors="replace"`. Recruiter agencies sending `charset=ISO-8859-1` or `windows-1252` will arrive at the classifier with replacement chars throughout the body, degrading signal. If we see misclassifications cluster on non-UTF-8 emails during the dry-run audit, switch to walking parts via `email.message_from_bytes(...)` and using each part's `get_content_charset()`.
- **Recipient address discovery.** First-run UX: user has to know their girlfriend's email and put it in TOML. That's fine but could be improved with a one-time `--init` wizard later. Not in scope for MVP.
- **Telemetry.** No counters/metrics emitted. If the script feels like a black box after a few weeks, add a daily summary line to the log: `today: N scanned, M flagged, K forwarded, J errors`.
