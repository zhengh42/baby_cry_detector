#!/usr/bin/env python3
import os
import sys

# Suppress JACK and ALSA warnings
os.environ['JACK_NO_START_SERVER'] = '1'

import pyaudio
import numpy as np


def _create_pyaudio_silently():
    """Create PyAudio instance with stderr suppressed on Linux to hide JACK warnings."""
    if not sys.platform.startswith('linux'):
        return pyaudio.PyAudio()

    # Redirect stderr to /dev/null during PyAudio initialization
    stderr_fd = os.dup(2)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull_fd, 2)
    try:
        audio = pyaudio.PyAudio()
    finally:
        os.dup2(stderr_fd, 2)
        os.close(stderr_fd)
        os.close(devnull_fd)
    return audio


import argparse
from collections import deque
import time
import wave
import urllib.request
import json
import signal
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from pushover import Client

# Import Pushover credentials from config file
try:
    from config import PUSHOVER_USER_KEY, PUSHOVER_API_TOKEN
except ImportError:
    PUSHOVER_USER_KEY = None
    PUSHOVER_API_TOKEN = None
    print("Warning: config.py not found. Pushover notifications disabled.")

# ANSI color codes
RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
RESET = '\033[0m'

# Audio settings
RATE = 44100  # Sample rate
CHUNK = 8192  # Chunks for frequency resolution (~186ms per chunk)
FORMAT = pyaudio.paInt16

# Cry detection parameters - ADJUSTED FOR MORE SENSITIVITY
CRY_FREQ_MIN = 400   # Hz - minimum frequency for baby cry (wider range)
CRY_FREQ_MAX = 2000   # Hz - maximum frequency for baby cry (wider range)
VOLUME_THRESHOLD = 800  # Lower threshold to catch quieter cries
CRY_RATIO_THRESHOLD = 0.40  # Threshold for cry energy ratio
SMOOTHING_WINDOW = 5  # Number of recent chunks to consider
CRY_CONFIRMATION_COUNT = 2  # Need this many positive detections in window
ALERT_WINDOW = 720  # Alert if crying actively happening at 12 minutes (wall clock time)
RESET_WINDOW = 300  # Reset after 5 minutes of silence
MIN_CRY_DURATION = 4  # Seconds of sustained crying before announcing episode (filters brief sounds)
SILENCE_GAP = 2  # Seconds of silence within crying that resets potential cry detection

# Recording settings
RECORDINGS_DIR = '/media/tinybaby/usb-data/recordings'
RECORDING_GRACE_PERIOD = 10  # Keep recording for 10 seconds after crying stops
MIN_RECORDING_DURATION = 10  # Only save recordings at least 10 seconds long
# ~15 minutes at 44100/8192 (~5.4 chunks/sec). Frames are held in RAM until the
# episode ends, so this caps one episode at roughly 82 MB (5000 * 8192 * 2 bytes).
MAX_RECORDING_FRAMES = 5000

# Pushover emergency notification settings
PUSHOVER_RETRY = 30   # Retry every 30 seconds if not acknowledged
PUSHOVER_EXPIRE = 3600  # Give up after 1 hour
# RETRY/EXPIRE above are retries Pushover's own servers make once they hold the
# message. They do nothing for a message that never arrived, so we retry the
# delivery ourselves too.
PUSHOVER_SEND_RETRY = 30  # Seconds between our own attempts to deliver an alert
PUSHOVER_SEND_TIMEOUT = 20  # Seconds to wait for one attempt before retrying

# Silent mic detection
MIC_SILENT_THRESHOLD = 5  # Volume below this is considered "dead silence"
MIC_SILENT_DURATION = 300  # 5 minutes of dead silence triggers alert
STREAM_ERROR_LOG_INTERVAL = 60  # Seconds between repeated audio-stream error lines

# Default HTTP status server port, bound to localhost only: the web controller is
# the only local consumer, and an open port means anyone on the WiFi can retune or
# mute the monitor. Pass --status-bind 0.0.0.0 to expose it (e.g. for receiver.py).
DEFAULT_STATUS_PORT = 8080
DEFAULT_STATUS_BIND = '127.0.0.1'

# Accepted range for each setting, as (minimum, maximum). Mirrors CONFIG_LIMITS in
# web_controller.py; zero or negative values break detection (alert_window=0 would
# alert on every audio chunk), so both the CLI and /config clamp or reject.
VOLUME_RANGE = (50, 30000)
CRY_FREQ_MIN_RANGE = (100, 1900)  # must stay under CRY_FREQ_MAX
RATIO_RANGE = (0.05, 0.95)
ALERT_MINUTES_RANGE = (1, 240)
RESET_MINUTES_RANGE = (1, 240)
MIN_CRY_RANGE = (1, 300)          # seconds
SILENCE_GAP_RANGE = (1, 120)      # seconds
HEARTBEAT_MINUTES_RANGE = (1, 60)
STATUS_PORT_RANGE = (1024, 65535)

