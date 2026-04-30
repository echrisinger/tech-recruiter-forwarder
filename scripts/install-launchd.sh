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
