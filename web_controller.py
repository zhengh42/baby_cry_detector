#!/usr/bin/env python3
"""
Web Controller for Baby Cry Detector.
Provides a mobile-friendly web interface to start/stop and configure cry_detector.py
"""
import subprocess
import functools
import os
import signal
import sys
import time
import json
import hmac
import threading
from datetime import datetime
from collections import deque
from flask import Flask, render_template_string, request, jsonify, Response

app = Flask(__name__)

# Import Healthcheck settings from config file
try:
    from config import HEALTHCHECK_URL
except ImportError:
    HEALTHCHECK_URL = None
    print("Warning: HEALTHCHECK_URL not found in config.py")

try:
    from config import HEALTHCHECK_API_KEY
except ImportError:
    HEALTHCHECK_API_KEY = None

# Optional HTTP basic auth. Off unless config.py defines WEB_PASSWORD, so nobody
# gets locked out of the monitor at 2am by an upgrade.
try:
    from config import WEB_PASSWORD
except ImportError:
    WEB_PASSWORD = None

try:
    from config import WEB_USERNAME
except ImportError:
    WEB_USERNAME = 'baby'

# Log file settings
LOG_DIR = "/var/log/babymonitor"
LOG_FILE = None  # Will be set when detector starts
# The per-run detector log is moved to USB on stop, so config changes made while
# stopped had nowhere to go. This log belongs to the controller and always exists.
CONTROLLER_LOG = "controller.log"
# Finished run logs are moved here, off the SD card
USB_LOG_DIR = "/media/tinybaby/usb-data/logs"
# How many run logs to leave on the card. The detector prints a line or two a
# second all night, so nothing that is never cleaned up can be left unbounded.
MAX_KEPT_LOGS = 20

# Configuration defaults
DEFAULT_CONFIG = {
    'volume': 900,
    'cry_freq_min': 400,   # Hz
    'alert': 12,           # minutes
    'reset': 5,           # minutes
    'min_cry': 4,          # seconds
    'silence_gap': 2,      # seconds
    'enable_stop_at': False,
    'stop_at': '07:00',
    'enable_restart_at': True,
    'restart_at': '09:00',
    'status_port': 8080,
    'pushover': True,
    'pushover_device': 'a_phone',
    'record': False,
    'enable_healthcheck': True,
    'heartbeat': 5,         # minutes
}

# Allowed range for each numeric setting, as (minimum, maximum).
# Zero or negative values break detection - alert=0 fires an alert on every audio
# chunk, min_cry=0 makes every cough an episode - so incoming values are clamped
# instead of trusted.
CONFIG_LIMITS = {
    'volume': (50, 30000),
    'cry_freq_min': (100, 1900),  # must stay under the detector's 2000 Hz ceiling
    'alert': (1, 240),            # minutes
    'reset': (1, 240),            # minutes
    'min_cry': (1, 300),          # seconds
    'silence_gap': (1, 120),      # seconds
    'heartbeat': (1, 60),         # minutes
    'status_port': (1024, 65535),
}

# Persisted settings, so a reboot or service restart keeps the last config
CONFIG_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'monitor_config.json')
# Kept apart from monitor_config.json: that file is the settings the phone can
# edit, this is one bit of runtime state that no client should be able to POST.
DETECTOR_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'detector_state.json')

# State
detector_process = None
stopping_pids = set()  # pids the web UI is deliberately stopping, so the process
                       # monitor doesn't mistake a requested stop for a crash
last_failure = None    # last unexpected detector exit, shown as a banner on every
                       # phone until dismissed or a new detector is started
running_status_port = None  # status port the live detector was launched with; not
                            # cleared on exit because every use is behind is_running()
detector_started_at = None  # when the live detector was launched, so the daily
                            # restart can leave a session it just missed alone
current_config = DEFAULT_CONFIG.copy()
config_lock = threading.Lock()
# Serialises START and STOP. Flask serves requests on threads, so two phones (or
# one double-tap, or a phone racing the boot resume) could both pass the "already
# running?" check and both spawn a detector. The loser cannot bind the status port
# and dies, while the winner keeps the microphone in a process the controller is
# no longer tracking: the dashboard says STOPPED, STOP does nothing, and the
# orphan's heartbeats keep the healthcheck green. Nothing short of ssh gets you
# out of that, so the check and the spawn have to happen as one step.
#
# Reentrant so the daily restart can hold it across its stop and its start: a
# phone that pressed START in the gap would own a detector this one is about to
# replace, which is the same orphan as above by another route.
process_lock = threading.RLock()
_last_saved_config = None  # last snapshot written to disk, to skip no-op saves
log_buffer = deque(maxlen=100)  # Keep last 100 log lines
event_log_buffer = deque(maxlen=300)  # Key events only
log_lock = threading.Lock()

# Patterns that indicate key events worth showing in the event log
EVENT_PATTERNS = ['Baby started crying', 'Ignored brief sound', 'ALERT', 'Baby settled',
                  'MICROPHONE SILENT', 'Microphone recovered', 'Heartbeat FAIL', 'Heartbeat failed',
                  'Audio stream error', 'Audio stream recovered', 'Reconnection failed']


