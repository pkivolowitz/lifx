# Quick Start

```bash
# 1. Find your LIFX devices
python3 glowup.py discover

# 2. See what effects are available
python3 glowup.py effects

# 3. Run an effect (replace IP with your device's IP)
python3 glowup.py play cylon --ip <device-ip>

# 4. Or animate a group of bulbs as a virtual multizone
python3 glowup.py play cylon --config schedule.json --group office

# 5. Press Ctrl+C to stop (fades to black gracefully)
```

