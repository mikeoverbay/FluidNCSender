# 8. Development and testing

## Code layout

| File | Role |
|---|---|
| `server.py` | `http.server` HTTP server: static files and the `/api/...` JSON endpoints |
| `machine.py` | the FluidNC link: termios serial (exclusive), line flow, status polling, realtime bytes, safety rules |
| `job.py` | job streaming (character counting, 128 bytes, the 127-character line limit) |
| `settings.py` | `settings.json`, written atomically |
| `probe.py`, `scan.py` | probe protocol and zigzag scan (`.xyz` output) |
| `leveling.py`, `leveler.py` | auto-level (UGS-identical output) |
| `pendant.py` | Bluetooth pendant protocol (CONREQ/CONACK, REQ → 44-byte SYNC, KEY:n) |
| `static/` | the page: plain ES modules, no build step |

The API groups are `/api/machine/…` (ports, state, events, program and the commands), `/api/probe/…`,
`/api/pendant/…`, `/api/scan/…`, plus `/api/list`, `/api/file` and `/api/save` for files.
`events` is an NDJSON stream.

## Simulators

Three simulators run on pseudo-terminals, so you can work without the machine:

```bash
python3 tools/fake_fluidnc.py              # prints its port, e.g. /dev/pts/7
FLUIDSENDER_EXTRA_PORTS=/dev/pts/7 FLUIDSENDER_ONLY_EXTRA_PORTS=1 python3 server.py --port 8790
```

Then open `http://localhost:8790`. A second port keeps it apart from the real one on 8765.

Useful `fake_fluidnc.py` options:

| Option | |
|---|---|
| `--fw 3.9.4` | act as FluidNC 3.9.4 (4.1.1 by default) |
| `--alarm` | boot in alarm |
| `--soft-limits` | turn on soft limits |
| `--inches` | report in inches |
| `--stall-after N` | stop answering after N lines |
| `--spinup S` | spindle spin-up delay in seconds |
| `--log FILE` | log what it receives |

There are also `tools/fake_probe.py` (`--values`, `--overtravel-at N`) and `tools/fake_pendant.py`
(`--firmware original|fixed`).

## Tests

```bash
python3 -m unittest discover -s tests          # everything, a few minutes
python3 -m unittest tests.test_leveler         # one module
```

The tests run only on the simulators and pseudo-terminals, and never open a real serial port.
Run the full suite before you push.

## Rules the code keeps

- Never open a serial port without the user clicking Connect.
- Inches first.
- Warn, don't block, for the user's normal workflow (for example, moving the table after a scan).
  Refuse only what is unsafe.
- Python standard library only, and render the page only on demand: it runs on a 2-core all-in-one PC.
