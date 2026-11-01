import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("PYTRADE_NO_TRIAL", None)

from pytrade.trial import (
    TRIAL_HOURS, TrialExpired, TrialState,
    check_trial, load_trial, trial_enabled,
)


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.state = Path(self.directory.name) / "state"
        self.install = Path(self.directory.name) / "install"
        self.state.mkdir(parents=True)
        self.install.mkdir(parents=True)
        self.now = datetime(2026, 11, 1, 9, 0, 0)

    def tearDown(self):
        self.directory.cleanup()

    def test_first_run_starts_trial(self):
        state = load_trial(self.state, self.install, now=self.now)
        self.assertIsNone(state.expired_reason)
        self.assertEqual(state.remaining_text(self.now), "24 小时")
        self.assertTrue((self.state / ".cache.bin").exists())
        self.assertTrue((self.install / ".cache.bin").exists())

    def test_expires_after_24_hours(self):
        load_trial(self.state, self.install, now=self.now)
        later = self.now + timedelta(hours=TRIAL_HOURS)
        state = load_trial(self.state, self.install, now=later)
        self.assertIsNotNone(state.expired_reason)
        with self.assertRaises(TrialExpired):
            check_trial(self.state, self.install, now=later)

    def test_still_valid_before_deadline(self):
        load_trial(self.state, self.install, now=self.now)
        state = check_trial(self.state, self.install, now=self.now + timedelta(hours=TRIAL_HOURS - 1))
        self.assertTrue(state.label().startswith("试用版"))

    def test_clock_rollback_detected(self):
        load_trial(self.state, self.install, now=self.now)
        rolled_back = self.now - timedelta(days=3)
        state = load_trial(self.state, self.install, now=rolled_back)
        self.assertIsNotNone(state.expired_reason)
        self.assertIn("时钟", state.expired_reason)

    def test_small_clock_adjustment_tolerated(self):
        load_trial(self.state, self.install, now=self.now)
        state = load_trial(self.state, self.install, now=self.now - timedelta(minutes=2))
        self.assertIsNone(state.expired_reason)

    def test_tampered_state_file_rejected(self):
        load_trial(self.state, self.install, now=self.now)
        target = self.state / ".cache.bin"
        blob = bytearray(target.read_bytes())
        blob[10] ^= 0xFF
        target.write_bytes(bytes(blob))
        later = self.now + timedelta(minutes=30)
        state = load_trial(self.state, self.install, now=later)
        self.assertIsNone(state.expired_reason)
        self.assertEqual(state.first_run, self.now)
        self.assertGreaterEqual(state.last_run, later.replace(microsecond=0))

    def test_deleting_single_state_file_keeps_trial(self):
        load_trial(self.state, self.install, now=self.now)
        (self.state / ".cache.bin").unlink()
        state = load_trial(self.state, self.install, now=self.now + timedelta(hours=2))
        self.assertIsNone(state.expired_reason)
        self.assertLess(state.first_run, self.now + timedelta(hours=2) - timedelta(hours=1, minutes=59))
        self.assertTrue((self.state / ".cache.bin").exists())

    def test_deleting_both_state_files_restarts_but_install_root_recovers(self):
        load_trial(self.state, self.install, now=self.now)
        (self.state / ".cache.bin").unlink()
        (self.install / ".cache.bin").unlink()
        state = load_trial(self.state, self.install, now=self.now + timedelta(hours=2))
        self.assertEqual(state.first_run, (self.now + timedelta(hours=2)).replace(microsecond=0))

    def test_check_period_updates_last_run(self):
        load_trial(self.state, self.install, now=self.now)
        later = self.now + timedelta(hours=5)
        load_trial(self.state, self.install, now=later)
        state = load_trial(self.state, self.install, now=later + timedelta(minutes=1))
        self.assertGreaterEqual(state.last_run, later)

    def test_no_trial_switch_for_developer(self):
        os.environ["PYTRADE_NO_TRIAL"] = "1"
        try:
            self.assertFalse(trial_enabled())
        finally:
            del os.environ["PYTRADE_NO_TRIAL"]
        self.assertTrue(trial_enabled())

    def test_state_label_format(self):
        state = TrialState(datetime(2026, 11, 1, 9, 0, 0), datetime(2026, 11, 1, 9, 0, 0))
        self.assertEqual(state.remaining_text(datetime(2026, 11, 1, 10, 30, 0)), "22 小时 30 分")
        self.assertEqual(state.remaining_text(datetime(2026, 11, 1, 9, 0, 30)), "23 小时 59 分")
        self.assertEqual(state.remaining_text(datetime(2026, 11, 2, 8, 59, 0)), "2 分钟")
        self.assertEqual(state.remaining_text(datetime(2026, 11, 2, 9, 0, 0)), "已到期限")


if __name__ == "__main__":
    unittest.main()
