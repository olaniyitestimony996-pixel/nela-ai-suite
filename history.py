"""
Chat history — one JSON file per conversation plus a tiny index file, so
building the sidebar never means opening and parsing every conversation
(slow on an older laptop with a spinning hard drive).
"""

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

DATA_ROOT = Path(os.environ.get("NELA_DATA_DIR") or (Path(__file__).parent.parent / ".nela_data"))
DATA_DIR = DATA_ROOT / "chats"
DATA_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH = DATA_DIR / "_index.json"

_lock = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _chat_path(chat_id: str) -> Optional[Path]:
    if not isinstance(chat_id, str) or not _ID_RE.match(chat_id):
        return None
    return DATA_DIR / f"{chat_id}.json"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # a crash mid-write can't leave a half-written file


def _read_index() -> Optional[dict]:
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _rebuild_index() -> dict:
    index = {}
    for path in DATA_DIR.glob("*.json"):
        if path.name == INDEX_PATH.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            index[data["chat_id"]] = {
                "title": data.get("title", "Untitled chat"),
                "updated_at": data.get("updated_at", ""),
            }
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    _atomic_write(INDEX_PATH, json.dumps(index))
    return index


def load_chat(chat_id: str) -> Optional[dict]:
    path = _chat_path(chat_id)
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_chat(chat_id: str, title: str, messages: List[dict]) -> None:
    path = _chat_path(chat_id)
    if path is None:
        raise ValueError("invalid chat id")
    updated = datetime.utcnow().isoformat()
    payload = {"chat_id": chat_id, "title": title, "messages": messages, "updated_at": updated}
    with _lock:
        _atomic_write(path, json.dumps(payload))
        index = _read_index()
        if index is None:
            index = _rebuild_index()
        index[chat_id] = {"title": title, "updated_at": updated}
        _atomic_write(INDEX_PATH, json.dumps(index))


def delete_chat(chat_id: str) -> bool:
    path = _chat_path(chat_id)
    if path is None:
        return False
    with _lock:
        existed = path.exists()
        if existed:
            path.unlink()
        index = _read_index()
        if index is not None and chat_id in index:
            del index[chat_id]
            _atomic_write(INDEX_PATH, json.dumps(index))
    return existed


def list_chats() -> List[dict]:
    with _lock:
        index = _read_index()
        if index is None:
            index = _rebuild_index()  # first run of this version, or index lost — self-heals
    chats = []
    for chat_id, meta in index.items():
        path = _chat_path(chat_id)
        if path is not None and path.exists():
            chats.append({"chat_id": chat_id, "title": meta.get("title", "Untitled chat"), "updated_at": meta.get("updated_at", "")})
    chats.sort(key=lambda c: c["updated_at"], reverse=True)
    return chats


def make_title(first_message: str) -> str:
    text = first_message.strip().replace("\n", " ")
    return (text[:42] + "…") if len(text) > 42 else text
