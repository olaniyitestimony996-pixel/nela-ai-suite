"""
The chat loop, built to be light on a budget laptop:

  * ONE streamed call decides "answer or use a tool" while it writes, so the
    first words appear immediately — no more running the model twice per
    message (once to decide, once to answer).
  * Tools are only re-run through the model when one was actually used.
  * Only the last few messages are sent as context (long chats don't get
    slower and slower), and the model is kept loaded in RAM between messages.
  * Streamed text is batched (~25 updates/second) so a fast PC isn't flooding
    the window with a bridge call per word.

Events go to a `notify` callback (pushed into the page by api.py).
"""

import json
import os
import re
import time
import uuid
from typing import Callable, List, Optional

from pydantic import ValidationError

import history
import tools
import tts
from schemas import (
    ToolResult, OpenAppArgs, CloseAppArgs, OpenPathArgs, ReadFileArgs,
    OpenSettingsArgs, SetWifiArgs, WebSearchArgs, SpeakArgs, NoArgs,
)

OLLAMA_MODEL = os.environ.get("NELA_MODEL", "qwen2.5:3b")
NUM_CTX = int(os.environ.get("NELA_NUM_CTX", "4096"))            # smaller = less RAM, faster
KEEP_ALIVE = os.environ.get("NELA_KEEP_ALIVE", "15m")             # keep model loaded between messages
MAX_HISTORY_MESSAGES = int(os.environ.get("NELA_MAX_HISTORY", "12"))
FLUSH_INTERVAL_S = 0.04

_OPTIONS = {"num_ctx": NUM_CTX}

SYSTEM_PROMPT = (
    "You are Nela, a warm, sharp, genuinely helpful AI assistant running locally "
    "on the user's computer. Conversational and direct, never robotic.\n\n"
    "You have real tools to open/close apps, open files and folders, jump to "
    "Windows settings pages, search the web (only when online), and speak replies "
    "aloud. Use them when the user actually asks for one of those things.\n\n"
    "CRITICAL HONESTY RULE: every tool result includes a `success` field. "
    "NEVER say something is 'done', 'opened', or succeeded unless the tool result "
    "explicitly says success: true. If success is false, tell the user plainly "
    "what went wrong using the tool's own message — for example, if a file "
    "wasn't found, say so directly: something isn't on their computer or you "
    "can't access it. Do not soften a failure into something that sounds like "
    "it might have worked."
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def _do_speak(args: SpeakArgs) -> ToolResult:
    if not tts.is_enabled():
        return ToolResult(success=False, message="Speech is switched off. Turn on 'Speak replies' in Tools first.")
    tts.speak(args.text)
    return ToolResult(success=True, message="Speaking now.")


def _do_web_search(args: WebSearchArgs) -> ToolResult:
    import websearch  # loaded only when actually used — keeps startup light

    return websearch.web_search(args.query)


TOOL_REGISTRY = {
    "open_app": (OpenAppArgs, lambda a: tools.open_app(a.name)),
    "close_app": (CloseAppArgs, lambda a: tools.close_app(a.name)),
    "open_path": (OpenPathArgs, lambda a: tools.open_path(a.name)),
    "read_text_file": (ReadFileArgs, lambda a: tools.read_text_file(a.name)),
    "read_pdf": (ReadFileArgs, lambda a: tools.read_pdf(a.name)),
    "open_settings": (OpenSettingsArgs, lambda a: tools.open_settings(a.page)),
    "take_screenshot_snip": (NoArgs, lambda a: tools.take_screenshot_snip()),
    "set_wifi": (SetWifiArgs, lambda a: tools.set_wifi(a.enabled)),
    "web_search": (WebSearchArgs, _do_web_search),
    "speak": (SpeakArgs, _do_speak),
}


def _tool_schema(name: str, description: str, model) -> dict:
    schema = model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}


