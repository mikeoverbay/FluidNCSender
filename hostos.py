"""
FluidSender's differences between Linux and Windows outside the serial ports (those are the ports package): where
the file list starts, and the Windows share it may have to mount first. Chosen once, here, at import. (The launchers
are fluidsender.sh on Linux and FluidSender.bat on Windows.) Python standard library only.

    DEFAULT_DIR         where the file list starts (server.py --dir sets another)
    needs_mount(path)   whether path is on the share and the share isn't mounted (yet)
    mount_share()       mounts it (at most every MOUNT_RETRY s; never a prompt)

Linux: the user's CarveMaker folder on MIKESMACHINE's share, which gvfs mounts (gio, with its saved login).
Windows: Documents (or the home folder if there is none); nothing to mount.
"""

import os
import subprocess
import threading
import time

WINDOWS = os.name == "nt"
MOUNT_RETRY = 30        # s between tries to mount the share while it isn't there

if WINDOWS:
    GVFS = None
    _docs = os.path.join(os.path.expanduser("~"), "Documents")
    DEFAULT_DIR = _docs if os.path.isdir(_docs) else os.path.expanduser("~")

    def needs_mount(path):
        return False

    def mount_share():
        pass

else:
    GVFS = f"/run/user/{os.getuid()}/gvfs/"
    SHARE_URL = "smb://10.0.0.31/mikes_creations"     # MIKESMACHINE's share; gvfs shows it at DEFAULT_DIR's top
    DEFAULT_DIR = GVFS + "smb-share:server=10.0.0.31,share=mikes_creations/!_CarveMaker"
    _mount_lock = threading.Lock()
    _mount_tried = 0.0

    def needs_mount(path):
        return os.path.realpath(path).startswith(GVFS) and not os.path.isdir(path)     # (or a link to the share)

    def mount_share():
        """Mounts the Windows share with gio (its saved login, never a prompt), at most every MOUNT_RETRY s."""
        global _mount_tried
        with _mount_lock:
            if time.monotonic() - _mount_tried < MOUNT_RETRY:
                return
            _mount_tried = time.monotonic()
            try:
                subprocess.run(["gio", "mount", SHARE_URL], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15)
            except (OSError, subprocess.SubprocessError):
                pass