# Live-tunable via POST /config: request key -> (attribute, coerce, minimum, maximum).
# Windows arrive in seconds here, unlike the CLI which takes minutes.
LIVE_CONFIG_FIELDS = {
    'volume_threshold': ('volume_threshold', int) + VOLUME_RANGE,
    'cry_freq_min': ('cry_freq_min', int) + CRY_FREQ_MIN_RANGE,
    'alert_window': ('alert_window', int, ALERT_MINUTES_RANGE[0] * 60, ALERT_MINUTES_RANGE[1] * 60),
    'reset_window': ('reset_window', int, RESET_MINUTES_RANGE[0] * 60, RESET_MINUTES_RANGE[1] * 60),
    'min_cry_duration': ('min_cry_duration', int) + MIN_CRY_RANGE,
    'silence_gap': ('silence_gap', int) + SILENCE_GAP_RANGE,
}


def bounded_int(low, high):
    """argparse type that rejects out-of-range integers with a clear message."""
    def parse(text):
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"'{text}' is not an integer")
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}, got {value}")
        return value
    return parse


def bounded_float(low, high):
    """argparse type that rejects out-of-range floats with a clear message."""
    def parse(text):
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"'{text}' is not a number")
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}, got {value}")
        return value
    return parse


def create_status_handler(detector):
    """Create a request handler class with access to the detector instance"""
    class StatusHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Suppress default logging
            pass

        def do_GET(self):
            if self.path == '/status':
                # Calculate episode duration if crying
                episode_duration = 0
                if detector.initial_start_time is not None:
                    episode_duration = time.time() - detector.initial_start_time

                # Time since last cry detection (within current episode)
                since_last_cry = 0
                if detector.initial_start_time is not None and detector.last_cry_time is not None:
                    since_last_cry = time.time() - detector.last_cry_time

                # Silence duration when not in episode
                silence_since = 0
                if detector.initial_start_time is None:
                    ref_time = detector.last_episode_end_time or detector.session_start_time
                    if ref_time:
                        silence_since = time.time() - ref_time

                # Audio stream health, so consumers can tell "all quiet" apart
                # from "not listening at all". Falls back to the session start
                # when no chunk has ever arrived: a mic that opens but never
                # delivers leaves the monitor loop parked in read() with nothing
                # to notice it, and "no audio since we started" is the honest
                # answer there - reporting nothing let the dashboard draw a
                # reassuring "Quiet" over a microphone that was never listening.
                reference_time = detector.last_chunk_time or detector.session_start_time
                seconds_since_audio = None
                if reference_time is not None:
                    seconds_since_audio = round(time.time() - reference_time, 1)

                status = {
                    "crying": detector.initial_start_time is not None,
                    "alarm": detector.alarm_active,
                    "mic_silent": detector.mic_silent,
                    "stream_ok": detector.stream_ok,
                    "seconds_since_audio": seconds_since_audio,
                    "episode_duration": round(episode_duration, 1),
                    "since_last_cry": round(since_last_cry, 1),
                    "silence_since": round(silence_since, 1),
                    "timestamp": datetime.now().isoformat(),
                    "pushover_device": detector.pushover_device
                }
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps(status).encode())
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path != '/config':
                self.send_response(404)
                self.end_headers()
                return

            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8', errors='replace')
            try:
                config = json.loads(body)
            except json.JSONDecodeError:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
                return
            if not isinstance(config, dict):
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Expected a JSON object"}).encode())
                return

            rejected = []

            if 'pushover_device' in config:
                device = config['pushover_device']
                if isinstance(device, str):
                    old_device = detector.pushover_device
                    detector.pushover_device = device
                    print(f"{GREEN}Pushover device changed: {old_device} -> {device}{RESET}")
                else:
                    rejected.append('pushover_device')

            for key, (attr, coerce, low, high) in LIVE_CONFIG_FIELDS.items():
                if key not in config:
                    continue
                try:
                    value = coerce(config[key])
                except (TypeError, ValueError):
                    rejected.append(key)
                    continue
                clamped = max(low, min(high, value))
                old = getattr(detector, attr)
                setattr(detector, attr, clamped)
                note = f" (clamped from {value})" if clamped != value else ""
                print(f"{GREEN}Config: {key} {old} -> {clamped}{note}{RESET}")

            if rejected:
                print(f"{YELLOW}Config: ignored invalid values for {', '.join(rejected)}{RESET}")

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "rejected": rejected}).encode())
    return StatusHandler