BASE_TOOLS = [
    _tool_schema("open_app", "Open/launch an application installed on the user's PC by name.", OpenAppArgs),
    _tool_schema("close_app", "Close/terminate a currently running application by name.", CloseAppArgs),
    _tool_schema("open_path", "Open a file or folder on the user's PC by name or description.", OpenPathArgs),
    _tool_schema("read_text_file", "Read the contents of a plain text file, by name/description or full path.", ReadFileArgs),
    _tool_schema("read_pdf", "Extract and read the text of a PDF file, by name/description or full path.", ReadFileArgs),
    _tool_schema(
        "open_settings",
        "Open a specific Windows settings page. Valid pages: network, wifi, vpn, mobile hotspot, "
        "bluetooth, airplane mode, night light, battery saver, focus assist, location, all settings, project.",
        OpenSettingsArgs,
    ),
    _tool_schema("take_screenshot_snip", "Open Windows' screen snip tool.", NoArgs),
    _tool_schema("set_wifi", "Turn Wi-Fi on or off. Requires Nela running as Administrator.", SetWifiArgs),
    _tool_schema("speak", "Speak text aloud using text-to-speech.", SpeakArgs),
]
WEB_SEARCH_TOOL = _tool_schema("web_search", "Search the web for current information. Only available when online.", WebSearchArgs)


def _execute_tool(name, raw_args) -> ToolResult:
    entry = TOOL_REGISTRY.get(name)
    if not entry:
        return ToolResult(success=False, message=f"Unknown tool: {name}")
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError:
            raw_args = {}
    model_cls, func = entry
    try:
        validated = model_cls.model_validate(dict(raw_args or {}))
    except (ValidationError, TypeError, ValueError) as e:
        return ToolResult(success=False, message=f"Invalid arguments for {name}: {e}")
    try:
        return func(validated)
    except Exception as e:
        return ToolResult(success=False, message=f"{name} crashed unexpectedly: {e}")


# ---------------------------------------------------------------------------
# Small models sometimes write a tool call as plain text instead of using the
# proper tool-call channel. Catch that instead of showing raw JSON to the user.
# ---------------------------------------------------------------------------
_TOOL_TAG = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.DOTALL)


def _looks_like_tool_call(text: str) -> bool:
    return text.lstrip().startswith(("{", "[", "<tool_call"))


def _parse_inline_tool_call(text: str):
    candidate = text.strip()
    match = _TOOL_TAG.search(candidate)
    if match:
        candidate = match.group(1)
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and obj.get("name") in TOOL_REGISTRY:
        return {"function": {"name": obj["name"], "arguments": obj.get("arguments", obj.get("parameters", {}))}}
    return None


# ---------------------------------------------------------------------------
# Batches streamed text so we cross the Python->page bridge ~25x/second, not
# once per word.
# ---------------------------------------------------------------------------
class _DeltaBatcher:
    def __init__(self, notify: Callable[[dict], None], chat_id: str):
        self._notify = notify
        self._chat_id = chat_id
        self._buf: List[str] = []
        self._last = 0.0
        self.total = ""

    def add(self, piece: str):
        if not piece:
            return
        self._buf.append(piece)
        self.total += piece
        now = time.monotonic()
        if now - self._last >= FLUSH_INTERVAL_S:
            self.flush()

    def flush(self):
        if self._buf:
            self._notify({"chat_id": self._chat_id, "delta": "".join(self._buf)})
            self._buf = []
        self._last = time.monotonic()


def _first_pass_streamed(ollama, messages, tool_list, batcher):
    """One streamed call. Plain answers are shown live; anything that looks
    like a tool call is held back until we know what it is."""
    text, tool_calls, holding = "", [], None  # holding: None=undecided, True=held back, False=shown live
    for chunk in ollama.chat(
        model=OLLAMA_MODEL, messages=messages, tools=tool_list,
        stream=True, options=_OPTIONS, keep_alive=KEEP_ALIVE,
    ):
        msg = chunk.get("message") or {}
        tool_calls.extend(msg.get("tool_calls") or [])
        piece = msg.get("content") or ""
        if not piece:
            continue
        text += piece
        if holding is None and text.strip():
            holding = _looks_like_tool_call(text)
            if not holding:
                batcher.add(text)
                continue
        if holding is False:
            batcher.add(piece)
    return text, tool_calls, holding


