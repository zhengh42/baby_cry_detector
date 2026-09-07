"""Detector process lifecycle as seen by the web controller.

The theme is that a monitor which is not monitoring must say so. Pausing the
healthcheck after a crash would silence the one alert that says nobody is
watching the baby, so the pause is reserved for clean shutdowns.
"""
import os
import signal
import subprocess
import unittest

import fakes

EXIT_CLEAN = "import sys; sys.exit(0)"
EXIT_CRASH = "import sys; sys.exit(1)"
EXIT_BAD_ARGS = "import sys; sys.exit(2)"
RUN_FOREVER = "import time; time.sleep(60)"


class ExitClassificationTests(fakes.WebControllerTestCase):
    def watch(self, code, last_output=None):
        """Let the process monitor observe a process that exits on its own."""
        process = self.spawn(code)
        self.wc.detector_process = process
        if last_output:
            self.wc.log_buffer.append(f"[ts] {last_output}")
        self.wc.process_monitor(process)
        return process

    def test_a_clean_exit_pauses_the_healthcheck(self):
        self.watch(EXIT_CLEAN)
        self.assertEqual(self.healthcheck_calls, ['pause'])
        self.assertIn('Detector exited normally (auto-stop)', self.events())
        self.assertIsNone(self.wc.last_failure)

    def test_a_crash_escalates_instead_of_pausing(self):
        self.watch(EXIT_CRASH, last_output="AttributeError: 'NoneType' ...")
        self.assertEqual(self.healthcheck_calls, ['fail'])
        self.assertNotIn('pause', self.healthcheck_calls,
                         "pausing after a crash would silence the alert")

    def test_a_crash_is_labelled_as_not_monitoring(self):
        self.watch(EXIT_CRASH)
        joined = ' '.join(self.events())
        self.assertIn('UNEXPECTEDLY', joined)
        self.assertIn('NOT monitoring', joined)
        self.assertIn('exit code 1', joined)

    def test_a_bad_argument_reports_the_detector_error(self):
        self.watch(EXIT_BAD_ARGS,
                   last_output="error: argument --alert: invalid int value: 'abc'")
        self.assertIn('exit code 2', ' '.join(self.events()))
        self.assertIn('invalid int value', self.wc.last_failure['detail'])

    def test_being_killed_from_outside_is_reported_as_a_signal(self):
        process = self.spawn(RUN_FOREVER)
        self.wc.detector_process = process
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        self.wc.process_monitor(process)
        self.assertEqual(self.healthcheck_calls, ['fail'])
        self.assertIn('killed by signal 9', ' '.join(self.events()))

    def test_a_crash_raises_the_banner_with_the_cause(self):
        self.watch(EXIT_CRASH, last_output="ModuleNotFoundError: No module named 'pyaudio'")
        failure = self.wc.last_failure
        self.assertEqual(failure['reason'], 'exit code 1')
        self.assertIn('ModuleNotFoundError', failure['detail'])
        self.assertTrue(self.client.get('/api/status').get_json()['failure'])

    def test_the_banner_can_be_dismissed_for_every_phone(self):
        self.watch(EXIT_CRASH)
        self.assertTrue(self.client.post('/api/dismiss-failure').get_json()['success'])
        self.assertIsNone(self.client.get('/api/status').get_json()['failure'])

    def test_the_crash_cause_reaches_the_persistent_log(self):
        self.watch(EXIT_CRASH, last_output="RuntimeError: No USB audio device found")
        log = ' '.join(self.controller_log())
        self.assertIn('UNEXPECTEDLY', log)
        self.assertIn('No USB audio device found', log)

    def test_a_stale_process_is_ignored(self):
        old = self.spawn(EXIT_CRASH)
        current = self.spawn(RUN_FOREVER)
        self.wc.detector_process = current
        self.wc.process_monitor(old)          # the one that exited is not current
        self.assertEqual(self.healthcheck_calls, [])
        self.assertIs(self.wc.detector_process, current)


