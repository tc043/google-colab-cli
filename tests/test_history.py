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

import json
import multiprocessing
import tempfile
import shutil
import unittest
from colab_cli.history import HistoryLogger


def _write_history_events(log_dir: str, worker: int, count: int):
    logger = HistoryLogger(log_dir=log_dir)
    for index in range(count):
        logger.log_event("shared", "event", {"worker": worker, "index": index})


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.logger = HistoryLogger(log_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_log_and_get_history(self):
        self.logger.log_event("test-session", "session_created", {"variant": "DEFAULT"})
        self.logger.log_event(
            "test-session", "execution", {"code": "print(1)", "outputs": []}
        )

        history = self.logger.get_history("test-session")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["event_type"], "session_created")
        self.assertEqual(history[1]["event_type"], "execution")
        self.assertEqual(history[1]["code"], "print(1)")

    def test_list_sessions(self):
        self.logger.log_event("s1", "event", {})
        self.logger.log_event("s2", "event", {})

        sessions = self.logger.list_sessions()
        self.assertIn("s1", sessions)
        self.assertIn("s2", sessions)
        self.assertEqual(len(sessions), 2)

    def test_get_history_recovers_leading_nul_corruption(self):
        path = self.logger._get_log_path("test-session")
        event = {"timestamp": "2026-09-12T00:00:00+00:00", "event_type": "console_started"}
        with open(path, "wb") as f:
            f.write(b"\x00" * 128)
            f.write(json.dumps(event).encode("utf-8") + b"\n")

        history = self.logger.get_history("test-session")

        self.assertEqual(history, [event])

    def test_get_history_skips_unrecoverable_bad_line(self):
        path = self.logger._get_log_path("test-session")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"timestamp":"t1","event_type":"ok"}\n')
            f.write("definitely-not-json\n")
            f.write('{"timestamp":"t2","event_type":"ok2"}\n')

        history = self.logger.get_history("test-session")

        self.assertEqual([event["event_type"] for event in history], ["ok", "ok2"])

    def test_concurrent_process_writes_remain_valid_jsonl(self):
        workers = 4
        events_per_worker = 50
        processes = [
            multiprocessing.Process(
                target=_write_history_events,
                args=(self.test_dir, worker, events_per_worker),
            )
            for worker in range(workers)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            self.assertEqual(process.exitcode, 0)

        path = self.logger._get_log_path("shared")
        with open(path, "r", encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]

        self.assertEqual(len(lines), workers * events_per_worker)
        parsed = [json.loads(line) for line in lines]
        self.assertEqual(len(parsed), workers * events_per_worker)


if __name__ == "__main__":
    unittest.main()
