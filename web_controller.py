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
import threading
from datetime import datetime
from collections import deque
from flask import Flask, render_template_string, request, jsonify

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

# Log file settings
LOG_DIR = "/var/log/babymonitor"
LOG_FILE = None  # Will be set when detector starts

# Configuration defaults
DEFAULT_CONFIG = {
    'volume': 900,
    'cry_freq_min': 400,   # Hz
    'ratio': 0.40,
    'alert': 10,           # minutes
    'reset': 10,           # minutes
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

# State
detector_process = None
current_config = DEFAULT_CONFIG.copy()
log_buffer = deque(maxlen=100)  # Keep last 100 log lines
event_log_buffer = deque(maxlen=300)  # Key events only
log_lock = threading.Lock()

# Patterns that indicate key events worth showing in the event log
EVENT_PATTERNS = ['Baby started crying', 'Ignored brief sound', 'ALERT', 'Baby settled',
                  'MICROPHONE SILENT', 'Microphone recovered', 'Heartbeat FAIL', 'Heartbeat failed']

# ANSI color code stripping
import re
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def strip_ansi(text):
    return ANSI_ESCAPE.sub('', text)

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
    """Monitor detector process and pause healthcheck if it exits on its own (e.g. auto-stop)."""
    global detector_process
    process.wait()  # Block until process exits
    # Only act if this process is still the current one (not already stopped via web UI)
    if detector_process is not None and detector_process.pid == process.pid:
        detector_process = None
        hc_paused = pause_healthcheck()
        msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Detector exited (auto-stop)"
        if hc_paused:
            msg += " (healthcheck paused)"
        with log_lock:
            log_buffer.append(msg)
            event_log_buffer.append(msg)
        if LOG_FILE and os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'a') as f:
                f.write(msg + "\n")

def start_detector():
    """Start the cry detector process"""
    global detector_process, log_buffer, LOG_FILE

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
    with log_lock:
        log_buffer.clear()
        event_log_buffer.clear()
        log_buffer.append(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {start_msg}")

    # Write to log file
    with open(LOG_FILE, 'w') as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {start_msg}\n")

    try:
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

def stop_detector():
    """Stop the cry detector process"""
    global detector_process

    if detector_process is None or detector_process.poll() is not None:
        detector_process = None
        return False, "Detector is not running"

    try:
        os.killpg(os.getpgid(detector_process.pid), signal.SIGTERM)
        detector_process.wait(timeout=5)
        detector_process = None

        # Pause healthcheck to avoid false alerts
        hc_paused = pause_healthcheck()

        stop_msg = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Detector stopped"
        if hc_paused:
            stop_msg += " (healthcheck paused)"
        with log_lock:
            log_buffer.append(stop_msg)
        # Write to log file
        if LOG_FILE and os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'a') as f:
                f.write(stop_msg + "\n")
            # Copy log to USB drive for backup
            usb_log_dir = "/media/tinybaby/ESD-USB1/logs"
            try:
                os.makedirs(usb_log_dir, exist_ok=True)
                import shutil
                shutil.copy2(LOG_FILE, usb_log_dir)
                print(f"Log copied to {usb_log_dir}/{os.path.basename(LOG_FILE)}")
            except Exception as e:
                print(f"Failed to copy log to USB: {e}")
        return True, "Detector stopped"
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
        <button id="btn-h" class="btn-device {{ 'selected' if config.pushover_device == 'h_phone' else '' }}" onclick="selectDevice('h_phone')">mama</button>
        <button id="btn-a" class="btn-device {{ 'selected' if config.pushover_device == 'a_phone' else '' }}" onclick="selectDevice('a_phone')">papa</button>
        <button id="btn-g" class="btn-device {{ 'selected' if config.pushover_device == 'grandma_phone' else '' }}" onclick="selectDevice('grandma_phone')">grandma</button>
    </div>
    <div class="restart-note">Alerts go to selected device. Switches instantly when running.</div>

    <div class="card">
        <h2>Configuration</h2>
        <div class="restart-note">Restart to apply changes.</div>
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
        let statusInterval = null;
        let selectedDevice = '{{ config.pushover_device }}';

        function stepValue(id, delta) {
            const input = document.getElementById(id);
            let val = parseInt(input.value) || 0;
            val = Math.max(0, val + delta);
            input.value = val;
        }

        async function selectDevice(device) {
            selectedDevice = device;
            document.getElementById('btn-h').classList.toggle('selected', device === 'h_phone');
            document.getElementById('btn-a').classList.toggle('selected', device === 'a_phone');
            document.getElementById('btn-g').classList.toggle('selected', device === 'grandma_phone');

            // If running, update the device immediately
            if (isRunning) {
                try {
                    await fetch('/api/device', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({pushover_device: device})
                    });
                } catch (e) {
                    console.error('Failed to update device:', e);
                }
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
                if (babyStatus.mic_silent) {
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

        function colorLog(line) {
            if (line.includes('CRY') || line.includes('ALARM') || line.includes('crying')) {
                return '<span class="log-cry">' + line + '</span>';
            } else if (line.includes('OK') || line.includes('settled') || line.includes('Started')) {
                return '<span class="log-ok">' + line + '</span>';
            } else if (line.includes('Warning') || line.includes('Ignored')) {
                return '<span class="log-warn">' + line + '</span>';
            }
            return line;
        }

        async function refreshStatus() {
            try {
                const resp = await fetch('/api/status');
                const data = await resp.json();
                updateUI(data.running, data.baby_status);

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
        setInterval(refreshStatus, 2000);
        refreshStatus();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, config=current_config)

@app.route('/api/status')
def api_status():
    """Get current status, baby status, and logs"""
    running = is_running()
    baby_status = None

    # Try to fetch baby status from detector's status endpoint
    if running:
        try:
            import urllib.request
            url = f"http://localhost:{current_config['status_port']}/status"
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
        'logs': logs
    })

@app.route('/api/device', methods=['POST'])
def api_device():
    """Change pushover device on running detector"""
    global current_config

    if not is_running():
        return jsonify({'success': False, 'message': 'Detector not running'})

    if request.json and 'pushover_device' in request.json:
        device = request.json['pushover_device']
        current_config['pushover_device'] = device

        # Forward to detector's config endpoint
        try:
            import urllib.request
            url = f"http://localhost:{current_config['status_port']}/config"
            data = json.dumps({'pushover_device': device}).encode('utf-8')
            req = urllib.request.Request(url, data=data, method='POST')
            req.add_header('Content-Type', 'application/json')
            urllib.request.urlopen(req, timeout=2)
            return jsonify({'success': True, 'device': device})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})

    return jsonify({'success': False, 'message': 'No device specified'})

@app.route('/api/start', methods=['POST'])
def api_start():
    """Start the detector with given config"""
    global current_config

    if request.json:
        for key in request.json:
            if key in current_config:
                current_config[key] = request.json[key]

    success, message = start_detector()
    return jsonify({'success': success, 'message': message})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    """Stop the detector"""
    success, message = stop_detector()
    return jsonify({'success': success, 'message': message})

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """Get or update configuration"""
    global current_config

    if request.method == 'POST' and request.json:
        for key in request.json:
            if key in current_config:
                current_config[key] = request.json[key]

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

    app.run(host=args.host, port=args.port, debug=False, threaded=True)
