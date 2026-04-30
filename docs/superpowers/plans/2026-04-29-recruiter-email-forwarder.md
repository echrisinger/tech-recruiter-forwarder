# Recruiter Email Forwarder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a launchd-scheduled Python CLI that scans personal Gmail for tech-recruiter emails, classifies them with Claude Haiku via the Anthropic API, and forwards positives (preserving original headers/body) to a configurable recipient. Idempotent via a Gmail label.

**Architecture:** Single uv project at `~/Projects/recruiter-forwarder/`. Four modules: `config` (TOML + secrets.env loader), `gmail` (google-api-python-client wrapper for list/get/forward/label), `classifier` (Anthropic SDK with tool-use, cached system prompt), `main` (orchestration + logging). launchd plist runs every 900s. Gmail label `AutoForwarded` is the single source of truth for dedup.

**Tech Stack:** Python 3.12+, uv, anthropic SDK, google-api-python-client, google-auth-oauthlib, tomllib (stdlib), launchd.

**No automated tests.** Per spec, validation is via `dry_run = true` mode; each task ends with a manual smoke step instead of pytest.

**Spec:** `docs/superpowers/specs/2026-04-29-recruiter-email-forwarder-design.md`

---

## File Structure

| Path | Responsibility |
|---|---|
| `pyproject.toml` | uv project config, dependencies, `recruiter-forwarder` console script entry. |
| `.gitignore` | Ignore `.venv/`, `__pycache__/`, local config-dir mirrors used during dev. |
| `README.md` | One-time setup (GCP OAuth, config files) + install/uninstall commands. |
| `config.example.toml` | Reference config for the user to copy. |
| `src/recruiter_forwarder/__init__.py` | Package marker; exposes `__version__`. |
| `src/recruiter_forwarder/config.py` | Load + validate TOML config and `secrets.env`; populate env vars. |
| `src/recruiter_forwarder/classifier.py` | Anthropic-SDK-based recruiter classifier (tool-use, cached system prompt). |
| `src/recruiter_forwarder/gmail.py` | OAuth flow + Gmail API wrapper (list, get, forward, label). |
| `src/recruiter_forwarder/main.py` | CLI entry; orchestrates per-message: get → classify → forward+label. Logging. |
| `scripts/install-launchd.sh` | Render plist template, install to `~/Library/LaunchAgents/`, bootstrap. |
| `scripts/uninstall-launchd.sh` | Bootout + remove plist. |
| `scripts/com.evanc.recruiter-forwarder.plist.template` | launchd plist with `{{PROJECT_DIR}}`, `{{HOME}}`, `{{UV_BIN}}` placeholders. |

---

## Task 1: Bootstrap the uv project

**Files:**
- Create: `~/Projects/recruiter-forwarder/pyproject.toml`
- Create: `~/Projects/recruiter-forwarder/.gitignore`
- Create: `~/Projects/recruiter-forwarder/src/recruiter_forwarder/__init__.py`

- [ ] **Step 1: Verify uv is installed**

Run: `which uv && uv --version`
Expected: a path under `/opt/homebrew/bin/uv` or similar, plus a version line. If not found, install with `brew install uv`.

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "recruiter-forwarder"
version = "0.1.0"
description = "Forward tech-recruiter emails from personal Gmail to a configurable recipient."
requires-python = ">=3.12"
dependencies = [
    "anthropic>=0.40.0",
    "google-api-python-client>=2.150.0",
    "google-auth-oauthlib>=1.2.0",
    "google-auth-httplib2>=0.2.0",
    "beautifulsoup4>=4.12.0",
]

[project.scripts]
recruiter-forwarder = "recruiter_forwarder.main:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/recruiter_forwarder"]
```

- [ ] **Step 3: Write `.gitignore`**

```
.venv/
__pycache__/
*.pyc
*.egg-info/
dist/
build/
.DS_Store
# Local-only artifacts (real config + secrets live in ~/.config/recruiter-forwarder)
local/
```

- [ ] **Step 4: Write `src/recruiter_forwarder/__init__.py`**

```python
__version__ = "0.1.0"
```

- [ ] **Step 5: Verify the project builds and the entry point resolves**

Run: `cd ~/Projects/recruiter-forwarder && uv sync`
Expected: creates `.venv/`, installs all deps, no errors.

Run: `uv run python -c "from recruiter_forwarder import __version__; print(__version__)"`
Expected: prints `0.1.0`.

- [ ] **Step 6: Commit**

```bash
cd ~/Projects/recruiter-forwarder
git add pyproject.toml .gitignore src/
git commit -m "Bootstrap uv project skeleton"
```

---

## Task 2: Config loader

**Files:**
- Create: `src/recruiter_forwarder/config.py`
- Create: `config.example.toml`

The config loader reads TOML at `~/.config/recruiter-forwarder/config.toml` and a key=value `secrets.env` from the same directory, exporting secrets into `os.environ` so downstream code can read them. Missing required fields raise `ConfigError` with a clear message.

- [ ] **Step 1: Write `config.example.toml`**

```toml
# Where forwarded recruiter emails should be sent. List, so multi-recipient is trivial.
recipient_emails = ["girlfriend@example.com"]

