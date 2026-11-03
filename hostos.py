"""
FluidSender's differences between Linux and Windows outside the serial ports (those are the ports package): where
the file list starts, and the Windows share it may have to mount first. Chosen once, here, at import. (The launchers
are fluidsender.sh on Linux and FluidSender.bat on Windows.) Python standard library only.

    DEFAULT_DIR         where the file list starts (server.py --dir sets another)
    places()            the places to pick a working folder from: [{"name", "path"}] (the Files panel's folder button)
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


def places():
    """The places to pick a working folder from (the Files panel's folder button): [{"name", "path"}], the ones that
    exist now. Windows: the home and Documents folders, then every drive letter (local, USB, mapped network
    drives). Linux: the home folder, /, the drives mounted under /media/<user> and /mnt, and gvfs's network shares."""
    out, home = [], os.path.expanduser("~")

    def add(name, path, check=True):
        if path and (not check or os.path.isdir(path)) and all(p["path"] != path for p in out):
            out.append({"name": name, "path": path})

    add("Home", home)
    add("Documents", os.path.join(home, "Documents"))
    if WINDOWS:
        import ctypes      # (Windows only: the drive letters in use)
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask & (1 << i):
                drive = f"{chr(65 + i)}:\\"
                add(f"Drive {drive[:2]}", drive, check=False)     # (no look: a dead network drive can hang)
        return out
    add("Computer (/)", "/")
    for top in (os.path.join("/media", os.path.basename(home)), "/mnt"):
        try:
            names = sorted(os.listdir(top))
        except OSError:
            continue
        for n in names:
            add(n, os.path.join(top, n))
    try:
        shares = sorted(os.listdir(GVFS))
    except OSError:
        shares = []
    for n in shares:            # "smb-share:server=10.0.0.31,share=mikes_creations" -> "mikes_creations on 10.0.0.31"
        server = n.split("server=")[-1].split(",")[0] if "server=" in n else ""
        share = n.split("share=")[-1] if "share=" in n else n
        add(f"{share} on {server}" if server else share, os.path.join(GVFS, n))
    return out
