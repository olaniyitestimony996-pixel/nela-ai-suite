"""
What Nela actually knows about your PC — built from real Windows sources,
not guessed from folder names.

App index (merged from three sources):
  1. Windows Registry "Uninstall" keys (what Add/Remove Programs reads)
  2. shell:AppsFolder — the only way to see Microsoft Store / UWP apps
  3. Start Menu .lnk shortcuts — catches anything the first two miss

File index: your Documents, Downloads, Desktop, Pictures, Videos, Music
(deliberately not the whole drive).

Built for a budget laptop:
  * Windows-only libraries are imported lazily, so they don't slow app start.
  * The last index is cached on disk and loaded instantly at launch; the fresh
    scan then runs quietly in the background and updates the cache.
  * The folder scan yields regularly so it can never starve the window.

Matching uses RapidFuzz with a real confidence threshold — below it, Nela says
"not found" instead of launching the nearest wrong thing.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

USER_HOME = Path.home()
INDEXED_FOLDERS = [USER_HOME / n for n in ("Documents", "Downloads", "Desktop", "Pictures", "Videos", "Music")]
SPECIAL_FOLDERS = {
    "documents": USER_HOME / "Documents",
    "downloads": USER_HOME / "Downloads",
    "desktop": USER_HOME / "Desktop",
    "pictures": USER_HOME / "Pictures",
    "videos": USER_HOME / "Videos",
    "music": USER_HOME / "Music",
}

MATCH_THRESHOLD = 72
SKIP_DIR_NAMES = {"node_modules", ".git", "__pycache__", "$RECYCLE.BIN"}

DATA_ROOT = Path(os.environ.get("NELA_DATA_DIR") or (Path(__file__).parent.parent / ".nela_data"))
CACHE_PATH = DATA_ROOT / "index_cache.json"

_lock = threading.Lock()
_app_index: dict[str, str] = {}    # display name -> launch target (path or shell URI)
_file_index: dict[str, str] = {}   # file name -> full path


# ---------------------------------------------------------------------------
# Disk cache — so the first "open chrome" after launch works instantly
# ---------------------------------------------------------------------------
def save_cache() -> None:
    try:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        with _lock:
            payload = {"apps": _app_index, "files": _file_index, "saved_at": time.time()}
        tmp = CACHE_PATH.with_name(CACHE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass  # cache is a nice-to-have; never break the app over it


def load_cache() -> bool:
    global _app_index, _file_index
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    with _lock:
        if not _app_index:
            _app_index = data.get("apps", {}) or {}
        if not _file_index:
            _file_index = data.get("files", {}) or {}
    return True


# ---------------------------------------------------------------------------
# App indexing
# ---------------------------------------------------------------------------
def _scan_registry_apps() -> dict[str, str]:
    import winreg

    found: dict[str, str] = {}
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                subkey = winreg.OpenKey(key, winreg.EnumKey(key, i))
                name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                try:
                    exe_path = winreg.QueryValueEx(subkey, "DisplayIcon")[0].split(",")[0].strip('"')
                except OSError:
                    exe_path = None
                if not exe_path:
                    try:
                        exe_path = winreg.QueryValueEx(subkey, "InstallLocation")[0]
                    except OSError:
                        continue
                if name and exe_path and Path(exe_path).exists():
                    found[name.lower()] = exe_path
            except OSError:
                continue
    return found


def _scan_uwp_apps() -> dict[str, str]:
    """Store/UWP apps, via the same namespace Explorer's 'All apps' uses."""
    import win32com.client

    found: dict[str, str] = {}
    try:
        shell = win32com.client.Dispatch("Shell.Application")
        for item in shell.NameSpace("shell:AppsFolder").Items():
            if item.Name and item.Path:
                found[item.Name.lower()] = f"shell:appsFolder\\{item.Path}"
    except Exception:
        pass  # can fail on some systems — the other sources still work
    return found