# How far back to scan on each sweep. Should comfortably exceed the cron interval
# so a missed run self-heals on the next sweep.
lookback_hours = 1

# Gmail label applied to processed messages. Source of truth for dedup.
gmail_label = "AutoForwarded"

# When true, log "would forward..." but do not call forward/label APIs. Used during cutover.
dry_run = true

# Optional prefix prepended to forwarded subject lines, e.g. "[recruiter] ". Empty = no prefix.
forward_subject_prefix = ""
```

- [ ] **Step 2: Write `src/recruiter_forwarder/config.py`**

```python
"""Config + secrets loader.

Reads ~/.config/recruiter-forwarder/config.toml and ~/.config/recruiter-forwarder/secrets.env.
Secrets file is a simple KEY=VALUE format (one per line, # comments allowed). Loaded values
are set into os.environ so the Anthropic SDK and Google libraries pick them up automatically.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


CONFIG_DIR = Path.home() / ".config" / "recruiter-forwarder"
CONFIG_PATH = CONFIG_DIR / "config.toml"
SECRETS_PATH = CONFIG_DIR / "secrets.env"
GMAIL_TOKEN_PATH = CONFIG_DIR / "gmail_token.json"
GMAIL_CREDENTIALS_PATH = CONFIG_DIR / "gmail_credentials.json"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    recipient_emails: list[str]
    lookback_hours: int
    gmail_label: str
    dry_run: bool
    forward_subject_prefix: str


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        raise ConfigError(
            f"Config file not found at {path}. "
            f"Copy config.example.toml from the project to this location and edit it."
        )
    with path.open("rb") as f:
        data = tomllib.load(f)

    try:
        recipients = list(data["recipient_emails"])
        if not recipients or not all(isinstance(r, str) and "@" in r for r in recipients):
            raise ConfigError("recipient_emails must be a non-empty list of email strings")
        return Config(
            recipient_emails=recipients,
            lookback_hours=int(data.get("lookback_hours", 1)),
            gmail_label=str(data.get("gmail_label", "AutoForwarded")),
            dry_run=bool(data.get("dry_run", True)),
            forward_subject_prefix=str(data.get("forward_subject_prefix", "")),
        )
    except KeyError as e:
        raise ConfigError(f"Missing required config key: {e.args[0]}") from e


def load_secrets(path: Path = SECRETS_PATH) -> None:
    """Parse a simple KEY=VALUE file and set values into os.environ.

    Lines starting with # are comments. Blank lines are ignored. Values are taken verbatim
    (no quote stripping); add quotes if your value needs them.

    Required keys: ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN.
    """
    if not path.exists():
        raise ConfigError(
            f"Secrets file not found at {path}. "
            f"Create it with ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN. chmod 600 it."
        )

    for line_no, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path}:{line_no}: expected KEY=VALUE, got {raw!r}")
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip()

    for required in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        if not os.environ.get(required):
            raise ConfigError(f"{path} did not set {required}")
```

- [ ] **Step 3: Set up real config files for development**

```bash
mkdir -p ~/.config/recruiter-forwarder
chmod 700 ~/.config/recruiter-forwarder
cp ~/Projects/recruiter-forwarder/config.example.toml ~/.config/recruiter-forwarder/config.toml

cat > ~/.config/recruiter-forwarder/secrets.env <<'EOF'
ANTHROPIC_BASE_URL=https://your-gateway.example.com/
ANTHROPIC_AUTH_TOKEN=<your-gateway-token>
EOF
chmod 600 ~/.config/recruiter-forwarder/secrets.env
```

Then edit `~/.config/recruiter-forwarder/config.toml` and replace `girlfriend@example.com` with the real recipient address. Leave `dry_run = true` for now.

- [ ] **Step 4: Verify the loader works**

Run:
```bash
cd ~/Projects/recruiter-forwarder
uv run python -c "
from recruiter_forwarder.config import load_config, load_secrets
import os
load_secrets()
print('ANTHROPIC_BASE_URL =', os.environ.get('ANTHROPIC_BASE_URL'))
print('ANTHROPIC_AUTH_TOKEN set:', bool(os.environ.get('ANTHROPIC_AUTH_TOKEN')))
print(load_config())
"
```
Expected: prints the gateway URL, `True` for token set, and a `Config(recipient_emails=[...], lookback_hours=1, gmail_label='AutoForwarded', dry_run=True, forward_subject_prefix='')`.

- [ ] **Step 5: Commit**

```bash
git add config.example.toml src/recruiter_forwarder/config.py
git commit -m "Add config + secrets loader"
```

---

## Task 3: Classifier

**Files:**
- Create: `src/recruiter_forwarder/classifier.py`

The classifier sends From + Subject + first ~600 chars of plaintext body to Claude Haiku 4.5 via the Anthropic API. Tool-use forces a structured `{is_recruiter, reason}` response. The system prompt is cached so repeat sweeps in the same 5-minute TTL window pay only the cache-read cost.

- [ ] **Step 1: Write `src/recruiter_forwarder/classifier.py`**

```python
"""Recruiter classifier: Anthropic SDK with tool-use via the Anthropic API."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from anthropic import Anthropic


