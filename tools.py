"""
Nela's "hands" — the real actions it can take on your PC.

Every function here returns a ToolResult with an honest success/failure
flag. Nothing is ever reported as done unless it actually happened —
that's enforced structurally (the model sees `success: false` in the
tool result and is instructed to relay that honestly), not just by
asking the model nicely.

App and file locations come from indexer.py's real Windows data sources
— never guessed from a folder-name pattern.
"""

import os
import subprocess

import indexer
from schemas import ToolResult


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------
def open_app(name: str) -> ToolResult:
    match = indexer.resolve_app(name)
    if not match:
        suggestions = indexer.suggest_apps(name)
        msg = f"I couldn't find an app called '{name}' on this computer."
        if suggestions:
            msg += f" Did you mean: {', '.join(suggestions)}?"
        return ToolResult(success=False, message=msg)
    matched_name, target = match

    try:
        if target.startswith("shell:appsFolder\\"):
            # UWP/Store apps launch through explorer.exe, not directly.
            subprocess.Popen(["explorer.exe", target])
        else:
            subprocess.Popen([target])
        return ToolResult(success=True, message=f"Opened {matched_name}.")
    except Exception as e:
        return ToolResult(
            success=False,
            message=f"Found {matched_name} but couldn't launch it: {e}",
        )


def close_app(name: str) -> ToolResult:
    import psutil  # lazy: only needed here

    name_lower = name.lower().strip()
    closed = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            proc_name = (proc.info["name"] or "").lower()
            if name_lower in proc_name:
                proc.terminate()
                closed.append(proc.info["name"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if closed:
        return ToolResult(success=True, message=f"Closed {', '.join(set(closed))}.")
    return ToolResult(
        success=False,
        message=f"I couldn't find a running process matching '{name}' — it may not be open.",
    )


# ---------------------------------------------------------------------------
# Files & folders
# ---------------------------------------------------------------------------
def open_path(name: str) -> ToolResult:
    match = indexer.resolve_file(name)
    if not match:
        return ToolResult(
            success=False,
            message=f"I couldn't find anything matching '{name}' in your Documents, Downloads, Desktop, Pictures, Videos, or Music.",
        )
    matched_name, path = match

    if not os.path.exists(path):
        return ToolResult(
            success=False,
            message=f"I found a reference to '{matched_name}' but the file isn't there anymore — it may have been moved or deleted.",
        )

    try:
        os.startfile(path)
        return ToolResult(success=True, message=f"Opened {matched_name}.")
    except Exception as e:
        return ToolResult(success=False, message=f"Found {matched_name} but couldn't open it: {e}")


MAX_CHARS = 6000  # smaller = less for a weak CPU to chew through


def read_text_file(name: str) -> ToolResult:
    path = _resolve_readable_path(name)
    if not path:
        return ToolResult(success=False, message=f"I couldn't find a file matching '{name}'.")
    if not os.path.exists(path):
        return ToolResult(success=False, message=f"That file isn't on this computer, or I can't access it: {path}")

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        truncated = text[:MAX_CHARS] + ("\n...[truncated]" if len(text) > MAX_CHARS else "")
        return ToolResult(success=True, message=f"Read {os.path.basename(path)}.", data=truncated)
    except Exception as e:
        return ToolResult(success=False, message=f"Found the file but couldn't read it: {e}")


def read_pdf(name: str) -> ToolResult:
    path = _resolve_readable_path(name)
    if not path:
        return ToolResult(success=False, message=f"I couldn't find a PDF matching '{name}'.")
    if not os.path.exists(path):
        return ToolResult(success=False, message=f"That file isn't on this computer, or I can't access it: {path}")

    try:
        import fitz  # pymupdf

        doc = fitz.open(path)
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        truncated = text[:MAX_CHARS] + ("\n...[truncated]" if len(text) > MAX_CHARS else "")
        return ToolResult(success=True, message=f"Read {os.path.basename(path)}.", data=truncated)
    except Exception as e:
        return ToolResult(success=False, message=f"Found the PDF but couldn't read it: {e}")


def _resolve_readable_path(name: str) -> str | None:
    # If it already looks like a real path, trust it directly. Otherwise
    # resolve it through the file index like everything else.
    if os.path.sep in name or (len(name) > 1 and name[1] == ":"):
        return name
    match = indexer.resolve_file(name)
    return match[1] if match else None


# ---------------------------------------------------------------------------
# Windows settings shortcuts — reliable, official, no guessing at toggles
# Windows genuinely doesn't expose to other programs.
# ---------------------------------------------------------------------------
SETTINGS_PAGES = {
    "network": "ms-settings:network",
    "wifi": "ms-settings:network-wifi",
    "vpn": "ms-settings:network-vpn",
    "mobile hotspot": "ms-settings:network-mobilehotspot",
    "bluetooth": "ms-settings:bluetooth",
    "airplane mode": "ms-settings:network-airplanemode",
    "night light": "ms-settings:nightlight",
    "battery saver": "ms-settings:batterysaver",
    "focus assist": "ms-settings:quiethours",
    "location": "ms-settings:privacy-location",
    "all settings": "ms-settings:",
    "project": "ms-availablenetworks:",
}


def open_settings(page: str) -> ToolResult:
    key = page.lower().strip()
    uri = SETTINGS_PAGES.get(key)
    if not uri:
        return ToolResult(success=False, message=f"I don't have a direct link for '{page}' — try 'all settings'.")
    try:
        os.startfile(uri)
        return ToolResult(success=True, message=f"Opened {page} settings.")
    except Exception as e:
        return ToolResult(success=False, message=f"Couldn't open {page} settings: {e}")


def take_screenshot_snip() -> ToolResult:
    try:
        os.startfile("ms-screenclip:")
        return ToolResult(success=True, message="Opened screen snip.")
    except Exception as e:
        return ToolResult(success=False, message=f"Couldn't open screen snip: {e}")


def set_wifi(enabled: bool) -> ToolResult:
    action = "enabled" if enabled else "disabled"
    try:
        result = subprocess.run(
            ["netsh", "interface", "set", "interface", "Wi-Fi", f"admin={action}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return ToolResult(
                success=False,
                message=(
                    f"Couldn't {action[:-1]} Wi-Fi — this needs Nela running as "
                    "Administrator. Windows blocks this otherwise."
                ),
            )
        return ToolResult(success=True, message=f"Wi-Fi {action}.")
    except Exception as e:
        return ToolResult(success=False, message=f"Wi-Fi toggle failed: {e}")