def serialized(fn):
    """Run fn holding process_lock, so only one start or stop happens at a time."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with process_lock:
            return fn(*args, **kwargs)
    return wrapper


def sanitize_config_value(key, value):
    """Coerce and clamp one setting to something the detector can actually use.

    Returns (accepted, value). Unknown keys are rejected.
    """
    if key not in DEFAULT_CONFIG:
        return False, None
    default = DEFAULT_CONFIG[key]
    # bool is checked first because bool is a subclass of int
    if isinstance(default, bool):
        return (True, value) if isinstance(value, bool) else (False, None)
    if key in CONFIG_LIMITS:
        if isinstance(value, bool):
            return False, None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return False, None
        low, high = CONFIG_LIMITS[key]
        return True, max(low, min(high, number))
    if key == 'pushover_device':
        # isinstance first: `value in DEVICE_LABELS` raises TypeError on a list/dict
        return (True, value) if isinstance(value, str) and value in DEVICE_LABELS else (False, None)
    if key in ('stop_at', 'restart_at'):
        # A malformed time makes the detector exit(1) at startup, so normalise
        # it here (07:00) or reject it outright
        try:
            return True, datetime.strptime(value, '%H:%M').strftime('%H:%M')
        except (TypeError, ValueError):
            return False, None
    if isinstance(default, str):
        return (True, value) if isinstance(value, str) else (False, None)
    return True, value


def sanitize_config(patch):
    """Filter a dict of settings down to accepted, clamped values.

    Returns (clean, rejected) where rejected lists keys that were dropped.
    """
    if not isinstance(patch, dict):
        return {}, []
    clean, rejected = {}, []
    for key, value in patch.items():
        accepted, cleaned = sanitize_config_value(key, value)
        if accepted:
            clean[key] = cleaned
        else:
            rejected.append(key)
    return clean, rejected


def load_config():
    """Load persisted settings over the defaults, ignoring unknown or bad values."""
    if not os.path.exists(CONFIG_STATE_FILE):
        return
    try:
        with open(CONFIG_STATE_FILE) as f:
            saved = json.load(f)
    except Exception as e:
        print(f"Warning: could not read {CONFIG_STATE_FILE}: {e}")
        return
    if not isinstance(saved, dict):
        return
    global _last_saved_config
    clean, rejected = sanitize_config(saved)
    current_config.update(clean)
    _last_saved_config = dict(current_config)
    if rejected:
        print(f"Warning: ignored bad saved settings: {', '.join(sorted(rejected))}")
    print(f"Loaded saved config from {CONFIG_STATE_FILE}")


def save_config():
    """Persist the current settings atomically so a restart picks them back up."""
    global _last_saved_config
    try:
        with config_lock:
            snapshot = dict(current_config)
            if snapshot == _last_saved_config:
                return  # nothing actually changed; skip the SD-card write
            tmp_path = CONFIG_STATE_FILE + '.tmp'
            with open(tmp_path, 'w') as f:
                json.dump(snapshot, f, indent=2, sort_keys=True)
            os.replace(tmp_path, CONFIG_STATE_FILE)
            _last_saved_config = snapshot
    except Exception as e:
        print(f"Warning: could not save config to {CONFIG_STATE_FILE}: {e}")

# Boot resume: how hard to try before giving up and escalating
RESUME_ATTEMPTS = 3
RESUME_RETRY_DELAY = 15      # seconds between attempts
RESUME_STARTUP_GRACE = 3     # seconds to confirm the detector survived startup
# True while the boot resume is still retrying. A detector that dies on an early
# attempt is expected (USB may not be up yet) and the resume escalates itself if
# it never comes up, so per-attempt escalation would only cry wolf.
resume_in_progress = False


def save_detector_state(running):
    """Remember whether the detector is meant to be listening.

    Written on every start and stop so a reboot can tell an interrupted session
    apart from one that was deliberately ended.
    """
    try:
        tmp_path = DETECTOR_STATE_FILE + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump({'running': bool(running)}, f)
            # This flag exists to survive a power cut, so it has to reach the card
            # before one: without the fsyncs the write can still be in the page
            # cache, and the rename itself can be lost, when the power goes.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, DETECTOR_STATE_FILE)
        dir_fd = os.open(os.path.dirname(DETECTOR_STATE_FILE) or '.', os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception as e:
        print(f"Warning: could not save detector state to {DETECTOR_STATE_FILE}: {e}")


def detector_was_running():
    """Whether the detector was listening when the monitor last went down."""
    try:
        with open(DETECTOR_STATE_FILE) as f:
            state = json.load(f)
    except FileNotFoundError:
        return False
    except Exception as e:
        print(f"Warning: could not read {DETECTOR_STATE_FILE}: {e}")
        return False
    return bool(state.get('running')) if isinstance(state, dict) else False


def resume_detector_after_boot():
    """Start listening again if that is what the monitor was doing when it died.

    Only the web controller is a systemd service; the detector is a child process
    it launches on START. A power cut therefore came back as a working dashboard
    with nothing listening to the baby - and, from the healthcheck alert alone,
    that looks exactly like the power still being out.

    The flag is cleared by STOP and by a clean auto-stop, so this only ever
    resumes an interrupted session. A crash leaves it set: a fresh boot is a
    plausible cure for whatever the detector died of, and it is one attempt, not
    a retry loop. A session resumed after its --stop-at time has passed runs
    until the next one.
    """
    if not detector_was_running():
        return
    log_event("Detector was running when the monitor last went down - resuming")
    threading.Thread(target=_resume_worker, daemon=True).start()


def _resume_worker():
    """Keep trying to bring the detector up, then escalate if it will not come.

    Retries because USB is not guaranteed to have enumerated by the time systemd
    reaches this service: the detector exits rather than waiting for a mic that
    is not there yet, and a single attempt at the wrong moment would leave the
    night unmonitored. Runs on its own thread so the dashboard is reachable
    immediately - that page is the manual way out if all of this fails.
    """
    global resume_in_progress
    resume_in_progress = True
    try:
        _resume_attempts()
    finally:
        resume_in_progress = False


def _resume_attempts():
    for attempt in range(1, RESUME_ATTEMPTS + 1):
        # A failed resume must never take the dashboard down with it
        try:
            started, message = start_detector()
        except Exception as e:
            started, message = False, f"{type(e).__name__}: {e}"

        if started:
            # Popen succeeding only means it launched; a missing mic exits right
            # afterwards, and process_monitor logs why
            time.sleep(RESUME_STARTUP_GRACE)
            if is_running():
                return
            message = "the detector exited immediately after starting"

        if attempt < RESUME_ATTEMPTS:
            log_event(f"Resume attempt {attempt} failed ({message}); "
                      f"retrying in {RESUME_RETRY_DELAY}s")
            time.sleep(RESUME_RETRY_DELAY)
            continue

        log_event(f"Could not resume the detector after {attempt} attempts "
                  f"({message}) - NOT monitoring")
        # A clean reboot pauses the healthcheck on the way down, so without this
        # a resume that never succeeds is silent: nothing listening, nothing due
        # to ping, nobody told.
        fail_healthcheck()


# ANSI color code stripping
import re
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def strip_ansi(text):
    return ANSI_ESCAPE.sub('', text)

# The only accepted Pushover device values, in the order their buttons appear.
# Also the whitelist: anything else is rejected rather than forwarded to Pushover.
DEVICE_LABELS = {
    'h_phone': 'mama',
    'a_phone': 'papa',
    'grandma_phone': 'grandma',
    '__muted__': 'mute',
}

# Human-readable labels and units for config keys, used in event-log messages
CONFIG_LABELS = {
    'volume': ('Volume threshold', ''),
    'cry_freq_min': ('Cry freq min', ' Hz'),
    'alert': ('Alert window', ' min'),
    'reset': ('Reset window', ' min'),
    'min_cry': ('Min cry duration', ' sec'),
    'silence_gap': ('Silence gap', ' sec'),
    'stop_at': ('Stop-at time', ''),
    'restart_at': ('Daily restart time', ''),
    'status_port': ('Status port', ''),
    'heartbeat': ('Heartbeat interval', ' min'),
    'enable_stop_at': ('Stop-at', None),
    'enable_restart_at': ('Daily restart', None),
    'enable_healthcheck': ('Healthcheck', None),
    'record': ('Recording', None),
    'pushover': ('Pushover alerts', None),
}

def describe_config_change(key, old_value, new_value):
    """Build a human-readable event-log message for a single config change."""
    label, unit = CONFIG_LABELS.get(key, (key, ''))
    if unit is None:  # boolean toggle
        return f"Config: {label} {'ENABLED' if new_value else 'DISABLED'}"
    return f"Config: {label} changed {old_value}{unit} -> {new_value}{unit}"

def detector_endpoint(path):
    """Build a URL for the running detector's HTTP server.

    Deliberately uses the port the process was launched with rather than the current
    setting: the detector binds its port once at startup, so editing status_port while
    it runs would point the controller at a dead port - status polling would go blank
    and live updates (including a duty switch) would be silently dropped until restart.
    """
    port = running_status_port if running_status_port is not None else current_config['status_port']
    return f"http://localhost:{port}{path}"


def write_controller_log(line):
    """Append to the controller's own log. Never raises: logging must not break the UI."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, CONTROLLER_LOG), 'a') as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_event(message):
    """Record a key event (e.g. config change) to the log buffers and files."""
    log_line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    with log_lock:
        log_buffer.append(log_line)
        event_log_buffer.append(log_line)
    write_controller_log(log_line)
    # Also to stdout, so boot-time messages (a resume, a resume that failed) land
    # in journalctl -u babymonitor-web even if /var/log is not writable yet
    print(log_line)
    if LOG_FILE and os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, 'a') as f:
                f.write(log_line + "\n")
        except Exception:
            pass

def archive_log(path=None):
    """Move the finished run's log off the SD card and onto the USB drive.

    Called for every exit that ends a session, not only a STOP from the phone:
    a night that ended at its own --stop-at time used to leave its log behind,
    and at a few MB a night that is a slow way to fill up the card /var/log
    lives on. The copy has to succeed before the original goes, so an unmounted
    USB drive costs nothing but the move.
    """
    path = LOG_FILE if path is None else path
    if not path or not os.path.exists(path):
        return
    try:
        import shutil
        os.makedirs(USB_LOG_DIR, exist_ok=True)
        shutil.copy2(path, USB_LOG_DIR)
        os.remove(path)
        print(f"Log moved to {USB_LOG_DIR}/{os.path.basename(path)}")
    except Exception as e:
        # The daily restart exists to get this file onto the USB drive, so a
        # failure here is that feature quietly not working - and in
        # MAX_KEPT_LOGS runs' time prune_old_logs() deletes the log off the
        # card. Say so where a parent will see it, not only in journalctl.
        log_event(f"Log NOT archived to USB ({e}) - {os.path.basename(path)} "
                  f"is still on the card")


def prune_old_logs():
    """Keep only the newest MAX_KEPT_LOGS run logs on the card.

    A crashed run's log is deliberately left behind to be read, and every other
    kind of exit archives its own, so the ones nobody archived would otherwise
    pile up for as long as the Pi runs.
    """
    try:
        logs = sorted(
            (entry.path for entry in os.scandir(LOG_DIR)
             if entry.is_file() and entry.name.startswith('detector_')
             and entry.name.endswith('.log')),
            key=os.path.getmtime, reverse=True)
    except Exception:
        return
    for path in logs[MAX_KEPT_LOGS:]:
        try:
            os.remove(path)
            print(f"Removed old log {os.path.basename(path)}")
        except Exception:
            pass


