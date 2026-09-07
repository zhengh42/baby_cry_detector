"""Test doubles for the baby monitor.

Importing this module installs fake ``pyaudio`` and ``pushover`` modules into
``sys.modules``, so the detector can be imported and driven on a machine with no
microphone and with no possibility of reaching the network. Import this before
importing ``cry_detector`` or ``web_controller``.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import numpy as np

# The AudioSource the next FakePyAudio instance will serve. make_detector() sets it.
ACTIVE_SOURCE = None

# Segment markers usable in a timeline instead of audio bytes
READ_ERROR = object()    # stream.read() raises IOError, as when the USB mic drops
DEVICE_GONE = object()   # the mic also disappears from the device list


class FakeStream:
    """Stands in for a PyAudio input stream."""

    def __init__(self, source):
        self.source = source
        self.closed = False

    def read(self, frames, exception_on_overflow=True):
        return self.source.next_chunk()

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class FakePyAudio:
    """Stands in for pyaudio.PyAudio, serving whatever ACTIVE_SOURCE describes."""

    def __init__(self):
        self.source = ACTIVE_SOURCE if ACTIVE_SOURCE is not None else AudioSource()

    def get_device_count(self):
        return 1

    def get_device_info_by_index(self, index):
        # A reconnect attempt happens between reads, so re-evaluate the timeline
        self.source.refresh()
        if self.source.device_present:
            return {'name': 'USB PnP Sound Device', 'maxInputChannels': 1}
        return {'name': 'bcm2835 Headphones', 'maxInputChannels': 0}

    def open(self, **kwargs):
        self.source.refresh()
        self.source.opens += 1
        if not self.source.device_present:
            raise IOError("cannot open a device that is not there")
        return FakeStream(self.source)

    def get_sample_size(self, fmt):
        return 2

    def terminate(self):
        self.source.terminates += 1


def _install_module_stubs():
    audio = types.ModuleType('pyaudio')
    audio.PyAudio = FakePyAudio
    audio.paInt16 = 8
    sys.modules['pyaudio'] = audio

    pushover = types.ModuleType('pushover')

    class Client:
        """Records messages instead of sending them."""
        sent = []

        def __init__(self, *args, **kwargs):
            pass

        def send_message(self, message, **kwargs):
            Client.sent.append((message, kwargs))

    pushover.Client = Client
    sys.modules['pushover'] = pushover


_install_module_stubs()

import cry_detector as cd  # noqa: E402  (needs the stubs above)

RATE = cd.RATE
CHUNK = cd.CHUNK
CHUNK_SECONDS = CHUNK / RATE


# --- synthetic audio -------------------------------------------------------

def tone(freq_hz, amplitude):
    """One chunk of a pure sine wave, as the mic would deliver it."""
    t = np.arange(CHUNK) / RATE
    return (amplitude * np.sin(2 * np.pi * freq_hz * t)).astype(np.int16).tobytes()


def silence():
    """One chunk of digital silence, as a dead mic delivers."""
    return np.zeros(CHUNK, dtype=np.int16).tobytes()


def white_noise(amplitude, seed=0):
    """One chunk of broadband noise: loud but with no dominant cry frequency."""
    rng = np.random.default_rng(seed)
    return rng.normal(0, amplitude, CHUNK).astype(np.int16).tobytes()


def cry(amplitude=8000, freq_hz=1000):
    """One chunk that the detector should read as crying."""
    return tone(freq_hz, amplitude)


# --- fake time -------------------------------------------------------------

class FakeClock:
    """A clock the test controls; sleep() advances it instead of blocking.

    Lets an hours-long timeline run in milliseconds, deterministically.
    """

    def __init__(self, start=1_000_000.0):
        self.start = start
        self._now = start

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now += seconds

    def sleep(self, seconds):
        self._now += seconds

    def elapsed(self):
        return self._now - self.start

    def as_time_module(self):
        return types.SimpleNamespace(time=self.now, sleep=self.sleep)


@contextlib.contextmanager
def patched_clock(module, clock):
    """Swap a module's ``time`` for the fake clock for the duration of the block."""
    original = module.time
    module.time = clock.as_time_module()
    try:
        yield clock
    finally:
        module.time = original


# --- the fake microphone ---------------------------------------------------

