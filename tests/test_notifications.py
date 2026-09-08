"""Emergency notification delivery.

The alert has to survive a network that is briefly down. Pushover's own
retry/expire only apply once its servers hold the message, so a failed POST used
to throw the alert away and wait a full alert_window before trying again - and,
because the library posts with no timeout, it could park the monitor loop in a
connect while it did so.
"""
import threading
import types
import unittest

import fakes
from fakes import cd


class FlakyPushover:
    """A Pushover client whose first ``fail_attempts`` sends fail."""

    def __init__(self, fail_attempts=0):
        self.fail_attempts = fail_attempts
        self.attempts = 0
        self.sent = []

    def send_message(self, message, **kwargs):
        self.attempts += 1
        if self.attempts <= self.fail_attempts:
            raise OSError("network is unreachable")
        self.sent.append((message, kwargs))


class StalledPushover:
    """A client that never returns, as an untimed POST on a half-dead network."""

    def __init__(self):
        self.released = threading.Event()
        self.attempts = 0

    def send_message(self, message, **kwargs):
        self.attempts += 1
        # outlasts the per-attempt bound the test sets, without stalling teardown
        self.released.wait(0.5)


class ThreadRecordingPushover:
    """Records which thread each send runs on."""

    def __init__(self):
        self.threads = []

    def send_message(self, message, **kwargs):
        self.threads.append(threading.current_thread())


class NotificationDeliveryTests(unittest.TestCase):
    def alert(self, client, alert_window=720, healthcheck_url=None):
        """Fire one emergency alert; return (workers, output)."""
        self.clock = fakes.FakeClock()
        detector = fakes.make_detector(alert_window=alert_window,
                                       healthcheck_url=healthcheck_url)
        detector.enable_pushover = True
        detector.pushover_client = client
        with fakes.patched_clock(cd, self.clock), fakes.captured_output() as buffer:
            workers = fakes.run_and_join(
                cd, lambda: detector.send_emergency_notification(720))
        return workers, fakes.strip_ansi(buffer.getvalue())

    def test_a_reachable_endpoint_sends_once(self):
        client = FlakyPushover()
        workers, output = self.alert(client)
        self.assertEqual(client.attempts, 1)
        # the retry loop, plus the bounded worker it waits on for one attempt
        self.assertEqual(len(workers), 2, "the send must not block the monitor loop")
        self.assertIn("Emergency notification sent", output)

    def test_a_brief_outage_still_delivers_the_alert(self):
        # the whole point: a blip at the wrong moment used to drop the alert and
        # wait another full alert_window of crying before trying again
        client = FlakyPushover(fail_attempts=3)
        _, output = self.alert(client)
        self.assertEqual(client.attempts, 4)
        self.assertEqual(len(client.sent), 1, "the alert must reach the phone")
        self.assertIn("Emergency notification sent", output)
        self.assertIn("after 3 failed tries", output)

    def test_a_delayed_alert_says_that_it_is_delayed(self):
        client = FlakyPushover(fail_attempts=4)
        self.alert(client)
        message = client.sent[0][0]
        self.assertIn("Alert delayed", message,
                      "a late alert must not read as current")
        self.assertIn("2 min", message)

    def test_an_immediate_alert_is_not_labelled_delayed(self):
        client = FlakyPushover()
        self.alert(client)
        self.assertNotIn("delayed", client.sent[0][0])

    def test_retries_stop_before_the_next_alert_could_fire(self):
        client = FlakyPushover(fail_attempts=10 ** 6)
        workers, output = self.alert(client, alert_window=720)
        self.assertLess(self.clock.elapsed(), 720,
                        "retrying past the alert window would stack up threads")
        self.assertGreater(client.attempts, 1)
        self.assertFalse(workers[0].is_alive())
        self.assertIn("Emergency notification FAILED", output)

    def test_a_short_alert_window_does_not_retry_at_all(self):
        client = FlakyPushover(fail_attempts=10 ** 6)
        self.alert(client, alert_window=30)
        self.assertEqual(client.attempts, 1)

    def test_giving_up_escalates_through_healthchecks(self):
        seen = []
        original = cd.urllib
        cd.urllib = types.SimpleNamespace(request=types.SimpleNamespace(
            urlopen=lambda url, timeout=None: seen.append(url)))
        try:
            self.alert(FlakyPushover(fail_attempts=10 ** 6), alert_window=60,
                       healthcheck_url='https://hc-ping.com/test-uuid')
        finally:
            cd.urllib = original
        self.assertTrue(seen and seen[0].endswith('/fail'),
                        "an undelivered alert must surface somewhere")

    def test_muting_while_retrying_abandons_the_alert(self):
        # the duty switch is live config; a parent who mutes mid-retry must not
        # get a priority-2 alert 30 seconds later
        client = FlakyPushover(fail_attempts=1)
        self.clock = fakes.FakeClock()
        detector = fakes.make_detector(alert_window=720)
        detector.enable_pushover = True
        detector.pushover_client = client
        original_sleep = self.clock.sleep

        def mute_during_the_retry_wait(seconds):
            detector.pushover_device = '__muted__'
            original_sleep(seconds)

        self.clock.sleep = mute_during_the_retry_wait
        with fakes.patched_clock(cd, self.clock), fakes.captured_output() as buffer:
            fakes.run_and_join(cd, lambda: detector.send_emergency_notification(720))
        output = fakes.strip_ansi(buffer.getvalue())
        self.assertEqual(client.sent, [], "a muted alert must not be delivered")
        self.assertIn("muted while retrying", output)

    def test_a_duty_switch_while_retrying_reaches_the_new_phone(self):
        client = FlakyPushover(fail_attempts=1)
        self.clock = fakes.FakeClock()
        detector = fakes.make_detector(alert_window=720)
        detector.enable_pushover = True
        detector.pushover_client = client
        detector.pushover_device = 'mum_phone'
        original_sleep = self.clock.sleep

        def switch_during_the_retry_wait(seconds):
            detector.pushover_device = 'dad_phone'
            original_sleep(seconds)

        self.clock.sleep = switch_during_the_retry_wait
        with fakes.patched_clock(cd, self.clock), fakes.captured_output():
            fakes.run_and_join(cd, lambda: detector.send_emergency_notification(720))
        self.assertEqual(client.sent[0][1]['device'], 'dad_phone',
                         "the alert should follow whoever is on duty now")

    def test_a_stalled_send_is_abandoned_rather_than_waited_on(self):
        # without a per-attempt bound the worker parks inside the library, the
        # deadline is never reached and the give-up escalation never fires
        client = StalledPushover()
        original_timeout = cd.PUSHOVER_SEND_TIMEOUT
        cd.PUSHOVER_SEND_TIMEOUT = 0.05
        try:
            _, output = self.alert(client, alert_window=60)
        finally:
            cd.PUSHOVER_SEND_TIMEOUT = original_timeout
            client.released.set()
        self.assertIn("Emergency notification FAILED", output)
        self.assertIn("no response after", output)

    def test_a_short_delay_is_reported_in_seconds(self):
        # "(Alert delayed 0 min)" is what minutes-only formatting produced
        client = FlakyPushover(fail_attempts=1)
        self.alert(client)
        message = client.sent[0][0]
        self.assertIn("Alert delayed 30 sec", message)
        self.assertNotIn("delayed 0 min", message)

    def test_a_transient_failure_does_not_cry_wolf_to_healthchecks(self):
        seen = []
        original = cd.urllib
        cd.urllib = types.SimpleNamespace(request=types.SimpleNamespace(
            urlopen=lambda url, timeout=None: seen.append(url)))
        try:
            self.alert(FlakyPushover(fail_attempts=2),
                       healthcheck_url='https://hc-ping.com/test-uuid')
        finally:
            cd.urllib = original
        self.assertEqual(seen, [], "the alert got through; nothing to escalate")


