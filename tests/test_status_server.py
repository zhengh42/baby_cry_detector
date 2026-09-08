"""The detector's own HTTP server: GET /status and POST /config.

This is the channel the web controller talks over, and it is bound to localhost
because an open port lets anyone on the WiFi mute the monitor.
"""
import json
import unittest
import urllib.error
import urllib.request

import fakes
from fakes import cd


class StatusEndpointTests(unittest.TestCase):
    # One server for the whole class: HTTPServer.shutdown() waits out a 0.5s poll
    # interval, which would otherwise dominate the suite's runtime.
    @classmethod
    def setUpClass(cls):
        cls.detector = fakes.make_detector(status_port=8241)
        with fakes.captured_output():
            cls.detector.start_status_server()

    @classmethod
    def tearDownClass(cls):
        cls.detector.stop_status_server()

    def setUp(self):
        # reset the state the endpoint reads, so tests stay independent
        self.detector.alarm_active = False
        self.detector.stream_ok = True
        self.detector.mic_silent = False
        self.detector.last_chunk_time = None
        self.detector.session_start_time = None
        self.detector.initial_start_time = None
        self.detector.last_cry_time = None
        self.detector.pushover_device = None
        self.detector.volume_threshold = cd.VOLUME_THRESHOLD
        self.detector.cry_freq_min = cd.CRY_FREQ_MIN
        self.detector.alert_window = cd.ALERT_WINDOW
        self.detector.reset_window = cd.RESET_WINDOW
        self.detector.min_cry_duration = cd.MIN_CRY_DURATION
        self.detector.silence_gap = cd.SILENCE_GAP

    def get_status(self):
        url = f"http://127.0.0.1:{self.detector.status_port}/status"
        return json.loads(urllib.request.urlopen(url, timeout=3).read().decode())

    def post_config(self, body, raw=False):
        url = f"http://127.0.0.1:{self.detector.status_port}/config"
        data = body if raw else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method='POST')
        request.add_header('Content-Type', 'application/json')
        try:
            with fakes.captured_output():
                response = urllib.request.urlopen(request, timeout=3)
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode())

    def test_status_reports_a_quiet_baby(self):
        status = self.get_status()
        self.assertFalse(status['crying'])
        self.assertFalse(status['alarm'])
        self.assertTrue(status['stream_ok'])

    def test_status_exposes_the_alarm_and_stream_health(self):
        self.detector.alarm_active = True
        self.detector.stream_ok = False
        status = self.get_status()
        self.assertTrue(status['alarm'], "the UI and receiver key off this field")
        self.assertFalse(status['stream_ok'])

    def test_seconds_since_audio_is_null_before_a_session_starts(self):
        self.assertIsNone(self.get_status()['seconds_since_audio'])
        self.detector.last_chunk_time = cd.time.time()
        self.assertIsNotNone(self.get_status()['seconds_since_audio'])

    def test_a_mic_that_never_delivered_a_chunk_reads_as_no_audio(self):
        """A mic can open and then simply never return any data.

        The monitor loop is parked inside read() with nothing left to notice
        that, so this field is all the dashboard has to go on - and reporting
        nothing let it draw a reassuring "Quiet" over a microphone that had
        never listened at all. Time since the session started is the honest
        answer, and it crosses the 30 seconds the dashboard calls NO AUDIO.
        """
        self.detector.last_chunk_time = None
        self.detector.session_start_time = cd.time.time() - 120
        self.assertGreater(self.get_status()['seconds_since_audio'], 30)

    def test_unknown_paths_are_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.detector.status_port}/nope", timeout=3)
        self.assertEqual(caught.exception.code, 404)

    def test_config_applies_valid_values(self):
        status, body = self.post_config({'alert_window': 900, 'min_cry_duration': 6})
        self.assertEqual(status, 200)
        self.assertEqual(body['rejected'], [])
        self.assertEqual(self.detector.alert_window, 900)
        self.assertEqual(self.detector.min_cry_duration, 6)

    def test_config_clamps_dangerous_values(self):
        # alert_window=0 would alert on every audio chunk
        self.post_config({'alert_window': 0, 'min_cry_duration': 0, 'volume_threshold': 0})
        self.assertGreaterEqual(self.detector.alert_window, 60)
        self.assertGreaterEqual(self.detector.min_cry_duration, 1)
        self.assertGreaterEqual(self.detector.volume_threshold, 50)

    def test_config_clamps_the_upper_end_too(self):
        self.post_config({'cry_freq_min': 9000})
        self.assertLessEqual(self.detector.cry_freq_min, cd.CRY_FREQ_MAX)

    def test_config_rejects_uncoercible_values_without_crashing(self):
        before = self.detector.volume_threshold
        status, body = self.post_config({'volume_threshold': 'abc', 'alert_window': None})
        self.assertEqual(status, 200, "a bad value must not raise out of the handler")
        self.assertEqual(sorted(body['rejected']), ['alert_window', 'volume_threshold'])
        self.assertEqual(self.detector.volume_threshold, before)

    def test_config_rejects_a_non_string_device(self):
        before = self.detector.pushover_device
        _, body = self.post_config({'pushover_device': 42})
        self.assertIn('pushover_device', body['rejected'])
        self.assertEqual(self.detector.pushover_device, before)

    def test_config_switches_the_device(self):
        self.post_config({'pushover_device': 'h_phone'})
        self.assertEqual(self.detector.pushover_device, 'h_phone')

    def test_malformed_bodies_are_rejected_with_400(self):
        self.assertEqual(self.post_config(b'not json{{{', raw=True)[0], 400)
        self.assertEqual(self.post_config(b'[1,2,3]', raw=True)[0], 400)
        self.assertEqual(self.post_config(b'', raw=True)[0], 400)


class BindAddressTests(unittest.TestCase):
    def test_default_binds_to_localhost_only(self):
        detector = fakes.make_detector(status_port=8242)
        with fakes.captured_output():
            detector.start_status_server()
        try:
            self.assertEqual(detector.status_server.server_address[0], '127.0.0.1')
        finally:
            detector.stop_status_server()

    def test_the_server_can_be_stopped_and_started_again(self):
        # note: this passes even without server_close(), because dropping the
        # reference closes the socket under CPython refcounting. It is here to
        # catch a stop/start cycle breaking, not to prove server_close() is called.
        for _ in range(2):
            detector = fakes.make_detector(status_port=8244)
            with fakes.captured_output():
                detector.start_status_server()
            detector.stop_status_server()

    def test_bind_can_be_opened_up_for_the_receiver(self):
        detector = fakes.make_detector(status_port=8243, status_bind='0.0.0.0')
        with fakes.captured_output():
            detector.start_status_server()
        try:
            self.assertEqual(detector.status_server.server_address[0], '0.0.0.0')
        finally:
            detector.stop_status_server()


if __name__ == '__main__':
    unittest.main()
