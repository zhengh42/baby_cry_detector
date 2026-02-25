#!/usr/bin/env python3
"""
Launcher script for Baby Monitor Receiver.
Touch button A to start/stop the receiver.
Runs as a background service.
"""
import subprocess
import time
import os
import signal
import sys
import urllib.request
import json

try:
    import rainbowhat
    RAINBOW_HAT_AVAILABLE = True
except ImportError:
    RAINBOW_HAT_AVAILABLE = False
    print("Error: rainbowhat not available")
    exit(1)

# Try to import gpiozero for buzzer
BUZZER_GPIO = 13
try:
    from gpiozero import TonalBuzzer
    from gpiozero.tones import Tone
    GPIOZERO_AVAILABLE = True
except ImportError:
    GPIOZERO_AVAILABLE = False

# Receiver settings
RECEIVER_DIR = "/home/tinybaby/baby_mobitor"
RECEIVER_URL = "http://192.168.68.62:8080/status"

# Track receiver process and state
receiver_process = None
alarm_acknowledged = False
error_acknowledged = False
connection_errors = 0
max_connection_errors = 5  # Buzzer after 5+ consecutive errors
buzzer = None

# Initialize buzzer
if GPIOZERO_AVAILABLE:
    try:
        buzzer = TonalBuzzer(BUZZER_GPIO)
    except Exception as e:
        print(f"Warning: Buzzer init failed ({e})")


def play_tone(frequency, duration):
    """Play a tone on the buzzer"""
    if buzzer is not None:
        try:
            buzzer.play(Tone(frequency))
            time.sleep(duration)
            buzzer.stop()
        except Exception:
            pass


def buzzer_alarm():
    """Sound alarm buzzer pattern - loud and urgent"""
    global alarm_acknowledged
    if buzzer is not None and not alarm_acknowledged:
        for _ in range(5):
            play_tone(880, 0.3)
            time.sleep(0.05)
            play_tone(440, 0.3)
            time.sleep(0.05)


def buzzer_error():
    """Sound error buzzer pattern"""
    global error_acknowledged
    if buzzer is not None and not error_acknowledged:
        for _ in range(2):
            play_tone(330, 0.3)
            time.sleep(0.2)


def set_leds_off():
    """Turn off LEDs - blue when stopped"""
    rainbowhat.rainbow.clear()
    rainbowhat.rainbow.set_brightness(0.05)
    rainbowhat.rainbow.set_pixel(3, 0, 0, 255)
    rainbowhat.rainbow.show()


def set_leds_ok():
    """Green for OK state"""
    rainbowhat.rainbow.clear()
    rainbowhat.rainbow.set_brightness(0.05)
    rainbowhat.rainbow.set_pixel(3, 0, 255, 0)
    rainbowhat.rainbow.show()


def set_leds_crying():
    """Orange/yellow for crying"""
    rainbowhat.rainbow.clear()
    rainbowhat.rainbow.set_brightness(0.05)
    for i in range(7):
        rainbowhat.rainbow.set_pixel(i, 255, 165, 0)
    rainbowhat.rainbow.show()


def set_leds_alarm():
    """Red for alarm"""
    rainbowhat.rainbow.clear()
    rainbowhat.rainbow.set_brightness(0.05)
    for i in range(7):
        rainbowhat.rainbow.set_pixel(i, 255, 0, 0)
    rainbowhat.rainbow.show()


def set_leds_error():
    """Blue on edges for connection error"""
    rainbowhat.rainbow.clear()
    rainbowhat.rainbow.set_brightness(0.05)
    rainbowhat.rainbow.set_pixel(0, 0, 0, 255)
    rainbowhat.rainbow.set_pixel(6, 0, 0, 255)
    rainbowhat.rainbow.show()


def fetch_status():
    """Fetch status from baby monitor"""
    global connection_errors
    try:
        response = urllib.request.urlopen(RECEIVER_URL, timeout=10)
        connection_errors = 0  # Reset on success
        return json.loads(response.read().decode())
    except Exception:
        connection_errors += 1
        return None


def set_status_led(running):
    """Set LED to show status: blue=ready, green=running"""
    if running:
        set_leds_ok()
    else:
        set_leds_off()