def _first_pass_blocking(ollama, messages, tool_list, batcher):
    """Fallback for Ollama setups that can't stream while tools are enabled."""
    resp = ollama.chat(
        model=OLLAMA_MODEL, messages=messages, tools=tool_list,
        stream=False, options=_OPTIONS, keep_alive=KEEP_ALIVE,
    )
    msg = resp.get("message") or {}
    text = msg.get("content") or ""
    tool_calls = list(msg.get("tool_calls") or [])
    holding = _looks_like_tool_call(text) if text.strip() else None
    if holding is False:
        batcher.add(text)
    return text, tool_calls, holding


def _friendly_error(e: Exception) -> str:
    text = str(e)
    low = text.lower()
    if "connect" in low or "refused" in low or "10061" in low:
        return "I can't reach Ollama. Make sure it's running (open the Ollama app), then try again."
    if "not found" in low and "model" in low:
        return f"The model '{OLLAMA_MODEL}' isn't installed. Run:  ollama pull {OLLAMA_MODEL}"
    return text or e.__class__.__name__


def warm_model():
    """Loads the model into RAM in the background so the FIRST reply isn't
    the slow one. Uses the same options as chat so Ollama doesn't reload it."""
    try:
        import ollama

        ollama.generate(model=OLLAMA_MODEL, prompt="", keep_alive=KEEP_ALIVE, options=_OPTIONS)
    except Exception:
        pass  # Ollama not running yet, model missing, etc. — chat will report it properly


# ---------------------------------------------------------------------------
# The chat turn
# ---------------------------------------------------------------------------
def run_chat(
    message: str,
    conv_history: List[dict],
    chat_id: Optional[str],
    network_status,
    notify: Callable[[dict], None],
) -> None:
    resolved_chat_id = chat_id or str(uuid.uuid4())

    def emit(**payload):
        notify({"chat_id": resolved_chat_id, **payload})

    try:
        import ollama
    except ImportError:
        emit(error="The 'ollama' Python package isn't installed. Run:  pip install ollama")
        return

    batcher = _DeltaBatcher(notify, resolved_chat_id)

    recent = conv_history[-MAX_HISTORY_MESSAGES:] if MAX_HISTORY_MESSAGES > 0 else conv_history
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + list(recent) + [{"role": "user", "content": message}]

    tool_list = list(BASE_TOOLS)
    if network_status.is_online:
        tool_list.append(WEB_SEARCH_TOOL)

    emit(status="thinking")

    try:
        try:
            text, tool_calls, holding = _first_pass_streamed(ollama, messages, tool_list, batcher)
        except Exception:
            if batcher.total:
                raise  # already showed part of a reply — don't retry over it
            text, tool_calls, holding = _first_pass_blocking(ollama, messages, tool_list, batcher)
        batcher.flush()

        if not tool_calls and holding:
            parsed = _parse_inline_tool_call(text)
            if parsed:
                tool_calls = [parsed]
            else:
                batcher.add(text)  # it just looked like JSON — show it
                batcher.flush()

        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": text if holding is False else "",
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name")
                emit(status="acting", tool=name)
                result = _execute_tool(name, fn.get("arguments"))
                messages.append({"role": "tool", "content": result.model_dump_json()})

            emit(status="responding")
            for chunk in ollama.chat(
                model=OLLAMA_MODEL, messages=messages,
                stream=True, options=_OPTIONS, keep_alive=KEEP_ALIVE,
            ):
                batcher.add((chunk.get("message") or {}).get("content") or "")
            batcher.flush()
    except Exception as e:
        batcher.flush()
        emit(error=_friendly_error(e))
        return

    full_reply = batcher.total
    if not full_reply.strip():
        full_reply = "I didn't get an answer back from the model — try sending that again."
        batcher.add(full_reply)
        batcher.flush()

    if tts.is_enabled():
        tts.speak(full_reply)

    saved = list(conv_history) + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": full_reply},
    ]
    existing = history.load_chat(chat_id) if chat_id else None
    title = (existing or {}).get("title") or history.make_title(message)
    try:
        history.save_chat(resolved_chat_id, title, saved)
    except (OSError, ValueError) as e:
        print(f"[history] couldn't save chat: {e}")  # reply was delivered; only history is affected

    emit(status="done")
