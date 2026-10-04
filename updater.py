"""
Checks your GitHub Releases for a version newer than this build, shows a
native Windows notification, and — if you click "Update" inside the app —
downloads the new installer and hands off to it.

Respects the same online/offline switch as the rest of the app: never
checks while offline, never nags, never blocks the window.
"""

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from version import APP_VERSION, GITHUB_REPO, INSTALLER_ASSET_NAME

API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CHECK_INTERVAL_S = int(os.environ.get("NELA_UPDATE_CHECK_INTERVAL", str(4 * 60 * 60)))  # every 4 hours


def _fetch_latest_release() -> Optional[dict]:
    import requests

    try:
        resp = requests.get(API_URL, timeout=10, headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None  # offline, GitHub hiccup, no releases yet — just skip this check


def check_once() -> Optional[dict]:
    """{'version', 'download_url', 'notes'} if newer, else None."""
    from packaging.version import Version, InvalidVersion

    release = _fetch_latest_release()
    if not release:
        return None

    tag = (release.get("tag_name") or "").lstrip("v")
    try:
        if Version(tag) <= Version(APP_VERSION):
            return None
    except InvalidVersion:
        return None

    asset = next((a for a in release.get("assets", []) if a.get("name") == INSTALLER_ASSET_NAME), None)
    if not asset:
        return None  # release exists but the installer isn't attached yet

    return {"version": tag, "download_url": asset["browser_download_url"], "notes": (release.get("body") or "").strip()[:300]}


def download_installer(download_url: str, on_progress=None) -> Optional[Path]:
    import requests

    dest = Path(tempfile.gettempdir()) / INSTALLER_ASSET_NAME
    try:
        with requests.get(download_url, stream=True, timeout=30) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            done = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=262144):
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress and total:
                        on_progress(done / total)
        return dest
    except Exception:
        return None


def install_and_restart(installer_path: Path):
    """Launches the new installer, then hard-exits THIS process immediately
    — Windows won't let the installer overwrite files this app still has
    open, so it has to actually be gone first."""
    subprocess.Popen([str(installer_path)], shell=False)
    time.sleep(0.5)
    os._exit(0)


def _show_toast(info: dict):
    # A plain attention-getter only. Wiring the toast's own click straight
    # back into a running Python app needs a registered custom URI scheme
    # and single-instance handling — real work I haven't built, so I'm not
    # claiming it works. The reliable click-to-update path is the in-app
    # banner (driven by the "update" events this module sends).
    try:
        from winotify import Notification, audio

        toast = Notification(
            app_id="Nela AI",
            title=f"Nela AI v{info['version']} is available",
            msg="Open Nela and click \u201cUpdate\u201d to install it.",
            duration="long",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        pass  # winotify missing or unsupported OS — the in-app banner still works


class UpdateChecker:
    def __init__(self, network_status, notify):
        self._network_status = network_status
        self._notify = notify  # pushes {"channel": "update", ...}
        self._latest: Optional[dict] = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        time.sleep(30)  # let the app finish starting up first
        while True:
            if self._network_status.is_online:
                self.check_now()
            time.sleep(CHECK_INTERVAL_S)

    def check_now(self) -> Optional[dict]:
        info = check_once()
        self._latest = info
        if info:
            self._notify({"available": True, **info})
            _show_toast(info)
        return info

    def download_and_install(self):
        if not self._latest:
            self._notify({"error": "No update is currently available."})
            return
        self._notify({"downloading": True})
        path = download_installer(self._latest["download_url"], on_progress=lambda p: self._notify({"progress": round(p, 2)}))
        if not path:
            self._notify({"error": "Download failed \u2014 check your connection and try again."})
            return
        self._notify({"installing": True})
        install_and_restart(path)  # process exits inside this call
