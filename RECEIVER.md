# Receiver Setup (Rainbow HAT) — Optional

A single Raspberry Pi with Pushover notifications is sufficient for most setups. The receiver scripts are optional and provide a secondary alert device using a second Raspberry Pi with a Rainbow HAT.

## Scripts

### `receiver.py` (Rainbow HAT Receiver)

Polls the baby monitor for status and displays it on the Rainbow HAT. Features:
- Rainbow LED indicators (green=OK, yellow=crying, red=alarm)
- Buzzer alarm when alert triggers
- Touch buttons to acknowledge alarm (silences buzzer)
- 4-digit display shows status/duration
- Works without Rainbow HAT in simulation mode

### `receiver_launcher.py` (Receiver Launcher)

Systemd-friendly launcher for receiver.py. Features:
- Touch button A to start/stop receiver
- Touch buttons B/C to acknowledge alarm
- Manages receiver process lifecycle
- Handles GPIO and buzzer independently from receiver

## Installation

```bash
pip install rainbowhat
```

## Usage

```bash
# Basic usage (default URL: http://babymonitor.local:8080/status)
python receiver.py

# Custom baby monitor URL
python receiver.py --url http://192.168.1.100:8080/status

# Custom polling interval (30 seconds)
python receiver.py --interval 30
```

**Options:**
- `--url` - Baby monitor status URL (default: http://babymonitor.local:8080/status)
- `--interval` - Polling interval in seconds (default: 10)

## LED Indicators

- **Green** (center LED): Baby is OK, no crying detected
- **Yellow** (all LEDs): Baby is crying, but alert not yet triggered
- **Red** (all LEDs): ALARM - baby has been crying past the alert threshold
- **Blue** (edge LEDs): Connection error to baby monitor

## Display

- `OK`: Baby is calm
- `1234`: Duration of current crying episode in seconds
- `CRY!`: Alarm state
- `ERR`: Connection error
