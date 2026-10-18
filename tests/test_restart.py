"""Replacing a running detector: every morning, and from the phone.

A detector left listening for days outlives the things under it - the USB mic
drops off the bus, ALSA hands back a stream that reads silence - so one is
replaced on a schedule rather than when someone finally notices.

The theme is restraint. A restart is a convenience, monitoring is not: it must
never start a session a parent ended, never interrupt a baby who is crying
towards an alarm, and never leave the mic down quietly if the new detector
cannot be started. The stop and the start are also one step under one lock,
whoever asked for it: in the gap between them there is no detector and nothing
to stop a second phone from starting one that the restart then replaces.
"""
import contextlib
import subprocess
import unittest
from datetime import datetime, timedelta

import fakes

RUN_FOREVER = "import time; time.sleep(60)"

AT = '08:00'
BEFORE = datetime(2026, 1, 1, 7, 59, 40)
DUE = datetime(2026, 1, 1, 8, 0, 10)
LATER_IN_MINUTE = datetime(2026, 1, 1, 8, 0, 50)
AFTER = datetime(2026, 1, 1, 8, 5, 0)
TOMORROW = datetime(2026, 1, 2, 8, 0, 10)


class DailyRestartTestCase(fakes.WebControllerTestCase):
    def setUp(self):
        super().setUp()
        # the real restart waits for ALSA to let go of the mic; tests must not
        self._settle = self.wc.RESTART_SETTLE_SECONDS
        self.wc.RESTART_SETTLE_SECONDS = 0
        self._crying = self.wc.detector_is_crying
        self.set_crying(False)
        self.wc.current_config['enable_restart_at'] = True
        self.wc.current_config['restart_at'] = AT
        self.starts = []

    def tearDown(self):
        self.wc.RESTART_SETTLE_SECONDS = self._settle
        self.wc.detector_is_crying = self._crying
        super().tearDown()

    def set_crying(self, crying):
        """Script what the detector's /status endpoint would say."""
        self.wc.detector_is_crying = lambda: crying

    def plant_running_detector(self):
        """A live detector the controller is tracking, started long ago."""
        process = self.spawn(RUN_FOREVER)
        self.wc.detector_process = process
        self.wc.detector_started_at = datetime.now() - timedelta(hours=8)
        self.wc.save_detector_state(True)
        return process

    @contextlib.contextmanager
    def stand_in_successor(self):
        """Make the next Popen hand back a live stand-in process.

        Spawned up front: spawning it inside the stand-in would recurse, since
        the test helper that spawns processes goes through Popen itself.
        """
        successor = self.spawn(RUN_FOREVER)
        original_popen = subprocess.Popen
        subprocess.Popen = lambda *a, **k: successor
        try:
            yield successor
        finally:
            subprocess.Popen = original_popen

    def poll(self, now):
        """One tick of the scheduler, with a real start that is stubbed out."""
        def fake_start():
            self.starts.append(now)
            self.wc.detector_process = self.spawn(RUN_FOREVER)
            self.wc.detector_started_at = now
            self.wc.save_detector_state(True)
            return True, 'started'

        original = self.wc.start_detector
        self.wc.start_detector = fake_start
        try:
            return self.wc.maybe_run_daily_restart(now)
        finally:
            self.wc.start_detector = original