class StopTests(fakes.WebControllerTestCase):
    def test_stopping_a_detector_that_is_not_running_is_reported(self):
        ok, message = self.wc.stop_detector()
        self.assertFalse(ok)
        self.assertIn('not running', message)

    def test_a_requested_stop_is_not_mistaken_for_a_crash(self):
        process = self.spawn(RUN_FOREVER)
        self.wc.detector_process = process
        self.wc.stopping_pids.add(process.pid)   # what stop_detector does
        ok, message = self.wc.stop_detector()
        self.wc.process_monitor(process)          # monitor wakes after the kill
        self.assertTrue(ok)
        self.assertNotIn('fail', self.healthcheck_calls,
                         "a deliberate stop must not raise a false alarm")
        self.assertNotIn('UNEXPECTEDLY', ' '.join(self.events()))
        self.assertIsNone(self.wc.last_failure)

    def test_a_wedged_detector_is_force_killed(self):
        process = self.spawn_wedged()
        # make the 5s SIGTERM grace expire immediately so the test stays fast
        real_wait = process.wait
        calls = {'n': 0}

        def wait(timeout=None):
            calls['n'] += 1
            if calls['n'] == 1:
                raise subprocess.TimeoutExpired(cmd='detector', timeout=timeout or 5)
            return real_wait(timeout=timeout)

        process.wait = wait
        self.wc.detector_process = process
        ok, message = self.wc.stop_detector()
        self.assertTrue(ok, "STOP must always work from the phone")
        self.assertIn('forced', message)
        self.assertEqual(process.returncode, -signal.SIGKILL,
                         "SIGTERM was ignored, so SIGKILL must have finished it")
        self.assertIsNone(self.wc.detector_process, "the UI must not be left stuck")
        self.assertFalse(self.wc.is_running())

    def test_the_forced_stop_says_the_recording_was_lost(self):
        process = self.spawn_wedged()
        real_wait = process.wait
        first = {'done': False}

        def wait(timeout=None):
            if not first['done']:
                first['done'] = True
                raise subprocess.TimeoutExpired(cmd='detector', timeout=timeout or 5)
            return real_wait(timeout=timeout)

        process.wait = wait
        self.wc.detector_process = process
        self.wc.stop_detector()
        self.assertTrue(any('SIGKILL' in line for line in self.wc.log_buffer))


class PortPinningTests(fakes.WebControllerTestCase):
    def test_the_status_port_is_pinned_when_the_detector_launches(self):
        self.wc.current_config['status_port'] = 8099
        process = self.spawn(RUN_FOREVER)
        original_popen = subprocess.Popen
        subprocess.Popen = lambda *a, **k: process
        try:
            ok, _ = self.wc.start_detector()
        finally:
            subprocess.Popen = original_popen
        self.assertTrue(ok)
        self.assertEqual(self.wc.running_status_port, 8099)

    def test_changing_the_port_while_running_does_not_move_the_pin(self):
        # the detector binds its port once at startup; repointing the controller
        # would blank the status display and silently drop a duty switch
        process = self.spawn(RUN_FOREVER)
        self.wc.detector_process = process
        self.wc.running_status_port = 8080
        self.client.post('/api/config', json={'status_port': 9000})
        self.assertEqual(self.wc.running_status_port, 8080)
        self.assertEqual(self.wc.current_config['status_port'], 9000)
        self.assertIn('http://localhost:8080/status', self.wc.detector_endpoint('/status'))

    def test_the_deferred_change_is_explained_in_the_event_log(self):
        process = self.spawn(RUN_FOREVER)
        self.wc.detector_process = process
        self.wc.running_status_port = 8080
        self.client.post('/api/config', json={'status_port': 9000})
        self.assertTrue(any('takes effect on next start' in event
                            for event in self.events()))

    def test_the_endpoint_falls_back_to_the_setting_when_nothing_runs(self):
        self.wc.running_status_port = None
        self.wc.current_config['status_port'] = 8080
        self.assertIn(':8080/', self.wc.detector_endpoint('/status'))


class EventLogRetentionTests(fakes.WebControllerTestCase):
    def start_with_fake_process(self):
        process = self.spawn(RUN_FOREVER)
        original_popen = subprocess.Popen
        subprocess.Popen = lambda *a, **k: process
        try:
            return self.wc.start_detector()
        finally:
            subprocess.Popen = original_popen

    def test_settings_changed_before_start_stay_in_the_event_log(self):
        # the log used to be cleared on start, so everything set up beforehand
        # vanished from the phone the moment you pressed START
        self.client.post('/api/config', json={'alert': 20})
        self.client.post('/api/device', json={'pushover_device': 'grandma_phone'})
        before = self.events()
        self.assertTrue(before)
        ok, _ = self.start_with_fake_process()
        self.assertTrue(ok)
        after = self.events()
        for event in before:
            self.assertIn(event, after, "pre-start changes must survive START")

    def test_a_session_marker_is_added_on_start(self):
        self.start_with_fake_process()
        self.assertIn('--- Detector started ---', self.events())

    def test_the_verbose_log_is_cleared_on_start(self):
        self.wc.log_buffer.append('[ts] stale line from the previous run')
        self.start_with_fake_process()
        self.assertFalse(any('stale line' in line for line in self.wc.log_buffer))