log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"
BODY_CHAR_LIMIT = 600

SYSTEM_PROMPT = """You are a binary classifier. For each email, decide whether it is an unsolicited tech-recruiter outreach.

Classify as RECRUITER OUTREACH (is_recruiter=true) when:
- An external recruiter, in-house talent acquisition rep, or staffing agency is contacting the user about a specific role or to start a conversation about opportunities.
- The email is unsolicited (not part of an existing thread the user started).
- Roles can be tech-flavored (engineering, data, ML, product, design at a tech company) but adjacent roles (PM, technical sales engineer, dev advocate) also count.

Classify as NOT RECRUITER (is_recruiter=false) when:
- It is a job-board digest or weekly summary (LinkedIn Jobs digest, Indeed alerts, GitHub Jobs, Hired.com weekly).
- It is a newsletter or content-marketing email even if it mentions hiring.
- It is a follow-up in a thread the user already replied in.
- It is sales/marketing for recruiting tools (Gem, Greenhouse, etc.) targeted at recruiters.
- It is an automated rejection / status update from an application the user submitted.

Bias toward RECRUITER OUTREACH when uncertain — false positives are recoverable; false negatives are missed opportunities.

Always call the record_judgment tool. Never answer in plain text."""

RECORD_JUDGMENT_TOOL = {
    "name": "record_judgment",
    "description": "Record whether the email is an unsolicited tech-recruiter outreach.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_recruiter": {
                "type": "boolean",
                "description": "True if this is an unsolicited recruiter outreach as defined in the system prompt.",
            },
            "reason": {
                "type": "string",
                "description": "One short sentence explaining the decision.",
            },
        },
        "required": ["is_recruiter", "reason"],
    },
}


@dataclass(frozen=True)
class RecruiterJudgment:
    is_recruiter: bool
    reason: str


def _format_email(from_header: str, subject: str, body: str) -> str:
    snippet = body[:BODY_CHAR_LIMIT]
    if len(body) > BODY_CHAR_LIMIT:
        snippet += "\n[...truncated]"
    return f"From: {from_header}\nSubject: {subject}\n\n{snippet}"


def make_client() -> Anthropic:
    return Anthropic(
        base_url=os.environ["ANTHROPIC_BASE_URL"],
        auth_token=os.environ["ANTHROPIC_AUTH_TOKEN"],
    )


def classify(client: Anthropic, from_header: str, subject: str, body: str) -> RecruiterJudgment:
    response = client.messages.create(
        model=MODEL,
        max_tokens=200,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[RECORD_JUDGMENT_TOOL],
        tool_choice={"type": "tool", "name": "record_judgment"},
        messages=[{"role": "user", "content": _format_email(from_header, subject, body)}],
    )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_judgment":
            args = block.input
            return RecruiterJudgment(
                is_recruiter=bool(args["is_recruiter"]),
                reason=str(args["reason"]),
            )

    raise RuntimeError(f"Classifier did not call record_judgment; got {response.content!r}")
```

- [ ] **Step 2: Smoke-test the classifier with two hardcoded emails**

Run:
```bash
cd ~/Projects/recruiter-forwarder
uv run python -c "
from recruiter_forwarder.config import load_secrets
from recruiter_forwarder.classifier import make_client, classify
load_secrets()
client = make_client()

# Should be true
j1 = classify(
    client,
    from_header='Jordan Reyes <jordan@scaleup-staffing.com>',
    subject='Senior Backend Engineer @ Series-B fintech ($230-280k + equity)',
    body=(
        \"Hi Evan,\\n\\nI came across your profile and thought you'd be a strong fit \"
        \"for a Senior Backend Engineer role at a Series-B fintech I'm working with. \"
        \"They're growing fast and the comp band is $230-280k base plus meaningful equity. \"
        \"Would you have 15 minutes this week to chat?\\n\\nBest, Jordan\"
    ),
)
print('R1:', j1)

