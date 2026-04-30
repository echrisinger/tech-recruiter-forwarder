"""Recruiter classifier: Anthropic SDK with tool-use."""

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
    """Construct an Anthropic client.

    Defaults to the public Anthropic API (the SDK reads ANTHROPIC_API_KEY from env).
    If ANTHROPIC_BASE_URL is set, routes through that endpoint instead and uses
    ANTHROPIC_AUTH_TOKEN as a Bearer token — for proxies / corporate LLM gateways.
    """
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url:
        return Anthropic(base_url=base_url, auth_token=os.environ["ANTHROPIC_AUTH_TOKEN"])
    return Anthropic()


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