def start_receiver():
    """Start the receiver script"""
    global receiver_process
    if receiver_process is None or receiver_process.poll() is not None:
        print("Starting receiver...")
        receiver_process = subprocess.Popen(
            ["python3", "-u", "receiver.py", "--url", RECEIVER_URL, "--no-gpio"],
            cwd=RECEIVER_DIR,
            preexec_fn=os.setsid,
            stdout=sys.stdout,
            stderr=sys.stderr
        )
        set_status_led(True)
        # Confirmation beep (ascending)
        play_tone(440, 0.1)
        time.sleep(0.1)
        play_tone(880, 0.1)


def stop_receiver():
    """Stop the receiver script"""
    global receiver_process
    if receiver_process is not None and receiver_process.poll() is None:
        print("Stopping receiver...")
        # Kill the process group
        os.killpg(os.getpgid(receiver_process.pid), signal.SIGTERM)
        receiver_process.wait()
        receiver_process = None
        set_status_led(False)
        # Confirmation beep (descending)
        play_tone(880, 0.1)
        time.sleep(0.1)
        play_tone(440, 0.1)


def toggle_receiver():
    """Toggle receiver on/off"""
    global receiver_process
    if receiver_process is None or receiver_process.poll() is not None:
        start_receiver()
    else:
        stop_receiver()


# Set up button handlers
@rainbowhat.touch.A.press()
def touch_a(channel):
    toggle_receiver()


@rainbowhat.touch.B.press()
def touch_b(channel):
    acknowledge_alarm()


@rainbowhat.touch.C.press()
def touch_c(channel):
    acknowledge_alarm()


def acknowledge_alarm():
    """Acknowledge alarm - stops buzzer"""
    global receiver_process, alarm_acknowledged, error_acknowledged
    acknowledged = False

    if receiver_process is not None and receiver_process.poll() is None:
        os.kill(receiver_process.pid, signal.SIGUSR1)

    if not alarm_acknowledged:
        alarm_acknowledged = True
        acknowledged = True

    if not error_acknowledged:
        error_acknowledged = True
        acknowledged = True

    if acknowledged:
        play_tone(440, 0.1)  # Confirmation beep


def update_display(status):
    """Update the 7-segment display based on status"""
    try:
        if status is None:
            rainbowhat.display.print_str("ERR ")
        elif status.get('alarm'):
            rainbowhat.display.print_str("CRY!")
        elif status.get('crying'):
            duration = int(status.get('episode_duration', 0))
            rainbowhat.display.print_number_str(f"{duration:4d}")
        else:
            rainbowhat.display.print_str(" OK ")
        rainbowhat.display.show()
    except OSError:
        pass


def update_leds():
    """Fetch status and update LEDs, display, and buzzer accordingly"""
    global receiver_process, alarm_acknowledged, error_acknowledged
    if receiver_process is None or receiver_process.poll() is not None:
        set_leds_off()
        try:
            rainbowhat.display.clear()
            rainbowhat.display.show()
        except OSError:
            pass
        return

    status = fetch_status()
    if status is None:
        set_leds_error()
        if connection_errors >= max_connection_errors:
            buzzer_error()
    elif status.get('alarm'):
        set_leds_alarm()
        buzzer_alarm()
    elif status.get('crying'):
        set_leds_crying()
        # Reset alarm acknowledgment when crying but not alarm yet
        alarm_acknowledged = False
    else:
        set_leds_ok()
        # Reset acknowledgments when OK
        alarm_acknowledged = False
        error_acknowledged = False

    update_display(status)


def main():
    global receiver_process
    print("Baby Monitor Launcher started")
    print("Touch A to start/stop receiver")
    print("Touch B or C to acknowledge alarm")
    print("Press Ctrl+C to exit launcher")

    set_leds_off()

    try:
        while True:
            # Check if receiver died unexpectedly
            if receiver_process is not None and receiver_process.poll() is not None:
                print("Receiver stopped unexpectedly")
                receiver_process = None
                set_leds_off()

            # Update LEDs based on current status
            if receiver_process is not None:
                update_leds()

            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping launcher...")
    finally:
        stop_receiver()
        rainbowhat.rainbow.clear()
        rainbowhat.rainbow.show()
        if buzzer is not None:
            try:
                buzzer.stop()
                buzzer.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
