// ============================================================
// Nela AI — frontend logic. Talks to Python only through
// window.pywebview.api.<method>(...) — no fetch, no HTTP, no server.
//
// Built light for budget laptops: streaming text updates ONE text node
// (never rebuilds the whole chat), timers only run while they're needed,
// and background polling pauses when the window is hidden.
// ============================================================

const state = {
  chatId: null,
  messages: [],
  streamFullText: "",     // everything the backend has sent so far
  streamRevealed: "",     // what the typewriter has shown so far
  status: null,
  tool: null,
  isStreaming: false,
  doneReceived: false,    // backend finished; wait for the typewriter to catch up
  wakeEnabled: false,
  ttsEnabled: false,
  userName: null,
};

let typewriterTimer = null;
let streamBubble = null;  // { textNode } for the live assistant bubble

const el = {};
[
  "sidebar", "historyList", "newChatBtn",
  "bellBtn", "notifDropdown", "notifContent",
  "avatarBtn", "avatarDropdown", "avatarName", "renameBtn", "openSettingsBtn",
  "winMinimize", "winMaximize", "winClose",
  "errorBanner",
  "homeView", "welcomeName", "chatWindow",
  "newFromInputBtn", "messageInput", "webSearchToggle", "toolsBtn", "toolsDropdown",
  "micBtn", "wakeToggle", "ttsToggleItem", "sendBtn",
  "voiceOverlay", "voiceRingWrap", "voiceLabel",
  "nameModal", "nameInput", "nameSaveBtn",
  "settingsModal", "settingsRenameBtn", "settingsTtsToggle", "settingsWakeToggle",
  "settingsRebuildBtn", "settingsCloseBtn",
  "updateBanner", "updateBannerText", "updateBannerBtn", "versionLabel", "checkUpdateBtn",
].forEach((id) => (el[id] = document.getElementById(id)));

// ============================================================
// Boot — wait until the Python methods are genuinely attached
// ============================================================
function apiReady() {
  return window.pywebview && window.pywebview.api && typeof window.pywebview.api.get_profile === "function";
}

function waitForApi() {
  if (apiReady()) {
    init();
  } else {
    setTimeout(waitForApi, 50);
  }
}

waitForApi();

async function init() {
  bindEvents();
  const v = await window.pywebview.api.get_app_version();
  el.versionLabel.textContent = "v" + v.version;
  await loadProfile();
  // Fired together instead of one after another — faster startup.
  await Promise.allSettled([refreshHistory(), refreshNetworkStatus(), refreshTtsStatus()]);
  // Slow poll, and paused entirely while the window is hidden/minimised.
  setInterval(() => { if (!document.hidden) refreshNetworkStatus(); }, 15000);
}

async function loadProfile() {
  const profile = await window.pywebview.api.get_profile();
  if (!profile.name) {
    openNameModal(false);
    return;
  }
  applyName(profile.name);
}

function applyName(name) {
  state.userName = name;
  el.welcomeName.textContent = name;
  el.avatarBtn.textContent = initials(name);
  el.avatarName.textContent = name;
}

function initials(name) {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0][0].toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

