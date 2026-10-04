"""
A local name for the greeting ("Welcome back, {name}") — NOT a login system.
"""

import json
import os
from pathlib import Path

DATA_ROOT = Path(os.environ.get("NELA_DATA_DIR") or (Path(__file__).parent.parent / ".nela_data"))
PROFILE_PATH = DATA_ROOT / "profile.json"
DATA_ROOT.mkdir(parents=True, exist_ok=True)


def get_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {"name": None}
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"name": None}


def set_name(name: str) -> dict:
    profile = {"name": name.strip()[:40]}
    PROFILE_PATH.write_text(json.dumps(profile), encoding="utf-8")
    return profile