def new_log_path(when):
    """A run-log path for `when` that no existing log already owns.

    The name is the start time to the second, which is not by itself unique:
    two sessions started inside one second would name the same file, and the
    second would open it with 'w' and truncate the first. That is only
    survivable while the earlier log has already been archived - and it has not
    when the USB drive is unmounted, nor after a crash, whose log is
    deliberately left on the card to be read.

    The archive owns a name too. Archiving hands the name back on the card, but
    archive_log() copies onto the USB drive by basename and overwrites, so the
    run already filed under it would lose its log to the next one.
    """
    stamp = f"detector_{when.strftime('%Y%m%d_%H%M%S')}"
    attempt = 1
    while True:
        name = f"{stamp}.log" if attempt == 1 else f"{stamp}_{attempt}.log"
        if not (os.path.exists(os.path.join(LOG_DIR, name))
                or os.path.exists(os.path.join(USB_LOG_DIR, name))):
            return os.path.join(LOG_DIR, name)
        attempt += 1


def log_reader(process, log_file_path):
    """Read process output and store in log buffer and file"""
    global log_buffer
    try:
        with open(log_file_path, 'a') as log_file:
            for line in iter(process.stdout.readline, b''):
                if line:
                    decoded = line.decode('utf-8', errors='replace').rstrip()
                    cleaned = strip_ansi(decoded)
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    log_line = f"[{timestamp}] {cleaned}"
                    with log_lock:
                        log_buffer.append(log_line)
                        # Check if this is a key event
                        if any(p in cleaned for p in EVENT_PATTERNS):
                            event_log_buffer.append(log_line)
                    # Write to file
                    log_file.write(log_line + "\n")
                    log_file.flush()
    except Exception as e:
        with log_lock:
            log_buffer.append(f"[ERROR] Log reader: {e}")

def process_monitor(process, log_path=None):
    """React when the detector exits on its own.

    A stop requested from the web UI is handled by stop_detector(), so this only
    fires for self-exits: a clean auto-stop, or a crash / startup failure.
    Pausing the healthcheck is reserved for clean exits - pausing after a crash
    would silence the one alert that says nobody is watching the baby.

    Carries the log path of the run it is watching, like log_reader does. This
    thread does its work after the exit, some of it behind network timeouts, and
    by then a phone may have started the next session: writing to the global
    would append this run's ending to the new run's log, and archiving it would
    move that live log to USB and unlink it mid-night.
    """
    global detector_process
    if log_path is None:
        log_path = LOG_FILE
    process.wait()  # Block until process exits

    if process.pid in stopping_pids:
        stopping_pids.discard(process.pid)
        return
    # Ignore a stale process that is no longer the current one
    if detector_process is None or detector_process.pid != process.pid:
        return

    detector_process = None
    returncode = process.returncode
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if returncode == 0:
        # Clean shutdown, i.e. the --stop-at time was reached. The session is over
        # on purpose, so a later reboot must not resume it.
        save_detector_state(False)
        hc_paused = pause_healthcheck()
        msg = f"[{timestamp}] Detector exited normally (auto-stop)"
        if hc_paused:
            msg += " (healthcheck paused)"
        extra = None
    else:
        # Crash, bad argument, or an outside kill: leave monitoring armed and
        # escalate now rather than pausing the check - unless the boot resume is
        # still retrying, which escalates once at the end if it never succeeds
        hc_failed = False if resume_in_progress else fail_healthcheck()
        reason = (f"killed by signal {-returncode}" if returncode < 0
                  else f"exit code {returncode}")
        msg = f"[{timestamp}] Detector exited UNEXPECTEDLY ({reason}) - NOT monitoring"
        if hc_failed:
            msg += " (healthcheck FAIL sent)"
        elif resume_in_progress:
            # nothing was attempted, so don't report it as a failed attempt
            msg += " (boot resume still trying; it escalates if it gives up)"
        elif current_config.get('enable_healthcheck'):
            msg += " (healthcheck FAIL could not be sent)"
        extra = None

    last_output = None
    with log_lock:
        if returncode != 0:
            # Surface the detector's last words; usually the actual cause
            last_output = next((l for l in reversed(log_buffer) if l.strip()), None)
            if last_output:
                extra = f"[{timestamp}] Last detector output: {last_output}"
        log_buffer.append(msg)
        event_log_buffer.append(msg)
        if extra:
            log_buffer.append(extra)
            event_log_buffer.append(extra)

    write_controller_log(msg)
    if extra:
        write_controller_log(extra)

    if returncode != 0:
        global last_failure
        last_failure = {
            'time': timestamp,
            'reason': reason,
            'detail': last_output or '',
            'escalated': bool(hc_failed),
        }

    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, 'a') as f:
                f.write(msg + "\n")
                if extra:
                    f.write(extra + "\n")
        except Exception:
            pass

    if returncode == 0:
        # The session ended on purpose, so file the log away exactly as a STOP
        # would. A crash keeps its log on the card, where it can still be read.
        archive_log(log_path)


@serialized
def start_detector():
    """Start the cry detector process"""
    global detector_process, log_buffer, LOG_FILE, last_failure, running_status_port
    global detector_started_at

    if detector_process is not None and detector_process.poll() is None:
        return False, "Detector is already running"

    # Create log directory if needed
    os.makedirs(LOG_DIR, exist_ok=True)
    prune_old_logs()

    # Create log file, named for the start time but never over an existing log
    LOG_FILE = new_log_path(datetime.now())

    # Build command
    cmd = [
        sys.executable, '-u', 'cry_detector.py',
        '--status-port', str(current_config['status_port']),
        '--alert', str(current_config['alert']),
        '--reset', str(current_config['reset']),
        '-v', str(current_config['volume']),
        '--cry-freq-min', str(current_config['cry_freq_min']),
        '--min-cry', str(current_config['min_cry']),
        '--silence-gap', str(current_config['silence_gap']),
    ]

    if current_config['pushover']:
        cmd.append('--pushover')
        if current_config['pushover_device']:
            cmd.extend(['--pushover-device', current_config['pushover_device']])

    if current_config['record']:
        cmd.append('--record')

    if current_config['enable_stop_at'] and current_config['stop_at']:
        cmd.extend(['--stop-at', current_config['stop_at']])

    if current_config['enable_healthcheck'] and HEALTHCHECK_URL:
        cmd.extend(['--healthcheck', HEALTHCHECK_URL])
        cmd.extend(['--heartbeat', str(current_config['heartbeat'])])

    # Clear log buffer and write initial log entry
    start_msg = f"Starting: {' '.join(cmd)}"
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with log_lock:
        log_buffer.clear()  # verbose per-run output
        # The event log is deliberately kept: settings changed before START, and
        # last night's alerts, stay visible. It self-trims at maxlen.
        event_log_buffer.append(f"[{timestamp}] --- Detector started ---")
        log_buffer.append(f"[{timestamp}] {start_msg}")
    write_controller_log(f"[{timestamp}] {start_msg}")

    # Write to log file
    with open(LOG_FILE, 'w') as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {start_msg}\n")

    try:
        # Pin the port before the process exists, so no status poll can slip in
        # between launch and pinning and read a stale port
        running_status_port = current_config['status_port']

        detector_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )

        # Start log reader thread
        log_thread = threading.Thread(target=log_reader, args=(detector_process, LOG_FILE), daemon=True)
        log_thread.start()

        # Start process monitor thread (detects auto-stop and pauses healthcheck)
        monitor_thread = threading.Thread(target=process_monitor,
                                          args=(detector_process, LOG_FILE), daemon=True)
        monitor_thread.start()

        detector_started_at = datetime.now()

        # A fresh start clears the previous crash banner
        last_failure = None

        # Survives a power cut: a reboot resumes this session
        save_detector_state(True)

        return True, f"Detector started (log: {LOG_FILE})"
    except Exception as e:
        return False, f"Failed to start: {e}"

