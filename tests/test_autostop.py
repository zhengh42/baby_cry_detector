"""The --stop-at auto-stop, including when the microphone is dead.

The auto-stop is checked once per pass of the monitor loop, and that loop does
not run at a steady rate: a healthy mic paces it at one chunk (~0.19s), while a
dropped stream makes it sleep 2 seconds between reconnect attempts and 5 more
when the mic is not on the bus at all. Anything narrower than the stop minute
gets stepped over on those nights.
"""
import unittest
from datetime import datetime

import fakes
from fakes import cd


def run_until_stop(wall_start, timeline, stop_at='07:00'):
    """Run the monitor from a given time of day and return (output, elapsed)."""
    clock = fakes.FakeClock()
    source = fakes.AudioSource(chunk=fakes.silence(), clock=clock)
    source.play(timeline)
    detector = fakes.make_detector(source=source, stop_time=(
        datetime.strptime(stop_at, '%H:%M').time()))
    with fakes.patched_clock(cd, clock), \
            fakes.patched_wall_clock(cd, clock, start=f'2026-01-01 {wall_start}'):
        output = fakes.run_monitor(detector)
    return output, clock.elapsed()


class AutoStopTests(unittest.TestCase):

    def test_it_stops_during_the_stop_minute(self):
        output, elapsed = run_until_stop('06:59:50', [(120, fakes.silence())])
        self.assertIn("Auto-stop time reached", output)
        # 07:00:00 is 10 seconds in; it should not have run on much past that
        self.assertLess(elapsed, 15, "should stop as soon as the minute arrives")

    def test_it_still_stops_when_the_mic_is_dead(self):
        """The morning stop has to happen even if the night went wrong.

        With the mic off the bus the loop sleeps 5 seconds per pass, so it lands
        somewhere random inside the target minute - it used to have to land in
        the first two seconds of it, and usually did not, leaving the detector
        running all day on a microphone that was not there.
        """
        output, elapsed = run_until_stop(
            '06:58:00', [(2, fakes.silence()), (600, fakes.DEVICE_GONE)])
        self.assertIn("Auto-stop time reached", output)
        self.assertNotIn("Stopping cry detector", output,
                         "the timeline should not have had to end the session")
        # 07:00:00 is 120 seconds in, and the whole minute is fair game
        self.assertTrue(120 <= elapsed < 180,
                        f"stopped at {elapsed:.1f}s, outside the 07:00 minute")

    def test_a_session_started_inside_the_stop_minute_keeps_running(self):
        """Otherwise pressing START at 07:00:30 with a 07:00 stop quits at once."""
        output, _ = run_until_stop('07:00:10', [(20, fakes.silence())])
        self.assertNotIn("Auto-stop time reached", output)
        self.assertIn("Stopping cry detector", output)

    def test_no_stop_time_means_it_runs_until_told_otherwise(self):
        clock = fakes.FakeClock()
        source = fakes.AudioSource(chunk=fakes.silence(), clock=clock)
        source.play([(120, fakes.silence())])
        detector = fakes.make_detector(source=source)
        with fakes.patched_clock(cd, clock), \
                fakes.patched_wall_clock(cd, clock, start='2026-01-01 06:59:50'):
            output = fakes.run_monitor(detector)
        self.assertNotIn("Auto-stop time reached", output)


if __name__ == '__main__':
    unittest.main()
