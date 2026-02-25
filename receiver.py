#!/usr/bin/env python3
"""
Baby Monitor Receiver for Raspberry Pi with Rainbow HAT.
Polls the baby monitor for status and alerts using LEDs and buzzer.
"""
import time
import urllib.request
import json
import argparse
import signal
from datetime import datetime

# Global receiver instance for signal handler
_receiver_instance = None


def _signal_acknowledge(signum, frame):
    """Handle SIGUSR1 signal to acknowledge alarm"""
    if _receiver_instance is not None:
        _receiver_instance.acknowledge_alarm()

# Try to import Rainbow HAT (will fail if not on Pi with HAT)
try:
    import rainbowhat
    RAINBOW_HAT_AVAILABLE = True
except ImportError:
    RAINBOW_HAT_AVAILABLE = False
    print("Warning: rainbowhat not available. Running in simulation mode.")

# Try to import gpiozero for buzzer (works better with newer Pi OS)
BUZZER_GPIO = 13  # Rainbow HAT buzzer pin
try:
    from gpiozero import TonalBuzzer
    from gpiozero.tones import Tone
    GPIOZERO_AVAILABLE = True
except ImportError:
    GPIOZERO_AVAILABLE = False

# ANSI color codes
RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
RESET = '\033[0m'

# Default settings
DEFAULT_POLL_INTERVAL = 10  # seconds
DEFAULT_MONITOR_URL = "http://babymonitor.local:8080/status"