class WhenItFiresTests(DailyRestartTestCase):
    def test_a_running_session_is_restarted_at_the_restart_time(self):
        old = self.plant_running_detector()
        self.assertTrue(self.poll(DUE))
        self.assertEqual(len(self.starts), 1)
        self.assertIsNotNone(old.poll(), "the old detector should be gone")

    def test_nothing_happens_before_the_restart_time(self):
        self.plant_running_detector()
        self.assertFalse(self.poll(BEFORE))
        self.assertEqual(self.starts, [])

    def test_a_minute_that_was_stepped_over_is_not_restarted_later(self):
        # the controller may have been down, or the clock jumped forward after a
        # power cut; a restart hours late would land in the middle of a night
        self.plant_running_detector()
        self.assertFalse(self.poll(AFTER))
        self.assertEqual(self.starts, [])

    def test_it_fires_once_however_often_the_minute_is_polled(self):
        self.plant_running_detector()
        self.poll(DUE)
        self.wc.detector_started_at = DUE - timedelta(hours=8)  # not just-started
        self.assertFalse(self.poll(LATER_IN_MINUTE))
        self.assertEqual(len(self.starts), 1)

    def test_it_fires_again_the_next_morning(self):
        self.plant_running_detector()
        self.poll(DUE)
        self.wc.detector_started_at = TOMORROW - timedelta(hours=8)
        self.assertTrue(self.poll(TOMORROW))
        self.assertEqual(len(self.starts), 2)

    def test_turning_it_off_stops_the_restarts(self):
        self.plant_running_detector()
        self.wc.current_config['enable_restart_at'] = False
        self.assertFalse(self.poll(DUE))
        self.assertEqual(self.starts, [])

    def test_a_retimed_restart_fires_at_its_new_minute_the_same_day(self):
        self.plant_running_detector()
        self.poll(DUE)
        self.wc.current_config['restart_at'] = '09:00'
        self.wc.detector_started_at = datetime(2026, 1, 1, 1, 0)
        self.assertTrue(self.poll(datetime(2026, 1, 1, 9, 0, 5)))
        self.assertEqual(len(self.starts), 2)


class RestraintTests(DailyRestartTestCase):
    def test_a_stopped_monitor_is_not_started(self):
        # the opposite of a stop-at: this keeps a session fresh, it does not
        # begin one, so a night ended on purpose stays ended
        self.assertFalse(self.poll(DUE))
        self.assertEqual(self.starts, [])

    def test_a_stopped_monitor_is_not_started_later_in_the_minute_either(self):
        self.poll(DUE)
        self.assertFalse(self.poll(LATER_IN_MINUTE))
        self.assertEqual(self.starts, [])

    def test_a_session_that_just_started_is_left_alone(self):
        # pressing START at 07:59:50, or a boot resume landing there, is already
        # as fresh as a restart could make it
        self.plant_running_detector()
        self.wc.detector_started_at = DUE - timedelta(seconds=5)
        self.assertFalse(self.poll(DUE))
        self.assertEqual(self.starts, [])

    def test_a_crying_baby_is_not_interrupted(self):
        # restarting mid-episode resets the alert timer, pushing the alarm back
        # by a whole alert window for a baby who is already crying
        self.plant_running_detector()
        self.set_crying(True)
        self.assertFalse(self.poll(DUE))
        self.assertEqual(self.starts, [])
        self.assertTrue(any('deferred' in event for event in self.events()))

    def test_the_deferred_restart_runs_once_the_baby_settles(self):
        self.plant_running_detector()
        self.set_crying(True)
        self.poll(DUE)
        self.set_crying(False)
        self.assertTrue(self.poll(DUE + timedelta(minutes=4)),
                        "the restart should still happen, just not mid-episode")
        self.assertEqual(len(self.starts), 1)

    def test_the_deferral_is_announced_only_once(self):
        self.plant_running_detector()
        self.set_crying(True)
        self.poll(DUE)
        self.poll(DUE + timedelta(minutes=1))
        deferrals = [event for event in self.events() if 'deferred' in event]
        self.assertEqual(len(deferrals), 1, "one deferral, not one per poll")

    def test_a_baby_still_crying_after_the_limit_gives_up_for_the_day(self):
        self.plant_running_detector()
        self.set_crying(True)
        self.poll(DUE)
        past_limit = DUE + timedelta(seconds=self.wc.RESTART_DEFER_LIMIT)
        self.assertFalse(self.poll(past_limit))
        self.assertTrue(any('skipped' in event for event in self.events()))
        # and it stays given up, even once the baby settles
        self.set_crying(False)
        self.assertFalse(self.poll(past_limit + timedelta(minutes=1)))
        self.assertEqual(self.starts, [])

    def test_turning_it_off_calls_off_a_deferred_restart(self):
        self.plant_running_detector()
        self.set_crying(True)
        self.poll(DUE)
        self.wc.current_config['enable_restart_at'] = False
        self.set_crying(False)
        self.assertFalse(self.poll(DUE + timedelta(minutes=1)))
        self.assertEqual(self.starts, [])

    def test_a_deferred_restart_does_not_follow_the_baby_into_the_next_day(self):
        self.plant_running_detector()
        self.set_crying(True)
        self.poll(DUE)
        self.set_crying(False)
        # a quiet moment the following afternoon is not this restart's cue
        self.assertFalse(self.poll(datetime(2026, 1, 2, 15, 0, 0)))
        self.assertEqual(self.starts, [])

    def test_a_detector_that_cannot_answer_is_restarted_anyway(self):
        # detector_is_crying() reports False when /status is unreachable: a
        # detector that cannot answer is the best reason there is to replace it
        self.plant_running_detector()
        self.wc.detector_is_crying = self._crying   # the real one; nothing listening
        self.wc.running_status_port = 1            # refused, fast
        self.assertTrue(self.poll(DUE))
        self.assertEqual(len(self.starts), 1)