class AudioSource:
    """Describes what the fake microphone is doing right now."""

    def __init__(self, chunk=None, clock=None, read_delay=None):
        self.chunk = silence() if chunk is None else chunk
        self.read_delay = read_delay   # real seconds per read, when no fake clock
        self.read_fails = False
        self.device_present = True
        self.stop_after_reads = None   # raise KeyboardInterrupt once reached
        self.on_read = None            # optional callable(self) before each chunk
        self.reads = 0
        self.opens = 0
        self.terminates = 0
        self.clock = clock

    def refresh(self):
        """Re-apply the scripted timeline for the current time, if any."""
        if self.on_read is not None:
            self.on_read(self)

    def next_chunk(self):
        self.reads += 1
        if self.clock is not None:
            # a real read blocks for one chunk of audio
            self.clock.advance(CHUNK_SECONDS)
        elif self.read_delay:
            import time as real_time
            real_time.sleep(self.read_delay)
        self.refresh()
        if self.stop_after_reads is not None and self.reads >= self.stop_after_reads:
            raise KeyboardInterrupt
        if self.read_fails:
            raise IOError("simulated audio stream failure")
        return self.chunk

    def play(self, segments, stop_at_end=True):
        """Script the mic over time: a list of (seconds, chunk_or_marker) segments.

        Markers: READ_ERROR (reads raise), DEVICE_GONE (mic leaves the bus too).
        Raises KeyboardInterrupt at the end, which is how the detector shuts down.
        """
        if self.clock is None:
            raise ValueError("play() needs an AudioSource built with a clock")
        boundaries, elapsed = [], 0.0
        for duration, value in segments:
            elapsed += duration
            boundaries.append((elapsed, value))

        def hook(source):
            now = source.clock.elapsed()
            for boundary, value in boundaries:
                if now < boundary:
                    source.read_fails = value in (READ_ERROR, DEVICE_GONE)
                    source.device_present = value is not DEVICE_GONE
                    if not source.read_fails:
                        source.chunk = value
                    return
            if stop_at_end:
                raise KeyboardInterrupt

        self.on_read = hook
        return self


# --- detector fixtures -----------------------------------------------------