def _scan_start_menu_shortcuts() -> dict[str, str]:
    import win32com.client

    found: dict[str, str] = {}
    dirs = [
        Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    shell = win32com.client.Dispatch("WScript.Shell")
    for base in dirs:
        if not base.exists():
            continue
        for shortcut_path in base.rglob("*.lnk"):
            try:
                target = shell.CreateShortCut(str(shortcut_path)).Targetpath
                if target:
                    found[shortcut_path.stem.lower()] = target
            except Exception:
                continue
    return found


def build_app_index() -> dict[str, str]:
    # COM objects require the thread using them to be registered with Windows
    # first. Without this, COM calls from a background thread can silently
    # hang instead of erroring — which froze the whole window.
    try:
        import pythoncom
    except ImportError:
        pythoncom = None

    if pythoncom:
        pythoncom.CoInitialize()
    try:
        merged: dict[str, str] = {}
        for scanner in (_scan_start_menu_shortcuts, _scan_registry_apps, _scan_uwp_apps):
            try:
                merged.update(scanner())
            except Exception:
                continue  # one broken source shouldn't take down the whole index
        global _app_index
        with _lock:
            _app_index = merged
        return merged
    finally:
        if pythoncom:
            pythoncom.CoUninitialize()


# ---------------------------------------------------------------------------
# File indexing
# ---------------------------------------------------------------------------
def build_file_index(max_files: int = 8000) -> dict[str, str]:
    found: dict[str, str] = {}
    count = 0
    for folder in INDEXED_FOLDERS:
        if not folder.exists():
            continue
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".")]
            for fname in files:
                found[fname.lower()] = str(Path(root) / fname)
                count += 1
                if count % 200 == 0:
                    time.sleep(0.002)  # hand the CPU back regularly — the window must stay smooth
                if count >= max_files:
                    break
            if count >= max_files:
                break
    global _file_index
    with _lock:
        _file_index = found
    return found


def rebuild_all() -> tuple[int, int]:
    apps = build_app_index()
    files = build_file_index()
    save_cache()
    return len(apps), len(files)


def build_all_indexes():
    """Startup: cached index instantly, then a quiet background refresh a few
    seconds later (so it never competes with the window's first paint)."""
    load_cache()
    time.sleep(6)
    rebuild_all()


# ---------------------------------------------------------------------------
# Fuzzy resolution — the actual "don't guess" logic
# ---------------------------------------------------------------------------
def resolve_app(name: str) -> Optional[tuple[str, str]]:
    from rapidfuzz import fuzz, process

    with _lock:
        index = dict(_app_index)
    if not index:
        return None
    result = process.extractOne(name.lower(), index.keys(), scorer=fuzz.WRatio)
    if result is None:
        return None
    matched_name, score, _ = result
    if score < MATCH_THRESHOLD:
        return None
    return matched_name, index[matched_name]


def suggest_apps(name: str, limit: int = 3) -> list[str]:
    """Near-misses below the match threshold — used to turn a flat 'not
    found' into a real, honest suggestion."""
    from rapidfuzz import fuzz, process

    with _lock:
        index = dict(_app_index)
    if not index:
        return []
    results = process.extract(name.lower(), index.keys(), scorer=fuzz.WRatio, limit=limit)
    return [r[0] for r in results if r[1] >= 45]  # still recognisably close, just not confident enough to auto-launch


def resolve_file(name: str) -> Optional[tuple[str, str]]:
    from rapidfuzz import fuzz, process

    key = name.lower().strip()
    for folder_name, path in SPECIAL_FOLDERS.items():
        if folder_name in key:
            return folder_name, str(path)

    with _lock:
        index = dict(_file_index)
    if not index:
        return None
    result = process.extractOne(key, index.keys(), scorer=fuzz.WRatio)
    if result is None:
        return None
    matched_name, score, _ = result
    if score < MATCH_THRESHOLD:
        return None
    return matched_name, index[matched_name]