// ============================================================
// Event bindings
// ============================================================
function bindEvents() {
  el.newChatBtn.addEventListener("click", startNewChat);
  el.newFromInputBtn.addEventListener("click", startNewChat);

  el.sendBtn.addEventListener("click", handleSend);
  el.messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  });
    el.messageInput.addEventListener("input", () => {
    el.sendBtn.disabled = state.isStreaming || !el.messageInput.value.trim();
    el.messageInput.style.height = "auto";
    el.messageInput.style.height = el.messageInput.scrollHeight + "px";
  });

  document.querySelectorAll(".suggestion-card").forEach((card) => {
    card.addEventListener("click", () => {
      el.messageInput.value = card.dataset.suggestion;
      handleSend();
    });
  });

  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.soon) {
        notify(`${btn.dataset.soon} isn't built yet — coming in a future update.`);
        return;
      }
      document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      if (btn.dataset.view === "home") startNewChat();
      if (btn.dataset.view === "chat" && window.innerWidth <= 720) el.sidebar.classList.add("open");
    });
  });
  document.getElementById("settingsNavBtn").addEventListener("click", openSettingsModal);

  el.bellBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    closeDropdowns(["notifDropdown"]);
    el.notifDropdown.classList.toggle("open");
  });

  el.avatarBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    closeDropdowns(["avatarDropdown"]);
    el.avatarDropdown.classList.toggle("open");
  });
  el.renameBtn.addEventListener("click", () => {
    el.avatarDropdown.classList.remove("open");
    openNameModal(true);
  });
  el.openSettingsBtn.addEventListener("click", () => {
    el.avatarDropdown.classList.remove("open");
    openSettingsModal();
  });

  document.addEventListener("click", () => closeDropdowns(["notifDropdown", "avatarDropdown", "toolsDropdown"]));

  el.winMinimize.addEventListener("click", () => window.pywebview.api.minimize_window());
  el.winMaximize.addEventListener("click", () => window.pywebview.api.toggle_maximize_window());
  el.winClose.addEventListener("click", () => window.pywebview.api.close_window());

  el.webSearchToggle.addEventListener("click", async () => {
    const current = await window.pywebview.api.get_network_status();
    const result = await window.pywebview.api.set_network_override(!current.manual_offline_override);
    renderWebSearchChip(result);
  });

  el.toolsBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    closeDropdowns(["toolsDropdown"]);
    el.toolsDropdown.classList.toggle("open");
  });
  el.micBtn.addEventListener("click", () => window.pywebview.api.manual_listen());
  el.wakeToggle.addEventListener("click", toggleWake);
  el.ttsToggleItem.addEventListener("click", toggleTts);

  el.nameSaveBtn.addEventListener("click", saveNameFromModal);
  el.nameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") saveNameFromModal(); });

  el.settingsRenameBtn.addEventListener("click", () => { closeSettingsModal(); openNameModal(true); });
  el.settingsTtsToggle.addEventListener("click", toggleTts);
  el.settingsWakeToggle.addEventListener("click", toggleWake);
  el.settingsRebuildBtn.addEventListener("click", async () => {
    el.settingsRebuildBtn.textContent = "Rebuilding…";
    await window.pywebview.api.rebuild_index();
    // The button text resets when the "index" event arrives — the rebuild
    // runs in the background so it can never freeze the window.
  });
  el.settingsCloseBtn.addEventListener("click", closeSettingsModal);

  el.checkUpdateBtn.addEventListener("click", async () => {
    el.checkUpdateBtn.textContent = "Checking…";
    const info = await window.pywebview.api.check_for_updates();
    el.checkUpdateBtn.textContent = "Check for updates";
    if (!info.available) notify("You're on the latest version.");
  });
  el.updateBannerBtn.addEventListener("click", () => window.pywebview.api.start_update());
}

async function toggleWake() {
  if (state.wakeEnabled) await window.pywebview.api.stop_wake_word();
  else await window.pywebview.api.start_wake_word();
}

async function toggleTts() {
  const result = await window.pywebview.api.set_tts(!state.ttsEnabled);
  applyTts(result.tts_enabled);
}

function closeDropdowns(ids) {
  ids.forEach((id) => el[id].classList.remove("open"));
}

// ============================================================
// Events pushed FROM Python
// ============================================================
window.nelaHandleEvent = function (payload) {
  if (payload.channel === "chat") handleChatEvent(payload);
  else if (payload.channel === "voice") handleVoiceEvent(payload);
  else if (payload.channel === "index") handleIndexEvent(payload);
  else if (payload.channel === "update") handleUpdateEvent(payload);
};

function handleUpdateEvent(payload) {
  if (payload.available) {
    el.updateBanner.style.display = "flex";
    el.updateBannerText.textContent = `Nela v${payload.version} is available.`;
  }
  if (payload.downloading) el.updateBannerText.textContent = "Downloading update…";
  if (typeof payload.progress === "number") el.updateBannerText.textContent = `Downloading update… ${Math.round(payload.progress * 100)}%`;
  if (payload.installing) el.updateBannerText.textContent = "Installing — Nela will restart…";
  if (payload.error) { el.updateBanner.style.display = "none"; showError(payload.error); }
}

function handleIndexEvent(payload) {
  el.settingsRebuildBtn.textContent = "Rebuild";
  notify(`Indexed ${payload.apps_indexed} apps and ${payload.files_indexed} files.`);
}

