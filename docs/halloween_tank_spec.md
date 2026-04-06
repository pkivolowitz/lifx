# Halloween Autonomous Tank — Day of the Dead

> Día de los Muertos — Bill of Materials & System Specification
>
> Revision 2 — 2026-03-31

---

## Project Overview

An autonomous tracked tank platform decorated in the Day of the Dead
(Día de los Muertos) theme, designed to patrol a 10x12 foot paver yard
area during Halloween.  The tank wanders arcs and pivots within a
bounded quadrilateral, its styrofoam head slowly panning side to side,
eye sockets glowing in marigold and purple.  Remote control is via iOS
app over WiFi.

The patrol area is flat pavers with no obstacles, no foot traffic, and
clear line of sight to all four corner beacons.  Maximum height is 3
feet.  Target runtime is 5+ hours on a single battery charge.

---

## Chassis

XiaoR GEEK tank chassis — upgraded version.  Aluminum alloy frame,
engineering plastic treads, 2x DC gear motors at 12V.  Purpose-built
for paver surfaces.  No electronics included in kit.  Chassis weight
approximately 4 lbs.

---

## Bill of Materials — Tank Electronics

| Item | Purpose | Qty | Est. Cost |
|------|---------|-----|-----------|
| ESP32 DevKit (38-pin) | Main controller — WiFi + BLE | 1 | $8 |
| BTS7960 43A Motor Driver | Left motor H-bridge | 1 | $5 |
| BTS7960 43A Motor Driver | Right motor H-bridge | 1 | $5 |
| WS2812B Jewel Board (7-LED) | Eye lighting (one per eye socket) | 2 | $4 |
| SG90 Micro Servo | Head pan ±30° | 1 | $3 |
| DC-DC Buck Converter 12V→5V 3A | Power ESP32, LEDs, servo from 12V rail | 1 | $6 |
| DC Barrel Jack (female) | Connect to 89Wh battery pack DC output | 1 | $2 |
| Resistor Divider (100K + 33K) | Battery voltage monitoring via ESP32 ADC | 1 | $1 |
| Prototype PCB (5cm x 7cm) | Mount and wire electronics | 1 | $3 |
| Dupont Wire Assortment | Signal and power connections | 1 | $5 |
| Small Project Enclosure | House electronics on chassis | 1 | $6 |
| M3 Standoffs + Screws Kit | Mount enclosure to chassis mounting holes | 1 | $4 |
| Heat Shrink Assortment | Wire protection and strain relief | 1 | $4 |

**Tank Electronics Subtotal:** $56

---

## Bill of Materials — Corner Beacons (x4)

Four ESP32-based BLE beacons placed at the corners of the patrol
quadrilateral.  The tank reads RSSI from all four simultaneously.
Relative RSSI ratios between beacons provide 2D boundary awareness
without ground-level sensors or external wiring.

| Item | Purpose | Qty | Est. Cost |
|------|---------|-----|-----------|
| ESP32 DevKit (38-pin) | BLE advertiser — one per corner | 4 | $32 |
| 18650 LiPo Cell (3000mAh) | Beacon power — 200+ hour runtime | 4 | $12 |
| 18650 Cell Holder | Single cell, with leads | 4 | $6 |
| Small Project Enclosure | Weather and dew protection | 4 | $12 |
| Power Toggle Switch | On/off per beacon | 4 | $4 |

**Beacon Subtotal:** $66

---

## Bill of Materials — Miscellaneous

| Item | Purpose | Qty | Est. Cost |
|------|---------|-----|-----------|
| USB-C to DC Barrel Jack Cable (12V PD) | Battery pack to tank input | 1 | $8 |
| Marigold Orange Spray Paint | Day of the Dead tank dress | 1 | $6 |
| Hot Glue Sticks | Eye socket diffuser material | 1 | $3 |

**Miscellaneous Subtotal:** $17

---

**Estimated Total (electronics only, excluding chassis): ~$139**

---

## Power Specification

Power source: existing 89 Wh battery pack with 12V-16.8V / 10A DC
output.  Direct barrel jack connection to tank.  No USB conversion
required.

