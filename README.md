# FluidNCSender (FluidSender)

A lean, fast G-code sender made **only for FluidNC** CNC controllers. It was written to replace
Universal G-code Sender (UGS), which is slow to start and slow to draw on small PCs.

It runs as a small Python server (standard library only, no `pip install`) with a browser UI in its own
Chrome app window. It is **inch-first** everywhere.

## Features

- **Dockable multi-panel layout** (UGS-like): Files, G-code Editor, Visualizer/Toolpath, Machine, Jog,
  Scan and Console. Drag panels around, float or maximize them (F11), and reset them from **View → Reset Layout**.
- **Dark, Medium and Light themes** (View → Theme).
- **G-code editor** synced with the 3D toolpath view. Save keeps the file's CRLF line endings.
- **Visualizer:** Top / Iso / Front views; **Toolpath**, **Rapids** and **Scan** checkboxes; a Z stretch;
  and a **camera button** that saves the view as a PNG.
- **Machine panel:** inch DRO, state badge, Hold / Resume / Soft Reset, Home ($H, $HX/$HY/$HZ), Unlock ($X),
  Zero per axis or all, **Undo Zero** (the last 5 zeros are kept), Go to XY 0 / Z 0, and feed / rapid / spindle overrides
  (amber when not 100 %).
- **"2nd chance" buttons:** Zero and Go-to buttons need a second click 0.5–3 s after the first.
  Double-clicks and keyboard presses never count, so a bumped button does nothing.
- **Jog panel:** 3×3 XY pad with diagonals, Z+/Z−, Stop, and steps of 10 / 1 / 0.1 / 0.01 / 0.001 in.
  Click for one step, or hold to jog continuously. A speed slider is capped at FluidNC's own max rate.
- **Vacuum (M8) and AUX (M7) toggles** for solid-state relays. The program's own M7/M8/M9 set their state.
- **Job streaming** with character counting (128-byte buffer). Every job starts with `G20 G90`. It pauses on error,
  and **SAFE STOP** holds, waits for Hold:0, then resets.
- **Surface scan** with a Z probe ("the nano"), in UGS's zigzag order and `.xyz` format.
- **Auto-level** that matches UGS output byte for byte, with arcs split into short segments. One **Apply** box
  (blinks red while on). Saved leveled files are marked in their header and are **never leveled twice**.
- **Bluetooth jog pendant** support (NANO_jogbox, UGS plugin protocol).
- **Safe serial handling:** ports are opened exclusively and never opened by themselves. A port UGS holds is refused.
  The port is released 10 s after the last window closes, and only when the machine is at rest.

## Requirements

| What | Notes |
|---|---|
| Linux | Built and used on Ubuntu 24.04 |
| Python 3.8+ | Standard library only |
| Google Chrome | The UI runs in a `--app` window (set `FLUIDSENDER_BROWSER` to use Chromium) |
| `curl`, `ss` (iproute2) | Used by the launcher |
| FluidNC controller | Tested with FluidNC **4.1.1** (and 3.9.4) over USB at 115200 baud |
| Optional: probe | Arduino surface probe at 115200 (e.g. `/dev/ttyACM0`) |
| Optional: pendant | Bluetooth serial pendant on `/dev/rfcomm*` at 9600 |

## Quick start

```bash
git clone git@github.com:mikeoverbay/FluidNCSender.git ~/fluidsender
cd ~/fluidsender
./setup.sh            # checks requirements, serial access, and adds a desktop icon
./fluidsender.sh      # or double-click the "FluidSender" desktop icon
```

Then **Machine → Connect…**, pick your FluidNC port (usually `/dev/ttyUSB0`), and you're running.

## How-to guides

| Guide | |
|---|---|
| [Install and set up](docs/01-install.md) | setup.sh, serial permissions, desktop icon, options |
| [Connect to FluidNC](docs/02-connect.md) | ports, what is sent at connect, disconnecting |
| [Jog, home and zero](docs/03-jog-and-zero.md) | jog panel, 2nd-chance zeroing, Undo Zero, Go to zero |
| [Run a job](docs/04-run-a-job.md) | open, check, run, hold, stop, overrides, Vac/AUX |
| [Surface scan and auto-level](docs/05-scan-and-level.md) | probe, scan settings, level, save leveled G-code |
| [Pendant](docs/06-pendant.md) | Bluetooth pairing, rfcomm, connecting |
| [Troubleshooting](docs/07-troubleshooting.md) | port busy, alarms, stuck states, logs |
| [Development and testing](docs/08-development.md) | layout of the code, simulators, test suite |

## Project layout

```
fluidsender.sh    launcher: starts the server if needed and opens the Chrome app window
setup.sh          one-time setup / requirements check
server.py         HTTP server + JSON API (port 8765)
machine.py        FluidNC link: serial, streaming, status, safety rules
job.py            job streaming
settings.py       settings.json (jog, scan, level, ports, zero history)
probe.py scan.py  surface probe and scan
leveling.py leveler.py   auto-level (UGS-compatible)
pendant.py        Bluetooth pendant
static/           the browser UI (vendored three.js, CodeMirror, dockview)
tools/            simulators: fake_fluidnc.py, fake_probe.py, fake_pendant.py
tests/            unit and end-to-end tests (run on the simulators)
docs/             how-to guides
```

## Safety

This software moves a real machine. Keep a hand near the **E-stop**. FluidSender's Stop, Hold and Soft Reset are
**not** a substitute for a hardware emergency stop.

## Author

[@mikeoverbay](https://github.com/mikeoverbay)
