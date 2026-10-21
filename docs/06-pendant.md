# 6. Bluetooth pendant

FluidSender drives the NANO_jogbox pendant (an ESP32 with a 28-key pad and a small screen) with the same protocol
as the UGS `serialControl` plugin, so the same pendant works with both senders.

## Pair it once (Blueman)

1. Turn the pendant on.
2. In **Blueman** (Bluetooth Manager), pair it and connect it as a **Serial Port**.
   This creates `/dev/rfcomm0` (or `rfcomm1`).
3. Blueman keeps the port open to hold the link. FluidSender allows that and never reads its handle.

## Connect

1. Connect to FluidNC first ([guide 2](02-connect.md)).
2. **Machine → Connect Pendant…**, pick `/dev/rfcomm0`, then **Connect** (9600 baud).
3. The pendant's screen shows the position, status, jog speed and step within a second or two.

The port you used is remembered in `settings.json` (`pendantPort`).

## What it does

- Jogs with the pendant's step and speed, and runs, pauses and stops jobs.
- Zero and go-to keys follow the same safety rules as the page:
  - **Go-to Z / XY keys are refused with work Z < 0.2 in.**
  - **$H from the pendant works only in ALARM.**
- Pause/Stop on the pendant also work for jobs started from the page (needs the fixed firmware, which sends `FW:2`).

## Firmware

The fixed firmware is in `~/Documents/Arduino/NANO_jogbox/NANO_jogbox.ino`, and `NANO_jogbox-CHANGES.txt` lists
the changes. Flash it with the Arduino IDE. It still works with UGS.

## Disconnect

**Machine → Disconnect Pendant**. It is also released when the last FluidSender window closes and the machine is
at rest.
