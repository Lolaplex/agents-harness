"""Unit tests for runner.schedule."""

from __future__ import annotations

import datetime as dt
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from runner.schedule import (
    add_schedule,
    list_dynamic_schedules,
    remove_schedule,
    tick,
    parse_due_time,
    is_cron_due,
)


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_schedules_")
        self.old_env = os.environ.get("AGENTS_SCHEDULES_DIR")
        os.environ["AGENTS_SCHEDULES_DIR"] = self.temp_dir

    def tearDown(self):
        if self.old_env is not None:
            os.environ["AGENTS_SCHEDULES_DIR"] = self.old_env
        else:
            os.environ.pop("AGENTS_SCHEDULES_DIR", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_parse_due_time(self):
        base = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_due_time("+10m", base), base + timedelta(minutes=10))
        self.assertEqual(parse_due_time("+2h", base), base + timedelta(hours=2))
        self.assertEqual(parse_due_time("+1d", base), base + timedelta(days=1))
        self.assertEqual(parse_due_time("30s", base), base + timedelta(seconds=30))
        
        iso = parse_due_time("2026-09-05T14:30:00Z")
        self.assertEqual(iso.hour, 14)
        self.assertEqual(iso.minute, 30)

    def test_is_cron_due(self):
        now = datetime(2026, 9, 5, 8, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(is_cron_due("0 8 * * *", now))
        self.assertTrue(is_cron_due("*/10 * * * *", now))
        self.assertFalse(is_cron_due("30 8 * * *", now))
        self.assertFalse(is_cron_due("0 9 * * *", now))

    def test_add_and_list_schedule(self):
        item = add_schedule(
            name="test_rem",
            at="+10m",
            text="Call dentist",
            channel="telegram",
            user="5712534571",
        )
        self.assertEqual(item["name"], "test_rem")
        self.assertTrue(item.get("one_shot"))
        self.assertIn("Call dentist", item["verb"])

        rows = list_dynamic_schedules()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "test_rem")

    def test_remove_schedule(self):
        add_schedule(name="to_delete", at="+1h", text="dummy")
        self.assertTrue(remove_schedule("to_delete"))
        self.assertFalse(remove_schedule("to_delete"))
        self.assertEqual(len(list_dynamic_schedules()), 0)

    def test_tick_executes_and_cleans_one_shot(self):
        base = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
        # Add past task with healthy quick python print verb
        add_schedule(
            name="past_task",
            at="2026-09-05T11:59:00Z",
            verb="python -c \"print('ran past')\"",
            one_shot=True,
        )
        # Add future task
        add_schedule(
            name="future_task",
            at="2026-09-05T12:30:00Z",
            verb="python -c \"print('ran future')\"",
            one_shot=True,
        )

        executed = tick(base_time=base)
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]["schedule"], "past_task")
        self.assertEqual(executed[0]["result"]["status"], "SUCCESS")

        remaining = list_dynamic_schedules()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["name"], "future_task")


if __name__ == "__main__":
    unittest.main()
