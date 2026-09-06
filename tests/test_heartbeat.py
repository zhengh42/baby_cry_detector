"""Heartbeat pings to healthchecks.io.

A long outage used to leave one forever-retrying thread per heartbeat interval,
all of which pinged at once when the network came back.
"""
import types
import unittest

import fakes
from fakes import cd


class HeartbeatTests(unittest.TestCase):
    def send(self, interval, fail_attempts):
        """Send one heartbeat under a fake clock; return (attempts, retry_seconds)."""
        clock = fakes.FakeClock()
        attempts = {'count': 0}

        def urlopen(url, timeout=None):
            attempts['count'] += 1
            if attempts['count'] <= fail_attempts:
                raise OSError("network is unreachable")
            return types.SimpleNamespace(read=lambda: b'OK')

        detector = fakes.make_detector(
            healthcheck_url='https://hc-ping.com/test-uuid',
            heartbeat_interval=interval)
        fake_urllib = types.SimpleNamespace(
            request=types.SimpleNamespace(urlopen=urlopen))
        original_urllib = cd.urllib
        cd.urllib = fake_urllib
        try:
            with fakes.patched_clock(cd, clock), fakes.captured_output():
                workers = fakes.run_and_join(cd, detector.send_heartbeat)
        finally:
            cd.urllib = original_urllib
        self.assertEqual(len(workers), 1, "one heartbeat thread per ping")
        self.assertFalse(workers[0].is_alive(), "the heartbeat thread should have exited")
        return attempts['count'], clock.elapsed()

    def test_a_reachable_endpoint_pings_once(self):
        attempts, elapsed = self.send(interval=300, fail_attempts=0)
        self.assertEqual(attempts, 1)
        self.assertEqual(elapsed, 0)

    def test_retries_stop_before_the_next_heartbeat_is_due(self):
        attempts, elapsed = self.send(interval=300, fail_attempts=10 ** 6)
        self.assertLess(elapsed, 300,
                        "retrying past the interval would stack up threads")
        self.assertLessEqual(attempts, 10)
        self.assertGreater(attempts, 1)

    def test_longer_intervals_allow_proportionally_more_retries(self):
        few, _ = self.send(interval=60, fail_attempts=10 ** 6)
        many, _ = self.send(interval=600, fail_attempts=10 ** 6)
        self.assertGreater(many, few)

    def test_a_short_interval_does_not_retry_at_all(self):
        attempts, _ = self.send(interval=30, fail_attempts=10 ** 6)
        self.assertEqual(attempts, 1)

    def test_recovery_mid_retry_is_reported(self):
        attempts, elapsed = self.send(interval=300, fail_attempts=3)
        self.assertEqual(attempts, 4)
        self.assertGreater(elapsed, 0)

    def test_failure_pings_the_fail_endpoint(self):
        seen = []

        def urlopen(url, timeout=None):
            seen.append(url)
            return types.SimpleNamespace(read=lambda: b'OK')

        detector = fakes.make_detector(healthcheck_url='https://hc-ping.com/test-uuid')
        detector.stream_ok = False
        original_urllib = cd.urllib
        cd.urllib = types.SimpleNamespace(
            request=types.SimpleNamespace(urlopen=urlopen))
        try:
            with fakes.captured_output():
                fakes.run_and_join(cd, detector.send_heartbeat)
        finally:
            cd.urllib = original_urllib
        self.assertTrue(seen[0].endswith('/fail'),
                        "a dead stream must ping /fail so healthchecks escalates")


if __name__ == '__main__':
    unittest.main()
