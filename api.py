"""
The bridge between the page and Python. Every PUBLIC method on NelaAPI is
callable from JavaScript as `window.pywebview.api.<method>(...)`.

IMPORTANT: everything that isn't a real API method is underscore-private on
purpose. pywebview walks every public attribute of this object when the page
loads to build the JS API — if a public attribute points at something big
(like the window itself), that walk can stall the whole app at startup.
Keep it private, keep it fast.
"""

import json
import os
import threading
import time
from typing import Optional

import history
import indexer
import tts
import user_profile
from network_status import NetworkStatus
from voice import VoiceManager
from updater import UpdateChecker

PRELOAD_MODEL = os.environ.get("NELA_PRELOAD_MODEL", "1") == "1"


class NelaAPI:
    def __init__(self):
        self._window = None
        self._network = NetworkStatus()
        self._voice = VoiceManager(notify=self._push_voice_event)
        self._updater = UpdateChecker(self._network, self._push_update_event)
        self._chat_lock = threading.Lock()

        # Heavy stuff happens AFTER the window is up, and staggered, so a
        # budget laptop isn't doing everything at once during first paint.
        threading.Thread(target=self._warm_up_model, daemon=True).start()
        threading.Thread(target=self._warm_up_index, daemon=True).start()

    def _attach_window(self, window):
        self._window = window

    # -- startup warm-up (background) --------------------------------------
    def _warm_up_model(self):
        time.sleep(1.5)
        try:
            import chat_engine  # imports ollama etc. off the UI path

            if PRELOAD_MODEL:
                chat_engine.warm_model()  # loads the model into RAM so the first reply isn't slow
        except Exception as e:
            print(f"[warm-up] {e}")

    def _warm_up_index(self):
        try:
            indexer.build_all_indexes()
        except Exception as e:
            print(f"[index warm-up] {e}")

    # -- pushing events into the page --------------------------------------
    def _push(self, channel: str, payload: dict):
        if not self._window:
            return
        try:
            data = json.dumps({"channel": channel, **payload})
            self._window.evaluate_js(f"window.nelaHandleEvent({data})")
        except Exception as e:
            print(f"[bridge push error] {e}")

    def _push_chat_event(self, payload: dict):
        self._push("chat", payload)

    def _push_voice_event(self, payload: dict):
        self._push("voice", payload)

    def _push_update_event(self, payload: dict):
        self._push("update", payload)

    # =======================================================================
    # Chat
    # =======================================================================
    def send_message(self, message: str, conv_history: list, chat_id: Optional[str] = None) -> dict:
        if not self._chat_lock.acquire(blocking=False):
            return {"started": False, "reason": "Nela is still answering the last message."}
        threading.Thread(
            target=self._chat_worker, args=(message, conv_history, chat_id), daemon=True
        ).start()
        return {"started": True}

    def _chat_worker(self, message, conv_history, chat_id):
        try:
            from chat_engine import run_chat

            run_chat(message, conv_history, chat_id, self._network, self._push_chat_event)
        except Exception as e:
            # Never fail silently — a silent failure looks exactly like a freeze.
            self._push_chat_event({"chat_id": chat_id, "error": f"Something went wrong: {e}"})
        finally:
            self._chat_lock.release()

    def get_history(self) -> list:
        return history.list_chats()

    def get_chat(self, chat_id: str) -> Optional[dict]:
        return history.load_chat(chat_id)

    def delete_chat(self, chat_id: str) -> dict:
        return {"deleted": history.delete_chat(chat_id)}

    # =======================================================================
    # Voice
    # =======================================================================
    def start_wake_word(self) -> dict:
        self._voice.start_wake_listener()
        return {"wake_enabled": True}

    def stop_wake_word(self) -> dict:
        self._voice.stop_wake_listener()
        return {"wake_enabled": False}

    def manual_listen(self) -> dict:
        self._voice.start_manual_listen()
        return {"status": "listening"}

    # =======================================================================
    # Network status
    # =======================================================================
    def get_network_status(self) -> dict:
        return self._network.status

    def set_network_override(self, force_offline: bool) -> dict:
        self._network.set_manual_override(force_offline)
        return self._network.status

    # =======================================================================
    # Text-to-speech
    # =======================================================================
    def get_tts_status(self) -> dict:
        return {"tts_enabled": tts.is_enabled()}

    def set_tts(self, enabled: bool) -> dict:
        tts.set_enabled(enabled)
        return {"tts_enabled": tts.is_enabled()}

    # =======================================================================
    # App/file index (runs in the background — never blocks the window)
    # =======================================================================
    def rebuild_index(self) -> dict:
        threading.Thread(target=self._rebuild_index_background, daemon=True).start()
        return {"started": True}

    def _rebuild_index_background(self):
        apps, files = indexer.rebuild_all()
        self._push("index", {"apps_indexed": apps, "files_indexed": files})

    # =======================================================================
    # Updates
    # =======================================================================
    def get_app_version(self) -> dict:
        from version import APP_VERSION

        return {"version": APP_VERSION}

    def check_for_updates(self) -> dict:
        info = self._updater.check_now()
        return info or {"available": False}

    def start_update(self) -> dict:
        threading.Thread(target=self._updater.download_and_install, daemon=True).start()
        return {"started": True}

    # =======================================================================
    # Profile — a name, not a login
    # =======================================================================
    def get_profile(self) -> dict:
        return user_profile.get_profile()

    def set_profile_name(self, name: str) -> dict:
        return user_profile.set_name(name)

    # =======================================================================
    # Window controls (frameless custom title bar)
    # =======================================================================
    def minimize_window(self) -> dict:
        if self._window:
            self._window.minimize()
        return {"ok": True}

    def toggle_maximize_window(self) -> dict:
        # toggle_fullscreen is the one call reliably present across pywebview versions
        if self._window:
            self._window.toggle_fullscreen()
        return {"ok": True}

    def close_window(self) -> dict:
        if self._window:
            self._window.destroy()
        return {"ok": True}