def pause_healthcheck():
    """Pause healthcheck monitoring to avoid false alerts.

    Uses the Management API which requires an API key.
    The ping URL is parsed to extract the UUID.
    """
    if not current_config.get('enable_healthcheck'):
        return False

    if not HEALTHCHECK_URL or not HEALTHCHECK_API_KEY:
        if HEALTHCHECK_URL and not HEALTHCHECK_API_KEY:
            print("Healthcheck pause skipped: no API key configured")
        return False

    try:
        import urllib.request
        import re

        # Extract UUID from ping URL (e.g., https://hc-ping.com/UUID)
        match = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', HEALTHCHECK_URL)
        if not match:
            print(f"Healthcheck pause failed: could not extract UUID from {HEALTHCHECK_URL}")
            return False

        uuid = match.group(1)
        pause_url = f"https://healthchecks.io/api/v3/checks/{uuid}/pause"

        req = urllib.request.Request(pause_url, data=b'', method='POST')
        req.add_header('X-Api-Key', HEALTHCHECK_API_KEY)
        urllib.request.urlopen(req, timeout=5)
        print(f"Healthcheck paused successfully")
        return True
    except Exception as e:
        print(f"Failed to pause healthcheck: {e}")
        with log_lock:
            log_buffer.append(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Healthcheck pause failed: {e}")
        return False

def fail_healthcheck():
    """Send a /fail ping so an unexpected detector exit escalates immediately
    instead of waiting for the healthchecks.io period to expire.

    Only needs the ping URL, not the management API key.
    """
    if not current_config.get('enable_healthcheck') or not HEALTHCHECK_URL:
        return False
    try:
        import urllib.request
        urllib.request.urlopen(HEALTHCHECK_URL.rstrip('/') + '/fail', timeout=5)
        print("Healthcheck FAIL sent (detector exited unexpectedly)")
        return True
    except Exception as e:
        print(f"Failed to send healthcheck FAIL: {e}")
        return False


@serialized
def stop_detector():
    """Stop the cry detector process"""
    global detector_process

    if detector_process is None or detector_process.poll() is not None:
        detector_process = None
        return False, "Detector is not running"

    process = detector_process
    # Flag it before signalling so the process monitor doesn't read this as a crash
    stopping_pids.add(process.pid)
    forced = False

    try:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass  # exited between the poll above and now
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Wedged detector: escalate so STOP always works from the phone
            forced = True
            print("Detector ignored SIGTERM after 5s; sending SIGKILL")
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # unkillable; clear our state anyway so the UI isn't stuck
    except Exception as e:
        stopping_pids.discard(process.pid)
        return False, f"Failed to stop: {e}"

    try:
        detector_process = None

        # Pause healthcheck to avoid false alerts
        hc_paused = pause_healthcheck()

        stop_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Detector stopped"
        if forced:
            stop_msg += " (forced with SIGKILL; any in-progress recording was lost)"
        if hc_paused:
            stop_msg += " (healthcheck paused)"
        write_controller_log(stop_msg)
        with log_lock:
            log_buffer.append(stop_msg)
        # Write to log file
        if LOG_FILE and os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'a') as f:
                f.write(stop_msg + "\n")
        archive_log()
        return True, "Detector stopped (forced)" if forced else "Detector stopped"
    except Exception as e:
        return False, f"Failed to stop: {e}"

def is_running():
    """Check if detector is running"""
    global detector_process
    if detector_process is None:
        return False
    if detector_process.poll() is not None:
        detector_process = None
        return False
    return True


# --- daily restart ---------------------------------------------------------
#
# A run's log only moves to the USB drive when the run ends - archive_log() is
# on the stop and auto-stop paths, nowhere else - so a session that never ends
# never archives. The detector is meant to run for a night at a time, but it
# can be left listening for days, and then the night's log sits on the card
# until prune_old_logs() deletes it unread.
#
# The things under the detector are no more patient: the USB mic drops off the
# bus, and ALSA hands back a stream that reads silence. Ending the run cures
# all of it and costs a second, so one is ended every morning rather than
# waiting for someone to notice.
#
# The schedule lives here rather than in the detector (where --stop-at lives)
# because a process cannot restart itself: only the controller that spawned it
# can hand it a successor.
RESTART_POLL_SECONDS = 20     # must stay well under a minute; the restart time is
                              # matched by the minute, like the detector's auto-stop
RESTART_SETTLE_SECONDS = 1    # pause between the stop and the start, so ALSA has
                              # let go of the USB mic before the next process opens it
RESTART_MIN_SESSION_AGE = 60  # don't restart a session this young (seconds)
# How long to keep waiting for a quiet moment before giving up on today's restart.
# Restarting mid-episode would reset the alert timer, so a baby crying at the
# restart time would have its alarm pushed back by a whole alert window.
RESTART_DEFER_LIMIT = 30 * 60

_restart_done_slot = None      # (date, 'HH:MM') the restart has been settled for
_restart_pending_slot = None   # slot that came due but is waiting for quiet
_restart_pending_since = None  # when that wait began


def detector_is_crying():
    """Whether the running detector says an episode is in progress.

    An unreachable or unparseable status counts as not crying: a detector that
    cannot answer is the strongest case there is for replacing it, so the
    restart must not be blocked by one.
    """
    try:
        import urllib.request
        with urllib.request.urlopen(detector_endpoint('/status'), timeout=2) as response:
            status = json.loads(response.read().decode())
        return bool(status.get('crying')) if isinstance(status, dict) else False
    except Exception:
        return False


@serialized
def restart_detector(reason):
    """Replace the running detector with a fresh one.

    Stop and start are one step under process_lock: a phone that pressed START
    in between would be left owning a detector this restart then replaces.

    The stop pauses the healthcheck, as every clean stop does. That is only
    safe because the successor sends its first heartbeat on its first pass of
    the monitor loop, which resumes the check a second later - so if the start
    fails instead, this has to escalate by hand.
    """
    global last_failure

    if not is_running():
        return False, "Detector is not running"

    log_event(f"{reason}: restarting the detector")
    stopped, message = stop_detector()
    if not stopped:
        # The old detector is probably still listening, so this is a failed
        # restart, not a gap in monitoring: say so and leave it alone.
        log_event(f"{reason} aborted: could not stop the detector ({message})")
        return False, message

    time.sleep(RESTART_SETTLE_SECONDS)

    started, message = start_detector()
    if started:
        log_event(f"{reason}: detector restarted")
        return True, message

    # The mic has been taken away and could not be given back. stop_detector()
    # paused the healthcheck on the way out, which would now keep the one alert
    # that says nobody is watching the baby from ever firing.
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    escalated = fail_healthcheck()
    note = " (healthcheck FAIL sent)" if escalated else ""
    log_event(f"{reason} FAILED to start the detector ({message}) - NOT monitoring{note}")
    last_failure = {
        'time': timestamp,
        'reason': f"{reason} could not start a new detector",
        'detail': message,
        'escalated': bool(escalated),
    }
    return False, message


def _restart_slot(now, at):
    """The identity of one day's restart, so it can fire at most once."""
    return (now.date().isoformat(), at)


def _is_restart_minute(now, at):
    """Whether `now` falls in the minute named by `at` ('HH:MM').

    Matched against the clock as it reads now, not a deadline computed in
    advance: the Pi has no battery-backed clock, so it comes back from a power
    cut believing it is still the time of the cut and NTP then jumps it forward.
    A missed minute therefore just does not match, and today's restart is
    skipped - which costs nothing, where a restart fired by a clock jump would
    land in the middle of a night.
    """
    try:
        wanted = datetime.strptime(at, '%H:%M')
    except (TypeError, ValueError):
        return False
    return (now.hour, now.minute) == (wanted.hour, wanted.minute)


def _settle_restart(slot):
    """Mark one day's restart as dealt with, however it turned out."""
    global _restart_done_slot, _restart_pending_slot, _restart_pending_since
    _restart_done_slot = slot
    _restart_pending_slot = _restart_pending_since = None


def maybe_run_daily_restart(now=None):
    """Run the daily restart if it is due. Returns True if one was attempted.

    Called on a timer, so every path has to decide whether this slot is finished
    with: anything that returns without restarting and without marking the slot
    done will be asked again in RESTART_POLL_SECONDS.
    """
    global _restart_pending_slot, _restart_pending_since

    now = datetime.now() if now is None else now
    at = current_config.get('restart_at')

    if not (current_config.get('enable_restart_at') and at):
        # Turning the restart off also calls off one that is waiting for quiet
        _restart_pending_slot = _restart_pending_since = None
        return False

    slot = _restart_slot(now, at)

    # A wait that belongs to another slot is stale: the time was edited, or
    # midnight arrived while the baby was still crying.
    if _restart_pending_slot is not None and _restart_pending_slot != slot:
        _restart_pending_slot = _restart_pending_since = None

    if _restart_pending_slot == slot:
        if (now - _restart_pending_since).total_seconds() >= RESTART_DEFER_LIMIT:
            log_event(f"Daily restart ({at}) skipped: the baby has been crying "
                      f"since it came due")
            _settle_restart(slot)
            return False
    elif _restart_done_slot == slot or not _is_restart_minute(now, at):
        return False

    if not is_running():
        # Nothing to refresh. A night that ended at its own stop-at time, or one
        # a parent ended from the phone, stays ended - the restart keeps an
        # existing session healthy, it does not start a new one.
        _settle_restart(slot)
        return False

    if detector_started_at is not None:
        # A session a parent (or the boot resume) started seconds ago is already
        # as fresh as a restart would make it. A negative age means the clock has
        # since jumped backwards, which leaves the real age unknowable; a
        # needless restart is the cheaper mistake there than a skipped one.
        age = (now - detector_started_at).total_seconds()
        if 0 <= age < RESTART_MIN_SESSION_AGE:
            _settle_restart(slot)
            return False

    if detector_is_crying():
        if _restart_pending_slot != slot:
            log_event(f"Daily restart ({at}) deferred: the baby is crying")
            _restart_pending_slot, _restart_pending_since = slot, now
        return False

    _settle_restart(slot)
    restart_detector(f"Daily restart ({at})")
    return True


