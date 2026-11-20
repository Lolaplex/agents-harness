"""Natural-language schedule phrasing -> at/cron."""

import unittest
from datetime import datetime, timezone

from runner.schedule import parse_natural_schedule

_NOW = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)  # Friday


class TestNaturalSchedule(unittest.TestCase):
    def _p(self, text: str) -> dict:
        return parse_natural_schedule(text, base_time=_NOW, timezone_name="UTC")

    def test_every_morning_hour(self):
        self.assertEqual(self._p("every morning 8"), {"cron": "0 8 * * *"})

    def test_every_morning_default(self):
        self.assertEqual(self._p("every morning"), {"cron": "0 8 * * *"})

    def test_every_day_at(self):
        self.assertEqual(self._p("every day at 20:30"), {"cron": "30 20 * * *"})
        self.assertEqual(self._p("daily 8pm"), {"cron": "0 20 * * *"})

    def test_every_weekday(self):
        self.assertEqual(self._p("every monday 9"), {"cron": "0 9 * * 1"})
        self.assertEqual(self._p("friday 18:00"), {"cron": "0 18 * * 5"})
        self.assertEqual(self._p("every sunday"), {"cron": "0 9 * * 0"})

    def test_weekdays_weekends(self):
        self.assertEqual(self._p("weekdays 9"), {"cron": "0 9 * * 1-5"})
        self.assertEqual(self._p("weekends"), {"cron": "0 10 * * 0,6"})

    def test_steps(self):
        self.assertEqual(self._p("every 30 minutes"), {"cron": "*/30 * * * *"})
        self.assertEqual(self._p("every 2 hours"), {"cron": "0 */2 * * *"})
        self.assertEqual(self._p("every minute"), {"cron": "* * * * *"})
        self.assertEqual(self._p("every hour"), {"cron": "0 * * * *"})

    def test_in_relative(self):
        self.assertEqual(self._p("in 10 minutes"), {"at": "+10m"})
        self.assertEqual(self._p("in 2 hours"), {"at": "+2h"})
        self.assertEqual(self._p("in 3 days"), {"at": "+3d"})

    def test_tomorrow(self):
        out = self._p("tomorrow 9")
        self.assertTrue(out["at"].startswith("2026-11-21T09:00"))

    def test_at_next_occurrence(self):
        out = self._p("at 11")  # same day, later
        self.assertTrue(out["at"].startswith("2026-11-20T11:00"))
        out = self._p("8:30pm")
        self.assertTrue(out["at"].startswith("2026-11-20T20:30"))
        out = self._p("at 8")  # already past -> tomorrow
        self.assertTrue(out["at"].startswith("2026-11-21T08:00"))

    def test_unparseable_raises(self):
        for bad in ("", "whenever", "every someday", "every 99 minutes"):
            with self.assertRaises(ValueError, msg=bad):
                self._p(bad)


if __name__ == "__main__":
    unittest.main()