class RestartMechanicsTests(DailyRestartTestCase):
    def restart(self):
        return self.wc.restart_detector('Daily restart (08:00)')

    def restart_with_a_stand_in_successor(self):
        with self.stand_in_successor():
            return self.restart()

    def test_restarting_nothing_is_reported_rather_than_started(self):
        ok, message = self.restart()
        self.assertFalse(ok)
        self.assertIn('not running', message)

    def test_a_restart_is_not_mistaken_for_a_crash(self):
        process = self.plant_running_detector()
        ok, _ = self.restart_with_a_stand_in_successor()
        self.assertTrue(ok)
        self.wc.process_monitor(process)   # the monitor thread wakes after the kill
        self.assertNotIn('fail', self.healthcheck_calls,
                         "a scheduled restart must not raise a false alarm")
        self.assertIsNone(self.wc.last_failure)

    def test_a_restart_leaves_the_session_armed_for_a_power_cut(self):
        self.plant_running_detector()
        self.restart_with_a_stand_in_successor()
        self.assertTrue(self.wc.detector_was_running(),
                        "a reboot after the restart must still resume the session")

    def test_a_restart_is_explained_in_the_event_log(self):
        self.plant_running_detector()
        self.restart_with_a_stand_in_successor()
        joined = ' '.join(self.events())
        self.assertIn('Daily restart (08:00)', joined)
        self.assertIn('restarted', joined)
        self.assertIn('Daily restart', ' '.join(self.controller_log()))

    def test_a_start_that_fails_escalates_instead_of_staying_quiet(self):
        # stop_detector() paused the healthcheck on the way out, which would
        # otherwise hold back the one alert that says nobody is watching
        self.plant_running_detector()
        original = self.wc.start_detector
        self.wc.start_detector = lambda: (False, 'Failed to start: boom')
        try:
            ok, _ = self.restart()
        finally:
            self.wc.start_detector = original
        self.assertFalse(ok)
        self.assertIn('fail', self.healthcheck_calls)
        self.assertIn('NOT monitoring', ' '.join(self.events()))

    def test_a_start_that_fails_raises_the_banner_on_every_phone(self):
        self.plant_running_detector()
        original = self.wc.start_detector
        self.wc.start_detector = lambda: (False, 'Failed to start: boom')
        try:
            self.restart()
        finally:
            self.wc.start_detector = original
        failure = self.client.get('/api/status').get_json()['failure']
        self.assertTrue(failure)
        self.assertIn('boom', failure['detail'])

    def test_a_stop_that_fails_leaves_the_running_detector_alone(self):
        self.plant_running_detector()
        original = self.wc.stop_detector
        self.wc.stop_detector = lambda: (False, 'Failed to stop: boom')
        try:
            ok, _ = self.restart()
        finally:
            self.wc.stop_detector = original
        self.assertFalse(ok)
        self.assertEqual(self.healthcheck_calls, [],
                         "the old detector is probably still listening")
        self.assertIn('aborted', ' '.join(self.events()))


