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