function handleChatEvent(payload) {
  if (payload.chat_id) state.chatId = payload.chat_id;  // keeps one conversation, one history entry
  if (payload.error) {
    // Keep whatever was already written, then show the problem.
    if (state.streamFullText) {
      state.messages.push({ role: "assistant", content: state.streamFullText });
    }
    state.streamFullText = "";
    state.streamRevealed = "";
    state.doneReceived = false;
    stopStreamingUI();
    showError(payload.error);
    renderChat();
    return;
  }

  if (payload.status === "done") {
    state.doneReceived = true;
    maybeFinalize();
    if (state.isStreaming) startTypewriter();  // still catching up — let it finish
    return;
  }

  if (payload.status) {
    state.status = payload.status;
    state.tool = payload.tool || null;
    if (state.streamRevealed.length === 0) renderChat();  // update the "Thinking… / Opening…" label
    return;
  }

  if (payload.delta) {
    // Only record the text. The typewriter (below) does the drawing, so
    // a burst of network chunks never causes a burst of re-rendering.
    state.streamFullText += payload.delta;
    startTypewriter();
  }
}

function handleVoiceEvent(payload) {
  const s = payload.state;

  if (s === "wake_enabled") state.wakeEnabled = true;
  if (s === "wake_disabled") state.wakeEnabled = false;
  renderWakeUI();

  if (s === "transcript") {
    el.messageInput.value = el.messageInput.value ? `${el.messageInput.value} ${payload.text}` : payload.text;
    el.sendBtn.disabled = state.isStreaming || !el.messageInput.value.trim();
    hideVoiceOverlay();
    return;
  }
  if (s === "error") {
    showError(payload.message || "Voice error");
    hideVoiceOverlay();
    return;
  }
  if (s === "idle") {
    hideVoiceOverlay();
    return;
  }
  if (["wake_detected", "listening", "transcribing"].includes(s)) {
    showVoiceOverlay(s);
  }
}

// ============================================================
// Sending messages
// ============================================================
async function handleSend() {
  const text = el.messageInput.value.trim();
  if (!text || state.isStreaming) return;

  state.messages.push({ role: "user", content: text });
  state.streamFullText = "";
  state.streamRevealed = "";
  state.status = "thinking";
  state.tool = null;
  state.isStreaming = true;
  state.doneReceived = false;

  el.messageInput.value = "";
  el.sendBtn.disabled = true;
  hideError();
  renderChat(true);

  const historyForModel = state.messages.slice(0, -1).map((m) => ({ role: m.role, content: m.content }));
  let result = null;
  try {
    result = await window.pywebview.api.send_message(text, historyForModel, state.chatId);
  } catch (e) {
    result = null;
  }
  if (!result || !result.started) {
    // Undo the optimistic user bubble so a failed send doesn't look like a sent one.
    state.messages.pop();
    stopStreamingUI();
    showError((result && result.reason) || "Couldn't reach Nela's backend.");
    el.messageInput.value = text;
    el.sendBtn.disabled = false;
    renderChat();
  }
}

function stopStreamingUI() {
  state.isStreaming = false;
  state.status = null;
  state.tool = null;
  stopTypewriter();
  el.sendBtn.disabled = !el.messageInput.value.trim();
}

// ============================================================
// Typewriter — reveals text on a steady beat, updating ONE text node.
// The timer only exists while there is text left to reveal.
// ============================================================
const TICK_MS = 30;

function startTypewriter() {
  if (typewriterTimer) return;
  typewriterTimer = setInterval(typewriterTick, TICK_MS);
}

function stopTypewriter() {
  if (typewriterTimer) clearInterval(typewriterTimer);
  typewriterTimer = null;
}

function typewriterTick() {
  const full = state.streamFullText;
  const shown = state.streamRevealed.length;

  if (shown >= full.length) {
    // Caught up. Nothing more to draw until the next chunk arrives.
    stopTypewriter();
    maybeFinalize();
    return;
  }

  // Speed adapts to the backlog: a quick reply types out at a readable pace,
  // a long one speeds up so you're never waiting on the animation.
  const backlog = full.length - shown;
  const step = Math.max(6, Math.ceil(backlog / 60));
  state.streamRevealed = full.slice(0, shown + step);

  if (streamBubble) {
    const stick = isNearBottom();
    streamBubble.textNode.nodeValue = state.streamRevealed;
    if (stick) scrollToBottom();
  } else {
    renderChat();  // first characters: swap the "Thinking…" dots for the real bubble
  }
}

