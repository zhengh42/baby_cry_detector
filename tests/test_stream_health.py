"""Audio stream health: a dead mic must never look like a quiet baby.

Covers the failure that used to crash the detector outright (a failed reconnect
left self.stream as None and the next read raised AttributeError) and the stale
"all quiet" status that made the phone show a sleeping baby while deaf.
"""
import unittest

import fakes
from fakes import cd


class StreamHealthTests(unittest.TestCase):
    def build(self, segments, **attributes):
        self.clock = fakes.FakeClock()
        source = fakes.AudioSource(clock=self.clock).play(segments)
        settings = dict(min_cry_duration=4, alert_window=30, reset_window=20)
        settings.update(attributes)
        self.detector = fakes.make_detector(source=source, **settings)
        with fakes.patched_clock(cd, self.clock):
            return fakes.run_monitor(self.detector)

    def test_a_healthy_stream_reports_ok(self):
        self.build([(5, fakes.silence())])
        self.assertTrue(self.detector.stream_ok)
        self.assertIsNotNone(self.detector.last_chunk_time)

    def test_read_failure_marks_the_stream_down(self):
        output = self.build([(2, fakes.silence()), (30, fakes.READ_ERROR)])
        self.assertFalse(self.detector.stream_ok,
                         "a failing read must not keep reporting a healthy stream")
        self.assertIn("Audio stream error", output)

    def test_unplugged_mic_does_not_crash_the_detector(self):
        # Reaching the end of the timeline at all proves the loop survived; the old
        # code raised AttributeError: 'NoneType' object has no attribute 'read'
        output = self.build([(2, fakes.silence()), (60, fakes.DEVICE_GONE)])
        self.assertIn("Audio stream error", output)
        self.assertFalse(self.detector.stream_ok)
        # cleanup() terminates PyAudio once on exit, so anything past 1 is a rebuild
        self.assertGreater(self.detector.source.terminates, 1,
                           "the loop should keep retrying instead of dying")

    def test_repeated_failures_report_the_reconnect_attempts(self):
        # error lines share one throttle window, so a long outage is needed to see both
        output = self.build([(2, fakes.silence()), (200, fakes.DEVICE_GONE)])
        self.assertIn("Audio stream error", output)
        self.assertIn("Reconnection failed", output)

    def test_mic_coming_back_is_detected_and_announced(self):
        output = self.build([(2, fakes.silence()), (30, fakes.DEVICE_GONE),
                             (10, fakes.silence())])
        self.assertIn("Audio stream recovered", output)
        self.assertTrue(self.detector.stream_ok)

    def test_the_backend_is_rebuilt_so_a_replugged_mic_is_seen(self):
        # PyAudio caches the device list at init, so recovery needs a fresh instance
        self.build([(2, fakes.silence()), (30, fakes.DEVICE_GONE),
                    (5, fakes.silence())])
        # one terminate() comes from cleanup() on exit; a rebuild adds more
        self.assertGreater(self.detector.source.terminates, 1,
                           "reset_audio_backend() should have rebuilt PyAudio")
        self.assertEqual(self.detector.source.opens, 2,
                         "the stream should have been opened again after the replug")

    def test_a_healthy_run_never_rebuilds_the_backend(self):
        # baseline for the assertions above: without failures, only cleanup() runs
        self.build([(5, fakes.silence())])
        self.assertEqual(self.detector.source.opens, 1)
        self.assertEqual(self.detector.source.terminates, 1)

    def test_stream_errors_are_throttled_in_the_log(self):
        output = self.build([(2, fakes.silence()), (600, fakes.DEVICE_GONE)])
        errors = output.count("Audio stream error") + output.count("Reconnection failed")
        # 10 minutes of failures, logged at most once a minute
        self.assertGreaterEqual(errors, 1)
        self.assertLessEqual(errors, 15,
                             f"a dead mic flooded the log with {errors} lines")

    def test_crying_is_still_detected_after_recovery(self):
        output = self.build([(2, fakes.silence()), (20, fakes.DEVICE_GONE),
                             (40, fakes.cry())])
        self.assertIn("Audio stream recovered", output)
        self.assertIn("Baby started crying", output)
        self.assertIn("ALERT!", output)

    def test_a_mic_that_dies_mid_cry_does_not_alarm_on_reconnect(self):
        # Smoothing needs only 2 positives in 5 chunks, so the pre-outage chunks
        # left in the window used to re-confirm crying on the first chunk back,
        # against an alert clock that ran the whole time the mic was dead
        output = self.build([(10, fakes.cry()), (60, fakes.DEVICE_GONE),
                             (20, fakes.silence())])
        self.assertIn("Audio stream recovered", output)
        self.assertNotIn("ALERT!", output,
                         "a quiet room after an outage must not raise the alarm")
        self.assertFalse(self.detector.alarm_active)

    def test_the_smoothing_window_is_empty_after_an_outage(self):
        self.build([(10, fakes.cry()), (30, fakes.DEVICE_GONE)])
        self.assertEqual(len(self.detector.recent_detections), 0,
                         "chunks from before the outage say nothing about the room now")
        self.assertIsNone(self.detector.potential_cry_start_time)

    def test_a_short_dropout_does_not_restart_a_real_episode(self):
        # the flip side: a blip during genuine crying should not lose the episode
        output = self.build([(10, fakes.cry()), (4, fakes.READ_ERROR),
                             (40, fakes.cry())])
        self.assertEqual(output.count("Baby started crying"), 1,
                         "a brief dropout should not look like a brand new episode")
        self.assertIn("ALERT!", output)

    def test_silent_mic_detection_still_works(self):
        # dead silence for longer than MIC_SILENT_DURATION, stream otherwise fine
        output = self.build([(cd.MIC_SILENT_DURATION + 30, fakes.silence())])
        self.assertIn("MICROPHONE SILENT", output)
        self.assertTrue(self.detector.mic_silent)

    def test_silent_mic_recovers_when_sound_returns(self):
        output = self.build([(cd.MIC_SILENT_DURATION + 30, fakes.silence()),
                             (5, fakes.tone(1000, 3000))])
        self.assertIn("Microphone recovered", output)
        self.assertFalse(self.detector.mic_silent)


if __name__ == '__main__':
    unittest.main()
