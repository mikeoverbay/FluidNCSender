#!/bin/bash
# One-time setup for FluidSender: checks what it needs, checks serial-port access,
# and adds a desktop / app-menu icon. Safe to run again. It never opens a serial port
# and never uses sudo itself: anything that needs root is printed for you to run.
#
#   ./setup.sh             check and install the icon
#   ./setup.sh --test      also run the test suite (a few minutes, simulators only)
#   ./setup.sh --no-icon   skip the desktop icon
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_TESTS=0; ICON=1
for a in "$@"; do
    case "$a" in
        --test) RUN_TESTS=1 ;;
        --no-icon) ICON=0 ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $a (try --help)"; exit 2 ;;
    esac
done

ok()   { printf '  \e[32mOK\e[0m    %s\n' "$*"; }
warn() { printf '  \e[33mWARN\e[0m  %s\n' "$*"; WARNED=1; }
bad()  { printf '  \e[31mMISSING\e[0m %s\n' "$*"; FAILED=1; }
WARNED=0; FAILED=0

echo "FluidSender setup in $DIR"
echo
echo "Requirements:"
if command -v python3 >/dev/null; then
    if python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))'; then
        ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"
    else
        bad "python3 3.8 or newer (found $(python3 --version 2>&1))"
    fi
else
    bad "python3   ->  sudo apt install python3"
fi

BROWSER="${FLUIDSENDER_BROWSER:-google-chrome}"
if command -v "$BROWSER" >/dev/null; then
    ok "browser: $BROWSER"
elif command -v chromium >/dev/null || command -v chromium-browser >/dev/null; then
    alt=$(command -v chromium || command -v chromium-browser)
    warn "google-chrome not found, but $alt is. Start with:  FLUIDSENDER_BROWSER=$alt ./fluidsender.sh"
else
    bad "Google Chrome  ->  https://www.google.com/chrome/  (or Chromium, with FLUIDSENDER_BROWSER)"
fi

for c in curl ss; do
    if command -v "$c" >/dev/null; then ok "$c"
    else bad "$c   ->  sudo apt install $([ "$c" = ss ] && echo iproute2 || echo "$c")"; fi
done

echo
echo "Serial ports:"
if id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
    ok "$USER is in the 'dialout' group"
else
    warn "$USER is not in 'dialout', so /dev/ttyUSB* and /dev/ttyACM* can't be opened. Run:"
    echo "          sudo usermod -aG dialout $USER     (then log out and back in)"
fi
found=0
for p in /dev/ttyUSB* /dev/ttyACM* /dev/rfcomm*; do
    [ -e "$p" ] || continue
    found=1
    if [ -r "$p" ] && [ -w "$p" ]; then ok "$p (readable and writable)"; else warn "$p exists but you can't open it"; fi
done
[ $found = 1 ] || echo "  (no serial devices plugged in right now; that's fine)"
if systemctl is-active --quiet brltty 2>/dev/null; then
    warn "brltty is running; it can grab USB-serial adapters (CH340). If /dev/ttyUSB0 vanishes:  sudo apt remove brltty"
fi

echo
echo "Files:"
chmod +x "$DIR/fluidsender.sh" "$DIR/setup.sh" "$DIR"/tools/*.py 2>/dev/null
ok "scripts are executable"
if [ -f "$DIR/settings.json" ]; then
    ok "settings.json exists (kept as is)"
else
    ok "settings.json will be created with the defaults the first time you change a setting"
fi

if [ $ICON = 1 ]; then
    entry="[Desktop Entry]
Version=1.0
Type=Application
Name=FluidSender
Comment=G-code sender for FluidNC
Exec=$DIR/fluidsender.sh
Icon=applications-engineering
Terminal=false
Categories=Development;Engineering;"
    apps="$HOME/.local/share/applications"
    mkdir -p "$apps"
    printf '%s\n' "$entry" > "$apps/fluidsender.desktop"
    ok "app-menu entry: $apps/fluidsender.desktop"
    desk=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
    if [ -d "$desk" ]; then
        if [ -e "$desk/FluidSender.desktop" ]; then
            ok "desktop icon already there: $desk/FluidSender.desktop (left alone)"
        else
            printf '%s\n' "$entry" > "$desk/FluidSender.desktop"
            chmod +x "$desk/FluidSender.desktop"
            gio set "$desk/FluidSender.desktop" metadata::trusted true 2>/dev/null
            ok "desktop icon: $desk/FluidSender.desktop (if GNOME asks, right-click it and choose 'Allow Launching')"
        fi
    fi
fi

if [ $RUN_TESTS = 1 ]; then
    echo
    echo "Tests (simulators only, no hardware is touched):"
    if (cd "$DIR" && python3 -m unittest discover -s tests); then ok "all tests passed"; else bad "some tests failed (see above)"; fi
fi

echo
if [ $FAILED = 1 ]; then
    echo "Setup found something missing. Install it and run ./setup.sh again."
    exit 1
fi
[ $WARNED = 1 ] && echo "Setup done, with warnings above." || echo "Setup done."
echo "Start FluidSender with:  $DIR/fluidsender.sh   (or the FluidSender icon)"
echo "Guides: $DIR/docs/"
