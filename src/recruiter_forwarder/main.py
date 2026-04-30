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