| Parameter | Value |
|-----------|-------|
| Battery Pack | 89 Wh, 6 Ah |
| Battery DC Output | 12V-16.8V, 10A |
| Tank Input Voltage | 12V nominal |
| Motor Driver (each) | BTS7960 — 43A peak, 12V motor supply |
| Motor Rated Current | 2A per motor running, 4.5A stall |
| Total Motor Draw | ~4A running (both motors), 9A stall peak |
| Buck Converter Output | 5V / 3A — powers ESP32, servo, LEDs |
| ESP32 Draw | ~250mA peak (WiFi active) |
| WS2812B Jewels (2x) | ~120mA max at full white (run at partial) |
| SG90 Servo | ~200mA stall, ~50mA idle |
| Est. Full-Load Draw | ~4.5A at 12V = 54W |
| Est. Patrol Draw | ~15-25W (30-50% motor duty cycle with pauses) |
| Estimated Runtime | 5+ hours at patrol duty cycle |
| Low-Battery Threshold | 11.5V — return to center and stop |
| Beacon Runtime (each) | ESP32 BLE at fixed low TX: ~15mA avg; 3000mAh = 200+ hours |

**NOTE:** The BTS7960 motor driver is required over the TB6612FNG.  The
XiaoR motors draw 2A running and 4.5A stall per channel — well beyond
the TB6612FNG's 1.2A continuous rating.  The BTS7960 handles 43A peak
with thermal protection.

---

## Controller & Communications Specification

| Parameter | Value |
|-----------|-------|
| Tank Controller | ESP32 DevKit — Arduino IDE, WiFi 802.11 b/g/n, BLE 4.2 |
| Motor Control | PWM via BTS7960 — independent left/right for arcs and pivots |
| Positioning | BLE RSSI from 4 corner beacons — rolling average filtered |
| Boundary Logic | Relative RSSI ratios between beacons — not absolute thresholds |
| Boundary Response | Gradual arc away from strong beacon — not hard reverse |
| Head Pan | SG90 servo — randomized ±30° sweep, independent timing |
| Eye Lighting | WS2812B Jewel x2 — NeoPixel protocol, Day of the Dead palette |
| Eye Diffuser | Hot-glue diffuser behind each eye socket for even glow |
| Beacon Hardware | ESP32 x4 — BLE advertising only, no scan response |
| Beacon TX Power | Fixed low level (-12 dBm) for voltage stability across discharge |
| Beacon Power | 18650 LiPo, single cell per beacon |
| iOS App Comms | WiFi — HTTP or WebSocket to tank ESP32 |
| iOS App Controls | Lights on/off, Motion on/off, Kill (immediate motor stop) |
| Battery Monitor | ESP32 ADC via resistor divider — auto-stop at 11.5V |
| EMI Mitigation | ESP32 antenna oriented away from motor drivers, upward |

---

## Patrol Behavior Specification

| Parameter | Value |
|-----------|-------|
| Patrol Area | 10 x 12 feet — paver surface, no grade change, no obstacles |
| Movement Style | Arcs, slow pivots, lazy wanders — not linear back-and-forth |
| Speed | Low — creep speed via PWM duty cycle; tunable constant |
| Motor Duty Cycle | 30-50% active, 50-70% dwelling — maximizes creep factor and battery life |
| Pause Behavior | Randomized dwell at positions — duration tunable |
| Head Pan Timing | Randomized independent of motion — eerie look-around effect |
| Boundary Detection | Relative RSSI ratios — strongest beacon indicates nearest edge |
| Boundary Response | Slow arc away from boundary — gradual, not abrupt |
| Low-Battery Behavior | Return to center, stop motors, eyes remain on |
| Kill Switch | Immediate PWM zero both motors — iOS app button |
| Motion Pause | Halt patrol loop — lights and head continue |
| Light Control | Toggle eye LEDs on/off — independent of motion state |

---

## Aesthetics — Day of the Dead

**Theme:** Día de los Muertos — vibrant, celebratory, not horror.
Traditional palette: marigold orange, deep purple, hot pink, white,
black.

### Head Assembly

- Styrofoam head on vertical stick, mounted to tank chassis
- Day of the Dead skull mask (Rubies) secured with rubber band
- Draped in black fabric — roughly 12" clearance from ground
- Total height under 3 feet
- WS2812B Jewel boards (7 LEDs each) mounted behind eye sockets
- Hot-glue diffuser behind each eye hole for even, soft glow
- LED color cycling: marigold orange → hot pink → deep purple — slow crossfade

