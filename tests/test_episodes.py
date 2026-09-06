"""Episode state machine: start, alarm, repeat alarm, settle.

Each test scripts the microphone over a timeline and runs the real monitor loop.
A fake clock advances with every chunk read, so a 70-second night of audio runs
in milliseconds and always the same way.
"""
import unittest

import fakes
from fakes import cd


class EpisodeTests(unittest.TestCase):
    def build(self, segments, **attributes):
        """Run the monitor loop over a scripted timeline; return its output."""
        self.clock = fakes.FakeClock()
        source = fakes.AudioSource(clock=self.clock).play(segments)
        settings = dict(min_cry_duration=4, alert_window=30,
                        reset_window=20, silence_gap=2)
        settings.update(attributes)
        self.detector = fakes.make_detector(source=source, **settings)
        with fakes.patched_clock(cd, self.clock):
            return fakes.run_monitor(self.detector)

    def test_sustained_crying_starts_an_episode(self):
        output = self.build([(10, fakes.cry())])
        self.assertIn("Baby started crying", output)
        self.assertIsNotNone(self.detector.initial_start_time)

    def test_brief_sound_is_ignored(self):
        output = self.build([(2, fakes.cry()), (6, fakes.silence())])
        self.assertIn("Ignored brief sound", output)
        self.assertNotIn("Baby started crying", output)
        self.assertIsNone(self.detector.initial_start_time)

    def test_crying_past_the_alert_window_raises_the_alarm(self):
        output = self.build([(40, fakes.cry())])
        self.assertIn("ALERT!", output)
        # the regression that mattered: /status reported alarm=False forever
        self.assertTrue(self.detector.alarm_active,
                        "alarm_active must be set so the UI and receiver can see it")

    def test_alarm_repeats_if_crying_continues(self):
        output = self.build([(70, fakes.cry())])
        self.assertEqual(output.count("ALERT!"), 2,
                         "a second alert should follow another full alert window")
        self.assertIn("Episode reset", output)

    def test_settling_ends_the_episode_and_clears_the_alarm(self):
        output = self.build([(40, fakes.cry()), (25, fakes.silence())])
        self.assertIn("ALERT!", output)
        self.assertIn("Baby settled", output)
        self.assertIsNone(self.detector.initial_start_time)
        self.assertFalse(self.detector.alarm_active,
                         "the alarm must clear once the baby settles")

    def test_a_gap_shorter_than_silence_gap_does_not_break_the_episode(self):
        output = self.build([(6, fakes.cry()), (1, fakes.silence()),
                             (6, fakes.cry())])
        self.assertEqual(output.count("Baby started crying"), 1,
                         "a 1s pause should not start a second episode")
        self.assertNotIn("Ignored brief sound", output)

    def test_out_of_band_noise_never_starts_an_episode(self):
        output = self.build([(30, fakes.tone(120, 9000))])
        self.assertNotIn("Baby started crying", output)
        self.assertNotIn("ALERT!", output)

    def test_muted_device_suppresses_the_notification_but_not_the_alarm(self):
        self.build([(40, fakes.cry())], pushover_device='__muted__')
        self.assertTrue(self.detector.alarm_active,
                        "muting silences the phone, not the local alarm state")

    def test_detector_state_is_consistent_after_an_alarm(self):
        # the /status payload is built from these fields (see test_status_server)
        self.build([(40, fakes.cry())])
        self.assertTrue(self.detector.alarm_active)
        self.assertIsNotNone(self.detector.initial_start_time)
        self.assertIsNotNone(self.detector.last_chunk_time)
        self.assertGreaterEqual(self.detector.last_cry_time,
                                self.detector.initial_start_time)


if __name__ == '__main__':
    unittest.main()