class BabyMonitorReceiver:
    def __init__(self, monitor_url, poll_interval=DEFAULT_POLL_INTERVAL, no_gpio=False):
        self.monitor_url = monitor_url
        self.poll_interval = poll_interval
        self.last_alarm_state = False
        self.alarm_acknowledged = False  # Track if alarm was acknowledged via touch button
        self.error_acknowledged = False  # Track if connection error was acknowledged
        self.connection_errors = 0
        self.max_connection_errors = 5  # Alert after this many consecutive errors
        self.no_gpio = no_gpio

        # Set up signal handler for external acknowledge (from launcher)
        global _receiver_instance
        _receiver_instance = self
        signal.signal(signal.SIGUSR1, _signal_acknowledge)

        # Initialize gpiozero buzzer (skip if no_gpio mode)
        self.buzzer = None
        self.buzzer_available = False
        if GPIOZERO_AVAILABLE and not no_gpio:
            try:
                self.buzzer = TonalBuzzer(BUZZER_GPIO)
                self.buzzer_available = True
            except Exception as e:
                print(f"Warning: Buzzer init failed ({e})")

        # Set up touch button handler for acknowledgment (skip if no_gpio mode)
        self.touch_available = False
        if RAINBOW_HAT_AVAILABLE and not no_gpio:
            try:
                @rainbowhat.touch.A.press()
                def touch_a(channel):
                    self.acknowledge_alarm()

                @rainbowhat.touch.B.press()
                def touch_b(channel):
                    self.acknowledge_alarm()

                @rainbowhat.touch.C.press()
                def touch_c(channel):
                    self.acknowledge_alarm()

                self.touch_available = True
            except RuntimeError as e:
                print(f"Warning: Touch buttons not available ({e})")

    def acknowledge_alarm(self):
        """Acknowledge the alarm or error - stops buzzer but keeps LEDs"""
        acknowledged = False
        timestamp = datetime.now().strftime("%H:%M:%S")

        if self.last_alarm_state and not self.alarm_acknowledged:
            self.alarm_acknowledged = True
            print(f"[{timestamp}] {GREEN}✓ Alarm acknowledged{RESET}")
            acknowledged = True

        if self.connection_errors >= self.max_connection_errors and not self.error_acknowledged:
            self.error_acknowledged = True
            print(f"[{timestamp}] {GREEN}✓ Connection error acknowledged{RESET}")
            acknowledged = True

        # Brief confirmation beep
        if acknowledged:
            self.play_tone(440, 0.1)  # A4

    def play_tone(self, frequency, duration):
        """Play a tone on the buzzer using gpiozero"""
        if self.buzzer_available and self.buzzer is not None:
            try:
                self.buzzer.play(Tone(frequency))
                time.sleep(duration)
                self.buzzer.stop()
            except Exception:
                pass

    def fetch_status(self):
        """Fetch status from baby monitor"""
        try:
            response = urllib.request.urlopen(self.monitor_url, timeout=10)
            data = json.loads(response.read().decode())
            self.connection_errors = 0  # Reset on successful connection
            self.error_acknowledged = False  # Reset error acknowledgment
            return data
        except Exception as e:
            self.connection_errors += 1
            return None

    def buzzer_error(self):
        """Sound the buzzer for connection error - different pattern from alarm"""
        if self.buzzer_available and not self.error_acknowledged:
            # Play lower, slower pattern to distinguish from alarm
            for _ in range(2):
                self.play_tone(330, 0.3)  # E4 (lower pitch)
                time.sleep(0.2)

    def set_leds_alarm(self):
        """Set LEDs to red for alarm state"""
        if RAINBOW_HAT_AVAILABLE and not self.no_gpio:
            # All LEDs red
            for i in range(7):
                rainbowhat.rainbow.set_pixel(i, 255, 0, 0)
            rainbowhat.rainbow.show()

    def set_leds_crying(self):
        """Set LEDs to yellow for crying (not yet alarm)"""
        if RAINBOW_HAT_AVAILABLE and not self.no_gpio:
            for i in range(7):
                rainbowhat.rainbow.set_pixel(i, 255, 165, 0)
            rainbowhat.rainbow.show()

    def set_leds_ok(self):
        """Set LEDs to green for OK state"""
        if RAINBOW_HAT_AVAILABLE and not self.no_gpio:
            # Single green LED
            rainbowhat.rainbow.clear()
            rainbowhat.rainbow.set_pixel(3, 0, 255, 0)
            rainbowhat.rainbow.show()

    def set_leds_error(self):
        """Set LEDs to indicate connection error"""
        if RAINBOW_HAT_AVAILABLE and not self.no_gpio:
            # Blue flashing pattern
            rainbowhat.rainbow.clear()
            rainbowhat.rainbow.set_pixel(0, 0, 0, 255)
            rainbowhat.rainbow.set_pixel(6, 0, 0, 255)
            rainbowhat.rainbow.show()

    def set_leds_off(self):
        """Turn off all LEDs"""
        if RAINBOW_HAT_AVAILABLE and not self.no_gpio:
            rainbowhat.rainbow.clear()
            rainbowhat.rainbow.show()

    def buzzer_alarm(self):
        """Sound the buzzer for alarm - loud alternating tones"""
        if self.buzzer_available and not self.alarm_acknowledged:
            # Play urgent alarm pattern (3 cycles of alternating tones)
            for _ in range(3):
                self.play_tone(880, 0.2)  # A5 (higher pitch)
                time.sleep(0.05)
                self.play_tone(440, 0.2)  # A4
                time.sleep(0.05)

    def display_status(self, status):
        """Display status on Rainbow HAT display"""
        if RAINBOW_HAT_AVAILABLE and status and not self.no_gpio:
            try:
                if status.get('alarm'):
                    # Show "CRY" on display
                    rainbowhat.display.print_str("CRY!")
                elif status.get('crying'):
                    # Show duration
                    duration = int(status.get('episode_duration', 0))
                    rainbowhat.display.print_number_str(f"{duration:4d}")
                else:
                    # Show "OK" or time
                    rainbowhat.display.print_str(" OK ")
                rainbowhat.display.show()
            except OSError:
                # Display not working, ignore
                pass

    def run(self):
        """Main polling loop"""
        print(f"Baby Monitor Receiver started")
        print(f"Polling: {self.monitor_url}")
        print(f"Interval: {self.poll_interval} seconds")
        print(f"Rainbow HAT: {'Available' if RAINBOW_HAT_AVAILABLE else 'Simulation mode'}")
        print(f"Buzzer: {'Available' if self.buzzer_available else 'Not available'}")
        if self.touch_available:
            print(f"Touch any button (A/B/C) to acknowledge alarm")
        print(f"\n{YELLOW}Press Ctrl+C to stop{RESET}\n")

        # Set LED brightness (0.0 to 1.0) - skip if no_gpio
        if RAINBOW_HAT_AVAILABLE and not self.no_gpio:
            rainbowhat.rainbow.set_brightness(0.05)  # 5% brightness

        # Initial state - skip if no_gpio
        if not self.no_gpio:
            self.set_leds_ok()

        try:
            while True:
                status = self.fetch_status()
                timestamp = datetime.now().strftime("%H:%M:%S")

                if status is None:
                    # Connection error
                    ack_status = " (acknowledged)" if self.error_acknowledged else ""
                    print(f"[{timestamp}] {RED}✗ Connection error (attempt {self.connection_errors}){ack_status}{RESET}")
                    self.set_leds_error()
                    if self.connection_errors >= self.max_connection_errors:
                        # Show connection error on display
                        if not self.no_gpio:
                            try:
                                if RAINBOW_HAT_AVAILABLE:
                                    rainbowhat.display.print_str("ERR ")
                                    rainbowhat.display.show()
                            except OSError:
                                pass
                        # Sound buzzer until acknowledged
                        self.buzzer_error()
                else:
                    crying = status.get('crying', False)
                    alarm = status.get('alarm', False)
                    duration = status.get('episode_duration', 0)

                    if alarm:
                        ack_status = " (acknowledged)" if self.alarm_acknowledged else ""
                        print(f"[{timestamp}] {RED}🚨 ALARM! Baby crying for {duration:.0f}s{ack_status}{RESET}")
                        self.set_leds_alarm()
                        self.display_status(status)
                        # Sound buzzer every poll until acknowledged
                        self.buzzer_alarm()
                        self.last_alarm_state = True
                    elif crying:
                        print(f"[{timestamp}] {YELLOW}👶 Baby crying ({duration:.0f}s){RESET}")
                        self.set_leds_crying()
                        self.display_status(status)
                        self.last_alarm_state = False
                        self.alarm_acknowledged = False  # Reset acknowledgment
                    else:
                        print(f"[{timestamp}] {GREEN}✓ OK{RESET}")
                        self.set_leds_ok()
                        self.display_status(status)
                        self.last_alarm_state = False
                        self.alarm_acknowledged = False  # Reset acknowledgment

                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            print(f"\n\n{YELLOW}Stopping receiver...{RESET}")
        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up Rainbow HAT and buzzer"""
        self.set_leds_off()
        if RAINBOW_HAT_AVAILABLE and not self.no_gpio:
            try:
                rainbowhat.display.clear()
                rainbowhat.display.show()
            except OSError:
                pass
        if self.buzzer is not None:
            try:
                self.buzzer.stop()
                self.buzzer.close()
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Baby Monitor Receiver for Rainbow HAT')
    parser.add_argument('--url', type=str, default=DEFAULT_MONITOR_URL,
                        help=f'Baby monitor status URL (default: {DEFAULT_MONITOR_URL})')
    parser.add_argument('--interval', type=int, default=DEFAULT_POLL_INTERVAL,
                        help=f'Polling interval in seconds (default: {DEFAULT_POLL_INTERVAL})')
    parser.add_argument('--no-gpio', action='store_true',
                        help='Disable buzzer and touch buttons (for use with launcher)')
    args = parser.parse_args()

    receiver = BabyMonitorReceiver(args.url, args.interval, no_gpio=args.no_gpio)
    receiver.run()
