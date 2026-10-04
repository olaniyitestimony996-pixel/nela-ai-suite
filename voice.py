"""
Voice pipeline for Nela.

Two different engines doing two different jobs, on purpose:

  - pocketsphinx: ONLY listens for the wake phrase ("hey nela"). Keyword
    spotting is the one thing pocketsphinx is genuinely reliable at —
    it's not being asked to transcribe full sentences.
  - Vosk: transcribes the actual command once the wake word fires (or
    once you click the mic manually). Meaningfully more accurate than
    pocketsphinx for real dictation, fully offline, just needs its model
    downloaded once (see README).

Everything runs in background threads (blocking audio I/O shouldn't ever
block the UI). State changes go straight to a `notify` callback — in the
pywebview version, that callback pushes directly into the page via
window.evaluate_js(), no HTTP/WebSocket layer needed at all.
"""

import io
import json
import os
import threading
import wave
from pathlib import Path
from typing import Callable, Optional


VOSK_MODEL_PATH = os.environ.get(
    "NELA_VOSK_MODEL", str(Path(__file__).parent / "vosk-model")
)
WAKE_PHRASE = os.environ.get("NELA_WAKE_PHRASE", "hey nela")


class VoiceManager:
    def __init__(self, notify: Callable[[dict], None]):
        self.notify = notify

        self._wake_thread: Optional[threading.Thread] = None
        self._stop_wake = threading.Event()
        self.wake_enabled = False

        self._vosk_model = None  # loaded lazily — it's a real chunk of memory

    # -- broadcasting state to the frontend -----------------------------
    def _emit(self, state: str, **extra):
        self.notify({"state": state, **extra})

    # -- Vosk model (loaded once, reused) --------------------------------
    def _get_vosk_model(self):
        if self._vosk_model is None:
            from vosk import Model  # imported lazily so the app can still

            # start even if vosk/its model aren't set up yet
            if not Path(VOSK_MODEL_PATH).exists():
                raise RuntimeError(
                    f"Vosk model not found at {VOSK_MODEL_PATH}. "
                    "See README for the download link."
                )
            self._vosk_model = Model(VOSK_MODEL_PATH)
        return self._vosk_model

    # -- wake word listener (pocketsphinx, background thread) -----------
    def start_wake_listener(self):
        if self._wake_thread and self._wake_thread.is_alive():
            return  # already running
        self._stop_wake.clear()
        self._wake_thread = threading.Thread(target=self._wake_loop, daemon=True)
        self._wake_thread.start()
        self.wake_enabled = True
        self._emit("wake_enabled")

    def stop_wake_listener(self):
        self._stop_wake.set()
        self.wake_enabled = False
        self._emit("wake_disabled")

    def _wake_loop(self):
        try:
            from pocketsphinx import LiveSpeech
        except Exception as e:
            self._emit("error", message=f"Wake word engine unavailable: {e}")
            return

        try:
            speech = LiveSpeech(
                verbose=False,
                sampling_rate=16000,
                buffer_size=2048,
                keyphrase=WAKE_PHRASE,
                kws_threshold=1e-20,
            )
            for phrase in speech:
                if self._stop_wake.is_set():
                    break
                self._emit("wake_detected")
                self._listen_and_transcribe()
                if self._stop_wake.is_set():
                    break
        except Exception as e:
            self._emit("error", message=f"Wake word listener crashed: {e}")

    # -- manual click-to-talk (skips the wake word) ----------------------
    def start_manual_listen(self):
        threading.Thread(target=self._listen_and_transcribe, daemon=True).start()

    # -- shared recording + transcription path ---------------------------
    def _listen_and_transcribe(self):
        import speech_recognition as sr  # lazy: keeps app startup light

        recognizer = sr.Recognizer()
        self._emit("listening")

        try:
            with sr.Microphone(sample_rate=16000) as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.4)
                # phrase_time_limit caps a runaway recording; the built-in
                # silence detection normally stops it sooner than that.
                audio = recognizer.listen(source, timeout=6, phrase_time_limit=12)
        except sr.WaitTimeoutError:
            self._emit("idle")  # nothing said — quietly go back to idle
            return
        except Exception as e:
            self._emit("error", message=f"Microphone error: {e}")
            return

        self._emit("transcribing")

        try:
            text = self._transcribe_with_vosk(audio)
        except Exception as e:
            self._emit("error", message=f"Transcription failed: {e}")
            return

        if text.strip():
            self._emit("transcript", text=text.strip())
        else:
            self._emit("idle")

    def _transcribe_with_vosk(self, audio) -> str:
        from vosk import KaldiRecognizer

        model = self._get_vosk_model()

        # AudioData -> mono 16kHz WAV bytes, which is what Vosk expects.
        wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
        wf = wave.open(io.BytesIO(wav_bytes), "rb")

        rec = KaldiRecognizer(model, wf.getframerate())
        rec.SetWords(False)

        result_text = []
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            if rec.AcceptWaveform(data):
                part = json.loads(rec.Result())
                if part.get("text"):
                    result_text.append(part["text"])

        final = json.loads(rec.FinalResult())
        if final.get("text"):
            result_text.append(final["text"])

        return " ".join(result_text)
