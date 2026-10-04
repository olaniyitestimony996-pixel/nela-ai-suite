"""
Nela speaking back — fully offline via pyttsx3 (Windows' built-in voices).

One dedicated worker thread owns the speech engine: it's created once (not
on every reply — that was slow), and pyttsx3 is imported INSIDE that thread,
which is what Windows' speech system needs to initialise properly. Only the
latest reply is kept queued, so a fast chat never builds a backlog of
speech.
"""

import queue
import re
import threading

_lock = threading.Lock()
_enabled = False
_queue: "queue.Queue[str]" = queue.Queue()
_worker = None


def set_enabled(enabled: bool):
    global _enabled
    with _lock:
        _enabled = enabled
    if not enabled:
        _drain()


def is_enabled() -> bool:
    with _lock:
        return _enabled


def _drain():
    try:
        while True:
            _queue.get_nowait()
    except queue.Empty:
        pass


def _clean(text: str) -> str:
    # don't read markdown punctuation aloud
    return re.sub(r"[*_`#>]+", "", text).strip()


def _run():
    global _worker
    try:
        import pyttsx3

        engine = pyttsx3.init()
    except Exception as e:
        print(f"[TTS unavailable] {e}")
        with _lock:
            _worker = None
        return

    while True:
        text = _queue.get()
        if not is_enabled():
            continue
        try:
            engine.say(text)
            engine.runAndWait()
        except Exception as e:
            print(f"[TTS error] {e}")


def speak(text: str):
    global _worker
    cleaned = _clean(text)
    if not is_enabled() or not cleaned:
        return
    _drain()
    _queue.put(cleaned)
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, daemon=True)
            _worker.start()
