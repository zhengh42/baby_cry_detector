"""Shutdown behaviour of the detector as a real process.

The SIGTERM handler lives in cry_detector's __main__ block, which the in-process
tests never execute, so these run the script for real with a fake microphone.
The web UI's STOP button sends SIGTERM; before the handler existed it killed the
process outright, losing any recording in progress.
"""
import os
import signal
import subprocess
import sys
import tempfile
import unittest

import fakes

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
DETECTOR = os.path.join(fakes.REPO_ROOT, 'cry_detector.py')

RUNNER = '''
import sys, runpy
sys.path.insert(0, {tests_dir!r})
import fakes
fakes.ACTIVE_SOURCE = fakes.AudioSource(chunk={chunk}, read_delay=0.02)
{extra}
sys.argv = ['cry_detector.py'] + {argv!r}
runpy.run_path({detector!r}, run_name='__main__')
'''

REDIRECT_RECORDINGS = '''
import os, wave
_recordings = {target!r}
_makedirs, _wave_open = os.makedirs, wave.open
os.makedirs = lambda path, *a, **k: _makedirs(
    _recordings if str(path).startswith('/media/') else path, *a, **k)
wave.open = lambda f, *a, **k: _wave_open(
    os.path.join(_recordings, os.path.basename(f))
    if isinstance(f, str) and f.startswith('/media/') else f, *a, **k)
'''


class SigtermTests(unittest.TestCase):
    def run_detector(self, argv, chunk='fakes.silence()', extra='', until=None):
        """Start the real script, wait for milestones, then SIGTERM it as STOP does.

        ``until`` is a list of (substring, count) stages consumed in order, so a
        test can say "wait until recording starts, then for 70 more chunks".
        Waiting on output rather than sleeping keeps this deterministic on a slow
        Pi as well as a fast laptop.
        """
        stages = list(until or [('Monitoring audio', 1)])
        script = RUNNER.format(tests_dir=TESTS_DIR, detector=DETECTOR,
                               chunk=chunk, extra=extra, argv=argv)
        process = subprocess.Popen([sys.executable, '-u', '-c', script],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   preexec_fn=os.setsid, text=True)
        prefix, seen = [], 0
        while stages:
            line = process.stdout.readline()
            if not line:
                break
            prefix.append(line)
            if stages[0][0] in line:
                seen += 1
                if seen >= stages[0][1]:
                    stages.pop(0)
                    seen = 0
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        rest, _ = process.communicate(timeout=30)
        return process.returncode, fakes.strip_ansi(''.join(prefix) + rest)

    def test_sigterm_exits_cleanly(self):
        code, output = self.run_detector([])
        self.assertEqual(code, 0, "SIGTERM used to kill the process outright (-15)")
        self.assertIn('Stopping cry detector', output)
        self.assertNotIn('Traceback', output)

    def test_sigterm_saves_a_recording_in_progress(self):
        with tempfile.TemporaryDirectory() as recordings:
            extra = REDIRECT_RECORDINGS.format(target=recordings)
            # frames only count once recording starts, and each is CHUNK/RATE =
            # 0.186s of audio; MIN_RECORDING_DURATION is 10s, so 70 chunks (13s)
            # clears the bar with margin
            code, output = self.run_detector(
                ['--record', '--min-cry', '1'], chunk='fakes.cry()', extra=extra,
                until=[('Started recording episode', 1), ('Cry: TRUE', 70)])
            self.assertEqual(code, 0)
            self.assertIn('Started recording episode', output)
            self.assertIn('Saved episode recording', output,
                          "the in-progress recording must be flushed on STOP")
            self.assertTrue([name for name in os.listdir(recordings)
                             if name.endswith('.wav')],
                            "a wav file should exist on disk")

    def test_a_bad_stop_at_is_refused_before_monitoring(self):
        script = RUNNER.format(tests_dir=TESTS_DIR, detector=DETECTOR,
                               chunk='fakes.silence()', extra='',
                               argv=['--stop-at', '7am'])
        result = subprocess.run([sys.executable, '-u', '-c', script],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Invalid --stop-at format', result.stdout)

    def test_out_of_range_arguments_are_refused(self):
        script = RUNNER.format(tests_dir=TESTS_DIR, detector=DETECTOR,
                               chunk='fakes.silence()', extra='',
                               argv=['--alert', '0'])
        result = subprocess.run([sys.executable, '-u', '-c', script],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2, "argparse should reject it")
        self.assertIn('must be between 1 and 240', result.stderr)


if __name__ == '__main__':
    unittest.main()