# Should be false (newsletter / digest)
j2 = classify(
    client,
    from_header='LinkedIn Jobs <jobs-noreply@linkedin.com>',
    subject='Your weekly job alert: 12 new matches',
    body=\"This week's top job matches based on your saved searches. Senior Engineer at Acme... View all jobs.\",
)
print('R2:', j2)
"
```
Expected: R1 prints `RecruiterJudgment(is_recruiter=True, reason='...')`. R2 prints `RecruiterJudgment(is_recruiter=False, reason='...')`. If either is wrong, tighten the rubric in `SYSTEM_PROMPT` and re-run.

- [ ] **Step 3: Commit**

```bash
git add src/recruiter_forwarder/classifier.py
git commit -m "Add Anthropic-SDK recruiter classifier"
```

---

## Task 4: Set up Gmail OAuth credentials (one-time manual step)

This task is mostly clicking through the GCP console. It produces `gmail_credentials.json` that we use in Task 5.

- [ ] **Step 1: Create a GCP project and OAuth client**

In the browser:
1. Go to https://console.cloud.google.com/projectcreate. Project name: `recruiter-forwarder`. Create.
2. Switch to the new project.
3. APIs & Services → Library → search "Gmail API" → Enable.
4. APIs & Services → OAuth consent screen → External → fill in app name `recruiter-forwarder`, your personal Gmail as support email and developer email → Save and continue.
5. On the Scopes step: skip (we'll request scopes from the client).
6. On Test users: add your personal Gmail address. Save and continue.
7. APIs & Services → Credentials → Create credentials → OAuth client ID → Application type: **Desktop app** → name: `recruiter-forwarder-cli` → Create.
8. Download the JSON. It will be named like `client_secret_<long-id>.apps.googleusercontent.com.json`.

- [ ] **Step 2: Move credentials into the config dir**

```bash
mv ~/Downloads/client_secret_*.apps.googleusercontent.com.json \
   ~/.config/recruiter-forwarder/gmail_credentials.json
chmod 600 ~/.config/recruiter-forwarder/gmail_credentials.json
```

- [ ] **Step 3: Verify**

Run:
```bash
ls -la ~/.config/recruiter-forwarder/gmail_credentials.json
```
Expected: file exists, mode `-rw-------`.

(No commit — credentials live outside the repo.)

---

## Task 5: Gmail wrapper — OAuth + list + get + label

**Files:**
- Create: `src/recruiter_forwarder/gmail.py`

Forward functionality is added in Task 6 to keep this task small. This task gets us through OAuth and read-side operations.

- [ ] **Step 1: Write `src/recruiter_forwarder/gmail.py` (read-side only)**

```python
"""Gmail wrapper: OAuth, list, get, label management.

The Gmail label `AutoForwarded` is the source of truth for "already processed."
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import message_from_bytes
from email.message import EmailMessage
from pathlib import Path

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


log = logging.getLogger(__name__)

# Scope: read messages, send mail, modify labels. modify is the umbrella that includes label changes.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


@dataclass
class ParsedMessage:
    id: str
    thread_id: str
    from_header: str
    subject: str
    raw_bytes: bytes  # full RFC 5322; used for forwarding
    plaintext_body: str  # extracted body for classification


def get_service(credentials_path: Path, token_path: Path):
    """OAuth flow on first run; cached refresh thereafter."""
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
        token_path.chmod(0o600)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def ensure_label(service, name: str) -> str:
    """Find the label ID by name, creating it if needed."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == name:
            return lbl["id"]
    created = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        .execute()
    )
    return created["id"]


def list_unforwarded(service, since: datetime, label_name: str) -> list[str]:
    """Return message IDs in the inbox newer than `since` that don't already have label_name."""
    delta = datetime.now() - since
    minutes = max(1, int(delta.total_seconds() // 60))
    # Gmail's `newer_than` accepts h/d but not m, so use h with rounding-up.
    hours = max(1, (minutes + 59) // 60)
    query = f"in:inbox newer_than:{hours}h -label:{label_name}"
    log.info("Gmail list query: %s", query)

    ids: list[str] = []
    page_token: str | None = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _decode_body(payload: dict) -> str:
    """Walk MIME parts; prefer text/plain, fall back to text/html stripped."""

    def find(part: dict, target_mime: str) -> str:
        if part.get("mimeType") == target_mime:
            data = part.get("body", {}).get("data")
            if data:
                return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
                    "utf-8", errors="replace"
                )
        for sub in part.get("parts", []) or []:
            found = find(sub, target_mime)
            if found:
                return found
        return ""

    plain = find(payload, "text/plain")
    if plain:
        return plain
    html = find(payload, "text/html")
    if html:
        return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
    return ""


def get_message(service, message_id: str) -> ParsedMessage:
    full = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="raw")
        .execute()
    )
    raw = base64.urlsafe_b64decode(full["raw"] + "=" * (-len(full["raw"]) % 4))
    msg = message_from_bytes(raw)

    metadata = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    body = _decode_body(metadata.get("payload", {}))

    return ParsedMessage(
        id=message_id,
        thread_id=full.get("threadId", ""),
        from_header=msg.get("From", ""),
        subject=msg.get("Subject", ""),
        raw_bytes=raw,
        plaintext_body=body,
    )


