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


from collections import deque
import time
import wave
import urllib.request
import json
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
MAX_RECORDING_FRAMES = 5000  # ~15 minutes at 44100/8192 (~5.4 chunks/sec) - prevents unbounded memory

# Pushover emergency notification settings
PUSHOVER_RETRY = 30   # Retry every 30 seconds if not acknowledged
PUSHOVER_EXPIRE = 3600  # Give up after 1 hour
ENABLE_PUSHOVER = True and PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN  # Auto-disable if credentials missing

# Silent mic detection
MIC_SILENT_THRESHOLD = 5  # Volume below this is considered "dead silence"
MIC_SILENT_DURATION = 300  # 5 minutes of dead silence triggers alert

# Default HTTP status server port
DEFAULT_STATUS_PORT = 8080


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

                status = {
                    "crying": detector.initial_start_time is not None,
                    "alarm": detector.alert_sent,
                    "mic_silent": detector.mic_silent,
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
            if self.path == '/config':
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                try:
                    config = json.loads(body)
                    # Update pushover device if provided
                    if 'pushover_device' in config:
                        old_device = detector.pushover_device
                        detector.pushover_device = config['pushover_device']
                        print(f"{GREEN}Pushover device changed: {old_device} -> {detector.pushover_device}{RESET}")

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True}).encode())
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            else:
                self.send_response(404)
                self.end_headers()
    return StatusHandler