class MonitorKeepsListeningTests(unittest.TestCase):
    """The loop must not wait on the network to alert."""

    def test_the_send_never_runs_on_the_monitor_loop(self):
        """The property the fix rests on, asserted where it cannot be faked.

        A hanging send cannot be simulated under the fake clock - a background
        thread sleeping on it advances the same clock the loop reads - so assert
        the structural guarantee instead: the send happens on some other thread,
        which is what stops an untimed POST from parking the loop at all.
        """
        clock = fakes.FakeClock()
        source = fakes.AudioSource(clock=clock).play([(30, fakes.cry())])
        detector = fakes.make_detector(source=source, min_cry_duration=4,
                                       alert_window=10, reset_window=300)
        detector.enable_pushover = True
        client = ThreadRecordingPushover()
        detector.pushover_client = client

        def run():
            detector.start()
            detector.monitor()

        with fakes.patched_clock(cd, clock), fakes.captured_output() as buffer:
            workers = fakes.run_and_join(cd, run)
        output = fakes.strip_ansi(buffer.getvalue())

        self.assertIn("ALERT!", output)
        self.assertTrue(client.threads, "the alert was never sent")
        self.assertGreaterEqual(len(workers), 1)
        self.assertNotIn(threading.main_thread(), client.threads,
                         "sending on the monitor loop lets a stuck network "
                         "stop the detector from listening")


if __name__ == '__main__':
    unittest.main()
