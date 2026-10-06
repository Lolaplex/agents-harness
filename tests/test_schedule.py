"""Unit tests for runner.schedule."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import os
import shutil
import tempfile
import unittest

import threading

from runner.schedule import (
    add_schedule,
    list_dynamic_schedules,
    register_routine_handler,
    remove_schedule,
    tick,
    parse_due_time,
    is_cron_due,
    wait_for_background_ticks,
)


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_schedules_")
        self.old_env = os.environ.get("AGENTS_SCHEDULES_DIR")
        os.environ["AGENTS_SCHEDULES_DIR"] = self.temp_dir

    def tearDown(self):
        wait_for_background_ticks()
        if self.old_env is not None:
            os.environ["AGENTS_SCHEDULES_DIR"] = self.old_env
        else:
            os.environ.pop("AGENTS_SCHEDULES_DIR", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        register_routine_handler(None)

    def test_parse_due_time(self):
        base = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_due_time("+10m", base), base + timedelta(minutes=10))
        self.assertEqual(parse_due_time("+2h", base), base + timedelta(hours=2))
        self.assertEqual(parse_due_time("+1d", base), base + timedelta(days=1))
        self.assertEqual(parse_due_time("30s", base), base + timedelta(seconds=30))

        iso = parse_due_time("2026-09-05T14:30:00Z")
        self.assertEqual(iso.hour, 14)
        self.assertEqual(iso.minute, 30)

    def test_naive_iso_uses_iana_timezone(self):
        berlin = parse_due_time("2026-09-07T17:20:00", timezone_name="Europe/Berlin")
        self.assertEqual(berlin, datetime(2026, 9, 7, 15, 20, tzinfo=timezone.utc))
        zulu = parse_due_time("2026-09-07T17:20:00Z", timezone_name="Europe/Berlin")
        self.assertEqual(zulu, datetime(2026, 9, 7, 17, 20, tzinfo=timezone.utc))

    def test_add_and_list_schedule(self):
        item = add_schedule(
            name="test_rem",
            at="+10m",
            text="Call dentist",
            channel="http",
            user="user1",
        )
        self.assertEqual(item["name"], "test_rem")
        self.assertTrue(item.get("one_shot"))
        self.assertIn("Call dentist", item["verb"])
        self.assertIn("--user user1", item["verb"])
        self.assertNotIn("telegram", item["verb"])

        rows = list_dynamic_schedules()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "test_rem")

    def test_text_without_channel_or_user_fails(self):
        with self.assertRaises(ValueError):
            add_schedule(name="no_target", at="+10m", text="hello")

    def test_is_cron_due(self):
        now = datetime(2026, 9, 5, 8, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(is_cron_due("0 8 * * *", now))
        self.assertTrue(is_cron_due("*/10 * * * *", now))
        self.assertFalse(is_cron_due("30 8 * * *", now))
        self.assertFalse(is_cron_due("0 9 * * *", now))

    def test_remove_schedule(self):
        add_schedule(name="to_delete", at="+1h", verb='python -c "print(1)"')
        self.assertTrue(remove_schedule("to_delete"))
        self.assertFalse(remove_schedule("to_delete"))
        self.assertEqual(len(list_dynamic_schedules()), 0)

    def test_tick_executes_and_cleans_one_shot(self):
        base = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
        add_schedule(
            name="past_task",
            at="2026-09-05T11:59:00Z",
            verb="python -c \"print('ran past')\"",
            one_shot=True,
        )
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

    def test_text_reminder_timeout_defaults_to_300(self):
        item = add_schedule(name="rem", at="+10m", text="hi", channel="http", user="1")
        self.assertEqual(item["timeout_sec"], 300)
        self.assertTrue(item.get("timezone"))

    def test_cron_uses_job_timezone(self):
        add_schedule(
            name="berlin_morning",
            cron="0 9 * * *",
            verb="python -c \"print('berlin')\"",
            timezone_name="Europe/Berlin",
        )
        # 07:00 UTC is 09:00 in Berlin (CEST). 09:00 UTC is 11:00 there.
        fired = tick(base_time=datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc))
        self.assertEqual([row["schedule"] for row in fired], ["berlin_morning"])
        self.assertEqual(fired[0]["result"]["status"], "SUCCESS")
        later = tick(base_time=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(later, [])

    def test_same_minute_and_grace_window(self):
        add_schedule(
            name="hourly",
            cron="0 * * * *",
            verb="python -c \"print('slot')\"",
            timezone_name="UTC",
        )
        now = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(len(tick(base_time=now)), 1)
        self.assertEqual(tick(base_time=now + timedelta(seconds=20)), [])

        add_schedule(
            name="missed",
            cron="15 8 * * *",
            verb="python -c \"print('grace')\"",
            timezone_name="UTC",
        )
        late = tick(base_time=datetime(2026, 9, 5, 8, 18, tzinfo=timezone.utc))
        self.assertEqual([row["schedule"] for row in late], ["missed"])
        self.assertEqual(tick(base_time=datetime(2026, 9, 5, 8, 19, tzinfo=timezone.utc)), [])

        add_schedule(
            name="too_late",
            cron="30 8 * * *",
            verb="python -c \"print('nope')\"",
            timezone_name="UTC",
        )
        self.assertEqual(tick(base_time=datetime(2026, 9, 5, 8, 36, tzinfo=timezone.utc)), [])

    def test_parallel_ticks_fire_once(self):
        add_schedule(
            name="slow",
            cron="0 * * * *",
            verb="python -c \"import time; time.sleep(0.4); print('once')\"",
            timezone_name="UTC",
        )
        now = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)
        batches: list[list] = []

        def _run() -> None:
            batches.append(tick(base_time=now))

        threads = [threading.Thread(target=_run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        fired = [item for batch in batches for item in batch]
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["result"]["status"], "SUCCESS")

    def test_routine_handler_and_skip_without_one(self):
        seen = []

        def handler(job):
            seen.append(job)
            return {"status": "SUCCESS", "stdout_tail": "ran"}

        register_routine_handler(handler)
        add_schedule(
            name="digest",
            cron="0 8 * * *",
            prompt="check mail",
            user="42",
            timezone_name="UTC",
        )
        fired = tick(base_time=datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["kind"], "routine")
        self.assertEqual(seen[0]["prompt"], "check mail")
        self.assertEqual(seen[0]["user"], "42")
        self.assertEqual(seen[0]["session"], "routine:digest")
        self.assertEqual(seen[0]["timeout_sec"], 300)
        self.assertEqual(fired[0]["result"]["status"], "SUCCESS")

        register_routine_handler(None)
        add_schedule(
            name="orphan",
            cron="0 9 * * *",
            prompt="nobody home",
            user="42",
            timezone_name="UTC",
        )
        skipped = tick(base_time=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(skipped[0]["result"]["status"], "SKIPPED")
        self.assertEqual(len(list_dynamic_schedules()), 2)
        again = tick(base_time=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(again[0]["schedule"], "orphan")
        self.assertEqual(again[0]["result"]["status"], "SKIPPED")

    def test_skipped_one_shot_routine_is_not_consumed(self):
        add_schedule(
            name="once",
            at="2026-09-05T11:00:00Z",
            prompt="ping",
            user="7",
            timezone_name="UTC",
        )
        when = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        first = tick(base_time=when)
        self.assertEqual(first[0]["result"]["status"], "SKIPPED")
        self.assertEqual([row["name"] for row in list_dynamic_schedules()], ["once"])
        second = tick(base_time=when)
        self.assertEqual(second[0]["result"]["status"], "SKIPPED")
        self.assertEqual([row["name"] for row in list_dynamic_schedules()], ["once"])

    def test_handler_skip_does_not_consume_slot(self):
        calls = []

        def handler(job):
            calls.append(job["name"])
            return {"status": "SKIPPED"}

        register_routine_handler(handler)
        add_schedule(
            name="digest",
            cron="0 8 * * *",
            prompt="check",
            user="7",
            timezone_name="UTC",
        )
        when = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(tick(base_time=when)[0]["result"]["status"], "SKIPPED")
        self.assertEqual(tick(base_time=when)[0]["result"]["status"], "SKIPPED")
        self.assertEqual(calls, ["digest", "digest"])

    def test_slow_routine_does_not_hold_tick_lock(self):
        import time

        holder: dict = {}

        def handler(job):
            if job["name"] != "slow":
                return {"status": "SUCCESS"}

            def _inner() -> None:
                holder["rows"] = tick(base_time=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc))

            thread = threading.Thread(target=_inner)
            thread.start()
            thread.join(2)
            holder["alive"] = thread.is_alive()
            return {"status": "SUCCESS"}

        register_routine_handler(handler)
        add_schedule(
            name="slow",
            cron="0 8 * * *",
            prompt="long",
            user="7",
            timezone_name="UTC",
        )
        add_schedule(
            name="other",
            cron="0 9 * * *",
            verb="python -c \"print('other')\"",
            timezone_name="UTC",
        )
        fired = tick(base_time=datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc))
        self.assertFalse(holder.get("alive"), "inner tick blocked on the outer tick lock")
        inner_names = [row["schedule"] for row in holder.get("rows") or []]
        self.assertIn("other", inner_names)
        self.assertEqual(fired[0]["schedule"], "slow")
        self.assertEqual(fired[0]["result"]["status"], "SUCCESS")

    def test_due_jobs_run_in_parallel(self):
        import time

        order: list[tuple[str, str]] = []

        def handler(job):
            order.append(("start", job["name"]))
            if job["name"] == "a_slow":
                time.sleep(0.35)
            order.append(("end", job["name"]))
            return {"status": "SUCCESS"}

        register_routine_handler(handler)
        add_schedule(
            name="a_slow",
            cron="0 8 * * *",
            prompt="slow",
            user="7",
            timezone_name="UTC",
        )
        add_schedule(
            name="b_fast",
            cron="0 8 * * *",
            prompt="fast",
            user="7",
            timezone_name="UTC",
        )
        tick(base_time=datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc))
        self.assertLess(order.index(("end", "b_fast")), order.index(("end", "a_slow")))

    def test_per_job_timeout(self):
        add_schedule(
            name="tight",
            cron="0 * * * *",
            verb="python -c \"import time; time.sleep(3)\"",
            timezone_name="UTC",
            timeout_sec=1,
        )
        fired = tick(base_time=datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc))
        self.assertEqual(fired[0]["result"]["status"], "TIMEOUT")

    def test_failed_one_shot_is_logged_and_not_retried(self):
        import io
        import json
        from contextlib import redirect_stderr
        from pathlib import Path

        add_schedule(
            name="boom",
            at="2026-09-05T11:59:00Z",
            verb='python -c "import sys; sys.exit(3)"',
            one_shot=True,
        )
        when = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        err = io.StringIO()
        with redirect_stderr(err):
            fired = tick(base_time=when)
        self.assertEqual(fired[0]["result"]["status"], "FAILED")
        self.assertEqual(list_dynamic_schedules(), [])
        state = json.loads((Path(self.temp_dir) / "tick-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["jobs"]["boom"]["last_result"]["status"], "FAILED")
        self.assertIn("schedule 'boom' FAILED", err.getvalue())
        self.assertEqual(tick(base_time=when), [])

    def test_tick_wait_false_returns_after_claim(self):
        import time

        add_schedule(
            name="slow",
            cron="0 * * * *",
            verb='python -c "import time; time.sleep(0.4); print(\'once\')"',
            timezone_name="UTC",
        )
        when = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)
        started = time.perf_counter()
        rows = tick(base_time=when, wait=False)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.25)
        self.assertEqual(rows[0]["result"]["status"], "CLAIMED")
        self.assertEqual(tick(base_time=when, wait=False), [])
        wait_for_background_ticks()
        self.assertEqual(tick(base_time=when), [])


if __name__ == "__main__":
    unittest.main()