class PhoneRestartTests(DailyRestartTestCase):
    """The RESTART button, which posts one request rather than driving it itself."""

    def test_the_phone_restarts_in_one_request(self):
        old = self.plant_running_detector()
        with self.stand_in_successor() as successor:
            body = self.client.post('/api/restart', json={}).get_json()
        self.assertTrue(body['success'])
        self.assertIsNotNone(old.poll(), "the old detector should be gone")
        self.assertIs(self.wc.detector_process, successor)

    def test_the_session_stays_armed_across_the_restart(self):
        # driving this from the browser went through /api/stop, which ends a
        # session for good: a power cut mid-restart came back unmonitored
        self.plant_running_detector()
        with self.stand_in_successor():
            self.client.post('/api/restart', json={})
        self.assertTrue(self.wc.detector_was_running())

    def test_settings_posted_with_it_are_applied(self):
        self.plant_running_detector()
        with self.stand_in_successor():
            self.client.post('/api/restart', json={'alert': 20, 'record': True})
        self.assertEqual(self.wc.current_config['alert'], 20)
        self.assertTrue(self.wc.current_config['record'])

    def test_a_bad_setting_is_dropped_rather_than_failing_the_restart(self):
        self.plant_running_detector()
        with self.stand_in_successor():
            body = self.client.post('/api/restart', json={'alert': 'abc'}).get_json()
        self.assertTrue(body['success'], "one bad field must not cost the restart")
        self.assertEqual(self.wc.current_config['alert'],
                         self.wc.DEFAULT_CONFIG['alert'])

    def test_a_non_object_body_is_refused(self):
        self.plant_running_detector()
        response = self.client.post('/api/restart', json=['alert', 20])
        self.assertEqual(response.status_code, 400)

    def test_restarting_nothing_is_refused_rather_than_starting_one(self):
        # the button is disabled while stopped, so this is a stale phone
        body = self.client.post('/api/restart', json={}).get_json()
        self.assertFalse(body['success'])
        self.assertIn('not running', body['message'])
        self.assertIsNone(self.wc.detector_process)

    def test_the_restart_says_where_it_came_from(self):
        self.plant_running_detector()
        with self.stand_in_successor():
            self.client.post('/api/restart', json={})
        self.assertIn('Restart from the phone', ' '.join(self.events()))


class RestartSettingTests(DailyRestartTestCase):
    def post(self, value):
        return self.client.post('/api/config', json={'restart_at': value}).get_json()

    def test_the_time_is_normalised(self):
        self.assertEqual(self.post('8:5')['restart_at'], '08:05')

    def test_a_malformed_time_is_refused(self):
        self.assertEqual(self.post('8am')['restart_at'], AT)
        self.assertEqual(self.post(None)['restart_at'], AT)

    def test_the_setting_survives_a_restart_of_the_controller(self):
        self.client.post('/api/config', json={'restart_at': '09:30',
                                              'enable_restart_at': False})
        self.wc.current_config.update(self.wc.DEFAULT_CONFIG)
        self.wc.load_config()
        self.assertEqual(self.wc.current_config['restart_at'], '09:30')
        self.assertFalse(self.wc.current_config['enable_restart_at'])

    def test_a_change_is_recorded_in_the_event_log(self):
        self.client.post('/api/config', json={'restart_at': '09:30'})
        self.client.post('/api/config', json={'enable_restart_at': False})
        joined = ' '.join(self.events())
        self.assertIn('Daily restart time changed 08:00 -> 09:30', joined)
        self.assertIn('Daily restart DISABLED', joined)

    def test_the_phone_can_see_and_set_it(self):
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('id="enable_restart_at"', page)
        self.assertIn('id="restart_at"', page)


if __name__ == '__main__':
    unittest.main()