def add_label(service, message_id: str, label_id: str) -> None:
    service.users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": [label_id]}
    ).execute()
```

- [ ] **Step 2: Run the OAuth flow once and verify list/get/label**

Run:
```bash
cd ~/Projects/recruiter-forwarder
uv run python -c "
from datetime import datetime, timedelta
from recruiter_forwarder.config import (
    GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH, load_secrets,
)
from recruiter_forwarder.gmail import (
    get_service, ensure_label, list_unforwarded, get_message,
)
load_secrets()
service = get_service(GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH)
label_id = ensure_label(service, 'AutoForwarded')
print('Label ID:', label_id)

ids = list_unforwarded(service, datetime.now() - timedelta(hours=24), 'AutoForwarded')
print(f'Found {len(ids)} unforwarded messages in past 24h')
if ids:
    msg = get_message(service, ids[0])
    print('First msg:', msg.from_header[:60], '|', msg.subject[:60])
    print('Body snippet:', msg.plaintext_body[:200])
"
```
Expected: a browser window opens for OAuth consent the first time, you grant access, the script prints a label ID and a count plus a sample. `gmail_token.json` now exists in the config dir. Subsequent runs skip the browser.

- [ ] **Step 3: Commit**

```bash
git add src/recruiter_forwarder/gmail.py
git commit -m "Add Gmail wrapper: OAuth, list, get, label management"
```

---

## Task 6: Gmail wrapper — forward

**Files:**
- Modify: `src/recruiter_forwarder/gmail.py`

A "true forward" is an RFC 5322 message we construct ourselves that wraps the original. Subject becomes `Fwd: <original>`, body has the standard `----- Forwarded message -----` block followed by the original headers and body.

- [ ] **Step 1: Append `forward` to `src/recruiter_forwarder/gmail.py`**

Add at the bottom of the file:

```python
def forward(
    service,
    msg: ParsedMessage,
    to: list[str],
    subject_prefix: str = "",
) -> None:
    """Forward `msg` to `to` as a new RFC 5322 message.

    Preserves the original headers and body inside a standard 'Forwarded message' block.
    """
    original = message_from_bytes(msg.raw_bytes)

    fwd = EmailMessage()
    fwd["To"] = ", ".join(to)
    base_subject = msg.subject or "(no subject)"
    if not base_subject.lower().startswith(("fwd:", "fw:")):
        base_subject = f"Fwd: {base_subject}"
    fwd["Subject"] = f"{subject_prefix}{base_subject}"

    header_block = "\n".join(
        f"{name}: {value}"
        for name in ("From", "Date", "Subject", "To")
        if (value := original.get(name))
    )
    body_text = msg.plaintext_body or "(no readable body)"
    fwd.set_content(
        "---------- Forwarded message ----------\n"
        f"{header_block}\n\n"
        f"{body_text}\n"
    )

    raw_b64 = base64.urlsafe_b64encode(fwd.as_bytes()).decode("ascii")
    service.users().messages().send(userId="me", body={"raw": raw_b64}).execute()
