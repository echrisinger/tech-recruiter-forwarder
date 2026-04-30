# recruiter-forwarder

Forwards unsolicited tech-recruiter emails from my personal Gmail to my girlfriend for her job search. Runs once a day at 2 PM via launchd. Classifies with Claude Haiku 4.5 via the Anthropic API.

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

6. **Create secrets file** with your Anthropic API key:
   ```bash
   cat > ~/.config/recruiter-forwarder/secrets.env <<'EOF'
   ANTHROPIC_API_KEY=sk-ant-...
   EOF
   chmod 600 ~/.config/recruiter-forwarder/secrets.env
   ```

   *Routing through a custom Anthropic-compatible gateway instead?* Set these two vars
   instead of `ANTHROPIC_API_KEY` — the script will detect them and use Bearer auth:
   ```
   ANTHROPIC_BASE_URL=https://your-gateway.example.com/
   ANTHROPIC_AUTH_TOKEN=<bearer-token>
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
- `~/.config/recruiter-forwarder/secrets.env` — Anthropic API key (or gateway URL + token).
- `~/.config/recruiter-forwarder/gmail_credentials.json` — OAuth client (downloaded once from GCP).
- `~/.config/recruiter-forwarder/gmail_token.json` — refresh token (auto-managed).
- `~/Library/Logs/recruiter-forwarder/forwarder.log` — rotated, 5MB cap.
- `~/Library/LaunchAgents/com.evanc.recruiter-forwarder.plist` — installed by `install-launchd.sh`.
