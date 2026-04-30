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
