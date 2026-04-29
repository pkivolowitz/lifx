# Flashing nRF52840 Dongle (PCA10059) with OpenThread RCP

## Prerequisites

nrfutil v7+ installed on the NUC (notapi.local):

```
ssh mortimer.snerd@notapi.local
curl -fsSL -o /tmp/nrfutil 'https://files.nordicsemi.com/artifactory/swtools/external/nrfutil/executables/x86_64-unknown-linux-gnu/nrfutil'
chmod +x /tmp/nrfutil
/tmp/nrfutil install nrf5sdk-tools
```

Pre-built RCP firmware at `tools/firmware/ot-rcp-USB.hex` in this repo.

## Steps

- Plug dongle into NUC (notapi.local) USB
- Press the reset button on the side of the dongle (push inward toward USB connector)
- Red LED starts pulse/fade — dongle is in DFU bootloader mode
- Verify it appears:

```
ssh mortimer.snerd@notapi.local "ls /dev/ttyACM*"
```

- Copy firmware if not already there:

```
scp ~/glowup/tools/firmware/ot-rcp-USB.hex mortimer.snerd@notapi.local:/tmp/
```

- Generate DFU package:

```
ssh mortimer.snerd@notapi.local "/tmp/nrfutil nrf5sdk-tools pkg generate --hw-version 52 --sd-req=0x00 --application /tmp/ot-rcp-USB.hex --application-version 1 /tmp/ot-rcp.zip"
```

- Flash:

```
ssh mortimer.snerd@notapi.local "/tmp/nrfutil nrf5sdk-tools dfu usb-serial -pkg /tmp/ot-rcp.zip -p /dev/ttyACM0"
```

- Output should say: `Device programmed.`
- Move dongle to its target machine (broker-2, clock Pi, etc.)

## Why the NUC

nrfutil v7 subcommands (nrf5sdk-tools) are not available for Linux ARM64.
The NUC is x86_64 — full subcommand support. Flash on NUC, deploy to Pi.

## Notes

- The PCA10059 factory bootloader uses Nordic's USB DFU protocol
- nrfutil 5.2.0 (pip) is broken on Python 3.10+ — do not use
- adafruit-nrfutil speaks a different DFU dialect — does not work with PCA10059
- The dongle stays in DFU mode across unplug/replug until successfully flashed
- After flashing, the dongle appears as `/dev/ttyACM0` on the target machine
- The firmware is OpenThread RCP — the dongle becomes a Thread radio only (no BLE)