class CryDetector:
    def __init__(self):
        self.audio = _create_pyaudio_silently()
        self.stream = None
        self.initial_start_time = None  # When confirmed crying episode began
        self.last_cry_time = None  # Most recent confirmed cry detection
        self.last_smoothed_cry_time = None  # Most recent smoothed detection (for brief sound logic)
        self.recent_detections = deque(maxlen=SMOOTHING_WINDOW)
        self.alarm_active = False  # True once an alert has fired, until the baby settles
        self.potential_cry_start_time = None  # When crying first detected (before confirmation)
        self.session_start_time = None  # When the detector session started
        self.session_start_minute = None  # (hour, minute) it started, for auto-stop
        self.last_episode_end_time = None  # When the last crying episode ended
        
        # Recording state - only record during crying episodes
        self.is_recording = False
        self.recording_truncated = False  # True once an episode hit the memory cap
        self.current_episode_frames = []
        self.episode_start_time = None
        self.last_detected_cry_time = None  # For grace period
        self.chunk_count = 0  # For deterministic debug logging
        
        # Configurable options (can be overridden before start())
        self.volume_threshold = VOLUME_THRESHOLD
        self.cry_ratio_threshold = CRY_RATIO_THRESHOLD
        self.cry_freq_min = CRY_FREQ_MIN
        self.enable_recording = False
        self.enable_pushover = False
        self.pushover_device = None  # Send to all devices by default
        self.alert_window = ALERT_WINDOW
        self.reset_window = RESET_WINDOW
        self.min_cry_duration = MIN_CRY_DURATION
        self.silence_gap = SILENCE_GAP

        # Healthcheck settings
        self.healthcheck_url = None
        self.heartbeat_interval = 300  # 5 minutes in seconds
        self.last_heartbeat_time = None

        # Auto-stop settings
        self.stop_time = None  # datetime.time object, e.g. 07:00

        # HTTP status server settings
        self.status_server = None
        self.status_port = DEFAULT_STATUS_PORT
        self.status_bind = DEFAULT_STATUS_BIND
        self.enable_status_server = False

        # Silent mic detection
        self.mic_silent_since = None  # When consecutive dead silence started
        self.mic_silent = False  # True when mic appears dead

        # Audio stream health
        self.stream_ok = True
        self.last_chunk_time = None  # Last successful chunk read
        self.last_stream_error_log = None  # For throttling error output

        # Initialize Pushover client
        if PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN:
            try:
                self.pushover_client = Client(PUSHOVER_USER_KEY, api_token=PUSHOVER_API_TOKEN)
                print(f"{GREEN}✓ Pushover client initialized{RESET}")
            except Exception as e:
                print(f"{YELLOW}⚠ Pushover initialization failed: {e}{RESET}")
                self.pushover_client = None
        else:
            self.pushover_client = None
            print(f"{YELLOW}⚠ Pushover credentials not found - notifications disabled{RESET}")

    def find_usb_audio_device(self, verbose=True):
        """Find USB audio device index by name"""
        for i in range(self.audio.get_device_count()):
            info = self.audio.get_device_info_by_index(i)
            if 'USB' in info['name'] and info['maxInputChannels'] > 0:
                if verbose:
                    print(f"{GREEN}✓ Found USB audio device: {info['name']} (index {i}){RESET}")
                return i
        return None

    def start_audio_stream(self, verbose=True):
        """Start or restart just the audio stream (safe for reconnection)."""
        device_index = self.find_usb_audio_device(verbose=verbose)
        if device_index is None:
            if verbose:
                print(f"{RED}✗ No USB audio device found. Available devices:{RESET}")
                for i in range(self.audio.get_device_count()):
                    info = self.audio.get_device_info_by_index(i)
                    if info['maxInputChannels'] > 0:
                        print(f"  [{i}] {info['name']}")
            raise RuntimeError("No USB audio device found")

        self.stream = self.audio.open(
            format=FORMAT,
            channels=1,
            rate=RATE,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=CHUNK
        )

    def log_stream_error(self, message):
        """Print an audio-stream error, throttled so a dead mic can't flood the log."""
        now = time.time()
        if (self.last_stream_error_log is not None
                and (now - self.last_stream_error_log) < STREAM_ERROR_LOG_INTERVAL):
            return
        self.last_stream_error_log = now
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{RED}[{timestamp}] ✗ {message}{RESET}")

    def reset_detection_state(self):
        """Discard the smoothing window and any unconfirmed cry after an outage.

        Chunks captured before the mic died say nothing about the room after it
        came back, and CRY_CONFIRMATION_COUNT of them are enough to re-confirm
        crying on the very first chunk of the reconnect. Since the alert window
        is measured from initial_start_time, which kept running while the mic
        was dead, a mic that dropped mid-cry would fire an emergency alert into
        a silent room. Clearing this forces a fresh min_cry_duration of real
        audio before anything can alarm again; the episode itself is left alone
        so a brief dropout does not lose a genuine cry, and an outage longer
        than reset_window still settles the episode on the silence that follows.
        """
        self.recent_detections.clear()
        self.potential_cry_start_time = None
        self.last_smoothed_cry_time = None

    def reset_audio_backend(self):
        """Re-create the PyAudio instance so a re-plugged USB mic is rediscovered.

        PyAudio enumerates devices at init, so a mic unplugged and plugged back in
        is invisible until the backend is torn down and rebuilt.
        """
        try:
            self.audio.terminate()
        except Exception:
            pass
        self.audio = _create_pyaudio_silently()

    def start(self):
        """Start the audio stream, status server, and print config."""
        self.start_audio_stream()
        self.session_start_time = time.time()
        now = datetime.now()
        self.session_start_minute = (now.hour, now.minute)
        print("Cry detector started. Monitoring audio...")
        print(f"Configuration: {self.cry_freq_min}-{CRY_FREQ_MAX} Hz, Chunk: {CHUNK} samples (~{CHUNK/RATE*1000:.0f}ms)")
        print(f"Volume threshold: {self.volume_threshold}, Ratio threshold: {self.cry_ratio_threshold}")
        print(f"Confirmation: {CRY_CONFIRMATION_COUNT}/{SMOOTHING_WINDOW} chunks")
        print(f"Min cry duration: {self.min_cry_duration} seconds (filters brief sounds)")
        print(f"Silence gap: {self.silence_gap} seconds (resets potential cry)")
        print(f"Alert window: {self.alert_window/60:.0f} minutes")
        print(f"Reset after {self.reset_window/60:.0f} minutes of silence")
        if self.enable_pushover and self.pushover_client:
            print(f"{GREEN}Pushover: Enabled (retry every {PUSHOVER_RETRY}s, expire after {PUSHOVER_EXPIRE/60:.0f} min){RESET}")
        else:
            print(f"{YELLOW}Pushover: Disabled (use --pushover to enable){RESET}")
        if self.enable_recording:
            print(f"{GREEN}Recording: Saving to {RECORDINGS_DIR}{RESET}")
            print(f"{GREEN}Recording grace period: {RECORDING_GRACE_PERIOD}s, Min duration: {MIN_RECORDING_DURATION}s{RESET}")
        else:
            print(f"{YELLOW}Recording: Disabled (use --record to enable){RESET}")
        if self.stop_time:
            print(f"{GREEN}Auto-stop: {self.stop_time.strftime('%H:%M')}{RESET}")
        else:
            print(f"{YELLOW}Auto-stop: Disabled (use --stop-at HH:MM to enable){RESET}")
        if self.healthcheck_url:
            print(f"{GREEN}Healthcheck: Enabled (ping every {self.heartbeat_interval // 60} min){RESET}")
        else:
            print(f"{YELLOW}Healthcheck: Disabled (use --healthcheck URL to enable){RESET}")
        if self.enable_status_server:
            self.start_status_server()
            print(f"{GREEN}Status server: http://{self.status_bind}:{self.status_port}/status{RESET}")
        else:
            print(f"{YELLOW}Status server: Disabled (use --status-port PORT to enable){RESET}")

    def start_status_server(self):
        """Start the HTTP status server in a background thread"""
        handler = create_status_handler(self)
        self.status_server = HTTPServer((self.status_bind, self.status_port), handler)
        thread = threading.Thread(target=self.status_server.serve_forever, daemon=True)
        thread.start()

    def stop_status_server(self):
        """Stop the HTTP status server and release its port"""
        if self.status_server:
            self.status_server.shutdown()
            # shutdown() only stops serve_forever. Dropping the reference below
            # happens to close the socket under CPython refcounting, but closing
            # it explicitly is deterministic and avoids a ResourceWarning.
            self.status_server.server_close()
            self.status_server = None

    def send_heartbeat(self):
        """Send heartbeat ping in a background thread so retries don't block monitoring."""
        if not self.healthcheck_url:
            return
        url = self.healthcheck_url
        is_fail = self.mic_silent or not self.stream_ok
        if is_fail:
            url = url.rstrip('/') + '/fail'

        def _send(url, is_fail):
            retry_delay = 30  # seconds
            # Give up before the next heartbeat is due, otherwise a long outage
            # leaves one retrying thread per interval, all pinging on recovery
            deadline = time.time() + max(self.heartbeat_interval, retry_delay)
            attempt = 0
            while True:
                try:
                    urllib.request.urlopen(url, timeout=10)
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    if is_fail:
                        reason = "mic silent" if self.mic_silent else "audio stream down"
                        print(f"{RED}[{timestamp}] ♥ Heartbeat FAIL sent ({reason}){RESET}")
                    else:
                        msg = f"[{timestamp}] ♥ Heartbeat sent"
                        if attempt > 0:
                            msg += f" (after {attempt} {'retry' if attempt == 1 else 'retries'})"
                        print(f"{GREEN}{msg}{RESET}")
                    return
                except Exception as e:
                    attempt += 1
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    if time.time() + retry_delay >= deadline:
                        print(f"{YELLOW}[{timestamp}] ⚠ Heartbeat failed after {attempt} "
                              f"{'try' if attempt == 1 else 'tries'}, giving up until next "
                              f"heartbeat: {e}{RESET}")
                        return
                    print(f"{YELLOW}[{timestamp}] ⚠ Heartbeat failed (retry in {retry_delay}s): {e}{RESET}")
                    time.sleep(retry_delay)

        threading.Thread(target=_send, args=(url, is_fail), daemon=True).start()

    def ping_healthcheck_fail(self, healthcheck_url):
        """Escalate through healthchecks.io when an alert could not be delivered,
        so a phone that never buzzed still surfaces somewhere."""
        if not healthcheck_url:
            return
        try:
            urllib.request.urlopen(healthcheck_url.rstrip('/') + '/fail', timeout=10)
            print(f"{YELLOW}⚠ Healthcheck FAIL sent (Pushover delivery failed){RESET}")
        except Exception:
            pass

    def send_emergency_notification(self, elapsed_time):
        """Deliver the emergency alert from a background thread, retrying on failure.

        Sending inline cost us twice over. The pushover library posts with no
        timeout, so a half-dead network (router up, nothing getting through) parked
        the monitor loop in a connect for as long as the OS allowed - not reading
        audio, not sending heartbeats, and reporting a stale status that made the
        web UI blame the microphone. And one failed POST discarded the alert
        outright, so a brief blip at the wrong moment meant no notification at all,
        with the next attempt a full alert_window of crying away.

        Retries stop before the next alert could fire, so an outage cannot stack up
        one retrying thread per alert.
        """
        client = self.pushover_client
        healthcheck_url = self.healthcheck_url
        start = time.time()
        deadline = start + max(self.alert_window, PUSHOVER_SEND_RETRY)

        def _attempt(message, device):
            """One delivery attempt, bounded in time.

            The pushover library posts with no timeout and accepts no timeout
            argument - extra keywords become request parameters - so a stalled
            connection can park a send indefinitely. Waiting on a worker keeps
            the retry schedule and the give-up escalation honest. A stalled
            attempt is abandoned rather than cancelled, so it may still land
            later: a duplicate emergency alert is a far better failure than a
            silent one.
            """
            outcome = {}

            def deliver():
                try:
                    client.send_message(
                        message,
                        title="🚨 BABY MONITOR EMERGENCY",
                        priority=2,  # Emergency - requires acknowledgment
                        retry=PUSHOVER_RETRY,    # Retry every 30 seconds
                        expire=PUSHOVER_EXPIRE,  # Give up after 1 hour
                        device=device  # None sends to all devices
                    )
                    outcome['sent'] = True
                except Exception as error:
                    outcome['error'] = error

            worker = threading.Thread(target=deliver, daemon=True)
            worker.start()
            worker.join(PUSHOVER_SEND_TIMEOUT)
            if outcome.get('sent'):
                return None
            return outcome.get(
                'error',
                TimeoutError(f"no response after {PUSHOVER_SEND_TIMEOUT}s"))

        def _send():
            attempt = 0
            while True:
                # Re-read each time round: a parent can switch who is on duty, or
                # mute, while we are still retrying a failed alert
                device = self.pushover_device
                if device == '__muted__':
                    print(f"{YELLOW}🔇 Notifications muted while retrying; "
                          f"alert abandoned{RESET}")
                    return

                message = (f"Baby has been crying for {elapsed_time/60:.1f} minutes! "
                           "Please check on the baby.")
                late = time.time() - start
                if late >= PUSHOVER_SEND_RETRY:
                    # Say so rather than letting a delayed alert read as current
                    how_late = f"{late:.0f} sec" if late < 90 else f"{late/60:.0f} min"
                    message += (f" (Alert delayed {how_late} - the monitor "
                                "could not reach the network.)")

                e = _attempt(message, device)
                if e is not None:
                    attempt += 1
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    if time.time() + PUSHOVER_SEND_RETRY >= deadline:
                        print(f"{RED}[{timestamp}] ✗ Emergency notification FAILED after "
                              f"{attempt} {'try' if attempt == 1 else 'tries'}, "
                              f"giving up: {e}{RESET}")
                        self.ping_healthcheck_fail(healthcheck_url)
                        return
                    print(f"{YELLOW}[{timestamp}] ⚠ Emergency notification failed "
                          f"(retry in {PUSHOVER_SEND_RETRY}s): {e}{RESET}")
                    time.sleep(PUSHOVER_SEND_RETRY)
                    continue

                timestamp = datetime.now().strftime("%H:%M:%S")
                msg = (f"[{timestamp}] ✓ Emergency notification sent! "
                       f"(will retry every {PUSHOVER_RETRY}s until acknowledged)")
                if attempt:
                    msg += f" (delivered after {attempt} failed "
                    msg += f"{'try' if attempt == 1 else 'tries'})"
                print(f"{GREEN}{msg}{RESET}")
                return

        threading.Thread(target=_send, daemon=True).start()

    def maybe_send_heartbeat(self, current_time):
        """Send a heartbeat if enabled and the interval has elapsed."""
        if not self.healthcheck_url:
            return
        if (self.last_heartbeat_time is None
                or (current_time - self.last_heartbeat_time) >= self.heartbeat_interval):
            self.send_heartbeat()
            self.last_heartbeat_time = current_time

    def analyze_audio(self, audio_data):
        """Analyze audio chunk for crying"""
        # Convert to numpy array
        audio_np = np.frombuffer(audio_data, dtype=np.int16)
        
        # Check volume level
        volume = np.abs(audio_np).mean()
        
        # Perform FFT to get frequency spectrum
        fft = np.fft.rfft(audio_np)
        freqs = np.fft.rfftfreq(len(audio_np), 1/RATE)
        magnitudes = np.abs(fft)
        
        # Find dominant frequency
        dominant_freq_idx = np.argmax(magnitudes)
        dominant_freq = freqs[dominant_freq_idx]
        
        # Calculate energy in cry frequency band
        cry_band_mask = (freqs >= self.cry_freq_min) & (freqs <= CRY_FREQ_MAX)
        cry_energy = np.sum(magnitudes[cry_band_mask])
        total_energy = np.sum(magnitudes)
        cry_ratio = cry_energy / total_energy if total_energy > 0 else 0
        
        # Detect cry based on multiple criteria
        has_volume = volume > self.volume_threshold
        in_cry_freq = self.cry_freq_min <= dominant_freq <= CRY_FREQ_MAX
        has_cry_energy = cry_ratio > self.cry_ratio_threshold
        
        is_crying_now = has_volume and (in_cry_freq or has_cry_energy)
        
        # Debug output - all positive events, 10% of negative events
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if is_crying_now:
            # Always print when crying detected
            print(f"{RED}[{timestamp}] [Monitor] Vol: {volume:.0f}, Freq: {dominant_freq:.1f} Hz, Ratio: {cry_ratio:.2f}, Cry: TRUE{RESET}")
        elif self.chunk_count % 10 == 0:
            # Print every 10th non-crying chunk for deterministic logging
            print(f"[{timestamp}] [Monitor] Vol: {volume:.0f}, Freq: {dominant_freq:.1f} Hz, Ratio: {cry_ratio:.2f}, Cry: False")
        
        return is_crying_now, volume, dominant_freq, cry_ratio
    
    def save_episode_recording(self):
        """Save the current episode's audio recording"""
        if not self.current_episode_frames or self.episode_start_time is None:
            return
        
        # Calculate duration
        duration = len(self.current_episode_frames) * CHUNK / RATE
        
        # Only save if duration meets minimum threshold
        if duration < MIN_RECORDING_DURATION:
            print(f"{YELLOW}⏭ Skipping short recording ({duration:.1f}s < {MIN_RECORDING_DURATION}s){RESET}")
            return
        
        # Create recordings directory on USB drive if it doesn't exist
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        
        # Generate filename with episode start timestamp
        timestamp = datetime.fromtimestamp(self.episode_start_time).strftime("%Y%m%d_%H%M%S")
        filename = f'{RECORDINGS_DIR}/episode_{timestamp}_{duration:.0f}s.wav'
        
        try:
            with wave.open(filename, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(self.audio.get_sample_size(FORMAT))
                wf.setframerate(RATE)
                wf.writeframes(b''.join(self.current_episode_frames))
            print(f"{GREEN}✓ Saved episode recording: {filename} ({len(self.current_episode_frames)} chunks, {duration:.1f}s){RESET}")
        except Exception as e:
            print(f"{RED}✗ Failed to save episode recording: {e}{RESET}")

    def monitor(self):
        """Main monitoring loop"""
        try:
            while True:
                # Auto-stop and heartbeats run first so they keep working even
                # while the audio stream is down
                if self.stop_time:
                    now = datetime.now()
                    # Match the whole minute, not just its first two seconds. A
                    # dropped audio stream makes this loop sleep 2-5 seconds per
                    # pass instead of ~0.19, and a two-second window is then
                    # simply stepped over - so a mic that died overnight also
                    # meant the detector never stopped in the morning.
                    #
                    # Still wall-clock rather than a deadline computed at start:
                    # the Pi has no battery-backed clock, so it comes back from a
                    # power cut believing it is still the time of the cut and NTP
                    # then jumps it forward. A precomputed deadline would already
                    # be in the past when that jump lands and would stop the
                    # detector on the spot; comparing the clock as it reads now
                    # just declines to match, and monitoring continues.
                    #
                    # A session that starts inside the stop minute is left alone,
                    # otherwise starting at 07:00:30 with a 07:00 stop would quit
                    # immediately.
                    if ((now.hour, now.minute) == (self.stop_time.hour, self.stop_time.minute)
                            and (now.hour, now.minute) != self.session_start_minute):
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"\n{GREEN}[{timestamp}] Auto-stop time reached ({self.stop_time.strftime('%H:%M')}). Shutting down...{RESET}")
                        break

                # Send heartbeat if enabled and interval has elapsed (sent as a /fail
                # ping while the stream is down, so a dead mic surfaces without
                # waiting for the healthchecks.io period to expire)
                self.maybe_send_heartbeat(time.time())

                # (Re)open the stream if a previous read or reconnect failed
                if self.stream is None:
                    try:
                        self.start_audio_stream(verbose=False)
                    except Exception as reconnect_error:
                        self.log_stream_error(f"Reconnection failed: {reconnect_error}")
                        # PyAudio caches the device list, so rebuild it to notice a re-plug
                        self.reset_audio_backend()
                        time.sleep(5)
                        continue

                # Read audio chunk; on failure drop the stream and reconnect next pass
                try:
                    audio_data = self.stream.read(CHUNK, exception_on_overflow=False)
                except (IOError, OSError) as e:
                    # Mark the stream down so /status stops reporting stale "all quiet"
                    self.stream_ok = False
                    self.log_stream_error(f"Audio stream error: {e} - attempting to reconnect")
                    self.cleanup_stream()
                    self.reset_detection_state()
                    time.sleep(2)
                    continue

                if not self.stream_ok:
                    self.stream_ok = True
                    self.last_stream_error_log = None
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(f"\n{GREEN}[{timestamp}] ✓ Audio stream recovered{RESET}")

                self.last_chunk_time = time.time()
                self.chunk_count += 1
                current_time = time.time()

                # Analyze for crying
                is_crying_now, volume, freq, ratio = self.analyze_audio(audio_data)

                # Silent mic detection
                if volume < MIC_SILENT_THRESHOLD:
                    if self.mic_silent_since is None:
                        self.mic_silent_since = current_time
                    elif not self.mic_silent and (current_time - self.mic_silent_since) >= MIC_SILENT_DURATION:
                        self.mic_silent = True
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"\n{RED}[{timestamp}] ⚠ MICROPHONE SILENT for {MIC_SILENT_DURATION // 60} minutes! Possible malfunction.{RESET}")
                else:
                    if self.mic_silent:
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"\n{GREEN}[{timestamp}] ✓ Microphone recovered{RESET}")
                    self.mic_silent_since = None
                    self.mic_silent = False

                # Add to recent detections for smoothing
                self.recent_detections.append(is_crying_now)

                # Count positive detections in recent window
                cry_count = sum(self.recent_detections)

                # Smoothed detection (immediate, before min_cry_duration check)
                smoothed_crying = cry_count >= CRY_CONFIRMATION_COUNT

                # Track potential cry start time for sustained detection
                if smoothed_crying:
                    if self.potential_cry_start_time is None:
                        self.potential_cry_start_time = current_time
                    self.last_smoothed_cry_time = current_time
                else:
                    # Check if brief silence should reset potential cry
                    if self.potential_cry_start_time is not None and self.last_smoothed_cry_time is not None:
                        silence_duration = current_time - self.last_smoothed_cry_time
                        if silence_duration >= self.silence_gap:
                            # Brief sound ended - not sustained crying
                            brief_duration = self.last_smoothed_cry_time - self.potential_cry_start_time
                            if brief_duration < self.min_cry_duration:
                                print(f"{YELLOW}⏭ Ignored brief sound ({brief_duration:.1f}s < {self.min_cry_duration}s){RESET}")
                            self.potential_cry_start_time = None

                # CRYING requires sustained smoothed detection for min_cry_duration
                crying = False
                if smoothed_crying and self.potential_cry_start_time is not None:
                    sustained_duration = current_time - self.potential_cry_start_time
                    if sustained_duration >= self.min_cry_duration:
                        crying = True

                # Update last cry time when confirmed crying
                if crying:
                    self.last_cry_time = current_time
                    self.last_detected_cry_time = current_time

                # Handle recording with grace period (only for confirmed crying)
                if self.enable_recording and crying:
                    if not self.is_recording:
                        # Start recording this episode
                        self.is_recording = True
                        self.episode_start_time = current_time
                        self.recording_truncated = False
                        self.current_episode_frames = []
                        print(f"{YELLOW}📹 Started recording episode{RESET}")

                    # Add audio chunk to current episode (with memory limit)
                    if len(self.current_episode_frames) < MAX_RECORDING_FRAMES:
                        self.current_episode_frames.append(audio_data)
                    elif not self.recording_truncated:
                        self.recording_truncated = True
                        print(f"{YELLOW}⚠ Recording hit the {MAX_RECORDING_FRAMES}-chunk "
                              f"memory cap; the rest of this episode is not recorded{RESET}")

                elif self.enable_recording and self.is_recording:
                    # Not currently crying but we're recording
                    # Continue recording during grace period
                    grace_elapsed = current_time - self.last_detected_cry_time if self.last_detected_cry_time else 0

                    if grace_elapsed < RECORDING_GRACE_PERIOD:
                        # Still within grace period - keep recording (with memory limit)
                        if len(self.current_episode_frames) < MAX_RECORDING_FRAMES:
                            self.current_episode_frames.append(audio_data)
                    else:
                        # Grace period expired - stop recording
                        self.is_recording = False
                        print(f"{YELLOW}⏹ Stopped recording episode (grace period expired){RESET}")
                        # Save the episode recording
                        self.save_episode_recording()
                        # Clear for next episode
                        self.current_episode_frames = []
                        self.episode_start_time = None

                # Handle confirmed crying detection (alert logic)
                if crying:
                    # Set initial start time if this is a new episode
                    if self.initial_start_time is None:
                        self.initial_start_time = self.potential_cry_start_time
                        self.alarm_active = False
                        sustained_duration = current_time - self.potential_cry_start_time
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"\n{RED}[{timestamp}] 🙁 Baby started crying! (sustained for {sustained_duration:.1f}s, Vol: {volume:.0f}, Freq: {freq:.1f} Hz){RESET}")

                    # Check if we should alert
                    elapsed_time = current_time - self.initial_start_time
                    if elapsed_time >= self.alert_window:
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        self.alarm_active = True

                        print(f"\n{RED}[{timestamp}] 🚨 ALERT! Baby has been crying for {elapsed_time/60:.1f} minutes!{RESET}")
                        print(f"{RED}   (Episode started at {datetime.fromtimestamp(self.initial_start_time).strftime('%H:%M:%S')}, currently crying){RESET}")

                        # Send EMERGENCY Pushover notification (skip if muted).
                        # Handed to a background thread: the monitor loop must keep
                        # listening to the baby whatever the network is doing.
                        if self.pushover_client and self.enable_pushover and self.pushover_device != '__muted__':
                            self.send_emergency_notification(elapsed_time)
                        elif self.pushover_device == '__muted__':
                            print(f"{YELLOW}🔇 Notification muted{RESET}")

                        # Reset episode — if baby keeps crying, a new episode starts immediately
                        print(f"{YELLOW}   Episode reset. New episode starts if crying continues.{RESET}")
                        self.last_episode_end_time = self.last_cry_time
                        self.initial_start_time = current_time
                        self.potential_cry_start_time = current_time

                else:
                    # Not crying - check if we should reset confirmed episode
                    if self.initial_start_time is not None and self.last_cry_time is not None:
                        silence_duration = current_time - self.last_cry_time

                        # Reset after silence window
                        if silence_duration >= self.reset_window:
                            episode_duration = self.last_cry_time - self.initial_start_time
                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            print(f"\n{GREEN}[{timestamp}] ☺️ Baby settled! Episode duration: {episode_duration/60:.1f} minutes{RESET}")
                            print(f"{GREEN}   (Silent for {silence_duration/60:.1f} minutes){RESET}")

                            # Reset everything
                            self.last_episode_end_time = self.last_cry_time
                            self.initial_start_time = None
                            self.last_cry_time = None
                            self.potential_cry_start_time = None
                            self.alarm_active = False

        except KeyboardInterrupt:
            print(f"\n\n{YELLOW}Stopping cry detector...{RESET}")

            # Save any ongoing recording
            if self.is_recording and self.current_episode_frames:
                print(f"{YELLOW}Saving final episode recording...{RESET}")
                self.save_episode_recording()

            if self.initial_start_time is not None and self.last_cry_time is not None:
                episode_duration = self.last_cry_time - self.initial_start_time
                print(f"Final episode duration: {episode_duration/60:.1f} minutes")
        finally:
            self.cleanup()

    def cleanup_stream(self):
        """Close audio stream without terminating PyAudio"""
        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def cleanup(self):
        """Clean up audio resources"""
        self.stop_status_server()
        self.cleanup_stream()
        self.audio.terminate()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Baby cry detector with USB audio and Pushover notifications')
    parser.add_argument('-v', '--volume', type=bounded_int(*VOLUME_RANGE), default=VOLUME_THRESHOLD,
                        help=f'Volume threshold (default: {VOLUME_THRESHOLD})')
    parser.add_argument('--cry-freq-min', type=bounded_int(*CRY_FREQ_MIN_RANGE), default=CRY_FREQ_MIN,
                        help=f'Minimum cry frequency in Hz (default: {CRY_FREQ_MIN})')
    parser.add_argument('-r', '--ratio', type=bounded_float(*RATIO_RANGE), default=CRY_RATIO_THRESHOLD,
                        help=f'Cry ratio threshold (default: {CRY_RATIO_THRESHOLD})')
    parser.add_argument('--record', action='store_true', default=False,
                        help='Enable recording of crying episodes (default: disabled)')
    parser.add_argument('--pushover', action='store_true', default=False,
                        help='Enable Pushover emergency notifications (default: disabled)')
    parser.add_argument('--pushover-device', type=str, default=None,
                        help='Pushover device name to send to (default: all devices)')
    parser.add_argument('--alert', type=bounded_int(*ALERT_MINUTES_RANGE), default=ALERT_WINDOW // 60,
                        help=f'Minutes of crying before alert (default: {ALERT_WINDOW // 60})')
    parser.add_argument('--reset', type=bounded_int(*RESET_MINUTES_RANGE), default=RESET_WINDOW // 60,
                        help=f'Minutes of silence before episode reset (default: {RESET_WINDOW // 60})')
    parser.add_argument('--min-cry', type=bounded_int(*MIN_CRY_RANGE), default=MIN_CRY_DURATION,
                        help=f'Seconds of sustained crying before announcing episode (default: {MIN_CRY_DURATION})')
    parser.add_argument('--silence-gap', type=bounded_int(*SILENCE_GAP_RANGE), default=SILENCE_GAP,
                        help=f'Seconds of silence within crying that resets detection (default: {SILENCE_GAP})')
    parser.add_argument('--healthcheck', type=str, default=None,
                        help='Healthchecks.io ping URL for heartbeat monitoring')
    parser.add_argument('--heartbeat', type=bounded_int(*HEARTBEAT_MINUTES_RANGE), default=5,
                        help='Heartbeat interval in minutes (default: 5)')
    parser.add_argument('--stop-at', type=str, default=None,
                        help='Time to auto-stop the script (HH:MM format, e.g. 07:00)')
    parser.add_argument('--status-port', type=bounded_int(*STATUS_PORT_RANGE), default=None,
                        help=f'Enable HTTP status server on this port (default: disabled, use {DEFAULT_STATUS_PORT})')
    parser.add_argument('--status-bind', type=str, default=DEFAULT_STATUS_BIND,
                        help=f'Address for the status server (default: {DEFAULT_STATUS_BIND}; '
                             'use 0.0.0.0 to allow other machines, e.g. receiver.py)')
    args = parser.parse_args()

    def _handle_sigterm(signum, frame):
        """Web UI STOP and `systemctl stop` send SIGTERM; reuse the Ctrl+C path so
        an in-progress recording is saved and audio/HTTP resources are released."""
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_sigterm)

    detector = CryDetector()
    detector.volume_threshold = args.volume
    detector.cry_freq_min = args.cry_freq_min
    detector.cry_ratio_threshold = args.ratio
    detector.enable_recording = args.record
    detector.enable_pushover = bool(args.pushover and PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN)
    detector.pushover_device = args.pushover_device
    detector.alert_window = args.alert * 60  # Convert minutes to seconds
    detector.reset_window = args.reset * 60
    detector.min_cry_duration = args.min_cry
    detector.silence_gap = args.silence_gap
    detector.healthcheck_url = args.healthcheck
    detector.heartbeat_interval = args.heartbeat * 60  # Convert minutes to seconds
    if args.status_port:
        detector.enable_status_server = True
        detector.status_port = args.status_port
        detector.status_bind = args.status_bind
    if args.stop_at:
        try:
            h, m = args.stop_at.split(':')
            detector.stop_time = datetime.strptime(f"{h}:{m}", "%H:%M").time()
        except ValueError:
            print(f"Invalid --stop-at format: '{args.stop_at}'. Use HH:MM (e.g. 07:00)")
            sys.exit(1)
    detector.start()
    detector.monitor()