@contextlib.contextmanager
def captured_output():
    """Capture the detector's chatty stdout so tests can assert on it."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


def strip_ansi(text):
    import re
    return re.sub(r'\x1B\[[0-9;]*m', '', text)


def make_detector(source=None, clock=None, **attributes):
    """Build a CryDetector wired to the fake mic, with notifications disabled."""
    global ACTIVE_SOURCE
    source = source if source is not None else AudioSource(clock=clock)
    # stays set for the detector's lifetime so reset_audio_backend() gets the same mic
    ACTIVE_SOURCE = source
    with captured_output():
        detector = cd.CryDetector()
    detector.enable_pushover = False
    detector.pushover_client = None
    detector.source = source          # convenience handle for tests
    for name, value in attributes.items():
        setattr(detector, name, value)
    return detector


def run_monitor(detector):
    """Run the monitor loop until the timeline raises KeyboardInterrupt.

    Returns the captured, colour-stripped output.
    """
    with captured_output() as buffer:
        detector.start()
        detector.monitor()
    return strip_ansi(buffer.getvalue())


# --- web controller fixtures ----------------------------------------------

class _ThreadRecorder:
    """Stands in for the threading module, remembering the threads it creates."""

    def __init__(self):
        import threading
        self._threading = threading
        self.threads = []

    def Thread(self, *args, **kwargs):
        thread = self._threading.Thread(*args, **kwargs)
        self.threads.append(thread)
        return thread

    def __getattr__(self, name):
        return getattr(self._threading, name)


def run_and_join(module, call, timeout=5):
    """Run something that spawns background threads and wait for them to finish.

    Captures threads at creation rather than polling enumerate(), which races
    when the worker finishes before the poll (as it does under a fake clock).
    Returns the list of threads it started.
    """
    recorder = _ThreadRecorder()
    original = module.threading
    module.threading = recorder
    try:
        call()
    finally:
        module.threading = original
    for thread in recorder.threads:
        thread.join(timeout=timeout)
    return recorder.threads


def spawn_process(code):
    """A throwaway process in its own group, standing in for the detector."""
    return subprocess.Popen([sys.executable, "-c", code], preexec_fn=os.setsid,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def spawn_sigterm_immune():
    """A process that ignores SIGTERM, like a wedged detector.

    Waits for the child to confirm its handler is installed: signalling before
    that races the default disposition and the process dies of SIGTERM instead.
    """
    code = ("import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "sys.stdout.write('ready\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)")
    process = subprocess.Popen([sys.executable, "-u", "-c", code],
                               preexec_fn=os.setsid, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True)
    if 'ready' not in (process.stdout.readline() or ''):
        process.kill()
        raise RuntimeError("helper process never became ready")
    return process


try:
    import web_controller as _web_controller
    WEB_CONTROLLER_IMPORT_ERROR = None
except Exception as error:  # flask missing, most likely
    _web_controller = None
    WEB_CONTROLLER_IMPORT_ERROR = error


class WebControllerTestCase(unittest.TestCase):
    """Base class giving each test isolated state and no outbound calls.

    config.py holds the user's real Pushover and healthchecks.io credentials, so
    the healthcheck helpers are always replaced: a stray /fail ping un-pauses the
    live check and alerts their phone.
    """

    def setUp(self):
        if _web_controller is None:
            self.skipTest(f"web_controller unavailable: {WEB_CONTROLLER_IMPORT_ERROR}")
        wc = _web_controller
        self.wc = wc
        self.tmpdir = tempfile.mkdtemp()
        self.healthcheck_calls = []
        self._saved = {
            'pause': wc.pause_healthcheck,
            'fail': wc.fail_healthcheck,
            'log_dir': wc.LOG_DIR,
            'state_file': wc.CONFIG_STATE_FILE,
            'detector_state_file': wc.DETECTOR_STATE_FILE,
            'config': dict(wc.current_config),
            'logger_disabled': wc.app.logger.disabled,
        }
        wc.pause_healthcheck = lambda: self._record('pause')
        wc.fail_healthcheck = lambda: self._record('fail')
        wc.LOG_DIR = os.path.join(self.tmpdir, 'log')
        wc.CONFIG_STATE_FILE = os.path.join(self.tmpdir, 'monitor_config.json')
        wc.DETECTOR_STATE_FILE = os.path.join(self.tmpdir, 'detector_state.json')
        wc.LOG_FILE = None
        wc.current_config.clear()
        wc.current_config.update(wc.DEFAULT_CONFIG)
        wc._last_saved_config = None
        wc.detector_process = None
        wc.running_status_port = None
        wc.last_failure = None
        wc.stopping_pids.clear()
        wc.log_buffer.clear()
        wc.event_log_buffer.clear()
        wc.app.logger.disabled = True
        # the controller prints warnings about rejected values; keep the test
        # output readable while still letting a test inspect them
        self._stdout_stack = contextlib.ExitStack()
        self.stdout = self._stdout_stack.enter_context(
            contextlib.redirect_stdout(io.StringIO()))
        self.client = wc.app.test_client()
        self._spawned = []

    def spawn(self, code):
        """Spawn a stand-in detector process, cleaned up after the test."""
        process = spawn_process(code)
        self._spawned.append(process)
        return process

    def spawn_wedged(self):
        """Spawn a stand-in detector that ignores SIGTERM."""
        process = spawn_sigterm_immune()
        self._spawned.append(process)
        return process

    def _record(self, name):
        self.healthcheck_calls.append(name)
        return False

    def tearDown(self):
        wc = self.wc
        self._stdout_stack.close()
        wc.detector_process = None
        for process in self._spawned:
            try:
                os.killpg(os.getpgid(process.pid), 9)
            except Exception:
                pass
            try:
                process.wait(timeout=5)
            except Exception:
                pass
            if process.stdout is not None:
                process.stdout.close()
        wc.pause_healthcheck = self._saved['pause']
        wc.fail_healthcheck = self._saved['fail']
        wc.LOG_DIR = self._saved['log_dir']
        wc.CONFIG_STATE_FILE = self._saved['state_file']
        wc.DETECTOR_STATE_FILE = self._saved['detector_state_file']
        wc.current_config.clear()
        wc.current_config.update(self._saved['config'])
        wc.app.logger.disabled = self._saved['logger_disabled']
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def events(self):
        """Event-log lines with their timestamps stripped."""
        return [line.split('] ', 1)[1] for line in self.wc.event_log_buffer]

    def controller_log(self):
        path = os.path.join(self.wc.LOG_DIR, self.wc.CONTROLLER_LOG)
        if not os.path.exists(path):
            return []
        with open(path) as handle:
            return [line.rstrip() for line in handle]
