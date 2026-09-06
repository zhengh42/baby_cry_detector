"""Per-chunk detection: does a given sound read as crying?

Synthetic signals go through the real analyze_audio(), so these pin down the
behaviour of the thresholds without a microphone.
"""
import unittest

import fakes
from fakes import cd


class AnalyzeAudioTests(unittest.TestCase):
    def setUp(self):
        self.detector = fakes.make_detector()

    def analyze(self, chunk):
        with fakes.captured_output():
            return self.detector.analyze_audio(chunk)

    def assert_cry(self, chunk, message):
        crying, volume, freq, ratio = self.analyze(chunk)
        self.assertTrue(crying, f"{message} should be detected "
                                f"(vol={volume:.0f} freq={freq:.0f} ratio={ratio:.2f})")

    def assert_not_cry(self, chunk, message):
        crying, volume, freq, ratio = self.analyze(chunk)
        self.assertFalse(crying, f"{message} should NOT be detected "
                                 f"(vol={volume:.0f} freq={freq:.0f} ratio={ratio:.2f})")

    def test_cries_in_band_are_detected(self):
        self.assert_cry(fakes.tone(1000, 8000), "a loud 1 kHz cry")
        self.assert_cry(fakes.tone(600, 3000), "a moderate 600 Hz cry")
        self.assert_cry(fakes.tone(1800, 4000), "a high 1.8 kHz cry")

    def test_loud_out_of_band_sounds_are_ignored(self):
        # These are the false alarms that matter: a fan, a door, plumbing
        self.assert_not_cry(fakes.tone(80, 8000), "an 80 Hz rumble")
        self.assert_not_cry(fakes.tone(120, 9000), "a 120 Hz hum")
        self.assert_not_cry(fakes.tone(5000, 8000), "a 5 kHz hiss")
        self.assert_not_cry(fakes.white_noise(4000), "loud white noise")

    def test_silence_is_not_a_cry(self):
        crying, volume, _, _ = self.analyze(fakes.silence())
        self.assertFalse(crying)
        self.assertEqual(volume, 0)

    def test_quiet_sounds_below_the_volume_threshold_are_ignored(self):
        self.assert_not_cry(fakes.tone(800, 700), "a quiet whimper")

    def test_volume_threshold_is_the_deciding_line(self):
        # mean(|sine|) is about 0.637 * amplitude, so solve for the threshold
        self.detector.volume_threshold = 900
        just_under = int(890 / 0.637)
        just_over = int(950 / 0.637)
        self.assert_not_cry(fakes.tone(1000, just_under), "a cry just under the threshold")
        self.assert_cry(fakes.tone(1000, just_over), "a cry just over the threshold")

    def test_raising_cry_freq_min_excludes_lower_cries(self):
        chunk = fakes.tone(450, 5000)
        self.detector.cry_freq_min = 400
        self.assert_cry(chunk, "a 450 Hz cry with the floor at 400")
        self.detector.cry_freq_min = 500
        self.assert_not_cry(chunk, "a 450 Hz cry with the floor at 500")

    def test_reported_measurements_match_the_signal(self):
        _, volume, freq, ratio = self.analyze(fakes.tone(1000, 8000))
        self.assertAlmostEqual(freq, 1000, delta=10)
        self.assertAlmostEqual(volume, 0.637 * 8000, delta=200)
        self.assertGreater(ratio, 0.5)

    def test_out_of_band_energy_ratio_stays_low(self):
        _, _, _, in_band = self.analyze(fakes.tone(1000, 8000))
        _, _, _, out_of_band = self.analyze(fakes.tone(80, 8000))
        self.assertGreater(in_band, cd.CRY_RATIO_THRESHOLD)
        self.assertLess(out_of_band, cd.CRY_RATIO_THRESHOLD)


class ConstantsTests(unittest.TestCase):
    """Guard the assumptions the other tests are built on."""

    def test_audio_format_is_what_the_generators_assume(self):
        self.assertEqual(cd.RATE, 44100)
        self.assertEqual(cd.CHUNK, 8192)

    def test_cry_band_is_sane(self):
        self.assertLess(cd.CRY_FREQ_MIN, cd.CRY_FREQ_MAX)
        self.assertLessEqual(cd.CRY_FREQ_MIN_RANGE[1], cd.CRY_FREQ_MAX)


if __name__ == '__main__':
    unittest.main()
