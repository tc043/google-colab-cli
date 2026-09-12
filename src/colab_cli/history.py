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

import datetime
import json
import logging
import os
from typing import Any, Dict, List

from filelock import FileLock


class HistoryLogger:
    def __init__(self, log_dir: str = "~/.config/colab-cli/history"):
        self.log_dir = os.path.expanduser(log_dir)
        os.makedirs(self.log_dir, exist_ok=True)

    def _get_log_path(self, session_name: str) -> str:
        return os.path.join(self.log_dir, f"{session_name}.jsonl")

    def _get_lock_path(self, session_name: str) -> str:
        return f"{self._get_log_path(session_name)}.lock"

    def log_event(self, session_name: str, event_type: str, data: Dict[str, Any]):
        """
        Appends a structured event to the session's history file.

        event_types:
          - session_created
          - session_terminated
          - execution (code + outputs)
          - input_requested (stdin prompts/replies)
          - file_operation (ls, rm, upload, download)
          - automation (auth, install, drivemount)
        """
        log_path = self._get_log_path(session_name)
        event = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event_type": event_type,
            **data,
        }
        payload = json.dumps(event) + "\n"
        # The foreground CLI and detached keep-alive daemon can append to the
        # same session history concurrently. Plain text-mode append is not a
        # safe cross-process transaction on Windows: concurrent writers can
        # lose records or leave zero-filled gaps in the JSONL file. Serialize
        # each append with a per-session file lock.
        with FileLock(self._get_lock_path(session_name)):
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(payload)
                f.flush()

    def list_sessions(self) -> List[str]:
        if not os.path.exists(self.log_dir):
            return []
        return [f[:-6] for f in os.listdir(self.log_dir) if f.endswith(".jsonl")]

    def get_history(self, session_name: str) -> List[Dict[str, Any]]:
        log_path = self._get_log_path(session_name)
        if not os.path.exists(log_path):
            return []

        history = []
        with FileLock(self._get_lock_path(session_name)):
            with open(log_path, "r", encoding="utf-8") as f:
                for line_number, line in enumerate(f, start=1):
                    # Older Windows builds could leave a zero-filled hole
                    # immediately before an otherwise valid JSON record when
                    # multiple processes appended concurrently. Recover that
                    # known shape in-place while reading so existing history
                    # remains usable without mutating the user's file.
                    cleaned = line.lstrip("\x00").strip()
                    if not cleaned:
                        continue
                    try:
                        history.append(json.loads(cleaned))
                    except json.JSONDecodeError as error:
                        logging.warning(
                            "Skipping malformed history record %s:%d: %s",
                            log_path,
                            line_number,
                            error,
                        )
        return history
