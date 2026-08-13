import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.database import Database
from app.web import seconds_until_collection, serve


class SchedulerTests(unittest.TestCase):
    def test_collection_job_allows_one_interval_of_late_coalesced_execution(self):
        captured = {}

        class FakeScheduler:
            def __init__(self, **_kwargs):
                pass

            def add_job(self, _func, _trigger, **kwargs):
                captured.update(kwargs)

            def start(self):
                pass

            def shutdown(self, **_kwargs):
                pass

        fake_app = SimpleNamespace(
            config={"background_collect": lambda: None, "comment_service": None},
            run=lambda **_kwargs: None,
        )
        settings = SimpleNamespace(raw={"app": {"interval_minutes": 60, "collect_on_startup": False}})
        database = SimpleNamespace(
            get_collection_interval_minutes=lambda _default: 60,
            latest_completed_run_at=lambda: None,
        )

        with (
            patch("app.web.create_app", return_value=fake_app),
            patch("app.web.BackgroundScheduler", FakeScheduler),
        ):
            serve(settings, database)

        self.assertTrue(captured["coalesce"])
        self.assertGreaterEqual(captured.get("misfire_grace_time", 0), 60 * 60)

    def test_empty_or_stale_database_collects_immediately(self):
        now = datetime(2026, 8, 9, 16, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(seconds_until_collection(None, 30, now), 0)
        self.assertEqual(
            seconds_until_collection((now - timedelta(minutes=31)).isoformat(), 30, now),
            0,
        )

    def test_fresh_database_waits_only_for_remaining_interval(self):
        now = datetime(2026, 8, 9, 16, 0, tzinfo=timezone(timedelta(hours=8)))
        delay = seconds_until_collection(
            (now - timedelta(minutes=12, seconds=30)).isoformat(),
            30,
            now,
        )
        self.assertEqual(delay, 17.5 * 60)

    def test_database_returns_latest_completed_collection_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "scheduler.db")
            first = database.begin_run()
            database.finish_run(first, 1, 1, [])
            expected = database.latest_completed_run_at()
            second = database.begin_run()
            self.assertIsNotNone(second)
            self.assertEqual(database.latest_completed_run_at(), expected)


if __name__ == "__main__":
    unittest.main()