class StatusTests(fakes.WebControllerTestCase):
    def test_status_reports_stopped_when_nothing_runs(self):
        body = self.client.get('/api/status').get_json()
        self.assertFalse(body['running'])
        self.assertIsNone(body['baby_status'])

    def test_the_page_renders(self):
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('Baby Monitor', page)
        self.assertIn('id="failure-banner"', page)
        for device in self.wc.DEVICE_LABELS:
            self.assertIn(f'btn-{device}', page)


class PowerCutResumeTests(fakes.WebControllerTestCase):
    """Coming back from a power cut.

    Only the web controller is a systemd service; the detector is a child process
    it launches on START. A reboot therefore used to come back as a working
    dashboard with nothing listening to the baby - which, from the healthcheck
    alert alone, is indistinguishable from the power still being out.
    """

    def start_with_fake_process(self):
        process = self.spawn(RUN_FOREVER)
        original_popen = subprocess.Popen
        subprocess.Popen = lambda *a, **k: process
        try:
            return self.wc.start_detector()
        finally:
            subprocess.Popen = original_popen

    def resume(self, outcome=(True, 'started')):
        """Run the boot-time resume with a stubbed start; did it try to start?"""
        calls = []

        def fake_start():
            calls.append(True)
            return outcome

        original = self.wc.start_detector
        self.wc.start_detector = fake_start
        try:
            self.wc.resume_detector_after_boot()
        finally:
            self.wc.start_detector = original
        return bool(calls)

    def test_starting_records_that_the_detector_should_be_listening(self):
        ok, _ = self.start_with_fake_process()
        self.assertTrue(ok)
        self.assertTrue(self.wc.detector_was_running())

    def test_a_reboot_resumes_an_interrupted_session(self):
        self.start_with_fake_process()
        self.assertTrue(self.resume(),
                        "a power cut would leave the baby unmonitored until "
                        "someone noticed and pressed START")

    def test_the_resume_is_explained_in_the_event_log(self):
        self.start_with_fake_process()
        self.resume()
        self.assertTrue(any('resuming' in event for event in self.events()),
                        "a detector that started on its own must say why")

    def test_pressing_stop_ends_the_session_for_good(self):
        self.start_with_fake_process()
        self.client.post('/api/stop')
        self.assertFalse(self.wc.detector_was_running())
        self.assertFalse(self.resume(),
                         "a session stopped on purpose must stay stopped")

    def test_a_clean_auto_stop_ends_the_session_for_good(self):
        self.start_with_fake_process()
        process = self.spawn(EXIT_CLEAN)
        self.wc.detector_process = process
        self.wc.process_monitor(process)
        self.assertFalse(self.resume(),
                         "--stop-at ended the night deliberately")

    def test_a_crash_is_retried_on_the_next_boot(self):
        self.start_with_fake_process()
        process = self.spawn(EXIT_CRASH)
        self.wc.detector_process = process
        self.wc.process_monitor(process)
        self.assertTrue(self.resume(),
                        "a fresh boot may well cure whatever it died of")

    def test_restarting_the_controller_still_resumes(self):
        # cleanup() stops the detector when the service is restarted or the Pi is
        # rebooted cleanly, and there the user never asked to stop monitoring
        self.start_with_fake_process()
        self.wc.stop_detector()
        self.assertTrue(self.resume())

    def test_a_fresh_install_starts_nothing(self):
        self.assertFalse(os.path.exists(self.wc.DETECTOR_STATE_FILE))
        self.assertFalse(self.resume())

    def test_an_unreadable_state_file_starts_nothing(self):
        with open(self.wc.DETECTOR_STATE_FILE, 'w') as f:
            f.write('{ not json')
        self.assertFalse(self.wc.detector_was_running())
        self.assertFalse(self.resume())

    def test_a_resume_that_fails_says_so_in_the_event_log(self):
        self.wc.save_detector_state(True)
        self.resume(outcome=(False, 'Failed to start: boom'))
        self.assertTrue(any('Could not resume' in event for event in self.events()))

    def test_a_resume_that_raises_does_not_take_the_dashboard_down(self):
        # runs unattended at boot: an unwritable log dir or a bad SD card must
        # still leave a page the phone can reach and press START on
        self.wc.save_detector_state(True)
        original = self.wc.start_detector

        def exploding_start():
            raise PermissionError("/var/log/babymonitor")

        self.wc.start_detector = exploding_start
        try:
            self.wc.resume_detector_after_boot()
        finally:
            self.wc.start_detector = original
        self.assertTrue(any('Could not resume' in event for event in self.events()))
        self.assertTrue(any('PermissionError' in event for event in self.events()))
        self.assertTrue(self.client.get('/api/status').get_json() is not None,
                        "the dashboard must still answer")


if __name__ == '__main__':
    unittest.main()
