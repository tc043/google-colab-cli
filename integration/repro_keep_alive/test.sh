#!/bin/bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Integration Test: Keep-Alive Daemon Lifecycle
# Verifies that `colab new` spawns a detached keep-alive daemon, persists its
# PID in the session state, and that `colab stop` reaps it cleanly.
#
# This test is a fast smoke test (~10s). For a soak test that verifies the
# daemon's pings actually succeed against the live backend, see
# integration/repro_keep_alive_scope/.

set -e

# Setup a clean session file for testing
TMP_DIR=$(mktemp -d)
SESSION_FILE="$TMP_DIR/sessions.json"
trap "rm -rf $TMP_DIR" EXIT

# To exercise the daemon we need OAuth2 or ADC with the `colaboratory`
# scope. Selection priority: OAuth2 (cached token present) > ADC (with the
# right scopes).
EXPECT_DAEMON=1
if [ -f "$HOME/.config/colab-cli/token.json" ]; then
    AUTH_FLAGS="--auth=oauth2"
elif command -v gcloud > /dev/null && gcloud auth application-default print-access-token > /dev/null 2>&1; then
    # Check that ADC has both required scopes (userinfo.email + colaboratory).
    ADC_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null)
    ADC_SCOPES=$(curl -s "https://www.googleapis.com/oauth2/v3/tokeninfo?access_token=$ADC_TOKEN" | python3 -c "import json,sys; print(json.load(sys.stdin).get('scope',''))" 2>/dev/null)
    if echo "$ADC_SCOPES" | grep -q "colaboratory" && echo "$ADC_SCOPES" | grep -q "userinfo.email"; then
        AUTH_FLAGS="--auth=adc"
    else
        echo "Error: ADC token lacks the required scopes."
        echo "Re-issue ADC creds with all required scopes:"
        echo "  gcloud auth application-default login \\"
        echo "      --scopes=openid,\\"
        echo "              https://www.googleapis.com/auth/cloud-platform,\\"
        echo "              https://www.googleapis.com/auth/userinfo.email,\\"
        echo "              https://www.googleapis.com/auth/colaboratory"
        exit 1
    fi
else
    echo "Error: No usable auth provider found."
    echo "Options:"
    echo "  - OAuth2: run 'uv run colab --auth=oauth2 sessions' to bootstrap"
    echo "  - ADC:    gcloud auth application-default login \\"
    echo "                --scopes=openid,\\"
    echo "                        https://www.googleapis.com/auth/cloud-platform,\\"
    echo "                        https://www.googleapis.com/auth/userinfo.email,\\"
    echo "                        https://www.googleapis.com/auth/colaboratory"
    exit 1
fi

process_is_alive() {
    uv run python - "$1" <<'PY'
import ctypes
import os
import sys

pid = int(sys.argv[1])
if os.name == "nt":
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise SystemExit(1)
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise SystemExit(1)
        raise SystemExit(0 if exit_code.value == STILL_ACTIVE else 1)
    finally:
        kernel32.CloseHandle(handle)

try:
    os.kill(pid, 0)
except ProcessLookupError:
    raise SystemExit(1)
except PermissionError:
    pass
PY
}

SESSION_NAME="test-live-keep-alive-$(date +%s)-$$"
HISTORY_FILE="$HOME/.config/colab-cli/history/${SESSION_NAME}.jsonl"

cleanup_session() {
    uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME" 2>/dev/null || true
    rm -f "$HISTORY_FILE"
}
trap "cleanup_session; rm -rf $TMP_DIR" EXIT

echo "[*] Creating new session (REAL API CALL) using $AUTH_FLAGS..."
uv run colab $AUTH_FLAGS --config "$SESSION_FILE" new -s "$SESSION_NAME"

# Verify session exists in state
if [ ! -f "$SESSION_FILE" ]; then
    echo "Error: Session file '$SESSION_FILE' not created."
    exit 1
fi

grep "$SESSION_NAME" "$SESSION_FILE"

# Extract PID (may be null if keep-alive was intentionally disabled).
PID=$(grep -A 15 "$SESSION_NAME" "$SESSION_FILE" | grep "keep_alive_pid" | awk '{print $2}' | tr -d ',')

if [ -z "$PID" ] || [ "$PID" == "null" ]; then
    echo "[FAILURE] No keep_alive_pid found under $AUTH_FLAGS (daemon should have spawned)."
    cat "$SESSION_FILE"
    exit 1
fi
echo "[*] Keep-alive PID: $PID"

if process_is_alive "$PID"; then
   echo "[*] Keep-alive process is running."
else
   echo "[FAILURE] Keep-alive process NOT running."
   exit 1
fi

LOG_OUTPUT=$(uv run colab $AUTH_FLAGS --config "$SESSION_FILE" log -s "$SESSION_NAME")
echo "$LOG_OUTPUT"
if ! echo "$LOG_OUTPUT" | grep -q "KEEP: started"; then
    echo "[FAILURE] keep_alive_started event missing from history."
    exit 1
fi
# The pre-flight in `colab new` calls keep_alive_assignment once
# synchronously before returning. If that succeeded, the structured
# history should NOT contain any KEEP: error events at this point.
if echo "$LOG_OUTPUT" | grep -q "KEEP: error"; then
    echo "[FAILURE] keep_alive_error events present immediately after 'colab new'."
    echo "          The pre-flight keep-alive ping failed. Check the body= field."
    exit 1
fi

echo "[*] Stopping session (REAL API CALL)..."
uv run colab $AUTH_FLAGS --config "$SESSION_FILE" stop -s "$SESSION_NAME"
sleep 1

if [ "$EXPECT_DAEMON" -eq 1 ]; then
    if ! process_is_alive "$PID"; then
       echo "[*] Keep-alive process terminated successfully."
    else
       echo "[FAILURE] Keep-alive process still running after stop!"
       exit 1
    fi
fi

# Disable the cleanup trap; we already cleaned up.
rm -f "$HISTORY_FILE"
trap "rm -rf $TMP_DIR" EXIT

echo "[SUCCESS] Live integration test passed!"