```

- [ ] **Step 2: Smoke-test the forward by sending one message to yourself**

Run (replacing `you@personal.com` with your own Gmail address — we send a test forward to yourself, NOT the real recipient):
```bash
cd ~/Projects/recruiter-forwarder
uv run python -c "
from datetime import datetime, timedelta
from recruiter_forwarder.config import (
    GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH, load_secrets,
)
from recruiter_forwarder.gmail import (
    get_service, list_unforwarded, get_message, forward,
)
load_secrets()
service = get_service(GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH)
ids = list_unforwarded(service, datetime.now() - timedelta(hours=24), 'AutoForwarded')
assert ids, 'need at least one recent unforwarded inbox message to test'
msg = get_message(service, ids[0])
forward(service, msg, ['you@personal.com'], subject_prefix='[test] ')
print('Sent test forward of:', msg.subject)
"
```
Expected: a `[test] Fwd: <subject>` email appears in your own inbox within seconds. Open it and confirm: From/Date/Subject/To of the original are visible, the body is intact.

- [ ] **Step 3: Clean up the test forward manually**

Delete the `[test] Fwd: ...` email from your inbox so it doesn't show up in later sweeps.

- [ ] **Step 4: Commit**

```bash
git add src/recruiter_forwarder/gmail.py
git commit -m "Add Gmail forward sender"
```

---

## Task 7: Main loop with logging

**Files:**
- Create: `src/recruiter_forwarder/main.py`

- [ ] **Step 1: Write `src/recruiter_forwarder/main.py`**

```python
"""CLI entry point: scan, classify, forward+label."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import classifier, gmail
from .config import (
    GMAIL_CREDENTIALS_PATH,
    GMAIL_TOKEN_PATH,
    Config,
    ConfigError,
    load_config,
    load_secrets,
)


LOG_DIR = Path.home() / "Library" / "Logs" / "recruiter-forwarder"
LOG_PATH = LOG_DIR / "forwarder.log"
log = logging.getLogger("recruiter_forwarder")


def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=5_000_000, backupCount=2
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def run(cfg: Config) -> int:
    service = gmail.get_service(GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH)
    label_id = gmail.ensure_label(service, cfg.gmail_label)

    since = datetime.now() - timedelta(hours=cfg.lookback_hours)
    ids = gmail.list_unforwarded(service, since, cfg.gmail_label)
    log.info(
        "scan: %d candidate message(s) in past %dh (dry_run=%s)",
        len(ids),
        cfg.lookback_hours,
        cfg.dry_run,
    )

    if not ids:
        return 0

    client = classifier.make_client()
    forwarded = 0
    skipped = 0
    errors = 0

    for mid in ids:
        try:
            msg = gmail.get_message(service, mid)
            judgment = classifier.classify(
                client, msg.from_header, msg.subject, msg.plaintext_body
            )
            log.info(
                "judge id=%s from=%r subject=%r is_recruiter=%s reason=%r",
                mid,
                msg.from_header[:80],
                msg.subject[:80],
                judgment.is_recruiter,
                judgment.reason,
            )
            if not judgment.is_recruiter:
                skipped += 1
                continue
            if cfg.dry_run:
                log.info(
                    "dry_run: would forward id=%s to %s with prefix=%r",
                    mid,
                    cfg.recipient_emails,
                    cfg.forward_subject_prefix,
                )
                forwarded += 1
                continue
            gmail.forward(service, msg, cfg.recipient_emails, cfg.forward_subject_prefix)
            gmail.add_label(service, mid, label_id)
            log.info("forwarded id=%s -> %s", mid, cfg.recipient_emails)
            forwarded += 1
        except Exception:
            errors += 1
            log.exception("error processing id=%s", mid)

    log.info(
        "summary: scanned=%d forwarded=%d skipped=%d errors=%d",
        len(ids),
        forwarded,
        skipped,
        errors,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Forward tech-recruiter emails.")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging.")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Override config.toml path (default: ~/.config/recruiter-forwarder/config.toml).",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    try:
        load_secrets()
        cfg = load_config(args.config) if args.config else load_config()
    except ConfigError as e:
        log.error("config error: %s", e)
        return 2

    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Smoke-test in dry-run**

(Make sure `dry_run = true` in `~/.config/recruiter-forwarder/config.toml`.)

Run:
```bash
cd ~/Projects/recruiter-forwarder
uv run recruiter-forwarder -v
```
Expected: logs to stderr and to `~/Library/Logs/recruiter-forwarder/forwarder.log`. You see one `scan: N candidate message(s) ...` line, one `judge ...` line per candidate, possibly `dry_run: would forward ...` lines, and a final `summary: scanned=... forwarded=... skipped=... errors=0`. No real forwards sent. No labels applied.

- [ ] **Step 3: Verify nothing was actually labeled**

Run:
```bash
cd ~/Projects/recruiter-forwarder
uv run python -c "
from recruiter_forwarder.config import GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH, load_secrets
from recruiter_forwarder.gmail import get_service
load_secrets()
service = get_service(GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH)
resp = service.users().messages().list(userId='me', q='label:AutoForwarded').execute()
print('Labeled messages:', len(resp.get('messages', [])))
"
```
Expected: `Labeled messages: 0` (the dry run did not label).

- [ ] **Step 4: Commit**

```bash
git add src/recruiter_forwarder/main.py
git commit -m "Add main CLI: scan, classify, forward+label with logging"
```

---

## Task 8: launchd plist + install/uninstall scripts

**Files:**
- Create: `scripts/com.evanc.recruiter-forwarder.plist.template`
- Create: `scripts/install-launchd.sh`
- Create: `scripts/uninstall-launchd.sh`

- [ ] **Step 1: Write `scripts/com.evanc.recruiter-forwarder.plist.template`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.evanc.recruiter-forwarder</string>

    <key>ProgramArguments</key>
    <array>
        <string>{{UV_BIN}}</string>
        <string>run</string>
        <string>--project</string>
        <string>{{PROJECT_DIR}}</string>
        <string>recruiter-forwarder</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{{PROJECT_DIR}}</string>

    <key>StartInterval</key>
    <integer>900</integer>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{{HOME}}/Library/Logs/recruiter-forwarder/forwarder.log</string>

    <key>StandardErrorPath</key>
    <string>{{HOME}}/Library/Logs/recruiter-forwarder/forwarder.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

- [ ] **Step 2: Write `scripts/install-launchd.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.evanc.recruiter-forwarder"
PLIST_DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="${PROJECT_DIR}/scripts/com.evanc.recruiter-forwarder.plist.template"
LOG_DIR="${HOME}/Library/Logs/recruiter-forwarder"

UV_BIN="$(command -v uv || true)"
if [[ -z "${UV_BIN}" ]]; then
    echo "uv not found in PATH. Install with: brew install uv" >&2
    exit 1
fi

mkdir -p "${LOG_DIR}"
mkdir -p "${HOME}/Library/LaunchAgents"

sed \
    -e "s|{{PROJECT_DIR}}|${PROJECT_DIR}|g" \
    -e "s|{{HOME}}|${HOME}|g" \
    -e "s|{{UV_BIN}}|${UV_BIN}|g" \
    "${TEMPLATE}" > "${PLIST_DEST}"

# Idempotent: bootout existing, then bootstrap fresh.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${PLIST_DEST}"
launchctl enable "gui/$(id -u)/${LABEL}"

echo "Installed: ${PLIST_DEST}"
echo "Logs:      ${LOG_DIR}/forwarder.log"
echo
echo "Tail logs with:  tail -f ${LOG_DIR}/forwarder.log"
echo "Run now with:    launchctl kickstart -k gui/\$(id -u)/${LABEL}"
echo "Uninstall with:  ${PROJECT_DIR}/scripts/uninstall-launchd.sh"
```

- [ ] **Step 3: Write `scripts/uninstall-launchd.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

LABEL="com.evanc.recruiter-forwarder"
PLIST_DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
rm -f "${PLIST_DEST}"

echo "Uninstalled ${LABEL}"
```

- [ ] **Step 4: Make the scripts executable**

```bash
chmod +x ~/Projects/recruiter-forwarder/scripts/install-launchd.sh
chmod +x ~/Projects/recruiter-forwarder/scripts/uninstall-launchd.sh
```

- [ ] **Step 5: Install and verify the agent loads**

Run:
```bash
~/Projects/recruiter-forwarder/scripts/install-launchd.sh
launchctl list | grep recruiter-forwarder
```
Expected: install script prints the plist destination; `launchctl list` shows a line like `-	0	com.evanc.recruiter-forwarder` (PID empty between runs, exit code 0 after a successful run).

`RunAtLoad=true` means it runs immediately. Wait ~10 seconds, then:
```bash
tail -n 30 ~/Library/Logs/recruiter-forwarder/forwarder.log
```
Expected: a fresh `scan: ...` and `summary: ...` block timestamped within the last minute.

- [ ] **Step 6: Commit**

```bash
cd ~/Projects/recruiter-forwarder
git add scripts/
git commit -m "Add launchd plist template + install/uninstall scripts"
```

---

## Task 9: README

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write `README.md`**

```markdown
# recruiter-forwarder

Forwards unsolicited tech-recruiter emails from my personal Gmail to a configurable recipient. Runs every 15 minutes via launchd. Classifies with Claude Haiku 4.5 via the Anthropic API.

## One-time setup

1. **Install uv** (if not already): `brew install uv`

2. **Clone / use this directory**: project lives at `~/Projects/recruiter-forwarder/`.

3. **Install dependencies**: `cd ~/Projects/recruiter-forwarder && uv sync`

4. **Create config dir**:
   ```bash
   mkdir -p ~/.config/recruiter-forwarder
   chmod 700 ~/.config/recruiter-forwarder
   ```

5. **Copy and edit config**:
   ```bash
   cp config.example.toml ~/.config/recruiter-forwarder/config.toml
   # edit recipient_emails; leave dry_run=true for first run
   ```

6. **Create secrets file**:
   ```bash
   cat > ~/.config/recruiter-forwarder/secrets.env <<'EOF'
   ANTHROPIC_BASE_URL=https://your-gateway.example.com/
   ANTHROPIC_AUTH_TOKEN=<your-gateway-token>
   EOF
   chmod 600 ~/.config/recruiter-forwarder/secrets.env
   ```

7. **Set up Gmail OAuth** (see `docs/superpowers/specs/2026-04-29-recruiter-email-forwarder-design.md` Task 4 for click-through). Place the downloaded JSON at:
   ```
   ~/.config/recruiter-forwarder/gmail_credentials.json
   chmod 600 ~/.config/recruiter-forwarder/gmail_credentials.json
   ```

8. **Run once interactively to do the OAuth dance** (browser opens):
   ```bash
   uv run recruiter-forwarder -v
   ```
   The first run will pop a browser for Gmail consent. After grant, `gmail_token.json` is cached and subsequent runs are silent.

9. **Install the launchd agent**:
   ```bash
   scripts/install-launchd.sh
   ```

## Going live

The default config has `dry_run = true`. Watch the log for a day or two:
```bash
tail -f ~/Library/Logs/recruiter-forwarder/forwarder.log
```

Once you're happy with the classifier's verdicts, edit `~/.config/recruiter-forwarder/config.toml` and set `dry_run = false`. The change takes effect on the next sweep (within 15 min); no re-install needed.

## Useful commands

| Action | Command |
|---|---|
| Run now (manual) | `uv run recruiter-forwarder -v` |
| Run now (via launchd) | `launchctl kickstart -k gui/$(id -u)/com.evanc.recruiter-forwarder` |
| Tail logs | `tail -f ~/Library/Logs/recruiter-forwarder/forwarder.log` |
| List labeled mail | search Gmail for `label:AutoForwarded` |
| Uninstall agent | `scripts/uninstall-launchd.sh` |

## How dedup works

Every successfully forwarded message gets the Gmail label `AutoForwarded`. The list query filters out anything with that label, so re-runs are safe even if a sweep is killed mid-flight. Worst case (forward succeeded, label add failed): the message gets re-forwarded on the next sweep. Better than silently dropping one.

## Files

- `~/.config/recruiter-forwarder/config.toml` — recipient list, lookback, dry_run.
- `~/.config/recruiter-forwarder/secrets.env` — gateway URL + token.
- `~/.config/recruiter-forwarder/gmail_credentials.json` — OAuth client (downloaded once from GCP).
- `~/.config/recruiter-forwarder/gmail_token.json` — refresh token (auto-managed).
- `~/Library/Logs/recruiter-forwarder/forwarder.log` — rotated, 5MB cap.
- `~/Library/LaunchAgents/com.evanc.recruiter-forwarder.plist` — installed by `install-launchd.sh`.
```

- [ ] **Step 2: Commit**

```bash
cd ~/Projects/recruiter-forwarder
git add README.md
git commit -m "Add README with setup + usage docs"
```

---

## Task 10: End-to-end dry-run validation, then go live

This is the validation gate. Don't flip `dry_run = false` until you've audited the log.

- [ ] **Step 1: Confirm the agent has been running on its 15-minute cadence**

Run:
```bash
launchctl list | grep recruiter-forwarder
grep -c "summary: scanned=" ~/Library/Logs/recruiter-forwarder/forwarder.log
```
Expected: agent listed; summary count grows across an hour or more (at least 4 sweeps).

- [ ] **Step 2: Audit classifier verdicts**

Run:
```bash
grep "judge " ~/Library/Logs/recruiter-forwarder/forwarder.log | tail -50
```
For each line, confirm the verdict makes sense: `is_recruiter=True` should be unsolicited recruiter outreach, `False` should be everything else (newsletters, personal mail, transactional, etc.).

If the classifier is systematically wrong on a class of email, edit `SYSTEM_PROMPT` in `src/recruiter_forwarder/classifier.py`, then:
```bash
cd ~/Projects/recruiter-forwarder
git commit -am "Tune classifier rubric"
launchctl kickstart -k gui/$(id -u)/com.evanc.recruiter-forwarder
```
And re-audit.

- [ ] **Step 3: Confirm dry-run did not actually label or forward anything**

Run:
```bash
grep -c "would forward" ~/Library/Logs/recruiter-forwarder/forwarder.log
grep -c "forwarded id=" ~/Library/Logs/recruiter-forwarder/forwarder.log
```
Expected: "would forward" count > 0 if any positives were seen; "forwarded id=" count = 0 (those only appear when not in dry run).

In Gmail, search `label:AutoForwarded` — should be empty.

- [ ] **Step 4: Flip to live mode**

Edit `~/.config/recruiter-forwarder/config.toml`:
```toml
dry_run = false
```

Force one immediate sweep:
```bash
launchctl kickstart -k gui/$(id -u)/com.evanc.recruiter-forwarder
```

Watch the log:
```bash
tail -f ~/Library/Logs/recruiter-forwarder/forwarder.log
```
Expected: the next sweep shows `forwarded id=...` lines (one per recruiter judgment) and the `AutoForwarded` label starts populating in Gmail. Confirm with the recipient that one or more emails arrived.

- [ ] **Step 5: Done**

The agent runs every 15 minutes. Re-audit the log and Gmail's `AutoForwarded` label periodically (weekly is plenty) to catch any classifier drift.

---

## Operational notes

- **Tuning the rubric:** edits to `SYSTEM_PROMPT` in `classifier.py` take effect on the next sweep — no re-install needed (uv runs from source).
- **Adding a recipient:** edit `recipient_emails` in `config.toml`. No re-install.
- **Pausing:** `launchctl bootout gui/$(id -u)/com.evanc.recruiter-forwarder`. Re-enable with the install script.
- **Token rotation:** edit `~/.config/recruiter-forwarder/secrets.env` and kickstart.
- **OAuth re-consent (rare):** delete `gmail_token.json` and run `uv run recruiter-forwarder -v` to redo the browser flow.