class CryDetector:
    def __init__(self):
        self.audio = _create_pyaudio_silently()
        self.stream = None
        self.initial_start_time = None  # When confirmed crying episode began
        self.last_cry_time = None  # Most recent confirmed cry detection
        self.last_smoothed_cry_time = None  # Most recent smoothed detection (for brief sound logic)
        self.recent_detections = deque(maxlen=SMOOTHING_WINDOW)
        self.alert_sent = False  # Track if we've already alerted for this episode
        self.potential_cry_start_time = None  # When crying first detected (before confirmation)
        self.session_start_time = None  # When the detector session started
        self.last_episode_end_time = None  # When the last crying episode ended
        
        # Recording state - only record during crying episodes
        self.is_recording = False
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
        self.enable_status_server = False

        # Silent mic detection
        self.mic_silent_since = None  # When consecutive dead silence started
        self.mic_silent = False  # True when mic appears dead

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

    def find_usb_audio_device(self):
        """Find USB audio device index by name"""
        for i in range(self.audio.get_device_count()):
            info = self.audio.get_device_info_by_index(i)
            if 'USB' in info['name'] and info['maxInputChannels'] > 0:
                print(f"{GREEN}✓ Found USB audio device: {info['name']} (index {i}){RESET}")
                return i
        return None

    def start_audio_stream(self):
        """Start or restart just the audio stream (safe for reconnection)."""
        device_index = self.find_usb_audio_device()
        if device_index is None:
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

    def start(self):
        """Start the audio stream, status server, and print config."""
        self.start_audio_stream()
        self.session_start_time = time.time()
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
            print(f"{GREEN}Status server: http://0.0.0.0:{self.status_port}/status{RESET}")
        else:
            print(f"{YELLOW}Status server: Disabled (use --status-port PORT to enable){RESET}")

    def start_status_server(self):
        """Start the HTTP status server in a background thread"""
        handler = create_status_handler(self)
        self.status_server = HTTPServer(('0.0.0.0', self.status_port), handler)
        thread = threading.Thread(target=self.status_server.serve_forever, daemon=True)
        thread.start()

    def stop_status_server(self):
        """Stop the HTTP status server"""
        if self.status_server:
            self.status_server.shutdown()
            self.status_server = None

    def send_heartbeat(self):
        """Send heartbeat ping in a background thread so retries don't block monitoring."""
        if not self.healthcheck_url:
            return
        url = self.healthcheck_url
        if self.mic_silent:
            url = url.rstrip('/') + '/fail'
        is_fail = self.mic_silent

        def _send(url, is_fail):
            retry_delay = 30  # seconds
            attempt = 0
            while True:
                try:
                    urllib.request.urlopen(url, timeout=10)
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    if is_fail:
                        print(f"{RED}[{timestamp}] ♥ Heartbeat FAIL sent (mic silent){RESET}")
                    else:
                        msg = f"[{timestamp}] ♥ Heartbeat sent"
                        if attempt > 0:
                            msg += f" (after {attempt} {'retry' if attempt == 1 else 'retries'})"
                        print(f"{GREEN}{msg}{RESET}")
                    return
                except Exception as e:
                    attempt += 1
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    print(f"{YELLOW}[{timestamp}] ⚠ Heartbeat failed (retry in {retry_delay}s): {e}{RESET}")
                    time.sleep(retry_delay)

        threading.Thread(target=_send, args=(url, is_fail), daemon=True).start()

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
                # Read audio chunk with reconnection on failure
                try:
                    audio_data = self.stream.read(CHUNK, exception_on_overflow=False)
                except (IOError, OSError) as e:
                    print(f"{RED}✗ Audio stream error: {e}{RESET}")
                    print(f"{YELLOW}Attempting to reconnect...{RESET}")
                    self.cleanup_stream()
                    time.sleep(2)
                    try:
                        self.start_audio_stream()
                        continue
                    except Exception as reconnect_error:
                        print(f"{RED}✗ Reconnection failed: {reconnect_error}{RESET}")
                        time.sleep(5)
                        continue

                self.chunk_count += 1
                current_time = time.time()

                # Check if we should auto-stop
                if self.stop_time:
                    now = datetime.now().time()
                    # Stop if current time matches stop time (within 1-second window)
                    if now.hour == self.stop_time.hour and now.minute == self.stop_time.minute and now.second < 2:
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"\n{GREEN}[{timestamp}] Auto-stop time reached ({self.stop_time.strftime('%H:%M')}). Shutting down...{RESET}")
                        break

                # Send heartbeat if enabled and interval has elapsed
                if self.healthcheck_url:
                    if self.last_heartbeat_time is None or (current_time - self.last_heartbeat_time) >= self.heartbeat_interval:
                        self.send_heartbeat()
                        self.last_heartbeat_time = current_time

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
                        self.current_episode_frames = []
                        print(f"{YELLOW}📹 Started recording episode{RESET}")

                    # Add audio chunk to current episode (with memory limit)
                    if len(self.current_episode_frames) < MAX_RECORDING_FRAMES:
                        self.current_episode_frames.append(audio_data)

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
                        self.alert_sent = False
                        sustained_duration = current_time - self.potential_cry_start_time
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"\n{RED}[{timestamp}] 🙁 Baby started crying! (sustained for {sustained_duration:.1f}s, Vol: {volume:.0f}, Freq: {freq:.1f} Hz){RESET}")

                    # Check if we should alert
                    elapsed_time = current_time - self.initial_start_time
                    if elapsed_time >= self.alert_window and not self.alert_sent:
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        print(f"\n{RED}[{timestamp}] 🚨 ALERT! Baby has been crying for {elapsed_time/60:.1f} minutes!{RESET}")
                        print(f"{RED}   (Episode started at {datetime.fromtimestamp(self.initial_start_time).strftime('%H:%M:%S')}, currently crying){RESET}")

                        # Send EMERGENCY Pushover notification (skip if muted)
                        if self.pushover_client and self.enable_pushover and self.pushover_device != '__muted__':
                            try:
                                self.pushover_client.send_message(
                                    f"Baby has been crying for {elapsed_time/60:.1f} minutes! Please check on the baby.",
                                    title="🚨 BABY MONITOR EMERGENCY",
                                    priority=2,  # Emergency - requires acknowledgment
                                    retry=PUSHOVER_RETRY,    # Retry every 30 seconds
                                    expire=PUSHOVER_EXPIRE,  # Give up after 1 hour
                                    device=self.pushover_device  # None sends to all devices
                                )
                                print(f"{GREEN}✓ Emergency notification sent! (will retry every {PUSHOVER_RETRY}s until acknowledged){RESET}")
                            except Exception as e:
                                print(f"{RED}✗ Failed to send notification: {e}{RESET}")
                                # Alert via healthcheck that Pushover failed
                                if self.healthcheck_url:
                                    try:
                                        fail_url = self.healthcheck_url.rstrip('/') + '/fail'
                                        urllib.request.urlopen(fail_url, timeout=10)
                                        print(f"{YELLOW}⚠ Healthcheck FAIL sent (Pushover delivery failed){RESET}")
                                    except Exception:
                                        pass
                        elif self.pushover_device == '__muted__':
                            print(f"{YELLOW}🔇 Notification muted{RESET}")

                        # Reset episode — if baby keeps crying, a new episode starts immediately
                        print(f"{YELLOW}   Episode reset. New episode starts if crying continues.{RESET}")
                        self.last_episode_end_time = self.last_cry_time
                        self.initial_start_time = current_time
                        self.alert_sent = False
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
                            self.alert_sent = False

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
    import argparse

    parser = argparse.ArgumentParser(description='Baby cry detector with USB audio and Pushover notifications')
    parser.add_argument('-v', '--volume', type=int, default=VOLUME_THRESHOLD,
                        help=f'Volume threshold (default: {VOLUME_THRESHOLD})')
    parser.add_argument('--cry-freq-min', type=int, default=CRY_FREQ_MIN,
                        help=f'Minimum cry frequency in Hz (default: {CRY_FREQ_MIN})')
    parser.add_argument('-r', '--ratio', type=float, default=CRY_RATIO_THRESHOLD,
                        help=f'Cry ratio threshold (default: {CRY_RATIO_THRESHOLD})')
    parser.add_argument('--record', action='store_true', default=False,
                        help='Enable recording of crying episodes (default: disabled)')
    parser.add_argument('--pushover', action='store_true', default=False,
                        help='Enable Pushover emergency notifications (default: disabled)')
    parser.add_argument('--pushover-device', type=str, default=None,
                        help='Pushover device name to send to (default: all devices)')
    parser.add_argument('--alert', type=int, default=ALERT_WINDOW // 60,
                        help=f'Minutes of crying before alert (default: {ALERT_WINDOW // 60})')
    parser.add_argument('--reset', type=int, default=RESET_WINDOW // 60,
                        help=f'Minutes of silence before episode reset (default: {RESET_WINDOW // 60})')
    parser.add_argument('--min-cry', type=int, default=MIN_CRY_DURATION,
                        help=f'Seconds of sustained crying before announcing episode (default: {MIN_CRY_DURATION})')
    parser.add_argument('--silence-gap', type=int, default=SILENCE_GAP,
                        help=f'Seconds of silence within crying that resets detection (default: {SILENCE_GAP})')
    parser.add_argument('--healthcheck', type=str, default=None,
                        help='Healthchecks.io ping URL for heartbeat monitoring')
    parser.add_argument('--heartbeat', type=int, default=5,
                        help='Heartbeat interval in minutes (default: 5)')
    parser.add_argument('--stop-at', type=str, default=None,
                        help='Time to auto-stop the script (HH:MM format, e.g. 07:00)')
    parser.add_argument('--status-port', type=int, default=None,
                        help=f'Enable HTTP status server on this port (default: disabled, use {DEFAULT_STATUS_PORT})')
    args = parser.parse_args()

    detector = CryDetector()
    detector.volume_threshold = args.volume
    detector.cry_freq_min = args.cry_freq_min
    detector.cry_ratio_threshold = args.ratio
    detector.enable_recording = args.record
    detector.enable_pushover = args.pushover and PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN
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
    if args.stop_at:
        try:
            h, m = args.stop_at.split(':')
            detector.stop_time = datetime.strptime(f"{h}:{m}", "%H:%M").time()
        except ValueError:
            print(f"Invalid --stop-at format: '{args.stop_at}'. Use HH:MM (e.g. 07:00)")
            exit(1)
    detector.start()
    detector.monitor()