def _restart_scheduler():
    """Poll for the daily restart time. Never dies: this thread is the schedule."""
    while True:
        time.sleep(RESTART_POLL_SECONDS)
        try:
            maybe_run_daily_restart()
        except Exception as e:
            print(f"Daily restart check failed: {type(e).__name__}: {e}")


def start_restart_scheduler():
    """Begin watching for the daily restart time."""
    threading.Thread(target=_restart_scheduler, daemon=True).start()

# HTML template - mobile-friendly
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Baby Monitor Control</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0; padding: 16px;
            background: #1a1a2e;
            color: #eee;
            min-height: 100vh;
        }
        h1 { text-align: center; margin: 0 0 20px; font-size: 24px; }
        .status-badge {
            display: inline-block;
            padding: 6px 16px;
            border-radius: 20px;
            font-weight: bold;
            margin-bottom: 16px;
        }
        .status-running { background: #27ae60; }
        .status-stopped { background: #7f8c8d; }
        .status-crying { background: #e74c3c; animation: pulse 1s infinite; }
        .status-alarm { background: #c0392b; animation: pulse 0.5s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.7; } }

        .failure-banner {
            background: #c0392b;
            color: #fff;
            border: 1px solid #e74c3c;
            border-radius: 12px;
            padding: 14px 16px;
            margin-bottom: 16px;
            font-size: 15px;
            font-weight: bold;
            line-height: 1.4;
        }
        .failure-detail {
            font-family: monospace;
            font-size: 12px;
            font-weight: normal;
            opacity: 0.9;
            margin-top: 8px;
            word-wrap: break-word;
        }
        .btn-dismiss {
            margin-top: 12px;
            padding: 8px 14px;
            border: 1px solid rgba(255, 255, 255, 0.6);
            border-radius: 8px;
            background: transparent;
            color: #fff;
            font-size: 14px;
            font-weight: bold;
            cursor: pointer;
        }
        .btn-dismiss:active { background: rgba(255, 255, 255, 0.15); }

        .card {
            background: #16213e;
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 16px;
        }
        .card h2 { margin: 0 0 12px; font-size: 18px; color: #aaa; }

        .btn-row { display: flex; gap: 12px; margin-bottom: 16px; }
        .btn {
            flex: 1;
            padding: 16px;
            border: none;
            border-radius: 12px;
            font-size: 18px;
            font-weight: bold;
            cursor: pointer;
            transition: transform 0.1s;
        }
        .btn:active { transform: scale(0.95); }
        .btn-start { background: #27ae60; color: white; }
        .btn-stop { background: #e74c3c; color: white; }
        .btn-restart { background: #f39c12; color: white; }
        .btn-start:disabled, .btn-stop:disabled, .btn-restart:disabled { opacity: 0.5; }

        .device-row { display: flex; gap: 12px; margin-bottom: 16px; }
        .btn-device {
            flex: 1;
            padding: 12px;
            border: 2px solid #444;
            border-radius: 12px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
            background: #16213e;
            color: #aaa;
            transition: all 0.2s;
        }
        .btn-device:active { transform: scale(0.95); }
        .btn-device.selected {
            border-color: #3498db;
            background: #1a3a5c;
            color: #fff;
        }
        .restart-note {
            text-align: center;
            font-size: 12px;
            color: #888;
            margin-bottom: 12px;
        }

        .form-group { margin-bottom: 12px; }
        label { display: block; margin-bottom: 4px; color: #aaa; font-size: 14px; }
        input[type="number"], input[type="text"], select {
            width: 100%;
            padding: 12px;
            border: 1px solid #333;
            border-radius: 8px;
            background: #0f0f1e;
            color: #fff;
            font-size: 16px;
        }
        .stepper {
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .stepper input[type="number"] {
            flex: 1;
            text-align: center;
            -moz-appearance: textfield;
        }
        .stepper input[type="number"]::-webkit-outer-spin-button,
        .stepper input[type="number"]::-webkit-inner-spin-button {
            -webkit-appearance: none;
            margin: 0;
        }
        .stepper-btn {
            width: 40px;
            height: 44px;
            border: 1px solid #333;
            border-radius: 8px;
            background: #1a1a2e;
            color: #fff;
            font-size: 20px;
            font-weight: bold;
            cursor: pointer;
        }
        .stepper-btn:active {
            background: #2a2a4e;
        }
        .checkbox-group {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        input[type="checkbox"] {
            width: 24px;
            height: 24px;
        }

        .row { display: flex; gap: 12px; }
        .row .form-group { flex: 1; }

        .logs {
            background: #0f0f1e;
            border-radius: 8px;
            padding: 12px;
            height: 200px;
            overflow-y: auto;
            font-family: monospace;
            font-size: 12px;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .log-line { margin: 2px 0; }
        .log-cry { color: #e74c3c; }
        .log-ok { color: #27ae60; }
        .log-warn { color: #f39c12; }

        .baby-status {
            text-align: center;
            padding: 20px;
            font-size: 48px;
        }
        .episode-duration {
            font-size: 24px;
            margin-top: 8px;
        }

        .inline-option {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .inline-option input[type="text"] {
            flex: 1;
        }
    </style>
</head>
<body>
    <h1>Baby Monitor</h1>

    <div style="text-align: center;">
        <span id="status-badge" class="status-badge status-stopped">STOPPED</span>
    </div>

    <div id="failure-banner" class="failure-banner" style="display: none;">
        <div id="failure-text"></div>
        <div id="failure-detail" class="failure-detail"></div>
        <button class="btn-dismiss" onclick="dismissFailure()">Dismiss</button>
    </div>

    <div id="baby-status" class="card baby-status" style="display: none;">
        <div id="baby-emoji"></div>
        <div id="episode-duration" class="episode-duration"></div>
        <div id="extra-info" style="font-size: 14px; color: #aaa; margin-top: 4px;"></div>
    </div>

    <div class="btn-row">
        <button id="btn-start" class="btn btn-start" onclick="startDetector()">START</button>
        <button id="btn-stop" class="btn btn-stop" onclick="stopDetector()" disabled>STOP</button>
        <button id="btn-restart" class="btn btn-restart" onclick="restartDetector()" disabled>RESTART</button>
    </div>

    <div style="text-align: center; color: #aaa; font-size: 14px; margin-bottom: 8px;">Who is on duty?</div>
    <div class="device-row">
        {% for key, label in devices %}
        <button id="btn-{{ key }}" class="btn-device {{ 'selected' if config.pushover_device == key else '' }}" onclick="selectDevice('{{ key }}')">{{ label }}</button>
        {% endfor %}
    </div>
    <div class="restart-note">Alerts go to selected device. Switches instantly, running or stopped.</div>

    <div class="card">
        <h2>Configuration</h2>
        <div class="restart-note">Changes save immediately, running or stopped, and apply instantly when running.</div>
        <div class="row">
            <div class="form-group">
                <label>Alert (min)</label>
                <div class="stepper">
                    <button type="button" class="stepper-btn" onclick="stepValue('alert', -1)">-</button>
                    <input type="number" id="alert" value="{{ config.alert }}">
                    <button type="button" class="stepper-btn" onclick="stepValue('alert', 1)">+</button>
                </div>
            </div>
            <div class="form-group">
                <label>Reset (min)</label>
                <div class="stepper">
                    <button type="button" class="stepper-btn" onclick="stepValue('reset', -1)">-</button>
                    <input type="number" id="reset" value="{{ config.reset }}">
                    <button type="button" class="stepper-btn" onclick="stepValue('reset', 1)">+</button>
                </div>
            </div>
        </div>
        <div class="row">
            <div class="form-group">
                <label>Volume Threshold</label>
                <div class="stepper">
                    <button type="button" class="stepper-btn" onclick="stepValue('volume', -100)">-</button>
                    <input type="number" id="volume" value="{{ config.volume }}">
                    <button type="button" class="stepper-btn" onclick="stepValue('volume', 100)">+</button>
                </div>
            </div>
            <div class="form-group">
                <label>Cry Freq Min (Hz)</label>
                <div class="stepper">
                    <button type="button" class="stepper-btn" onclick="stepValue('cry_freq_min', -100)">-</button>
                    <input type="number" id="cry_freq_min" value="{{ config.cry_freq_min }}">
                    <button type="button" class="stepper-btn" onclick="stepValue('cry_freq_min', 100)">+</button>
                </div>
            </div>
        </div>
        <div class="row">
            <div class="form-group">
                <label>Min Cry (sec)</label>
                <div class="stepper">
                    <button type="button" class="stepper-btn" onclick="stepValue('min_cry', -1)">-</button>
                    <input type="number" id="min_cry" value="{{ config.min_cry }}">
                    <button type="button" class="stepper-btn" onclick="stepValue('min_cry', 1)">+</button>
                </div>
            </div>
            <div class="form-group">
                <label>Silence Gap (sec)</label>
                <div class="stepper">
                    <button type="button" class="stepper-btn" onclick="stepValue('silence_gap', -1)">-</button>
                    <input type="number" id="silence_gap" value="{{ config.silence_gap }}">
                    <button type="button" class="stepper-btn" onclick="stepValue('silence_gap', 1)">+</button>
                </div>
            </div>
        </div>

        <div class="row">
            <div class="form-group">
                <label>Heartbeat (min)</label>
                <div class="stepper">
                    <button type="button" class="stepper-btn" onclick="stepValue('heartbeat', -1)">-</button>
                    <input type="number" id="heartbeat" value="{{ config.heartbeat }}">
                    <button type="button" class="stepper-btn" onclick="stepValue('heartbeat', 1)">+</button>
                </div>
            </div>
        </div>

        <div class="restart-note">Stop At, Healthcheck, Recording and Heartbeat are saved now but only take effect on next start/restart.</div>
        <div class="form-group inline-option">
            <input type="checkbox" id="enable_stop_at" {{ 'checked' if config.enable_stop_at else '' }}>
            <label for="enable_stop_at" style="margin: 0; white-space: nowrap;">Stop At</label>
            <input type="text" id="stop_at" value="{{ config.stop_at }}" placeholder="HH:MM">
        </div>

        <div class="form-group inline-option">
            <input type="checkbox" id="enable_restart_at" {{ 'checked' if config.enable_restart_at else '' }}>
            <label for="enable_restart_at" style="margin: 0; white-space: nowrap;">Restart At</label>
            <input type="text" id="restart_at" value="{{ config.restart_at }}" placeholder="HH:MM">
        </div>
        <div class="restart-note">A session still listening at this time gets a fresh
            detector; a stopped monitor stays stopped, and a crying baby waits.</div>

        <div class="form-group checkbox-group">
            <input type="checkbox" id="enable_healthcheck" {{ 'checked' if config.enable_healthcheck else '' }}>
            <label for="enable_healthcheck" style="margin: 0;">Enable Healthcheck</label>
        </div>

        <div class="form-group checkbox-group">
            <input type="checkbox" id="record" {{ 'checked' if config.record else '' }}>
            <label for="record" style="margin: 0;">Enable Recording</label>
        </div>
    </div>

    <div class="card">
        <h2 style="display: flex; justify-content: space-between; align-items: center;">
            Events
            <label style="font-size: 12px; font-weight: normal; display: flex; align-items: center; gap: 4px;">
                <input type="checkbox" id="events-auto-scroll" checked style="width: 16px; height: 16px;">
                Auto-scroll
            </label>
        </h2>
        <div id="events" class="logs" style="height: 300px;"></div>
    </div>

    <div class="card">
        <h2 style="display: flex; justify-content: space-between; align-items: center;">
            Logs
            <label style="font-size: 12px; font-weight: normal; display: flex; align-items: center; gap: 4px;">
                <input type="checkbox" id="auto-scroll" checked style="width: 16px; height: 16px;">
                Auto-scroll
            </label>
        </h2>
        <div id="logs" class="logs"></div>
    </div>

    <script>
        let isRunning = false;
        let selectedDevice = '{{ config.pushover_device }}';
        let devicePending = 0;  // in-flight /api/device posts; poll must not clobber them
        let dismissPending = 0;  // in-flight dismiss; poll must not bring the banner back
        let configPending = 0;  // in-flight /api/config posts; poll must not clobber them

        const NUMBER_FIELDS = ['volume', 'cry_freq_min', 'alert', 'reset', 'min_cry',
                               'silence_gap', 'heartbeat'];
        const CHECK_FIELDS = ['enable_stop_at', 'enable_restart_at', 'enable_healthcheck', 'record'];
        const TIME_FIELDS = ['stop_at', 'restart_at'];
        // Same [min, max] the server enforces, injected so the two can't drift apart
        const FIELD_LIMITS = {{ config_limits|tojson }};
        // Accepted device values, from the server's whitelist
        const DEVICES = {{ device_keys|tojson }};

        function highlightDevice(device) {
            for (const key of DEVICES) {
                document.getElementById('btn-' + key).classList.toggle('selected', key === device);
            }
        }

        function clampField(id, value) {
            const limit = FIELD_LIMITS[id];
            if (!limit) return value;
            return Math.max(limit[0], Math.min(limit[1], value));
        }

        // Save a config change on the server whether running or stopped: a running
        // detector picks up the live-tunable ones immediately, a stopped one uses
        // them all on next start.
        async function postConfig(patch) {
            configPending++;
            try {
                await fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(patch)
                });
            } catch (e) {
                console.error('Failed to update config:', e);
            } finally {
                configPending--;
            }
        }

        async function stepValue(id, delta) {
            const input = document.getElementById(id);
            let val = parseInt(input.value) || 0;
            val = clampField(id, val + delta);
            input.value = val;
            await postConfig({[id]: val});
        }

        // Typed-in numbers, the HH:MM times, and the checkboxes save on change too
        function wireConfigInputs() {
            for (const id of NUMBER_FIELDS) {
                const el = document.getElementById(id);
                if (!el) continue;  // listed but not rendered; don't kill the page script
                el.addEventListener('change', (e) => {
                    const val = clampField(id, parseInt(e.target.value) || 0);
                    e.target.value = val;
                    postConfig({[id]: val});
                });
            }
            for (const id of CHECK_FIELDS) {
                const el = document.getElementById(id);
                if (!el) continue;
                el.addEventListener('change', (e) => {
                    postConfig({[id]: e.target.checked});
                });
            }
            for (const id of TIME_FIELDS) {
                const el = document.getElementById(id);
                if (!el) continue;
                el.addEventListener('change', (e) => {
                    postConfig({[id]: e.target.value});
                });
            }
        }

        async function selectDevice(device) {
            selectedDevice = device;
            highlightDevice(device);

            // Persist on the server whether running or stopped: a running detector is
            // updated live, a stopped one uses it on next start.
            devicePending++;
            try {
                await fetch('/api/device', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({pushover_device: device})
                });
            } catch (e) {
                console.error('Failed to update device:', e);
            } finally {
                devicePending--;
            }
        }

        function updateFailureBanner(failure) {
            if (dismissPending > 0) return;  // a dismiss is in flight
            const banner = document.getElementById('failure-banner');
            if (!failure) {
                banner.style.display = 'none';
                return;
            }
            let text = '\u26A0 Detector stopped unexpectedly at ' + failure.time
                     + ' (' + failure.reason + ') - not monitoring.';
            if (failure.escalated) {
                text += ' Healthcheck alert sent.';
            }
            document.getElementById('failure-text').textContent = text;
            document.getElementById('failure-detail').textContent = failure.detail || '';
            banner.style.display = 'block';
        }

        async function dismissFailure() {
            document.getElementById('failure-banner').style.display = 'none';
            dismissPending++;
            try {
                await fetch('/api/dismiss-failure', {method: 'POST'});
            } catch (e) {
                console.error('Failed to dismiss banner:', e);
            } finally {
                dismissPending--;
            }
        }

        function formatDuration(seconds) {
            const s = Math.round(seconds);
            const h = Math.floor(s / 3600);
            const m = Math.floor((s % 3600) / 60);
            const sec = s % 60;
            const pad = n => String(n).padStart(2, '0');
            return h + ':' + pad(m) + ':' + pad(sec);
        }

        function updateUI(running, babyStatus) {
            isRunning = running;
            const badge = document.getElementById('status-badge');
            const btnStart = document.getElementById('btn-start');
            const btnStop = document.getElementById('btn-stop');
            const btnRestart = document.getElementById('btn-restart');
            const babyDiv = document.getElementById('baby-status');
            const babyEmoji = document.getElementById('baby-emoji');
            const durationDiv = document.getElementById('episode-duration');
            const extraInfo = document.getElementById('extra-info');

            btnStart.disabled = running;
            btnStop.disabled = !running;
            btnRestart.disabled = !running;

            if (running && babyStatus) {
                babyDiv.style.display = 'block';
                // Stream down (or no chunk for 30s) means the detector is not listening
                // at all - never let that render as "all quiet"
                const audioDown = babyStatus.stream_ok === false ||
                    (babyStatus.seconds_since_audio != null && babyStatus.seconds_since_audio > 30);
                if (audioDown) {
                    badge.className = 'status-badge status-alarm';
                    badge.textContent = 'NO AUDIO';
                    babyEmoji.textContent = '🔌';
                    durationDiv.textContent = 'Not listening - audio stream down!';
                    extraInfo.textContent = babyStatus.seconds_since_audio != null
                        ? 'No audio for ' + formatDuration(babyStatus.seconds_since_audio)
                        : 'Check the USB microphone';
                    extraInfo.style.color = '#e74c3c';
                } else if (babyStatus.mic_silent) {
                    badge.className = 'status-badge status-alarm';
                    badge.textContent = 'MIC SILENT';
                    babyEmoji.textContent = '🎤';
                    durationDiv.textContent = 'Microphone may be dead!';
                    extraInfo.textContent = '';
                    extraInfo.style.color = '#e74c3c';
                } else if (babyStatus.alarm) {
                    badge.className = 'status-badge status-alarm';
                    badge.textContent = 'ALARM!';
                    babyEmoji.textContent = '🚨';
                    durationDiv.textContent = formatDuration(babyStatus.episode_duration);
                    extraInfo.textContent = 'Since last cry: ' + formatDuration(babyStatus.since_last_cry);
                    extraInfo.style.color = '#aaa';
                } else if (babyStatus.crying) {
                    badge.className = 'status-badge status-crying';
                    badge.textContent = 'CRYING';
                    babyEmoji.textContent = '😢';
                    durationDiv.textContent = formatDuration(babyStatus.episode_duration);
                    extraInfo.textContent = 'Since last cry: ' + formatDuration(babyStatus.since_last_cry);
                    extraInfo.style.color = '#aaa';
                } else {
                    badge.className = 'status-badge status-running';
                    badge.textContent = 'RUNNING';
                    babyEmoji.textContent = '😴';
                    durationDiv.textContent = 'All quiet';
                    extraInfo.textContent = 'Quiet for ' + formatDuration(babyStatus.silence_since);
                    extraInfo.style.color = '#aaa';
                }
            } else if (running) {
                badge.className = 'status-badge status-running';
                badge.textContent = 'RUNNING';
                babyDiv.style.display = 'none';
            } else {
                badge.className = 'status-badge status-stopped';
                badge.textContent = 'STOPPED';
                babyDiv.style.display = 'none';
            }
        }

        function getConfig() {
            return {
                volume: parseInt(document.getElementById('volume').value),
                cry_freq_min: parseInt(document.getElementById('cry_freq_min').value),
                alert: parseInt(document.getElementById('alert').value),
                reset: parseInt(document.getElementById('reset').value),
                min_cry: parseInt(document.getElementById('min_cry').value),
                silence_gap: parseInt(document.getElementById('silence_gap').value),
                enable_stop_at: document.getElementById('enable_stop_at').checked,
                stop_at: document.getElementById('stop_at').value,
                enable_restart_at: document.getElementById('enable_restart_at').checked,
                restart_at: document.getElementById('restart_at').value,
                pushover: true,
                pushover_device: selectedDevice,
                enable_healthcheck: document.getElementById('enable_healthcheck').checked,
                record: document.getElementById('record').checked
            };
        }

        async function startDetector() {
            // The button is only re-enabled by the 2-second status poll, so
            // disable it here too: a second tap on a slow connection asks for a
            // second detector. The controller refuses one either way, but this
            // keeps the request from being sent at all.
            document.getElementById('btn-start').disabled = true;
            const config = getConfig();
            try {
                const resp = await fetch('/api/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(config)
                });
                const data = await resp.json();
                if (data.success) {
                    updateUI(true, null);
                } else {
                    alert(data.message);
                    updateUI(false, null);
                }
            } catch (e) {
                alert('Could not reach the monitor: ' + e);
                updateUI(false, null);
            }
        }

        async function stopDetector() {
            const resp = await fetch('/api/stop', {method: 'POST'});
            const data = await resp.json();
            if (data.success) {
                updateUI(false, null);
            } else {
                alert(data.message);
            }
        }

        async function restartDetector() {
            // Same reason as startDetector: no second request while this one runs
            document.getElementById('btn-restart').disabled = true;
            document.getElementById('btn-start').disabled = true;
            // One request, so the stop and the start happen under one lock on the
            // controller: doing it here as a STOP then a START left a window for
            // another phone to start a detector this restart would then replace.
            // The status poll puts the buttons and the badge right if it fails.
            try {
                const resp = await fetch('/api/restart', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(getConfig())
                });
                const data = await resp.json();
                if (data.success) {
                    updateUI(true, null);
                } else {
                    alert('Failed to restart: ' + data.message);
                }
            } catch (e) {
                alert('Could not reach the monitor: ' + e);
            }
        }

        function escapeHtml(s) {
            return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        }

        function colorLog(line) {
            const safe = escapeHtml(line);
            if (line.includes('CRY') || line.includes('ALARM') || line.includes('crying')) {
                return '<span class="log-cry">' + safe + '</span>';
            } else if (line.includes('OK') || line.includes('settled') || line.includes('Started')) {
                return '<span class="log-ok">' + safe + '</span>';
            } else if (line.includes('Warning') || line.includes('Ignored')) {
                return '<span class="log-warn">' + safe + '</span>';
            }
            return safe;
        }

        function syncConfigUI(config) {
            if (!config) return;
            // Skip while our own change is in flight, or the stale poll response
            // would snap the field back to the pre-change value.
            if (configPending === 0) {
                // Update number/text fields, but never clobber a field the user is editing
                for (const id of NUMBER_FIELDS.concat(TIME_FIELDS)) {
                    const el = document.getElementById(id);
                    if (el && document.activeElement !== el && config[id] !== undefined
                        && String(el.value) !== String(config[id])) {
                        el.value = config[id];
                    }
                }
                // Update checkboxes
                for (const id of CHECK_FIELDS) {
                    const el = document.getElementById(id);
                    if (el && document.activeElement !== el && config[id] !== undefined
                        && el.checked !== config[id]) {
                        el.checked = config[id];
                    }
                }
            }
            // Update device selection
            if (devicePending === 0 && config.pushover_device !== undefined
                && config.pushover_device !== selectedDevice) {
                selectedDevice = config.pushover_device;
                highlightDevice(selectedDevice);
            }
        }

        async function refreshStatus() {
            try {
                const resp = await fetch('/api/status');
                const data = await resp.json();
                updateUI(data.running, data.baby_status);
                updateFailureBanner(data.failure);
                syncConfigUI(data.config);

                const eventsDiv = document.getElementById('events');
                const eventsAutoScroll = document.getElementById('events-auto-scroll').checked;
                eventsDiv.innerHTML = data.events.map(colorLog).join('\\n');
                if (eventsAutoScroll) {
                    eventsDiv.scrollTop = eventsDiv.scrollHeight;
                }

                const logsDiv = document.getElementById('logs');
                const autoScroll = document.getElementById('auto-scroll').checked;
                logsDiv.innerHTML = data.logs.map(colorLog).join('\\n');
                if (autoScroll) {
                    logsDiv.scrollTop = logsDiv.scrollHeight;
                }
            } catch (e) {
                console.error('Status refresh failed:', e);
            }
        }

        // Refresh every 2 seconds
        wireConfigInputs();
        setInterval(refreshStatus, 2000);
        refreshStatus();
    </script>
</body>
</html>
"""

load_config()


@app.before_request
def require_auth():
    """Enforce basic auth only when config.py sets WEB_PASSWORD.

    The web UI and the detector's status port are otherwise open to anyone on the
    WiFi, who could mute alerts or retune detection.
    """
    if not WEB_PASSWORD:
        return None
    auth = request.authorization
    if auth and auth.password is not None:
        user_ok = hmac.compare_digest(str(auth.username or '').encode(), WEB_USERNAME.encode())
        pass_ok = hmac.compare_digest(auth.password.encode(), WEB_PASSWORD.encode())
        if user_ok and pass_ok:
            return None
    return Response('Authentication required', 401,
                    {'WWW-Authenticate': 'Basic realm="Baby Monitor"'})


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, config=current_config,
                                  config_limits=CONFIG_LIMITS,
                                  devices=list(DEVICE_LABELS.items()),
                                  device_keys=list(DEVICE_LABELS))

@app.route('/api/status')
def api_status():
    """Get current status, baby status, and logs"""
    running = is_running()
    baby_status = None

    # Try to fetch baby status from detector's status endpoint
    if running:
        try:
            import urllib.request
            url = detector_endpoint('/status')
            response = urllib.request.urlopen(url, timeout=2)
            baby_status = json.loads(response.read().decode())
        except Exception:
            pass

    with log_lock:
        logs = list(log_buffer)
        events = list(event_log_buffer)

    return jsonify({
        'running': running,
        'baby_status': baby_status,
        'events': events,
        'logs': logs,
        'config': current_config,
        'failure': last_failure
    })

@app.route('/api/device', methods=['POST'])
def api_device():
    """Change pushover device. Works whether or not the detector is running:
    a running detector is updated live, a stopped one picks it up on next start."""
    global current_config

    if request.json and 'pushover_device' in request.json:
        device = request.json['pushover_device']
        if not isinstance(device, str) or device not in DEVICE_LABELS:
            return jsonify({'success': False,
                            'message': f"Unknown device '{device}'",
                            'device': current_config['pushover_device']}), 400

        old_device = current_config['pushover_device']
        if device != old_device:
            if device == '__muted__':
                log_event("Config: Pushover alerts MUTED")
            elif old_device == '__muted__':
                log_event(f"Config: Pushover alerts UNMUTED ({DEVICE_LABELS.get(device, device)})")
            else:
                log_event(f"Config: Pushover device changed "
                          f"{DEVICE_LABELS.get(old_device, old_device)} -> {DEVICE_LABELS.get(device, device)}")
        current_config['pushover_device'] = device
        save_config()

        # Forward to detector's config endpoint (only meaningful while running)
        if is_running():
            try:
                import urllib.request
                url = detector_endpoint('/config')
                data = json.dumps({'pushover_device': device}).encode('utf-8')
                req = urllib.request.Request(url, data=data, method='POST')
                req.add_header('Content-Type', 'application/json')
                urllib.request.urlopen(req, timeout=2)
            except Exception as e:
                return jsonify({'success': False, 'message': str(e), 'device': device})

        return jsonify({'success': True, 'device': device})

    return jsonify({'success': False, 'message': 'No device specified'})

@app.route('/api/dismiss-failure', methods=['POST'])
def api_dismiss_failure():
    """Clear the crash banner. Shared state, so it clears on every phone."""
    global last_failure
    last_failure = None
    return jsonify({'success': True})

@app.route('/api/start', methods=['POST'])
def api_start():
    """Start the detector with given config"""
    global current_config

    if request.json:
        if not isinstance(request.json, dict):
            return jsonify({'success': False, 'message': 'Expected a JSON object'}), 400
        clean, rejected = sanitize_config(request.json)
        if rejected:
            print(f"Ignored invalid settings on start: {', '.join(sorted(rejected))}")
        current_config.update(clean)
        save_config()

    success, message = start_detector()
    return jsonify({'success': success, 'message': message})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    """Stop the detector"""
    success, message = stop_detector()
    # Unconditionally: stop_detector() reports failure when the detector has
    # already crashed, and pressing STOP on a crash banner still means the parent
    # is ending the session, not asking for it back at the next boot.
    #
    # Only an explicit STOP ends a session. cleanup() also stops the detector when
    # the controller itself is signalled (a service restart, a clean reboot), and
    # those must still resume afterwards, so the clearing lives here.
    save_detector_state(False)
    return jsonify({'success': success, 'message': message})

@app.route('/api/restart', methods=['POST'])
def api_restart():
    """Restart the detector as one step, applying any settings posted with it.

    The phone used to do this itself, as a STOP followed by a START. Between the
    two there was no detector and no lock held, so a second phone - or the daily
    restart, or a boot resume - could start one that this restart then replaced,
    leaving an untracked process holding the microphone. It also meant a power
    cut inside that window came back with the session marked ended, because STOP
    is what ends a session for good.
    """
    global current_config

    if request.json:
        if not isinstance(request.json, dict):
            return jsonify({'success': False, 'message': 'Expected a JSON object'}), 400
        clean, rejected = sanitize_config(request.json)
        if rejected:
            print(f"Ignored invalid settings on restart: {', '.join(sorted(rejected))}")
        current_config.update(clean)
        save_config()

    success, message = restart_detector('Restart from the phone')
    return jsonify({'success': success, 'message': message})

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """Get or update configuration, forwarding to running detector"""
    global current_config

    if request.method == 'POST' and request.json:
        if not isinstance(request.json, dict):
            return jsonify({'error': 'Expected a JSON object'}), 400
        clean, rejected = sanitize_config(request.json)
        if rejected:
            print(f"Ignored invalid settings: {', '.join(sorted(rejected))}")
        for key, new_value in clean.items():
            old_value = current_config[key]
            if old_value != new_value:
                log_event(describe_config_change(key, old_value, new_value))
            current_config[key] = new_value
        save_config()

        if ('status_port' in clean and is_running()
                and clean['status_port'] != running_status_port):
            log_event(f"Config: Status port takes effect on next start "
                      f"(detector still using {running_status_port})")

        # Forward to running detector if applicable
        if is_running():
            detector_config = {}
            mapping = {
                'volume': ('volume_threshold', lambda v: v),
                'cry_freq_min': ('cry_freq_min', lambda v: v),
                'alert': ('alert_window', lambda v: v * 60),
                'reset': ('reset_window', lambda v: v * 60),
                'min_cry': ('min_cry_duration', lambda v: v),
                'silence_gap': ('silence_gap', lambda v: v),
            }
            for key, (attr, convert) in mapping.items():
                if key in clean:
                    detector_config[attr] = convert(clean[key])

            if detector_config:
                try:
                    import urllib.request
                    url = detector_endpoint('/config')
                    data = json.dumps(detector_config).encode('utf-8')
                    req = urllib.request.Request(url, data=data, method='POST')
                    req.add_header('Content-Type', 'application/json')
                    urllib.request.urlopen(req, timeout=2)
                except Exception as e:
                    print(f"Failed to forward config to detector: {e}")

    return jsonify(current_config)

def port_is_free(host, port):
    """Whether the web port can still be bound, so we do not resume over a
    controller that is already running."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def cleanup(signum=None, frame=None):
    """Cleanup on exit"""
    stop_detector()
    sys.exit(0)

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Web Controller for Baby Cry Detector')
    parser.add_argument('--port', type=int, default=5000,
                        help='Port for web interface (default: 5000)')
    parser.add_argument('--host', type=str, default='0.0.0.0',
                        help='Host to bind to (default: 0.0.0.0 for all interfaces)')
    args = parser.parse_args()

    # Register cleanup handlers
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    print(f"Starting Baby Monitor Web Controller")
    print(f"Open http://<your-pi-ip>:{args.port} on your phone")
    print(f"Press Ctrl+C to stop")

    # Check the port before resuming: app.run() binds it below, and a second
    # instance that dies on "Address already in use" would otherwise leave the
    # detector it just spawned running in its own process group, untracked.
    if port_is_free(args.host, args.port):
        # Under __main__ only: importing this module must never spawn a detector,
        # and for the same reason neither may it schedule one
        resume_detector_after_boot()
        start_restart_scheduler()
        if current_config['enable_restart_at']:
            print(f"Daily restart: {current_config['restart_at']}")
    else:
        print(f"Port {args.port} is already in use; not resuming the detector")

    app.run(host=args.host, port=args.port, debug=False, threaded=True)