### Tank Body

- Marigold orange spray paint base coat
- Electronics enclosure mounted to chassis top plate using M3 standoffs
- Wiring routed internally where possible; heat shrink on all exposed runs
- Battery pack secured to chassis with velcro straps

---

## Software Deliverables

### ESP32 Firmware — Tank

- WiFi AP or STA mode — accepts iOS app connections
- BLE scanner — continuous RSSI read from 4 beacon MACs
- RSSI filtering: rolling average (window size tunable) per beacon
- Boundary logic: relative RSSI ratios between beacons, not absolute thresholds
- Motor control state machine: wander, arc, pivot, pause, stop
- Servo PWM: randomized pan sweeps ±30°, independent timing
- NeoPixel driver: Day of the Dead palette, slow crossfade cycle
- HTTP or WebSocket server: lights endpoint, motion endpoint, kill endpoint
- Battery voltage monitor: ADC read via resistor divider, auto-stop at threshold
- All tunable constants in a clearly marked CONSTANTS section
- ESP32 antenna oriented away from motor drivers (upward/outward)

### ESP32 Firmware — Beacons (x4)

- BLE advertising only — fixed MAC, fixed UUID, no scan response
- TX power set to fixed low level (-12 dBm) for voltage stability
- Deep sleep between advertisements for power efficiency
- Identical firmware, differentiated by flashed beacon ID constant

### iOS App

- SwiftUI — three buttons: Lights On/Off, Motion On/Off, Kill
- WiFi connection to tank ESP32
- Kill button: prominent, red, immediate — no confirmation dialog
- Connection status indicator — shows when tank is unreachable

---

## Implementation Notes

- RSSI boundary detection uses rolling average (window tunable) to suppress noise
- Use relative RSSI ratios between beacons, not absolute thresholds — ratios are stable across battery voltage changes and atmospheric conditions
- Boundary response: gradual arc away from boundary — not hard reverse
- All speed, timing, and RSSI threshold values are named constants — no magic numbers
- Patrol motion is intentionally slow — creep speed maximizes creep factor and battery life
- Beacons should be deployed and powered 10+ minutes before tank power-on to stabilize RSSI baseline
- Physical beacon placement: corner stakes or weighted enclosures
- ESP32 antenna must face away from BTS7960 motor drivers to minimize EMI interference — mount ESP32 at opposite end of chassis from motor drivers, antenna end upward
- Motor power wires should be twisted-pair to reduce radiated EMI
- Hot-glue diffuser fills the eye socket behind the mask — provides even glow without visible LED dots
- Low-battery auto-stop prevents motor brownout from crashing the ESP32 mid-patrol

---

## Pin Assignment (Reference)

| ESP32 Pin | Function | Notes |
|-----------|----------|-------|
| GPIO 25 | Left Motor RPWM | BTS7960 forward |
| GPIO 26 | Left Motor LPWM | BTS7960 reverse |
| GPIO 27 | Right Motor RPWM | BTS7960 forward |
| GPIO 14 | Right Motor LPWM | BTS7960 reverse |
| GPIO 13 | Servo PWM | SG90 head pan |
| GPIO 18 | NeoPixel Data | WS2812B Jewels (daisy-chained, 14 LEDs total) |
| GPIO 34 | Battery ADC | Via 100K/33K resistor divider (input only pin) |
| GPIO 32 | Left Motor Enable | BTS7960 R_EN + L_EN tied |
| GPIO 33 | Right Motor Enable | BTS7960 R_EN + L_EN tied |

**Note:** Pin assignments are reference suggestions.  Final assignment
depends on PCB layout and wire routing on the specific chassis.  Avoid
pins used by ESP32 internal flash (GPIO 6-11) and boot strapping
(GPIO 0, 2, 15 at startup).

---

## Project Timeline

| Milestone | Target |
|-----------|--------|
| BOM ordered | April |
| Chassis assembled, motors verified | May |
| Tank firmware: motor control + WiFi | June |
| Beacon firmware: BLE advertising | June |
| RSSI boundary calibration | July |
| iOS app: three-button control | July |
| Head assembly + eye lighting | August |
| Patrol behavior tuning | September |
| Full dress rehearsal | October 1 |
| Deployment | October 31 |