function maybeFinalize() {
  if (!state.doneReceived) return;
  if (state.streamRevealed.length < state.streamFullText.length) return;  // typewriter still going

  if (state.streamFullText) {
    state.messages.push({ role: "assistant", content: state.streamFullText });
  }
  state.streamFullText = "";
  state.streamRevealed = "";
  state.doneReceived = false;
  stopStreamingUI();
  refreshHistory();
  renderChat();
}

// ============================================================
// Chat + history
// ============================================================
function startNewChat() {
  state.chatId = null;
  state.messages = [];
  state.streamFullText = "";
  state.streamRevealed = "";
  state.doneReceived = false;
  stopStreamingUI();
  hideError();
  renderChat();
  document.querySelectorAll(".history-item").forEach((i) => i.classList.remove("active"));
}

async function selectChat(chatId) {
  if (state.isStreaming) return;  // don't yank the conversation out from under a live reply
  hideError();
  const chat = await window.pywebview.api.get_chat(chatId);
  if (!chat) { showError("Couldn't load that conversation."); return; }
  state.chatId = chat.chat_id;
  state.messages = chat.messages;
  state.streamFullText = "";
  state.streamRevealed = "";
  renderChat(true);
  refreshHistory();
}

async function deleteChatItem(chatId, evt) {
  evt.stopPropagation();
  await window.pywebview.api.delete_chat(chatId);
  if (state.chatId === chatId) startNewChat();
  refreshHistory();
}

async function refreshHistory() {
  const chats = await window.pywebview.api.get_history();
  const frag = document.createDocumentFragment();

  if (!chats || chats.length === 0) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = "No conversations yet.";
    frag.appendChild(empty);
  } else {
    chats.forEach((chat) => {
      const item = document.createElement("div");
      item.className = "history-item" + (chat.chat_id === state.chatId ? " active" : "");

      const title = document.createElement("span");
      title.className = "history-item-title";
      title.textContent = chat.title;  // textContent: no HTML injection from chat titles
      title.addEventListener("click", () => selectChat(chat.chat_id));

      const del = document.createElement("span");
      del.className = "history-item-delete";
      del.title = "Delete";
      del.textContent = "✕";
      del.addEventListener("click", (e) => deleteChatItem(chat.chat_id, e));

      item.appendChild(title);
      item.appendChild(del);
      frag.appendChild(item);
    });
  }
  el.historyList.replaceChildren(frag);
}

// ============================================================
// Rendering — full rebuild only on structural changes; token-by-token
// updates go through typewriterTick() instead.
// ============================================================
function isNearBottom() {
  const c = el.chatWindow;
  return c.scrollHeight - c.scrollTop - c.clientHeight < 80;
}

function scrollToBottom() {
  el.chatWindow.scrollTop = el.chatWindow.scrollHeight;
}

function renderChat(forceScroll) {
  const stick = forceScroll || isNearBottom();
  const hasContent = state.messages.length > 0 || state.isStreaming;
  el.homeView.style.display = hasContent ? "none" : "flex";
  el.chatWindow.style.display = hasContent ? "flex" : "none";

  const frag = document.createDocumentFragment();
  state.messages.forEach((m) => frag.appendChild(renderBubble(m.role, m.content, false)));

  streamBubble = null;
  if (state.isStreaming) {
    if (state.streamRevealed.length === 0) {
      if (state.status) frag.appendChild(renderTypingIndicator());
    } else {
      const row = renderBubble("assistant", state.streamRevealed, true);
      streamBubble = { textNode: row._textNode };
      frag.appendChild(row);
    }
  }

  el.chatWindow.replaceChildren(frag);
  if (stick) scrollToBottom();
}

function renderBubble(role, content, withCursor) {
  const row = document.createElement("div");
  row.className = `bubble-row ${role}`;
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  const textNode = document.createTextNode(content);
  bubble.appendChild(textNode);
  if (withCursor) {
    const cursor = document.createElement("span");
    cursor.className = "cursor-blink";
    bubble.appendChild(cursor);
  }
  row.appendChild(bubble);
  row._textNode = textNode;
  return row;
}

const TOOL_LABELS = {
  open_app: "Opening that for you", close_app: "Closing that for you",
  open_path: "Finding that file", read_text_file: "Reading that file",
  read_pdf: "Reading that PDF", open_settings: "Opening settings",
  take_screenshot_snip: "Opening screen snip", set_wifi: "Changing Wi-Fi",
  web_search: "Searching the web", speak: "Getting ready to speak",
};

