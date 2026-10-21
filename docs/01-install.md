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
| File list folder | `python3 server.py --dir ~/gcode` (or edit `DEFAULT_DIR` in `server.py`) | the CarveMaker share, else your home folder |

`settings.json` holds the jog, scan and level settings, the probe and pendant ports, and the last 5 zeros.
It is not in git, so every machine keeps its own.

## Update

```bash
cd ~/fluidsender
git pull
```

Then **Disconnect** and close the window. The next launch picks up the new code.
