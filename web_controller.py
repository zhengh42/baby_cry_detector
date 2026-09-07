#!/usr/bin/env python3
"""
Web Controller for Baby Cry Detector.
Provides a mobile-friendly web interface to start/stop and configure cry_detector.py
"""
import subprocess
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
current_config = DEFAULT_CONFIG.copy()
config_lock = threading.Lock()
_last_saved_config = None  # last snapshot written to disk, to skip no-op saves
log_buffer = deque(maxlen=100)  # Keep last 100 log lines
event_log_buffer = deque(maxlen=300)  # Key events only
log_lock = threading.Lock()

# Patterns that indicate key events worth showing in the event log
EVENT_PATTERNS = ['Baby started crying', 'Ignored brief sound', 'ALERT', 'Baby settled',
                  'MICROPHONE SILENT', 'Microphone recovered', 'Heartbeat FAIL', 'Heartbeat failed',
                  'Audio stream error', 'Audio stream recovered', 'Reconnection failed']


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
    if key == 'stop_at':
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

def save_detector_state(running):
    """Remember whether the detector is meant to be listening.

    Written on every start and stop so a reboot can tell an interrupted session
    apart from one that was deliberately ended.
    """
    try:
        tmp_path = DETECTOR_STATE_FILE + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump({'running': bool(running)}, f)
        os.replace(tmp_path, DETECTOR_STATE_FILE)
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
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    note = (f"[{timestamp}] Detector was running when the monitor last went down "
            "- resuming")
    with log_lock:
        event_log_buffer.append(note)
    write_controller_log(note)

    # Never let a failed resume take the dashboard down with it: this runs
    # unattended at boot, and a phone that can still reach the page and press
    # START is worth far more than a clean traceback in the journal.
    try:
        success, message = start_detector()
    except Exception as e:
        success, message = False, f"{type(e).__name__}: {e}"
    if not success:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        failed = f"[{timestamp}] Could not resume the detector: {message}"
        with log_lock:
            event_log_buffer.append(failed)
        write_controller_log(failed)
        print(failed)


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
    'status_port': ('Status port', ''),
    'heartbeat': ('Heartbeat interval', ' min'),
    'enable_stop_at': ('Stop-at', None),
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
    if LOG_FILE and os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, 'a') as f:
                f.write(log_line + "\n")
        except Exception:
            pass

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

def process_monitor(process):
    """React when the detector exits on its own.

    A stop requested from the web UI is handled by stop_detector(), so this only
    fires for self-exits: a clean auto-stop, or a crash / startup failure.
    Pausing the healthcheck is reserved for clean exits - pausing after a crash
    would silence the one alert that says nobody is watching the baby.
    """
    global detector_process
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
        # escalate now rather than pausing the check
        hc_failed = fail_healthcheck()
        reason = (f"killed by signal {-returncode}" if returncode < 0
                  else f"exit code {returncode}")
        msg = f"[{timestamp}] Detector exited UNEXPECTEDLY ({reason}) - NOT monitoring"
        if hc_failed:
            msg += " (healthcheck FAIL sent)"
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

    if LOG_FILE and os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, 'a') as f:
                f.write(msg + "\n")
                if extra:
                    f.write(extra + "\n")
        except Exception:
            pass

def start_detector():
    """Start the cry detector process"""
    global detector_process, log_buffer, LOG_FILE, last_failure, running_status_port

    if detector_process is not None and detector_process.poll() is None:
        return False, "Detector is already running"

    # Create log directory if needed
    os.makedirs(LOG_DIR, exist_ok=True)

    # Create log file with timestamp
    log_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    LOG_FILE = os.path.join(LOG_DIR, f"detector_{log_timestamp}.log")

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
        monitor_thread = threading.Thread(target=process_monitor, args=(detector_process,), daemon=True)
        monitor_thread.start()

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
            # Move log to USB drive for archival
            usb_log_dir = "/media/tinybaby/usb-data/logs"
            try:
                os.makedirs(usb_log_dir, exist_ok=True)
                import shutil
                shutil.copy2(LOG_FILE, usb_log_dir)
                os.remove(LOG_FILE)
                print(f"Log moved to {usb_log_dir}/{os.path.basename(LOG_FILE)}")
            except Exception as e:
                print(f"Failed to move log to USB: {e}")
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
        const CHECK_FIELDS = ['enable_stop_at', 'enable_healthcheck', 'record'];
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

        // Typed-in numbers, the stop-at time, and the checkboxes save on change too
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
            document.getElementById('stop_at').addEventListener('change', (e) => {
                postConfig({stop_at: e.target.value});
            });
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
                pushover: true,
                pushover_device: selectedDevice,
                enable_healthcheck: document.getElementById('enable_healthcheck').checked,
                record: document.getElementById('record').checked
            };
        }

        async function startDetector() {
            const config = getConfig();
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
            // Stop first
            const stopResp = await fetch('/api/stop', {method: 'POST'});
            const stopData = await stopResp.json();
            if (!stopData.success) {
                alert('Failed to stop: ' + stopData.message);
                return;
            }
            // Brief delay to ensure clean shutdown
            await new Promise(resolve => setTimeout(resolve, 500));
            // Start with current config
            const config = getConfig();
            const startResp = await fetch('/api/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(config)
            });
            const startData = await startResp.json();
            if (startData.success) {
                updateUI(true, null);
            } else {
                alert('Failed to restart: ' + startData.message);
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
                for (const id of NUMBER_FIELDS.concat(['stop_at'])) {
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
    if success:
        # Only an explicit STOP ends the session. cleanup() also stops the
        # detector when the controller itself is signalled (a service restart, a
        # clean reboot), and that must still resume afterwards.
        save_detector_state(False)
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

    # Under __main__ only: importing this module must never spawn a detector
    resume_detector_after_boot()

    app.run(host=args.host, port=args.port, debug=False, threaded=True)