function renderTypingIndicator() {
  const wrap = document.createElement("div");
  wrap.className = "bubble-row assistant";
  let label = "Thinking…";
  if (state.status === "acting") {
    label = state.tool && TOOL_LABELS[state.tool] ? `${TOOL_LABELS[state.tool]}…` : "Working on it…";
  }
  // label comes only from the fixed map above, never from user/model text
  wrap.innerHTML = `<div class="typing-indicator"><span class="typing-dots"><span></span><span></span><span></span></span>${label}</div>`;
  return wrap;
}

// ============================================================
// Toggles & status
// ============================================================
function renderWakeUI() {
  el.wakeToggle.textContent = state.wakeEnabled ? '✨ "Hey Nela": On' : '✨ Enable "Hey Nela"';
  el.wakeToggle.classList.toggle("on", state.wakeEnabled);
  el.settingsWakeToggle.classList.toggle("on", state.wakeEnabled);
}

function applyTts(enabled) {
  state.ttsEnabled = enabled;
  el.ttsToggleItem.textContent = enabled ? "🔊 Speak replies: On" : "🔇 Speak replies: Off";
  el.ttsToggleItem.classList.toggle("on", enabled);
  el.settingsTtsToggle.classList.toggle("on", enabled);
}

async function refreshTtsStatus() {
  const status = await window.pywebview.api.get_tts_status();
  applyTts(status.tts_enabled);
}

function renderWebSearchChip(status) {
  el.webSearchToggle.classList.remove("on", "forced-off");
  if (status.manual_offline_override) {
    el.webSearchToggle.classList.add("forced-off");
    el.webSearchToggle.textContent = "🌐 Web Search: Off";
  } else if (status.effective_online) {
    el.webSearchToggle.classList.add("on");
    el.webSearchToggle.textContent = "🌐 Web Search: On";
  } else {
    el.webSearchToggle.textContent = "🌐 Web Search (offline)";
  }
}

async function refreshNetworkStatus() {
  const status = await window.pywebview.api.get_network_status();
  renderWebSearchChip(status);
}

// ============================================================
// Voice overlay
// ============================================================
function showVoiceOverlay(voiceState) {
  el.voiceOverlay.classList.add("visible");
  el.micBtn.classList.add("active");
  const labels = { wake_detected: "Heard you — go ahead", listening: "Listening…", transcribing: "Working out what you said…" };
  el.voiceLabel.textContent = labels[voiceState] || "";
  let ringsHtml = "";
  if (voiceState === "listening") ringsHtml = '<div class="voice-ring ping"></div><div class="voice-ring ping2"></div>';
  else if (voiceState === "transcribing") ringsHtml = '<div class="voice-ring spin"></div>';
  else if (voiceState === "wake_detected") ringsHtml = '<div class="voice-ring pulse"></div>';
  el.voiceRingWrap.innerHTML = ringsHtml + '<div class="voice-core"></div>';
}

function hideVoiceOverlay() {
  el.voiceOverlay.classList.remove("visible");
  el.micBtn.classList.remove("active");
  el.voiceRingWrap.innerHTML = '<div class="voice-core"></div>';  // stop the animated rings completely
}

// ============================================================
// Modals
// ============================================================
function openNameModal(isRename) {
  el.nameInput.value = isRename ? state.userName || "" : "";
  el.nameModal.classList.add("visible");
  setTimeout(() => el.nameInput.focus(), 50);
}

async function saveNameFromModal() {
  const name = el.nameInput.value.trim();
  if (!name) return;
  await window.pywebview.api.set_profile_name(name);
  applyName(name);
  el.nameModal.classList.remove("visible");
}

function openSettingsModal() { el.settingsModal.classList.add("visible"); }
function closeSettingsModal() { el.settingsModal.classList.remove("visible"); }

// ============================================================
// Notifications (bell) — shows your most recent real system message
// ============================================================
function notify(message) {
  el.notifContent.textContent = message;
  el.notifContent.classList.remove("notif-empty");
}

// ============================================================
// Misc
// ============================================================
function showError(message) {
  el.errorBanner.textContent = message;
  el.errorBanner.style.display = "block";
  notify(message);
}
function hideError() { el.errorBanner.style.display = "none"; }
