# 1. Install and set up

## Get the code

```bash
git clone git@github.com:mikeoverbay/FluidNCSender.git ~/fluidsender
cd ~/fluidsender
```

Nothing has to be compiled and there is no `pip install`: FluidSender uses only the Python standard library.
The browser libraries (three.js, CodeMirror, dockview) are already in `static/vendor/`.

## Run setup

```bash
./setup.sh
```

It checks for Python 3.8+, Google Chrome, `curl` and `ss`. It checks that you can open serial ports
(the `dialout` group) and lists the serial devices it sees. Then it adds a **FluidSender** icon to
your app menu, and to the desktop if there isn't one there already.

It never opens a serial port and never runs `sudo`. If something needs root, it prints the command for you to run.

| Option | |
|---|---|
| `--test` | also run the full test suite on the simulators (no hardware) |
| `--no-icon` | don't create the desktop / menu icons |

### Serial permission

If setup warns that you're not in `dialout`:

```bash
sudo usermod -aG dialout $USER
```

Then log out and back in.

### brltty

On Ubuntu, the `brltty` braille service can grab CH340 USB-serial adapters, so `/dev/ttyUSB0` appears and then
disappears. If setup warns about it and you don't use a braille display, remove it:

```bash
sudo apt remove brltty
```

## Start it

Double-click the **FluidSender** icon, or run:

```bash
~/fluidsender/fluidsender.sh
```

The launcher starts the server on port 8765 if it isn't already running and opens a Chrome app window.
If a running server is older than the files and is **not connected**, the launcher restarts it on the new code.
A connected server is never restarted.

## Options

| Setting | How | Default |
|---|---|---|
| Server port | `FLUIDSENDER_PORT=8790 ./fluidsender.sh` | 8765 |
| Browser | `FLUIDSENDER_BROWSER=chromium ./fluidsender.sh` | google-chrome |
| Settings file | `FLUIDSENDER_SETTINGS=/path/settings.json` | `settings.json` next to `server.py` |
| File list folder | `python3 server.py --dir ~/gcode` (or edit `DEFAULT_DIR` in `hostos.py`) | the CarveMaker share, else your home folder (Windows: Documents) |

`settings.json` holds the jog, scan and level settings, the probe and pendant ports, and the last 5 zeros.
It is not in git, so every machine keeps its own.

## Running on Windows

Linux Mint is the recommended platform. Windows 10 and 11 work too:

1. Install **Python 3.12** (or newer) from [python.org](https://www.python.org/downloads/). In the installer, tick
   **Add python.exe to PATH**. (No `pip install`: FluidSender uses only the standard library.)
2. Get the code: on [GitHub](https://github.com/mikeoverbay/FluidNCSender) press **Code → Download ZIP** and unzip
   it somewhere (Documents is fine), or `git clone https://github.com/mikeoverbay/FluidNCSender.git`.
3. Double-click **`FluidSender.bat`** in that folder.

`FluidSender.bat` finds Python (the `py` launcher, else `python`), starts `server.py` in a minimized window called
*FluidSender server* if it isn't already running, and opens `http://localhost:8765/` as an app window in Chrome, else
Edge, else your default browser. Close the *FluidSender server* window to stop FluidSender (after **Disconnect**).
`setup.sh` is Linux only; Windows needs no setup and no `dialout` group (a CH340 controller may need WCH's driver if
Windows shows no COM port for it).

On Windows:
- FluidNC is a `COM` port in **Machine → Connect…** (`COM3`, …; Device Manager → Ports shows which).
- The file list starts in your **Documents** folder (`py server.py --dir C:\path` for another).
- A port another program has open (UGS, the Arduino IDE) can't be marked in the list: connecting to it is refused
  with that message.
- `FLUIDSENDER_PORT` sets the server port as on Linux.

## Try it without a machine: Demo machine

**Machine → Connect…** lists a **Demo machine** last. It is a simulated FluidNC 4.1.1 that runs inside FluidSender
(on Linux and Windows), so you can try the whole interface with no controller plugged in. Nothing real moves.

- It starts in **ALARM** (unhomed), like a machine with homing: press **Home** (or Unlock).
- Jog, Zero, Go to zero and the overrides work; soft limits are on (travel X 31.5, Y 23.6, Z 3.9 in).
- `M3 S12000` shows the spindle speed after a 1 s spin-up; the Vacuum and AUX switches work.
- **Run** streams a loaded G-code file, at real speed.

The status bar says *Connected: Demo machine (simulated: nothing real moves)*. The Demo machine is never chosen or
connected by itself, and **Disconnect** stops it.

## Update

```bash
cd ~/fluidsender
git pull
```

Then **Disconnect** and close the window. The next launch picks up the new code. (Windows: download the ZIP again
or pull; close the *FluidSender server* window, then double-click `FluidSender.bat`.)
