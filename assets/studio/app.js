"use strict";
const $ = (selector) => document.querySelector(selector);
const state = {
  token: "",
  config: null,
  id: null,
  detail: null,
  words: [],
  page: 0,
  candidatePage: 0,
  busy: false,
  tab: "clips",
  selected: new Set(),
  noticeTimer: null,
  captionDraft: null,
  captionKey: "",
  captionSerial: 0,
  captionPreset: "default",
  previewFile: null,
  pendingPreview: null,
};
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const basename = (source) =>
  source.split(/[\\/]/).filter(Boolean).pop() || source;
const projectName = (source) =>
  /^https?:/.test(source)
    ? source.replace(/^https?:\/\//, "").slice(0, 65)
    : basename(source);
// ----------------------------------------------------- dialogs (Phase 3)
// One accessible modal for confirmations and simple questions; replaces the
// browser's alert/confirm/prompt. Resolves to true / the typed text, or null.
function askDialog({ title, body = "", html = "", confirmText = "OK", danger = false,
  input = null, validate = null }) {
  const dialog = $("#app-dialog");
  $("#app-dialog-title").textContent = title;
  const content = $("#app-dialog-body");
  if (html) content.innerHTML = html;
  else content.textContent = body;
  const field = $("#app-dialog-field");
  const box = $("#app-dialog-input");
  field.hidden = !input;
  if (input) {
    $("#app-dialog-label").textContent = input.label;
    box.type = input.type || "text";
    box.value = input.value ?? "";
    box.placeholder = input.placeholder || "";
  }
  const error = $("#app-dialog-error");
  error.hidden = true;
  const ok = $("#app-dialog-ok");
  ok.textContent = confirmText;
  ok.classList.toggle("danger", danger);
  return new Promise((resolve) => {
    const submit = (event) => {
      if (event.submitter?.value !== "ok") return;
      const value = input ? box.value.trim() : true;
      const problem = validate ? validate(value) : "";
      if (problem) {
        event.preventDefault();
        error.textContent = problem;
        error.hidden = false;
      }
    };
    dialog.querySelector("form").addEventListener("submit", submit);
    dialog.addEventListener(
      "close",
      () => {
        dialog.querySelector("form").removeEventListener("submit", submit);
        resolve(dialog.returnValue === "ok" ? (input ? box.value.trim() : true) : null);
      },
      { once: true },
    );
    dialog.returnValue = "";
    dialog.showModal();
    (input ? box : ok).focus();
  });
}

function notice(message, error = false) {
  clearTimeout(state.noticeTimer);
  const box = $("#notice");
  box.hidden = false;
  box.textContent = message;
  box.classList.toggle("error", error);
  state.noticeTimer = setTimeout(
    () => {
      box.hidden = true;
    },
    error ? 15000 : 6000,
  );
}
// Every request has a time limit and a readable error. A request the
// browser could not deliver (Studio stopped, network) or that took too long
// never fails silently.
const API_TIMEOUT_MS = 30000;
class ApiError extends Error {
  constructor(message, { status = 0, requestId = null, kind = "http" } = {}) {
    super(message);
    this.status = status;
    this.requestId = requestId;
    this.kind = kind;
  }
}
async function api(path, data, { timeout = state.apiTimeout || API_TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  let response;
  try {
    response = await fetch(
      path,
      data === undefined
        ? { signal: controller.signal }
        : {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Chopify-Token": state.token,
            },
            body: JSON.stringify(data),
            signal: controller.signal,
          },
    );
  } catch (error) {
    if (error.name === "AbortError")
      throw new ApiError(
        `Studio did not answer within ${Math.round(timeout / 1000)} seconds. ` +
          "It may be busy or stopped; check the Studio terminal, then try again.",
        { kind: "timeout" },
      );
    throw new ApiError(
      "Cannot reach Chopify Studio. Check that it is still running " +
        "(python dashboard.py), then reload this page.",
      { kind: "network" },
    );
  } finally {
    clearTimeout(timer);
  }
  const requestId = response.headers.get("X-Request-ID");
  let result = null;
  try {
    result = await response.json();
  } catch (error) {
    result = null;
  }
  if (!response.ok || result === null) {
    const reason =
      (result && result.error) ||
      `Studio answered with HTTP ${response.status} and no details.`;
    const ref = requestId && response.status >= 500 ? ` (request ${requestId})` : "";
    throw new ApiError(reason + ref, { status: response.status, requestId });
  }
  return result;
}
function safe(handler) {
  return async (event) => {
    try {
      await handler(event);
    } catch (error) {
      notice(error.message, true);
    }
  };
}
function newProjectOptions() {
  return {
    aspect: $("#new-aspect").value,
    style: $("#new-style").value,
    profile: $("#new-profile").value,
    device: $("#new-device").value,
    subtitles: $("#new-subtitles").value,
    whisper_model: $("#new-model").value || null,
  };
}

// What the create form will do, before anything runs: profile, model,
// captions and the device that will really be used.
function showProcessingMode() {
  const out = $("#processing-mode");
  if (!out || !state.config) return;
  const profiles = state.config.profiles || {};
  const profile = profiles[$("#new-profile").value] || {};
  const model = $("#new-model").value || profile.model || "medium";
  const gpu = state.config.gpu || {};
  const device = $("#new-device").value;
  let where;
  let warn = false;
  if (device === "cpu") where = "CPU (chosen)";
  else if (gpu.usable) where = "GPU";
  else if (device === "cuda") {
    where = `GPU requested, but ${gpu.reason}: the job will stop with an error`;
    warn = true;
  } else {
    where = `CPU, because ${gpu.reason} (several times slower)`;
    warn = true;
  }
  const captionsOn = $("#new-subtitles").value === "auto" && profile.captions !== "off";
  const how = captionsOn
    ? `${profile.speed}. If the captions are missing or fail the checks, Whisper ${model} transcribes the whole video.`
    : `Whisper ${model} transcribes the whole video.`;
  out.textContent = `${profile.label || "Profile"} on ${where}: ${how}`;
  out.classList.toggle("warn", warn);
}
for (const id of ["#new-profile", "#new-device", "#new-subtitles", "#new-model"])
  $(id)?.addEventListener("change", showProcessingMode);

function showCompliance() {
  const note = $("#compliance-note");
  const info = state.config?.compliance;
  if (!note || !info) return;
  note.hidden = !info.message;
  note.textContent = info.message || "";
}

async function config() {
  state.config = await api("/api/config");
  state.token = state.config.token;
  showCompliance();
  showProcessingMode();
  updatePresets();
  updateBrands();
  renderCaptionGallery();
}
function presetNames() {
  return Object.keys(state.config.caption_presets || {});
}
function updatePresets() {
  const markup = presetNames()
    .map((name) => `<option>${escapeHTML(name)}</option>`)
    .join("");
  for (const select of [
    $("#new-style"),
    $("#caption-preset"),
    $("#settings-form").elements.style,
  ]) {
    if (!select) continue;
    const current = select.value;
    select.innerHTML = markup;
    if (presetNames().includes(current)) select.value = current;
  }
}
function aspectCaptionBase(aspect) {
  return (
    state.config.caption_aspects[aspect] ||
    state.config.caption_aspects["16:9"] || { font_size: 72, margin: 95 }
  );
}
function brandCaption(name) {
  return (state.config.caption_brands || {})[name] || {};
}
function savedCaptionStyle() {
  const options = state.detail.project.options;
  const brand = options.brand
    ? basename(options.brand).replace(/\.json$/, "")
    : "";
  const base = aspectCaptionBase(options.aspect);
  const merged = {
    ...state.config.caption_defaults,
    ...(state.config.caption_presets[options.style] || {}),
    ...brandCaption(brand),
    ...(options.caption || {}),
  };
  if (merged.font_size == null) merged.font_size = base.font_size;
  if (merged.margin == null) merged.margin = base.margin;
  return merged;
}
function captionFromForm() {
  const form = $("#caption-form");
  const value = (name) => form.elements[name].value;
  return {
    font: value("font").trim() || "Arial Black",
    font_size: Number(value("font_size")),
    color: value("color"),
    highlight: value("highlight"),
    emphasis_color: value("emphasis_color"),
    outline_color: value("outline_color"),
    uppercase: form.elements.uppercase.checked,
    words_per_cue: Number(value("words_per_cue")),
    position: value("position"),
    margin: Number(value("margin")),
    outline: Number(value("outline")),
    shadow: Number(value("shadow")),
    background: value("background"),
    background_color: value("background_color"),
    background_opacity: Number(value("background_opacity")),
    animation: value("animation"),
    highlight_mode: value("highlight_mode"),
    emoji: form.elements.emoji.checked,
    emphasis: form.elements.emphasis.checked,
  };
}
function fillCaptionForm(style, preset) {
  const form = $("#caption-form");
  for (const [key, value] of Object.entries(style)) {
    const input = form.elements.namedItem(key);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = !!value;
    else input.value = value ?? "";
  }
  $("#caption-preset").value = preset;
  state.captionDraft = captionFromForm();
  state.captionSerial++;
  state.captionKey = "";
  updateCaptionSummary();
  updateCaptionOverlay();
}
function syncCaption() {
  if (!state.detail) return;
  state.captionDraft = captionFromForm();
  state.captionSerial++;
  state.captionKey = "";
  updateCaptionSummary();
  updateCaptionOverlay();
}
function updateCaptionSummary() {
  const style = state.captionDraft;
  if (!style) return;
  $("#caption-summary").textContent =
    `${style.words_per_cue} words · ${style.position} · ` +
    `${style.animation === "none" ? "no motion" : style.animation}`;
}
function renderCaptionGallery() {
  const wrap = $("#caption-presets");
  if (!wrap) return;
  wrap.innerHTML = presetNames()
    .map(
      (name) =>
        `<button type="button" class="caption-preset" data-caption-preset="${escapeHTML(name)}" aria-pressed="false">${escapeHTML(name)}</button>`,
    )
    .join("");
  for (const button of wrap.querySelectorAll("[data-caption-preset]")) {
    const preset = state.config.caption_presets[button.dataset.captionPreset];
    button.style.setProperty("--preset-font", preset.font);
    button.style.setProperty("--preset-color", preset.color);
    button.style.setProperty("--preset-highlight", preset.highlight);
  }
  markCaptionPreset(state.captionPreset);
}
function markCaptionPreset(name) {
  state.captionPreset = name;
  for (const button of document.querySelectorAll("[data-caption-preset]"))
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.captionPreset === name),
    );
}
function applyCaptionPreset(name) {
  if (!state.detail || !state.config.caption_presets[name]) return;
  const base = aspectCaptionBase(state.detail.project.options.aspect);
  const style = { ...state.config.caption_defaults, ...state.config.caption_presets[name] };
  if (style.font_size == null) style.font_size = base.font_size;
  if (style.margin == null) style.margin = base.margin;
  fillCaptionForm(style, name);
  $("#settings-form").elements.style.value = name;
  markCaptionPreset(name);
}
function hexToRgba(hex, opacity) {
  const match = /^#([0-9a-f]{6})$/i.exec(String(hex));
  if (!match) return "rgba(0, 0, 0, 0.7)";
  const value = parseInt(match[1], 16);
  const alpha = Math.max(0, Math.min(100, Number(opacity) || 0)) / 100;
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
}
function isEmphasizedWord(text) {
  const plain = String(text)
    .trim()
    .replace(/^[^\w$€£¥]+|[^\w%$€£¥]+$/g, "");
  if (!plain) return false;
  if (/[\d$€£¥]/.test(plain)) return true;
  if (plain.length > 2 && plain === plain.toUpperCase() && /[A-Z]/.test(plain))
    return true;
  return state.config.caption_power_words.includes(
    plain.toLowerCase().replace(/[.,!?;:'"]+$/, ""),
  );
}
function emojiForWord(text) {
  const token = String(text)
    .toLowerCase()
    .replace(/[^a-z0-9']/g, "");
  if (!token) return "";
  for (const rule of state.config.caption_emoji_rules || [])
    if (rule.words.some((stem) => token.startsWith(stem))) return rule.emoji;
  return "";
}
function updateCaptionOverlay(force = false) {
  const overlay = $("#caption-overlay"),
    player = $("#source-player"),
    style = state.captionDraft;
  if (
    !state.detail ||
    !state.detail.transcript ||
    state.detail.playable_source === false ||
    state.previewFile ||
    state.tab !== "captions" ||
    !style
  ) {
    overlay.hidden = true;
    return;
  }
  const words = state.words.filter((item) => !item.hidden);
  const per = Math.max(1, Math.min(8, Number(style.words_per_cue) || 3));
  const current = player.currentTime || 0;
  let cue = null,
    cueIndex = -1;
  for (let i = 0; i < words.length; i += per) {
    const group = words.slice(i, i + per);
    if (
      group.length &&
      current >= group[0].start &&
      current <= group[group.length - 1].end
    ) {
      cue = group;
      cueIndex = i;
      break;
    }
  }
  if (!cue) {
    overlay.hidden = true;
    return;
  }
  let active = 0;
  for (let i = 0; i < cue.length; i++) if (current >= cue[i].start) active = i;
  const marked = [];
  for (let i = 0; i < cue.length; i++)
    if (cue[i].emphasis === true) marked.push(i);
  if (style.emphasis)
    for (let i = 0; i < cue.length; i++)
      if (!marked.includes(i) && isEmphasizedWord(cue[i].word)) marked.push(i);
  const emphasised = new Set(marked.slice(0, 2));
  let emojiIndex = -1,
    emoji = "";
  if (style.emoji) {
    for (let i = 0; i < cue.length; i++) {
      emoji = emojiForWord(cue[i].word);
      if (emoji) {
        emojiIndex = i;
        break;
      }
    }
  }
  const key = `${cueIndex}:${active}:${state.captionSerial}`;
  if (state.captionKey !== key || force) {
    const parts = [];
    cue.forEach((item, i) => {
      if (style.animation === "typewriter" && i > active) return;
      let token = escapeHTML(
        style.uppercase ? item.word.toUpperCase() : item.word,
      );
      if (i === emojiIndex) token += ` <span class="caption-emoji">${emoji}</span>`;
      let className = "caption-word";
      if (style.highlight_mode !== "none" && i === active)
        className += " is-active";
      else if (style.highlight_mode === "karaoke" && i < active)
        className += " is-karaoke";
      else if (style.highlight_mode !== "none" && emphasised.has(i))
        className += " is-emphasis";
      if (
        i === active &&
        style.animation !== "none" &&
        style.animation !== "typewriter"
      )
        className += ` is-${style.animation}`;
      parts.push(`<span class="${className}">${token}</span>`);
    });
    $("#caption-cue").innerHTML = parts.join(" ");
    state.captionKey = key;
  }
  const wrap = $(".player-wrap");
  const scale = Math.max(0.04, (wrap.clientHeight || 1) / 1080);
  const set = (name, value) => overlay.style.setProperty(name, value);
  overlay.className =
    `caption-overlay caption-${style.position}` +
    (style.background === "box" ? " caption-boxed" : "");
  set("--caption-font", `${style.font}, "Segoe UI Emoji", sans-serif`);
  set("--caption-font-size", `${Math.max(8, style.font_size * scale)}px`);
  set("--caption-margin", `${Math.max(0, style.margin * scale)}px`);
  set("--caption-outline", `${Math.max(0, style.outline * scale)}px`);
  set("--caption-shadow", `${Math.max(0, style.shadow * scale)}px`);
  set("--caption-color", style.color);
  set("--caption-highlight", style.highlight);
  set("--caption-emphasis", style.emphasis_color);
  set("--caption-outline-color", style.outline_color);
  set(
    "--caption-box-bg",
    hexToRgba(style.background_color, style.background_opacity),
  );
  set("--caption-box-pad", `${Math.max(2, style.outline * scale + 2)}px`);
  overlay.hidden = false;
}
function updateBrands(selected = "") {
  $("#brand-select").innerHTML =
    '<option value="">Use caption style</option>' +
    state.config.brands
      .map(
        (name) =>
          `<option value="${escapeHTML(name)}">${escapeHTML(name)}</option>`,
      )
      .join("");
  $("#brand-select").value = selected;
}
async function refreshProjects() {
  const result = await api("/api/projects");
  $("#project-count").textContent = result.projects.length;
  renderSourceSlots(result.scheduler);
  const markup = result.projects.length
    ? result.projects
        .map(
          (p) =>
            `<div class="project-row"><button class="project-item ${p.id === state.id ? "active" : ""}" data-id="${p.id}" title="${escapeHTML(p.source || "")}"><strong>${escapeHTML(p.display_title || projectName(p.source))}</strong><small>${escapeHTML(p.status_label || `${p.status} · ${p.stage}`)}</small></button><button type="button" class="kebab" data-menu-for="${p.id}" aria-haspopup="menu" aria-label="Actions for ${escapeHTML(p.display_title || "project")}">⋯</button></div>`,
        )
        .join("")
    : '<p class="muted">Add a recording to begin.</p>';
  // Preserve keyboard focus while polling unchanged projects.
  if (state.projectMarkup !== markup) {
    $("#projects").innerHTML = markup;
    state.projectMarkup = markup;
  }
}
async function selectProject(id) {
  state.id = id;
  state.page = 0;
  state.candidatePage = 0;
  state.tab = "clips";
  state.captionDraft = null;
  state.captionKey = "";
  state.previewFile = null;
  state.pendingPreview = null;
  $("#new-view").hidden = true;
  $("#dashboard-view").hidden = true;
  state.view = "project";
  if (location.hash) history.replaceState(null, "", location.pathname);
  $("#project-view").hidden = false;
  const detail = await api(`/api/projects/${id}`);
  if (state.id !== id) return;
  state.detail = detail;
  state.words = structuredClone(state.detail.transcript?.words || []);
  state.selected = new Set(state.detail.candidates.map((_, i) => i + 1));
  renderDetail();
  await refreshProjects();
  await loadYouTube();
  await loadConsent().catch(() => {});
}
function updateStatus(detail) {
  const progress = detail.progress || {};
  const total = Number(progress.total);
  const progressText =
    detail.busy && progress.stage && Number.isFinite(total) && total > 0
      ? ` · ${progress.stage} ${Number(progress.done) || 0}/${total}`
      : "";
  $("#status").textContent =
    `${detail.status_label || `${detail.project.status} · ${detail.project.stage}`}${progressText}`;
  renderClipCounts(detail);
  const errors = detail.render_errors || [];
  const error =
    detail.project.error ||
    (errors.length ? `${errors.length} clip(s) failed to render.` : "");
  const diagnosis = detail.diagnosis;
  const failedText =
    diagnosis && error
      ? `Failed at ${diagnosis.stage}: ${diagnosis.problem}` +
        (diagnosis.problem !== error ? ` — ${error}` : "")
      : error;
  $("#job-error").hidden = !failedText;
  $("#job-error").textContent = failedText;
  $("#job-advice").hidden = !(diagnosis && diagnosis.advice);
  $("#job-advice").textContent = diagnosis?.advice ? `What to do: ${diagnosis.advice}` : "";
  renderActivity(detail);
  $("#log").textContent = detail.log || "No job has run yet.";
  // Keep polling while the saved status still says work is pending, even if
  // the job lane reported idle at that instant (the status catches up).
  state.busy =
    detail.busy || ["queued", "running"].includes(detail.project?.status);
  $("#cancel-job").hidden = !detail.busy;
  renderRecording(detail);
  for (const selector of [
    "#prepare",
    "#preview",
    "#render",
    "#save-candidates",
    "#save-transcript",
  ])
    $(selector).disabled = detail.busy;
  for (const el of document.querySelectorAll(
    "#settings-form button, #caption-form button, #caption-preview",
  ))
    el.disabled = detail.busy;
  for (const el of document.querySelectorAll(
    "#settings-form input, #settings-form select, #candidates input, #transcript-words input, #caption-form input, #caption-form select, #caption-presets button",
  ))
    el.disabled = detail.busy;
}
function renderActivity(detail) {
  const activity = detail.activity;
  const box = $("#job-status");
  if (!activity) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const status = detail.project.status;
  $("#job-current").textContent =
    activity.current ||
    (status === "failed"
      ? "Stopped (see the error below)"
      : status === "cancelled"
        ? "Cancelled"
        : status === "interrupted"
          ? "Interrupted (press Retry on the dashboard or Find / refresh clips)"
          : "Idle");
  $("#job-last").textContent = activity.last_completed || "Nothing yet";
  const bar = $("#job-progress");
  const fraction = activity.progress?.fraction;
  bar.hidden = !(detail.busy && typeof fraction === "number");
  if (!bar.hidden) bar.value = Math.round(fraction * 100);
  renderTranscription(detail.project.transcription);
}
// Which device, profile and transcript source this project really used.
function renderTranscription(info) {
  const box = $("#job-mode-box");
  const warning = $("#job-warning");
  box.hidden = !info;
  warning.hidden = true;
  if (!info) return;
  const device = info.device === "cuda" ? "GPU" : "CPU";
  const source = String(info.source || "whisper").startsWith("youtube")
    ? `YouTube ${info.source.replace("youtube_", "")} captions (${info.track}, ${Math.round((info.agreement || 0) * 100)}% agreement with Whisper) + Whisper ${info.model} for exported clips`
    : `Whisper ${info.model}`;
  const speed = info.realtime_factor
    ? ` · ${info.seconds < 90 ? `${info.seconds}s` : `${Math.round(info.seconds / 60)} min`} (${info.realtime_factor}× video length)`
    : info.estimate?.whisper_minutes && !String(info.source).startsWith("youtube")
      ? ` · estimated ${info.estimate.whisper_minutes} min`
      : "";
  $("#job-mode").textContent = `${info.profile || "accurate"} · ${source} on ${device} (${info.compute})${speed}`;
  const notes = [info.warning, info.captions_refused && `Captions not used: ${info.captions_refused}.`].filter(Boolean);
  if (notes.length) {
    warning.textContent = notes.join(" ");
    warning.hidden = false;
  }
}
function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}
function recordingSettings(root) {
  const hours = Number(root.querySelector("[name=max_hours]").value) || 12;
  return {
    from_start: root.querySelector("[name=from_start]").checked,
    max_duration: Math.round(Math.min(48, Math.max(0.1, hours)) * 3600),
    after: root.querySelector("[name=after]").value,
  };
}
const RECORDING_LIVE_KINDS = ["live", "upcoming", "processing"];
function renderRecording(detail) {
  const panel = $("#recording-panel");
  const rec = detail.recording;
  const kind = detail.source_kind?.kind;
  const show =
    !!rec || (detail.can_record && RECORDING_LIVE_KINDS.includes(kind));
  panel.hidden = !show;
  if (!show) return;
  const status = rec?.state || "not started";
  const active = !!detail.recording_active;
  const parts = Number(detail.recording_parts) || 0;
  $("#recording-state").textContent = status.toUpperCase();
  $("#recording-message").textContent =
    rec?.error || rec?.message || detail.source_kind?.message || "";
  $("#recording-duration").textContent = formatDuration(rec?.seconds);
  $("#recording-size").textContent = formatBytes(rec?.bytes);
  $("#recording-parts").textContent = String(
    Math.max(parts, (rec?.parts || []).length),
  );
  $("#recording-retries").textContent = String(rec?.retries || 0);
  const canStart = !active && status !== "recorded" && detail.can_record;
  const resumable = ["interrupted", "failed", "stopped"].includes(status);
  $("#record-start").hidden = !canStart;
  $("#record-start").textContent =
    resumable && parts ? "● Resume recording" : "● Start recording";
  $("#record-stop").hidden = !(active && status !== "finalizing");
  $("#record-finalize").hidden = !(
    !active &&
    parts > 0 &&
    status !== "recorded"
  );
  $("#recording-form").hidden = !canStart;
  if (canStart && rec?.settings && state.recordingFormFor !== detail.project.id) {
    const form = $("#recording-form");
    form.elements.from_start.checked = !!rec.settings.from_start;
    form.elements.max_hours.value = String(
      Math.round((rec.settings.max_duration / 3600) * 10) / 10,
    );
    form.elements.after.value = rec.settings.after;
    state.recordingFormFor = detail.project.id;
  }
}
async function startRecording() {
  await api(`/api/projects/${state.id}/record`, {
    settings: recordingSettings($("#recording-form")),
  });
  notice("Recording started. You can keep working on other projects.");
  await selectProject(state.id);
}
const CANDIDATES_PER_PAGE = 25;
const CONTENT_PROFILE_LABELS = {
  talk: "podcast/talk",
  mixed: "mixed footage",
  "gameplay/action": "gameplay/action",
};
function formatDuration(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total <= 0) return "0s";
  if (total < 10) return `${Math.round(total * 10) / 10}s`;
  if (total < 60) return `${Math.round(total * 10) / 10}s`;
  const whole = Math.round(total);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const remainder = whole % 60;
  if (hours)
    return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  return `${minutes}m ${String(remainder).padStart(2, "0")}s`;
}
function renderSelectionSummary(detail) {
  const box = $("#selection-summary");
  const selection = detail.selection || {};
  const candidates = detail.candidates || [];
  const scores = candidates
    .map((item) => Number(item.overall))
    .filter(Number.isFinite);
  const minScore = Number(
    selection.min_score ?? detail.project.options.min_score ?? 8,
  );
  const passing = Number.isFinite(Number(selection.passing))
    ? Number(selection.passing)
    : scores.filter((score) => score >= minScore).length;
  const scoreLow = Number.isFinite(Number(selection.score_min))
    ? Number(selection.score_min)
    : scores.length
      ? Math.min(...scores)
      : null;
  const scoreHigh = Number.isFinite(Number(selection.score_max))
    ? Number(selection.score_max)
    : scores.length
      ? Math.max(...scores)
      : null;
  const passingScores = scores.filter((score) => score >= minScore);
  const passLow = Number.isFinite(Number(selection.passing_score_min))
    ? Number(selection.passing_score_min)
    : passingScores.length
      ? Math.min(...passingScores)
      : null;
  const passHigh = Number.isFinite(Number(selection.passing_score_max))
    ? Number(selection.passing_score_max)
    : passingScores.length
      ? Math.max(...passingScores)
      : null;
  const durations = candidates
    .map((item) => Number(item.duration ?? item.end - item.start))
    .filter((value) => Number.isFinite(value) && value >= 0);
  const durationLow = Number.isFinite(Number(selection.duration_min))
    ? Number(selection.duration_min)
    : durations.length
      ? Math.min(...durations)
      : null;
  const durationHigh = Number.isFinite(Number(selection.duration_max))
    ? Number(selection.duration_max)
    : durations.length
      ? Math.max(...durations)
      : null;
  const lines = [];
  if (!passing) {
    lines.push(
      scoreLow === null || scoreHigh === null
        ? "No scored candidates yet."
        : `Candidate scores ${scoreLow.toFixed(1)}-${scoreHigh.toFixed(1)}.`,
    );
    lines.push(
      `No clips at ${minScore.toFixed(1)}+. Lower the minimum score to review more.`,
    );
  } else {
    const range =
      passLow === null || passHigh === null
        ? ""
        : ` · passing scores ${passLow.toFixed(1)}-${passHigh.toFixed(1)}`;
    const length =
      durationLow === null || durationHigh === null
        ? ""
        : ` · ${formatDuration(durationLow)}-${formatDuration(durationHigh)}`;
    lines.push(`${passing} clips at ${minScore.toFixed(1)}+${range}${length}`);
    const limit = Number(selection.max_duration) || null;
    const rejected = (selection.rejected_too_long || []).length;
    if (limit)
      lines.push(
        `Length limit ${formatDuration(limit)}${rejected ? ` · ${rejected} clip(s) rejected: not self-contained within the limit` : ""}`,
      );
    const profile =
      CONTENT_PROFILE_LABELS[selection.content_profile] ||
      selection.content_profile;
    if (profile) lines.push(`Footage: ${profile}`);
  }
  const judge = selection.highlights;
  if (judge) {
    const status = {
      ok: `AI judge ${judge.judge}: ${judge.judged} of ${judge.shortlisted} shortlisted clips judged`,
      partial: `AI judge ${judge.judge}: ${judge.judged} of ${judge.shortlisted} judged (${judge.reason})`,
      unavailable: `AI judge off: ${judge.reason} Using rule-based scores.`,
      invalid: `AI judge gave no usable answers. Using rule-based scores.`,
      off: `Rule-based scores on ${judge.shortlisted} shortlisted clips (no AI judge model set).`,
    }[judge.status];
    if (status) lines.push(status);
    const extra = [`at most ${judge.cap} clips`];
    if (judge.near_duplicates)
      extra.push(`${judge.near_duplicates} near-duplicate(s) removed`);
    if (judge.from_cache) extra.push(`${judge.from_cache} from cache`);
    lines.push(`Quality first: ${extra.join(" · ")}`);
  }
  const cuts = selection.boundaries;
  if (cuts && cuts.mode === "auto" && cuts.refined) {
    const parts = [];
    if (cuts.setup_added) parts.push(`${cuts.setup_added} got their setup`);
    if (cuts.payoff_added) parts.push(`${cuts.payoff_added} got their payoff`);
    if (cuts.shortened) parts.push(`${cuts.shortened} shortened`);
    if (cuts.trimmed) parts.push(`${cuts.trimmed} trimmed`);
    lines.push(
      `Auto length: ${parts.length ? parts.join(" · ") : "all clips already complete"} (max ${formatDuration(cuts.max_allowed)})`,
    );
  }
  box.textContent = lines.join("\n");
  box.hidden = false;
}
function renderDetail() {
  const detail = state.detail,
    p = detail.project,
    options = p.options;
  $("#project-id").textContent = `PROJECT / ${p.id}`;
  $("#project-title").textContent = state.detail?.display_title || projectName(p.source);
  $("#project-source").textContent = p.source;
  const player = $("#source-player");
  const hint = $("#player-hint");
  if (
    state.previewFile &&
    !detail.files.some((f) => f.url === state.previewFile.url)
  )
    state.previewFile = null; // deleted or replaced
  if (state.previewFile) {
    player.hidden = false;
    hint.hidden = true;
    if (player.getAttribute("src") !== state.previewFile.url)
      player.src = state.previewFile.url;
    setPlayerLabel(state.previewFile);
  } else if (detail.playable_source) {
    player.hidden = !detail.transcript;
    hint.hidden = !!detail.transcript;
    hint.textContent = "Prepare this project to preview the source.";
    const source = `/media/${p.id}/source`;
    if (detail.transcript && player.getAttribute("src") !== source)
      player.src = source;
    setPlayerLabel(detail.transcript ? { kind: "source" } : null);
  } else {
    setPlayerLabel(null);
    player.hidden = true;
    hint.hidden = false;
    hint.textContent = detail.transcript
      ? "Long source: press Watch on a clip to render a 480p preview from a downloaded section."
      : "Prepare this project to preview the source.";
  }
  const form = $("#settings-form");
  for (const [key, value] of Object.entries(options)) {
    const input = form.elements.namedItem(key);
    if (input) {
      if (input.type === "checkbox") input.checked = value;
      else input.value = value ?? "";
    }
  }
  form.elements.center1.value = options.centers[0];
  form.elements.center2.value = options.centers[1];
  const brand = options.brand
    ? basename(options.brand).replace(/\.json$/, "")
    : "";
  updateBrands(brand);
  $("#search-mode").value = options.semantic
    ? "semantic"
    : options.search
      ? "search"
      : "auto";
  $("#query").value = options.semantic || options.search || "";
  fillCaptionForm(savedCaptionStyle(), options.style);
  markCaptionPreset(options.style);
  $("#candidate-count").textContent = detail.candidates.length;
  const total = detail.candidates.length;
  const pageCount = Math.max(1, Math.ceil(total / CANDIDATES_PER_PAGE));
  state.candidatePage = Math.min(Math.max(0, state.candidatePage), pageCount - 1);
  const start = state.candidatePage * CANDIDATES_PER_PAGE;
  const visible = detail.candidates.slice(start, start + CANDIDATES_PER_PAGE);
  $("#candidates").innerHTML = visible.length
    ? visible
        .map((c, offset) => {
          const i = start + offset;
          const rank = Number.isFinite(Number(c.rank)) ? Number(c.rank) : i + 1;
          const duration = Number(c.duration ?? c.end - c.start);
          return `<article class="candidate" data-index="${i}"><input type="checkbox" aria-label="Select clip ${i + 1}" data-select="${i + 1}" ${state.selected.has(i + 1) ? "checked" : ""}><div><div class="candidate-title"><input aria-label="Clip ${i + 1} title" data-field="hook" value="${escapeHTML(c.hook)}"></div><div class="candidate-meta">#${rank} · ${Number(c.overall).toFixed(1)}/10 · ${formatDuration(duration)}${c.similarity !== undefined ? " · SEMANTIC " + Number(c.similarity).toFixed(2) : ""}${c.scene ? ` · <span class="scene-tag" data-scene="${escapeHTML(c.scene)}">${escapeHTML(c.scene.replace("_", " "))}</span>` : ""}</div><div class="candidate-time"><label>Start (seconds)<input data-field="start" type="number" min="0" step="0.01" value="${c.start}"></label><label>End (seconds)<input data-field="end" type="number" min="0" step="0.01" value="${c.end}"></label></div><details class="candidate-reason"><summary>Why this clip</summary><p>${escapeHTML(c.reason || "")}</p><pre>${escapeHTML(JSON.stringify(c.score_parts || {}, null, 2))}</pre>${c.judge?.scores ? `<pre>${escapeHTML(JSON.stringify({ ai_judge: c.judge.model, verdict: c.judge.verdict, ai_score: c.judge.llm_score, rule_score: c.judge.rule_score, ...c.judge.scores }, null, 2))}</pre>` : ""}${c.evidence ? `<pre>${escapeHTML(JSON.stringify(c.evidence, null, 2))}</pre>` : ""}${c.boundaries?.notes?.length ? `<p>Length: ${escapeHTML(c.boundaries.notes.join("; "))} (was ${formatDuration(c.boundaries.original[1] - c.boundaries.original[0])})</p>` : ""}</details></div><button data-play="${i}">▶ Watch</button></article>`;
        })
        .join("")
    : '<p class="muted">Find clips to see candidates here. You can adjust selection and timing before export.</p>';
  $("#candidate-page").textContent =
    `${total ? start + 1 : 0}–${Math.min(start + CANDIDATES_PER_PAGE, total)} of ${total}`;
  $("#candidates-prev").disabled = state.candidatePage === 0;
  $("#candidates-next").disabled = start + CANDIDATES_PER_PAGE >= total;
  renderSelectionSummary(detail);
  renderWords();
  updateSelection();
  renderOutputs(detail);
  updateStatus(detail);
  setTab(state.tab);
}
function setTab(tab) {
  state.tab = tab;
  $("#clip-panel").hidden = tab !== "clips";
  $("#transcript-panel").hidden = tab !== "transcript";
  $("#caption-panel").hidden = tab !== "captions";
  $("#show-clips").setAttribute("aria-pressed", String(tab === "clips"));
  $("#show-transcript").setAttribute(
    "aria-pressed",
    String(tab === "transcript"),
  );
  $("#show-captions").setAttribute(
    "aria-pressed",
    String(tab === "captions"),
  );
  state.captionKey = "";
  updateCaptionOverlay(true);
}
function updateSelection() {
  $("#selection-count").textContent = `${state.selected.size} selected`;
}
function renderWords() {
  const start = state.page * 100,
    visible = state.words.slice(start, start + 100);
  $("#transcript-words").innerHTML = visible.length
    ? '<div class="word-header"><span></span><span>WORD</span><span>START (S)</span><span>END (S)</span></div>' +
      visible
        .map(
          (word, i) =>
            `<div class="word-row" data-word="${start + i}"><button data-seek="${start + i}" aria-label="Play word ${start + i + 1}">${start + i + 1}</button><input aria-label="Word ${start + i + 1}" data-word-field="word" value="${escapeHTML(word.word)}"><input aria-label="Word ${start + i + 1} start" data-word-field="start" type="number" min="0" step="0.01" value="${word.start}"><input aria-label="Word ${start + i + 1} end" data-word-field="end" type="number" min="0" step="0.01" value="${word.end}"></div>`,
        )
        .join("")
    : '<p class="muted">Prepare the project to get an editable transcript.</p>';
  $("#word-page").textContent =
    `${state.words.length ? start + 1 : 0}–${Math.min(start + 100, state.words.length)} of ${state.words.length} words`;
  $("#words-prev").disabled = state.page === 0;
  $("#words-next").disabled = start + 100 >= state.words.length;
  for (const el of document.querySelectorAll("#transcript-words input"))
    el.disabled = state.busy;
}
function renderOutputs(detail) {
  const videos = detail.files.filter((f) => f.name.endsWith(".mp4"));
  $("#output-count").textContent = `${videos.length} CLIPS`;
  $("#legacy-captions").hidden = !(detail.legacy_captions || []).length;
  const finals = videos.filter((f) => f.kind === "clips");
  $("#clip-tools").hidden = !finals.length;
  const cleaned = detail.project.storage?.cleaned_at;
  $("#cleaned-note").hidden = !cleaned;
  if (cleaned)
    $("#cleaned-note").textContent = `Supporting files were cleaned up after upload (${formatSize(detail.project.storage.bytes_freed)} freed). The final clips below are kept; Find clips again to make new ones.`;
  $("#outputs").className = videos.length ? "output-grid" : "";
  $("#outputs").innerHTML = videos.length
    ? videos
        .map((file) => {
          const stem = file.name.slice(0, -4),
            related = detail.files.filter(
              (f) =>
                f.kind === file.kind &&
                [stem + ".srt", stem + ".png", stem + ".meta.json"].includes(
                  f.name,
                ),
            );
          const pick =
            file.kind === "clips"
              ? `<label class="check clip-pick"><input type="checkbox" data-clip-select="${escapeHTML(file.name)}" aria-label="Select ${escapeHTML(file.name)} for deletion"> Select</label>`
              : "";
          const playing = state.previewFile?.url === file.url ? " playing" : "";
          const del =
            file.kind === "clips"
              ? `<button type="button" data-delete-clip="${escapeHTML(file.name)}">Delete…</button>`
              : "";
          return `<article class="output-card${playing}" data-file-url="${escapeHTML(file.url)}"><button type="button" class="output-play" data-load-file="${escapeHTML(file.url)}">▶ Play in player</button><div class="output-info">${pick}<p>${escapeHTML(file.kind === "previews" ? "DRAFT · " : "EXPORT · ")}${escapeHTML(stem)}</p><div class="output-links"><a href="${encodeURI(file.url)}" download>MP4 ↓</a>${related.map((f) => `<a href="${encodeURI(f.url)}" download>${f.name.endsWith(".srt") ? "SRT" : f.name.endsWith(".png") ? "POSTER" : "METADATA"} ↓</a>`).join("")}</div>${file.kind === "clips" ? publishControl(file.name) : ""}${del}</div></article>`;
        })
        .join("")
    : '<p class="muted">Choose candidates and make a preview or export.</p>';
}
async function saveCandidates(showNotice = true) {
  if (!state.detail.candidates.length)
    throw new Error("Find clips before exporting.");
  await api(`/api/projects/${state.id}/candidates`, {
    candidates: state.detail.candidates,
  });
  if (showNotice) notice("Clip edits saved.");
}
async function run(action, preview = false) {
  if (action === "render") {
    if (!state.selected.size) throw new Error("Select at least one clip.");
    await saveCandidates(false);
  }
  await api(`/api/projects/${state.id}/run`, {
    action,
    preview,
    pick:
      action === "render"
        ? [...state.selected].sort((a, b) => a - b).join(",")
        : null,
  });
  state.detail.busy = true;
  state.detail.project.status = "queued";
  state.detail.project.stage = action;
  updateStatus(state.detail);
  notice(
    action === "prepare"
      ? "Queued. Saved stages will be reused when their inputs match."
      : "Queued selected clips.",
  );
  await refreshProjects();
}
$("#new-project").addEventListener("click", () => {
  state.id = null;
  state.view = "new";
  if (location.hash) history.replaceState(null, "", location.pathname);
  $("#new-view").hidden = false;
  $("#project-view").hidden = true;
  $("#dashboard-view").hidden = true;
  $("#sources").focus();
  refreshProjects().catch((e) => notice(e.message, true));
});
$("#projects").addEventListener(
  "click",
  safe(async (event) => {
    const menu = event.target.closest("[data-menu-for]");
    if (menu) return openProjectMenu(menu.dataset.menuFor, menu);
    const button = event.target.closest("[data-id]");
    if (button) await selectProject(button.dataset.id);
  }),
);
// Find clips. The form is "novalidate": its hidden recording options must
// never block it; sources are checked here and by the server.
$("#create-form").addEventListener("submit", (event) => {
  event.preventDefault();
  findClips(event.target).catch((error) =>
    notice(`Find clips failed: ${error.message}`, true),
  );
});
async function findClips(form) {
  if (state.submitting) return; // a second click while submitting
  const sources = $("#sources")
    .value.split(/\r?\n/)
    .map((s) => s.trim().replace(/^"|"$/g, ""))
    .filter(Boolean);
  if (!sources.length) {
    $("#sources").focus();
    throw new Error("Paste a video URL or a local file path first.");
  }
  const button = form.querySelector("button[type=submit]");
  const label = button.innerHTML;
  state.submitting = true;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "Submitting…";
  notice(
    `Submitting ${sources.length} source${sources.length === 1 ? "" : "s"} to Studio…`,
  );
  try {
    const options = newProjectOptions();
    const result = await api("/api/batch", { sources, options });
    const skipped = result.duplicates || [];
    const failed = result.errors || [];
    if (result.projects.length) await selectProject(result.projects[0].id);
    else if (skipped[0]?.project_id) await selectProject(skipped[0].project_id);
    const made = result.projects.length;
    const parts = [];
    if (made)
      parts.push(
        `Project created${made === 1 ? "" : ` (${made})`} · Find clips is queued; progress shows above.`,
      );
    if (skipped.length)
      parts.push(
        `${skipped.length} source${skipped.length === 1 ? " was" : "s were"} skipped: already in a project (no duplicate processing).`,
      );
    for (const item of failed) parts.push(`${item.source}: ${item.error}`);
    notice(parts.join(" "), !made || failed.length > 0);
    if (made) $("#sources").value = "";
  } finally {
    state.submitting = false;
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.innerHTML = label;
  }
}
$("#detect").addEventListener(
  "click",
  safe(async () => {
    const source = $("#sources")
      .value.split(/\r?\n/)
      .map((s) => s.trim().replace(/^"|"$/g, ""))
      .find(Boolean);
    if (!source) throw new Error("Add a video path or URL first.");
    const button = $("#detect");
    button.disabled = true;
    button.textContent = "Checking…";
    try {
      const result = await api("/api/detect", { source }, { timeout: 120000 });
      state.detected = result;
      $("#detect-result").hidden = false;
      $("#detect-label").textContent = result.label;
      $("#detect-title").textContent = result.title || "";
      $("#detect-message").textContent = result.message;
      $("#detect-record").hidden = !result.can_record;
    } finally {
      button.disabled = false;
      button.textContent = "Check source";
    }
  }),
);
$("#detect-record-start").addEventListener(
  "click",
  safe(async () => {
    if (!state.detected?.can_record) throw new Error("Check a live stream first.");
    const project = await api("/api/record", {
      source: state.detected.source,
      settings: recordingSettings($("#detect-record")),
      options: newProjectOptions(),
    });
    $("#detect-result").hidden = true;
    $("#sources").value = "";
    state.detected = null;
    await selectProject(project.id);
    notice("Recording started. Chopify finds clips when it finishes.");
  }),
);
$("#record-start").addEventListener("click", safe(startRecording));
$("#record-stop").addEventListener(
  "click",
  safe(async () => {
    await api(`/api/projects/${state.id}/record-stop`, {});
    notice("Stopping safely. The recording is kept and then processed.");
  }),
);
$("#record-finalize").addEventListener(
  "click",
  safe(async () => {
    await api(`/api/projects/${state.id}/record-finalize`, {});
    notice("Joining the recorded parts…");
    await selectProject(state.id);
  }),
);
$("#cancel-job").addEventListener(
  "click",
  safe(async () => {
    if (!(await askCancelJob())) return;
    await api(`/api/projects/${state.id}/cancel`, {});
    notice("Cancelling…");
  }),
);
$("#settings-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    const form = event.target,
      values = Object.fromEntries(new FormData(form));
    for (const key of ["min_len", "max_len"]) values[key] = Number(values[key]);
    values.max_clips =
      values.max_clips === "" ? null : Number(values.max_clips);
    values.min_score =
      values.min_score === "" ? null : Number(values.min_score);
    values.preferred_len = (values.preferred_len || "").trim() || null;
    values.tighten = form.elements.tighten.checked;
    values.loudnorm = form.elements.loudnorm.checked;
    values.highlights = form.elements.highlights.checked;
    values.hinglish_ai = form.elements.hinglish_ai.checked;
    values.centers = [Number(values.center1), Number(values.center2)];
    delete values.center1;
    delete values.center2;
    values.llm = values.llm.trim() || null;
    values.search = null;
    values.semantic = null;
    const mode = $("#search-mode").value;
    if (mode !== "auto") {
      if (!$("#query").value.trim())
        throw new Error("Enter a topic or keywords.");
      values[mode] = $("#query").value.trim();
    }
    await api(`/api/projects/${state.id}/settings`, values);
    notice(
      "Settings saved. Find clips again for search changes, or export with your new style.",
    );
  }),
);
$("#brand-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.target));
    const name = values.name;
    delete values.name;
    for (const key of ["font_size", "margin", "logo_width"])
      values[key] = Number(values[key]);
    values.logo = values.logo.trim() || null;
    await api("/api/brands", { name, preset: values });
    await config();
    $("#brand-select").value = name;
    notice("Brand preset saved. Save settings to apply it to this project.");
  }),
);
$("#candidates").addEventListener("input", (event) => {
  const selected = event.target.dataset.select;
  if (selected) {
    event.target.checked
      ? state.selected.add(Number(selected))
      : state.selected.delete(Number(selected));
    updateSelection();
  }
  const field = event.target.dataset.field;
  if (field) {
    const i = Number(event.target.closest("[data-index]").dataset.index);
    state.detail.candidates[i][field] =
      field === "hook" ? event.target.value : Number(event.target.value);
  }
});
let stopAt = null;
async function play(start, end = null) {
  const player = $("#source-player");
  stopAt = end;
  player.currentTime = start;
  await player.play();
}
async function watchCandidate(index) {
  const candidate = state.detail.candidates[index];
  if (state.detail.playable_source) {
    state.previewFile = null;
    renderDetail();
    await play(candidate.start, candidate.end);
    return;
  }
  if (state.busy || state.pendingPreview)
    throw new Error("Wait for the current job to finish.");
  const before = new Set(
    state.detail.files
      .filter((file) => file.kind === "previews" && file.name.endsWith(".mp4"))
      .map((file) => file.name),
  );
  state.pendingPreview = { index, before };
  state.previewFile = null;
  renderDetail();
  $("#player-hint").textContent =
    "Rendering this clip from its downloaded source section…";
  try {
    await saveCandidates(false);
    await api(`/api/projects/${state.id}/run`, {
      action: "render",
      preview: true,
      pick: String(index + 1),
    });
  } catch (error) {
    state.pendingPreview = null;
    renderDetail();
    throw error;
  }
  state.detail.busy = true;
  state.detail.project.status = "queued";
  state.detail.project.stage = "render";
  updateStatus(state.detail);
  notice("Rendering a 480p preview from a downloaded section…");
  await refreshProjects();
}
function setPlayerLabel(file) {
  const label = $("#player-label");
  label.hidden = !file;
  if (!file) return;
  const kind = file.kind === "previews" ? "preview" : file.kind === "clips" ? "export" : "source";
  label.dataset.kind = kind;
  label.textContent =
    kind === "preview"
      ? `PREVIEW · 480p draft (not the final export) · ${file.name}`
      : kind === "export"
        ? `FINAL EXPORT · ${file.name}`
        : "SOURCE VIDEO";
  for (const card of document.querySelectorAll("#outputs [data-file-url]"))
    card.classList.toggle("playing", card.dataset.fileUrl === file.url);
}
// The one shared player: Watch previews and rendered clips load here.
function loadPreview(file) {
  state.previewFile = file;
  stopAt = null;
  const player = $("#source-player");
  if (player.getAttribute("src") !== file.url) player.src = file.url;
  player.hidden = false;
  $("#player-hint").hidden = true;
  setPlayerLabel(file);
  player.currentTime = 0;
  player.play().catch(() => {});
  updateCaptionOverlay(true);
}
$("#source-player").addEventListener("timeupdate", () => {
  if (stopAt !== null && $("#source-player").currentTime >= stopAt) {
    $("#source-player").pause();
    stopAt = null;
  }
  updateCaptionOverlay();
});
$("#source-player").addEventListener("seeked", () =>
  updateCaptionOverlay(true),
);
$("#source-player").addEventListener("loadedmetadata", () =>
  updateCaptionOverlay(true),
);
window.addEventListener("resize", () => {
  state.captionKey = "";
  updateCaptionOverlay(true);
});
$("#candidates").addEventListener(
  "click",
  safe(async (event) => {
    const button = event.target.closest("[data-play]");
    if (button) {
      await watchCandidate(Number(button.dataset.play));
    }
  }),
);
$("#transcript-words").addEventListener("input", (event) => {
  const field = event.target.dataset.wordField;
  if (field) {
    const i = Number(event.target.closest("[data-word]").dataset.word);
    state.words[i][field] =
      field === "word" ? event.target.value : Number(event.target.value);
  }
});
$("#transcript-words").addEventListener(
  "click",
  safe(async (event) => {
    const button = event.target.closest("[data-seek]");
    if (button) await play(state.words[Number(button.dataset.seek)].start);
  }),
);
$("#words-prev").addEventListener("click", () => {
  state.page--;
  renderWords();
});
$("#words-next").addEventListener("click", () => {
  state.page++;
  renderWords();
});
$("#candidates-prev").addEventListener("click", () => {
  state.candidatePage = Math.max(0, state.candidatePage - 1);
  renderDetail();
});
$("#candidates-next").addEventListener("click", () => {
  state.candidatePage++;
  renderDetail();
});
$("#select-all").addEventListener("click", () => {
  state.selected = new Set(state.detail.candidates.map((_, i) => i + 1));
  renderDetail();
});
$("#select-none").addEventListener("click", () => {
  state.selected.clear();
  renderDetail();
});
$("#show-clips").addEventListener("click", () => setTab("clips"));
$("#show-transcript").addEventListener("click", () => setTab("transcript"));
$("#show-captions").addEventListener("click", () => setTab("captions"));
$("#caption-form").addEventListener("input", () => syncCaption());
$("#caption-preset").addEventListener("change", (event) =>
  applyCaptionPreset(event.target.value),
);
$("#caption-presets").addEventListener("click", (event) => {
  const button = event.target.closest("[data-caption-preset]");
  if (button) applyCaptionPreset(button.dataset.captionPreset);
});
$("#caption-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    const caption = captionFromForm();
    const style = $("#caption-preset").value;
    await api(`/api/projects/${state.id}/settings`, { caption, style });
    state.detail.project.options.caption = caption;
    state.detail.project.options.style = style;
    state.captionDraft = caption;
    markCaptionPreset(style);
    notice("Caption style saved for this project.");
  }),
);
$("#save-caption-preset").addEventListener(
  "click",
  safe(async () => {
    if (!state.detail) throw new Error("Select a project first.");
    const name = $("#caption-preset-name").value.trim();
    if (!name) throw new Error("Name the preset first.");
    await api("/api/brands", { name, preset: captionFromForm() });
    await config();
    $("#brand-select").value = name;
    notice(
      "Caption preset saved. Choose it under Brand preset, then Save settings.",
    );
  }),
);
$("#caption-preview").addEventListener(
  "click",
  safe(async () => {
    if (!state.detail?.transcript) throw new Error("Prepare the project first.");
    if (state.detail.playable_source === false)
      throw new Error(
        "Long sources have no full player. Use Watch on a clip to render and play its preview.",
      );
    const candidates = state.detail.candidates;
    const selected = [...state.selected].sort((a, b) => a - b);
    const clip = candidates[(selected[0] || 1) - 1] || candidates[0];
    setTab("captions");
    const start = clip ? Number(clip.start) : state.words[0]?.start || 0;
    await play(start, start + 5);
  }),
);
$("#prepare").addEventListener(
  "click",
  safe(() => run("prepare")),
);
$("#preview").addEventListener(
  "click",
  safe(() => run("render", true)),
);
$("#render").addEventListener(
  "click",
  safe(() => run("render")),
);
$("#save-candidates").addEventListener(
  "click",
  safe(() => saveCandidates()),
);
$("#save-transcript").addEventListener(
  "click",
  safe(async () => {
    await api(`/api/projects/${state.id}/transcript`, { words: state.words });
    notice("Caption edits saved. Export clips to apply them.");
  }),
);
let polling = false;
setInterval(async () => {
  if (polling) return;
  polling = true;
  try {
    await refreshProjects();
    if (state.id && state.busy) {
      const id = state.id,
        detail = await api(`/api/projects/${id}`);
      if (id !== state.id) return;
      const finished = !detail.busy;
      updateStatus(detail);
      if (finished) {
        const pending = state.pendingPreview;
        state.detail = detail;
        state.words = structuredClone(detail.transcript?.words || []);
        if (!pending) {
          state.selected = new Set(detail.candidates.map((_, i) => i + 1));
          state.candidatePage = 0;
        }
        let match = null;
        if (pending) {
          const previews = detail.files.filter(
            (file) => file.kind === "previews" && file.name.endsWith(".mp4"),
          );
          const fresh = previews.filter(
            (file) => !pending.before.has(file.name),
          );
          const prefix = `${String(pending.index + 1).padStart(3, "0")}-`;
          const failed =
            !!detail.project.error || (detail.render_errors || []).length > 0;
          match =
            fresh.find((file) => file.name.startsWith(prefix)) ||
            fresh[0] ||
            (!failed
              ? previews.find((file) => file.name.startsWith(prefix))
              : null) ||
            null;
          state.pendingPreview = null;
          state.previewFile = match;
        }
        renderDetail();
        if (pending) {
          if (match) {
            loadPreview(match);
            notice("Preview rendered from a downloaded source section.");
          } else {
            notice(
              detail.project.error ||
                "Preview finished, but no clip was produced.",
              true,
            );
          }
        } else {
          notice(
            detail.project.error || "Project ready for review.",
            !!detail.project.error,
          );
        }
      }
    }
    $("#connection").textContent = "● Local session";
    $("#connection-banner").hidden = true;
  } catch (error) {
    $("#connection").textContent = "Connection lost · restart Studio";
    const banner = $("#connection-banner");
    banner.hidden = false;
    banner.textContent =
      error.kind === "network" || error.kind === "timeout"
        ? `${error.message} Your projects are safe on disk.`
        : `Studio reported a problem: ${error.message}`;
  } finally {
    polling = false;
  }
}, 2000);
safe(async () => {
  await config();
  await refreshProjects();
  if (location.hash === "#dashboard") await showDashboard();
})();

/* ------------------------------------------------------------------ YouTube
   Phase 5. Nothing uploads without the user pressing "Upload to YouTube…"
   and confirming. Tokens never reach the browser. */
const UPLOAD_LABELS = {
  draft: "Draft – not uploaded",
  queued: "Approved – waiting to upload",
  uploading: "Uploading",
  uploaded: "Uploaded",
  quota_wait: "Waiting for YouTube quota",
  failed: "Upload failed",
  cancelled: "Withdrawn",
};
state.youtube = null;
state.publishEntry = null;

function uploadFor(name) {
  return (state.youtube?.uploads || []).find(
    (u) => u.name === name && u.state !== "cancelled",
  );
}
function publishControl(name) {
  const entry = uploadFor(name);
  const status = entry
    ? `<span class="upload-chip upload-${entry.state}">${escapeHTML(
        UPLOAD_LABELS[entry.state] || entry.state,
      )}${entry.state === "uploading" ? ` ${Math.round((entry.progress || 0) * 100)}%` : ""}</span>`
    : "";
  const link =
    entry?.url && entry.state === "uploaded"
      ? `<a href="${encodeURI(entry.url)}" target="_blank" rel="noopener">Open on YouTube ↗</a>`
      : "";
  return `<div class="publish-row">${status}${link}<button type="button" data-publish="${escapeHTML(name)}">${entry ? "Details" : "Publish…"}</button></div>`;
}
function formatReset(seconds) {
  const date = new Date(seconds * 1000);
  return date.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
}
function renderYouTube() {
  const yt = state.youtube;
  if (!yt) return;
  const account = yt.account;
  const signin = account.signin;
  let text;
  if (!account.client_configured)
    text = "Add your Google OAuth client file to connect a channel.";
  else if (account.connected)
    text = `Connected: ${account.channel?.title || "YouTube channel"}`;
  else text = "Not connected.";
  if (signin && signin.state !== "connected") text += ` ${signin.message}`;
  if (account.error) text += ` ${account.error}`;
  $("#yt-status").textContent = text;
  $("#yt-client-form").hidden = account.client_configured && account.connected;
  $("#yt-connect").hidden = !account.client_configured || account.connected;
  $("#yt-disconnect").hidden = !account.connected;
  $("#yt-settings-form").hidden = !account.connected;
  const usage = yt.usage;
  const blocked = {
    upload_quota: "YouTube's upload quota is used up for today.",
    app_limit: "Chopify's daily upload limit is reached.",
  }[usage.blocked_by];
  $("#yt-usage").innerHTML = account.connected
    ? [
        `<li><strong>${usage.uploads_left_today}</strong> upload(s) can start today${blocked ? ` – ${escapeHTML(blocked)}` : ""}</li>`,
        `<li>Chopify limit: ${usage.uploads_started} of ${usage.max_uploads_per_day} uploads started</li>`,
        `<li>YouTube upload quota: ${usage.upload_calls} of ${usage.upload_quota_calls} videos.insert calls (each attempt counts)</li>`,
        `<li>YouTube quota for other calls: ${usage.general_units} of ${usage.general_quota_units} units</li>`,
        `<li>All reset at midnight Pacific Time (${escapeHTML(formatReset(usage.resets_at))} your time)</li>`,
      ].join("")
    : "";
  const form = $("#yt-settings-form");
  if (!form.contains(document.activeElement))
    for (const key of ["max_uploads_per_day", "upload_quota_calls", "general_quota_units", "privacy"])
      form.elements[key].value = yt.settings[key];
  if (state.detail) renderOutputs(state.detail);
  if (state.publishEntry && $("#publish-dialog").open) {
    const fresh = (yt.uploads || []).find((u) => u.id === state.publishEntry.id);
    if (fresh) showPublishState(fresh);
  }
}
async function loadYouTube() {
  const query = state.id ? `?project=${encodeURIComponent(state.id)}` : "";
  state.youtube = await api(`/api/youtube${query}`);
  renderYouTube();
}
function youtubeActive() {
  const yt = state.youtube;
  return (
    !!yt &&
    (yt.account.signin?.state === "waiting" ||
      yt.worker_busy ||
      (yt.uploads || []).some((u) => ["queued", "uploading"].includes(u.state)))
  );
}
let youtubeTick = 0;
setInterval(async () => {
  youtubeTick += 1;
  if (!state.youtube || (!youtubeActive() && youtubeTick % 10)) return;
  try {
    await loadYouTube();
  } catch {
    /* polling errors are shown on the next action */
  }
}, 2000);

function metadataFromForm() {
  const form = $("#publish-form");
  return {
    title: form.elements.title.value.trim(),
    description: form.elements.description.value,
    hashtags: form.elements.hashtags.value.split(/\s+/).filter(Boolean),
    tags: form.elements.tags.value.split(",").map((t) => t.trim()).filter(Boolean),
    privacy: form.elements.privacy.value,
    category_id: form.elements.category_id.value,
    made_for_kids: form.elements.made_for_kids.checked,
  };
}
function fillPublishForm(entry) {
  const form = $("#publish-form");
  const meta = entry.metadata || {};
  const select = form.elements.category_id;
  select.innerHTML = Object.entries(state.youtube.categories)
    .map(([id, name]) => `<option value="${escapeHTML(id)}">${escapeHTML(name)}</option>`)
    .join("");
  const tagLine = (meta.hashtags || []).map((h) => "#" + h).join(" ");
  form.elements.title.value = meta.title || "";
  // the hashtag line is shown in its own field and re-added on save
  form.elements.description.value = (meta.description || "")
    .replace(tagLine, "")
    .trim();
  form.elements.hashtags.value = (meta.hashtags || []).join(" ");
  form.elements.tags.value = (meta.tags || []).join(", ");
  form.elements.privacy.value = meta.privacy || "private";
  select.value = meta.category_id || "22";
  form.elements.made_for_kids.checked = !!meta.made_for_kids;
  $("#publish-source").textContent =
    meta.source === "ai" ? "AI DRAFT" : meta.source === "rules" ? "SIMPLE DRAFT" : "EDITED";
  $("#publish-clip").textContent = entry.name + (meta.note ? ` · ${meta.note}` : "");
}
function showPublishState(entry) {
  state.publishEntry = entry;
  const locked = ["queued", "uploading", "uploaded"].includes(entry.state);
  for (const el of $("#publish-form").elements)
    if (el.tagName !== "BUTTON") el.disabled = locked;
  let text = UPLOAD_LABELS[entry.state] || entry.state;
  if (entry.error) text += `: ${entry.error}`;
  if (entry.state === "uploaded") text += ` → ${entry.url}`;
  if (entry.next_attempt && ["quota_wait", "failed"].includes(entry.state))
    text += ` (next try ${formatReset(entry.next_attempt)})`;
  $("#publish-state").textContent = text;
  const bar = $("#publish-progress");
  bar.hidden = !["uploading", "queued"].includes(entry.state);
  bar.value = entry.progress || 0;
  $("#publish-approve").hidden = locked;
  $("#publish-regenerate").hidden = locked;
  $("#publish-save").hidden = locked;
  $("#publish-retry").hidden = !["failed", "quota_wait"].includes(entry.state);
  $("#publish-cancel").hidden = !["draft", "queued", "failed", "quota_wait"].includes(entry.state);
}
async function openPublish(name, regenerate = false) {
  if (!state.youtube) await loadYouTube();
  notice(regenerate ? "Writing title and description…" : "Preparing metadata…");
  const entry = await api(`/api/projects/${state.id}/publish`, { clip: name, regenerate });
  fillPublishForm(entry);
  showPublishState(entry);
  const dialog = $("#publish-dialog");
  if (!dialog.open) dialog.showModal();
  await loadYouTube();
}
$("#outputs").addEventListener(
  "click",
  safe(async (event) => {
    const button = event.target.closest("[data-publish]");
    if (button) await openPublish(button.dataset.publish);
  }),
);
$("#publish-close").addEventListener("click", () => $("#publish-dialog").close());
$("#publish-regenerate").addEventListener(
  "click",
  safe(async () => {
    const entry = await api(`/api/uploads/${state.publishEntry.id}/regenerate`, {});
    fillPublishForm(entry);
    showPublishState(entry);
  }),
);
$("#publish-save").addEventListener(
  "click",
  safe(async () => {
    const entry = await api(`/api/uploads/${state.publishEntry.id}/metadata`, {
      metadata: metadataFromForm(),
    });
    fillPublishForm(entry);
    showPublishState(entry);
    notice("Draft saved. Nothing was uploaded.");
  }),
);
$("#publish-approve").addEventListener(
  "click",
  safe(async () => {
    const meta = metadataFromForm();
    const channel = state.youtube?.account?.channel?.title || "your channel";
    const ok = await askDialog({
      title: `Upload to ${channel} as ${meta.privacy.toUpperCase()}?`,
      body: `"${meta.title}" will be sent to YouTube with visibility ${meta.privacy}.`,
      confirmText: "Upload",
    });
    if (!ok) return;
    const entry = await api(`/api/uploads/${state.publishEntry.id}/approve`, {
      metadata: meta,
      confirm: true,
    });
    showPublishState(entry);
    notice("Approved. Uploading in the background.");
    await loadYouTube();
  }),
);
$("#publish-retry").addEventListener(
  "click",
  safe(async () => {
    showPublishState(await api(`/api/uploads/${state.publishEntry.id}/retry`, {}));
    await loadYouTube();
  }),
);
$("#publish-cancel").addEventListener(
  "click",
  safe(async () => {
    showPublishState(await api(`/api/uploads/${state.publishEntry.id}/cancel`, {}));
    await loadYouTube();
  }),
);
$("#yt-client-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    await api("/api/youtube/client", { path: event.target.elements.path.value });
    event.target.reset();
    notice("Client file added. Now connect your channel.");
    await loadYouTube();
  }),
);
$("#yt-connect").addEventListener(
  "click",
  safe(async () => {
    await api("/api/youtube/connect", {});
    notice("Finish signing in in the browser tab that opened.");
    await loadYouTube();
  }),
);
$("#yt-disconnect").addEventListener(
  "click",
  safe(async () => {
    if (!(await askDialog({ title: "Disconnect YouTube?",
      body: "Chopify will revoke its access. Upload history is kept.",
      confirmText: "Disconnect", danger: true }))) return;
    await api("/api/youtube/disconnect", {});
    notice("YouTube disconnected.");
    await loadYouTube();
  }),
);
$("#yt-settings-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    const form = event.target;
    await api("/api/youtube/settings", {
      settings: {
        max_uploads_per_day: Number(form.elements.max_uploads_per_day.value),
        upload_quota_calls: Number(form.elements.upload_quota_calls.value),
        general_quota_units: Number(form.elements.general_quota_units.value),
        privacy: form.elements.privacy.value,
      },
    });
    notice("Upload settings saved.");
    await loadYouTube();
  }),
);

// ------------------------------------------------------------------
// Phase 6: automation dashboard (overview of every project and queue).
// ------------------------------------------------------------------
const STAGES = ["source", "transcribe", "discover", "select", "render", "upload"];
const STAGE_WORDS = {
  done: "done",
  active: "running",
  failed: "failed",
  pending: "not yet",
};
function relTime(seconds) {
  if (!seconds) return "";
  const delta = Date.now() / 1000 - seconds;
  if (delta < 60) return "just now";
  if (delta < 3600) return `${Math.round(delta / 60)} min ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)} h ago`;
  return new Date(seconds * 1000).toLocaleDateString();
}
function dashCard(label, value, detail, tone = "") {
  return `<div class="dash-card ${tone}"><span>${escapeHTML(label)}</span><strong>${escapeHTML(String(value))}</strong><small>${escapeHTML(detail || "")}</small></div>`;
}
function renderOverview(data) {
  state.overview = data;
  const rows = data.projects;
  const failed = rows.filter((p) => ["failed", "interrupted"].includes(p.status));
  const running = data.jobs.active.filter((j) => j.state === "running");
  const queued = data.jobs.active.filter((j) => j.state === "queued");
  const up = data.uploads.counts || {};
  const usage = data.uploads.usage || {};
  $("#dash-cards").innerHTML = [
    dashCard("Running", running.length, running.map((j) => j.kind).join(", ") || "idle", running.length ? "live" : ""),
    dashCard("Queued", queued.length, data.jobs.paused.process ? "processing paused" : "waiting their turn", data.jobs.paused.process ? "warn" : ""),
    dashCard("Needs attention", failed.length, failed.length ? "see the red stages below" : "nothing failed", failed.length ? "bad" : ""),
    dashCard("Uploaded", up.uploaded || 0, `${up.queued || 0} approved · ${up.draft || 0} drafts`),
    dashCard("Uploads left today", usage.uploads_left_today ?? "–", data.jobs.paused.uploads ? "uploads paused" : `resets ${usage.resets_at ? formatReset(usage.resets_at) : ""}`, data.jobs.paused.uploads ? "warn" : ""),
    dashCard("New sources today", `${data.usage.new_sources}/${data.usage.limit}`, "from channel monitoring"),
  ].join("");
  $("#dash-project-count").textContent = `${rows.length} PROJECT${rows.length === 1 ? "" : "S"}`;
  $("#dash-rows").innerHTML = rows.length
    ? rows.map(dashRow).join("")
    : `<tr><td colspan="9" class="muted">No projects yet. Add sources under New project or monitor a channel.</td></tr>`;
  const pausedP = data.jobs.paused.process;
  const pausedU = data.jobs.paused.uploads;
  const pp = $("#dash-pause-processing");
  pp.textContent = pausedP ? "▶ Resume processing" : "❚❚ Pause processing";
  pp.setAttribute("aria-pressed", String(pausedP));
  pp.classList.toggle("primary", pausedP);
  const pu = $("#dash-pause-uploads");
  pu.textContent = pausedU ? "▶ Resume uploads" : "❚❚ Pause uploads";
  pu.setAttribute("aria-pressed", String(pausedU));
  pu.classList.toggle("primary", pausedU);
  // uploads
  const active = data.uploads.active || [];
  $("#dash-upload-summary").textContent = `${active.length} IN QUEUE`;
  $("#dash-uploads").innerHTML = active.length
    ? active
        .map(
          (u) =>
            `<li><span class="upload-chip upload-${escapeHTML(u.state)}">${escapeHTML(UPLOAD_LABELS[u.state] || u.state)}${u.state === "uploading" ? ` ${Math.round((u.progress || 0) * 100)}%` : ""}</span> <span>${escapeHTML(u.metadata?.title || u.name)}</span>${u.error ? `<small class="dash-error">${escapeHTML(u.error)}</small>` : ""}<button type="button" class="link" data-dash-open="${escapeHTML(u.project_id)}">Open</button></li>`,
        )
        .join("")
    : `<li class="muted">No approved uploads waiting.</li>`;
  // jobs
  const jobs = [...data.jobs.active, ...data.jobs.recent].slice(0, 12);
  $("#dash-job-summary").textContent = `${data.jobs.active.length} ACTIVE`;
  $("#dash-jobs").innerHTML = jobs.length
    ? jobs
        .map(
          (j) =>
            `<li><span class="job-state job-${escapeHTML(j.state)}">${escapeHTML(j.waiting ? "paused" : j.state)}</span> <span>${escapeHTML(j.kind)} · ${escapeHTML(j.project_id)}</span><small class="muted">${escapeHTML(relTime(j.finished || j.started || j.created))}</small>${j.error ? `<small class="dash-error">${escapeHTML(j.error)}</small>` : ""}</li>`,
        )
        .join("")
    : `<li class="muted">No jobs since Studio started.</li>`;
  // monitoring + settings
  const s = data.settings;
  $("#dash-monitor-state").textContent = s.monitor_enabled
    ? data.monitor_next
      ? `ON · NEXT ${new Date(data.monitor_next * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
      : "ON"
    : "OFF";
  $("#dash-channels").innerHTML = data.channels.length
    ? data.channels
        .map(
          (c) =>
            `<li><span>${escapeHTML(c.label || c.url)}</span><small class="muted">${escapeHTML(c.last_check ? `checked ${relTime(c.last_check)} · ${c.found || 0} found` : "not checked yet")}</small>${c.inactive ? `<small class="dash-error">Inactive: ${escapeHTML(c.inactive_reason || "")}</small>` : c.own ? `<small class="muted">Your channel · official YouTube Data API · <a href="${escapeHTML(c.url)}" target="_blank" rel="noopener">open on YouTube</a></small>` : ""}${c.last_error ? `<small class="dash-error">${escapeHTML(c.last_error)}</small>` : ""}<button type="button" class="link" data-remove-channel="${escapeHTML(c.id)}">Remove</button></li>`,
        )
        .join("")
    : `<li class="muted">No channels monitored.</li>`;
  $("#dash-usage").textContent = `${data.usage.left} NEW SOURCES LEFT TODAY`;
  const form = $("#dash-settings-form");
  if (!form.contains(document.activeElement)) {
    for (const input of form.elements) {
      if (!input.name) continue;
      const value = input.name === "aspect" ? s.new_project_options?.aspect || "9:16" : s[input.name];
      if (input.type === "checkbox") input.checked = !!value;
      else if (value !== undefined) input.value = value;
    }
  }
  // events
  $("#dash-events").innerHTML = data.events.length
    ? data.events
        .map(
          (e) =>
            `<li class="event-${escapeHTML(e.level)}"><time>${escapeHTML(new Date(e.time * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }))}</time><span>${escapeHTML(e.message)}${e.advice ? ` <em>${escapeHTML(e.advice)}</em>` : ""}</span>${e.project_id ? `<button type="button" class="link" data-dash-open="${escapeHTML(e.project_id)}">${escapeHTML(e.project_id)}</button>` : ""}</li>`,
        )
        .join("")
    : `<li class="muted">Nothing has happened yet.</li>`;
  updateDashBadge(data);
}
function dashRow(p) {
  const stages = STAGES.map((stage) => {
    const status = p.pipeline[stage];
    let extra = "";
    if (p.progress && p.progress.stage === stage && status === "active")
      extra = `<progress class="mini-bar" max="100" value="${Math.round(p.progress.fraction * 100)}" title="${escapeHTML(p.progress.detail || "")}">${Math.round(p.progress.fraction * 100)}%</progress>`;
    if (stage === "render" && p.clips)
      extra = `<small>${p.clips}${p.legacy_captions ? ` · ${p.legacy_captions} need re-export for Hinglish captions` : ""}</small>`;
    return `<td><i class="stage-dot ${status}" title="${stage}: ${STAGE_WORDS[status] || status}"></i><span class="sr-only">${stage}: ${STAGE_WORDS[status] || status}</span>${extra}</td>`;
  }).join("");
  const canRetry = ["failed", "interrupted", "cancelled"].includes(p.status);
  const diagnosis = p.diagnosis
    ? `<div class="dash-diagnosis"><strong>Failed at ${escapeHTML(p.diagnosis.stage)}:</strong> ${escapeHTML(p.diagnosis.problem)} <span>${escapeHTML(p.diagnosis.advice)}</span></div>`
    : "";
  return `<tr class="${canRetry ? "row-bad" : ""}"><th scope="row"><button type="button" class="link dash-name" data-dash-open="${escapeHTML(p.id)}">${escapeHTML(p.title || p.source || p.id)}</button><small class="muted">${escapeHTML(p.id)} · ${escapeHTML(relTime(p.updated))}</small>${diagnosis}</th>${stages}<td><span class="status-chip status-${escapeHTML(p.status)}">${escapeHTML(p.status)}</span><small class="muted">${escapeHTML(p.stage || "")}</small></td><td class="dash-actions">${canRetry ? `<button type="button" data-dash-retry="${escapeHTML(p.id)}">Retry</button>` : ""}${p.busy ? `<button type="button" data-dash-cancel="${escapeHTML(p.id)}">Cancel</button>` : ""}</td></tr>`;
}
function updateDashBadge(data) {
  const bad = data.projects.filter((p) => ["failed", "interrupted"].includes(p.status)).length;
  const badge = $("#dash-badge");
  badge.hidden = !bad;
  badge.textContent = bad ? String(bad) : "";
}
async function loadOverview() {
  renderOverview(await api("/api/overview"));
  await loadLive().catch((error) => notice(`Live sessions unavailable: ${error.message}`, true));
  await loadWatchlist().catch(() => {});
  await loadAI().catch((error) => aiShow(`AI settings unavailable: ${error.message}`, true));
  await loadStorage().catch((error) =>
    showStorageResult(`Storage information unavailable: ${error.message}`, true),
  );
}
async function showDashboard() {
  if (location.hash !== "#dashboard") history.replaceState(null, "", "#dashboard");
  state.view = "dashboard";
  state.id = null;
  $("#new-view").hidden = true;
  $("#project-view").hidden = true;
  $("#dashboard-view").hidden = false;
  await loadOverview();
  await refreshProjects();
}
$("#open-dashboard").addEventListener("click", safe(showDashboard));
$("#dash-refresh").addEventListener("click", safe(loadOverview));
$("#dash-pause-processing").addEventListener(
  "click",
  safe(async () => {
    const paused = !state.overview?.jobs.paused.process;
    await api("/api/automation/pause", { target: "processing", paused });
    notice(paused ? "Processing paused. The running job finishes; nothing new starts." : "Processing resumed.");
    await loadOverview();
  }),
);
$("#dash-pause-uploads").addEventListener(
  "click",
  safe(async () => {
    const paused = !state.overview?.jobs.paused.uploads;
    await api("/api/automation/pause", { target: "uploads", paused });
    notice(paused ? "Uploads paused. An upload in progress finishes; approved clips wait." : "Uploads resumed.");
    await loadOverview();
  }),
);
$("#dashboard-view").addEventListener(
  "click",
  safe(async (event) => {
    const open = event.target.closest("[data-dash-open]");
    if (open) return selectProject(open.dataset.dashOpen);
    const retry = event.target.closest("[data-dash-retry]");
    if (retry) {
      await api(`/api/projects/${retry.dataset.dashRetry}/retry`, {});
      notice("Queued again. Finished stages are reused.");
      return loadOverview();
    }
    const cancel = event.target.closest("[data-dash-cancel]");
    if (cancel) {
      if (!(await askCancelJob())) return;
      await api(`/api/projects/${cancel.dataset.dashCancel}/cancel`, {});
      notice("Cancelling…");
      return loadOverview();
    }
    const remove = event.target.closest("[data-remove-channel]");
    if (remove) {
      if (!(await askDialog({ title: "Stop monitoring this channel?",
        body: "Chopify stops checking it for new videos. Projects already made are kept.",
        confirmText: "Stop monitoring", danger: true }))) return;
      await api(`/api/automation/channels/${remove.dataset.removeChannel}/remove`, {});
      return loadOverview();
    }
  }),
);
$("#dash-channel-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    await api("/api/automation/channels", {
      url: $("#dash-channel-url").value.trim(),
      label: $("#dash-channel-label").value.trim(),
    });
    $("#dash-channel-url").value = "";
    $("#dash-channel-label").value = "";
    notice("Channel added. Its existing videos are skipped; new ones become projects.");
    await loadOverview();
  }),
);
$("#dash-check-now").addEventListener(
  "click",
  safe(async () => {
    await api("/api/automation/check", {});
    notice("Checking channels in the background…");
    setTimeout(() => loadOverview().catch(() => {}), 4000);
  }),
);
$("#dash-settings-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    const form = event.target;
    const current = state.overview?.settings || {};
    const settings = {
      monitor_enabled: form.elements.monitor_enabled.checked,
      record_live: form.elements.record_live.checked,
      resume_after_restart: form.elements.resume_after_restart.checked,
      monitor_interval_minutes: Number(form.elements.monitor_interval_minutes.value),
      max_new_sources_per_day: Number(form.elements.max_new_sources_per_day.value),
      new_source_action: form.elements.new_source_action.value,
      new_project_options: { ...(current.new_project_options || {}), aspect: form.elements.aspect.value },
    };
    await api("/api/automation/settings", { settings });
    notice("Automation settings saved.");
    await loadOverview();
  }),
);
let dashPolling = false;
setInterval(async () => {
  if (dashPolling) return;
  dashPolling = true;
  try {
    if (state.view === "dashboard") await loadOverview();
    else if (!state.overview || Date.now() - (state.overviewAt || 0) > 15000) {
      state.overviewAt = Date.now();
      updateDashBadge(await api("/api/overview"));
    }
  } catch (error) {
    /* shown by the main poller */
  } finally {
    dashPolling = false;
  }
}, 3000);

// ------------------------------------------------------------- storage
function formatSize(bytes) {
  const n = Number(bytes) || 0;
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.round(n / 1024)} KB`;
}
function uploadBadge(state) {
  return state === "uploaded" ? "UPLOADED" : "NOT UPLOADED";
}
async function loadStorage() {
  const data = await api("/api/storage", undefined, { timeout: 60000 });
  state.storage = data;
  const t = data.totals;
  const cards = [
    ["Used by Chopify", data.chopify_bytes],
    ["Final clips (kept)", t.final_clips],
    ["Source media", t.source_media],
    ["Temporary + previews", t.temporary + t.previews],
    ["Transcripts + AI caches", t.transcripts_ai],
    ["Safe to reclaim now", data.reclaimable_bytes],
    ["Free on this drive", data.disk_free],
  ];
  $("#storage-cards").innerHTML = cards
    .map(
      ([label, value]) =>
        `<div class="dash-card"><span>${escapeHTML(label)}</span><strong>${value == null ? "–" : formatSize(value)}</strong></div>`,
    )
    .join("");
  $("#storage-clean-all").disabled = !data.eligible.length;
  $("#storage-rows").innerHTML = data.projects
    .map((row) => {
      const supporting = row.total_bytes - row.totals.final_clips - row.totals.kept_metadata;
      const cleaned = row.last_cleanup?.state === "done";
      const status = row.eligible
        ? "Eligible"
        : cleaned && !supporting
          ? `Cleaned (${formatSize(row.last_cleanup.total_bytes_freed)} freed)`
          : escapeHTML(row.reasons[0] || "Not eligible");
      return `<tr data-storage="${escapeHTML(row.project_id)}"><td>${escapeHTML(String(row.title || row.project_id).slice(0, 60))}<br><small>${escapeHTML(row.project_id)}${row.auto_cleanup ? " · auto" : ""}</small></td><td>${formatSize(row.total_bytes)}</td><td>${row.clips.length} · ${formatSize(row.totals.final_clips)}</td><td>${formatSize(supporting)}</td><td title="${escapeHTML(row.reasons.join("; "))}">${status}</td><td><button type="button" data-clean="${escapeHTML(row.project_id)}" ${row.eligible ? "" : "disabled"}>Clean up</button> <button type="button" data-delete-project="${escapeHTML(row.project_id)}" ${row.clips.length ? "" : "disabled"}>Delete clips…</button></td></tr>`;
    })
    .join("");
}
function showStorageResult(text, error = false) {
  const out = $("#storage-result");
  out.hidden = false;
  out.textContent = text;
  out.classList.toggle("error", error);
}
async function cleanUp(projectId) {
  const result = await api(
    "/api/storage/cleanup",
    projectId ? { project_id: projectId } : {},
    { timeout: 300000 },
  );
  if (projectId && !result.cleaned)
    showStorageResult(`Not cleaned: ${result.reasons.join("; ")}`, true);
  else if (projectId)
    showStorageResult(
      `Deleted ${result.files_deleted} supporting files, recovered ${formatSize(result.bytes_freed)}. Final clips kept.`,
    );
  else
    showStorageResult(
      `${result.projects_cleaned} project(s) cleaned: ${result.files_deleted} files deleted, ${formatSize(result.bytes_freed)} recovered. Final clips kept.`,
    );
  await loadStorage();
}
// Two steps: the server lists exactly what would go, the user confirms that
// list, and only then is the same list deleted.
async function deleteClips(projectId, names) {
  if (!names.length) throw new Error("Select the clips to delete.");
  const plan = await api(`/api/projects/${projectId}/delete-clips`, {
    files: names,
    confirm: false,
  });
  if (plan.blocked.length)
    throw new Error(`Cancel the upload first: ${plan.blocked.join(", ")}`);
  $("#delete-summary").textContent =
    `${plan.clips.length} clip(s), ${formatSize(plan.bytes)} will be released: ` +
    `${plan.uploaded} uploaded, ${plan.not_uploaded} NOT uploaded.`;
  $("#delete-list").innerHTML = plan.clips
    .map(
      (c) =>
        `<li class="${c.upload_state === "uploaded" ? "uploaded" : "not-uploaded"}"><strong>${uploadBadge(c.upload_state)}</strong> ${escapeHTML(c.name)} · ${formatSize(c.bytes)}<br><small>${c.files.map(escapeHTML).join(", ")}</small></li>`,
    )
    .join("");
  const dialog = $("#delete-dialog");
  $("#delete-confirm").checked = false;
  $("#delete-go").disabled = true;
  const choice = await new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue), { once: true });
    dialog.showModal();
  });
  if (choice !== "delete" || !$("#delete-confirm").checked) {
    notice("Nothing was deleted.");
    return null;
  }
  const result = await api(`/api/projects/${projectId}/delete-clips`, {
    files: names,
    confirm: true,
    confirmed: plan.clips.map((c) => c.name),
  });
  notice(`Deleted ${result.files_deleted} file(s), released ${formatSize(result.bytes_freed)}.`);
  return result;
}
$("#delete-confirm").addEventListener("change", (event) => {
  $("#delete-go").disabled = !event.target.checked;
});
$("#storage-refresh").addEventListener("click", safe(loadStorage));
$("#storage-clean-all").addEventListener("click", safe(() => cleanUp(null)));
$("#storage-rows").addEventListener(
  "click",
  safe(async (event) => {
    const clean = event.target.closest("[data-clean]");
    if (clean) return cleanUp(clean.dataset.clean);
    const del = event.target.closest("[data-delete-project]");
    if (del) {
      const row = state.storage.projects.find((p) => p.project_id === del.dataset.deleteProject);
      if (await deleteClips(row.project_id, row.clips.map((c) => c.name))) await loadStorage();
    }
  }),
);
function selectedClipNames() {
  return [...document.querySelectorAll("#outputs [data-clip-select]:checked")].map(
    (el) => el.dataset.clipSelect,
  );
}
async function deleteFromProject(names) {
  if (await deleteClips(state.id, names)) await selectProject(state.id);
}
$("#delete-selected-clips").addEventListener(
  "click",
  safe(() => deleteFromProject(selectedClipNames())),
);
$("#delete-all-clips").addEventListener(
  "click",
  safe(() =>
    deleteFromProject(
      state.detail.files
        .filter((f) => f.kind === "clips" && f.name.endsWith(".mp4"))
        .map((f) => f.name),
    ),
  ),
);

$("#outputs").addEventListener(
  "click",
  safe(async (event) => {
    const play = event.target.closest("[data-load-file]");
    if (play) {
      const file = state.detail.files.find((f) => f.url === play.dataset.loadFile);
      if (file) {
        loadPreview(file);
        $("#source-player").scrollIntoView({ block: "nearest" });
      }
      return;
    }
    const del = event.target.closest("[data-delete-clip]");
    if (del) await deleteFromProject([del.dataset.deleteClip]);
  }),
);

// Selected vs queued vs rendering vs rendered vs failed, never mixed up.
function renderClipCounts(detail) {
  const out = $("#clip-counts");
  const c = detail.clip_counts;
  out.hidden = !c;
  if (!c) return;
  const selected = state.selected ? state.selected.size : c.selected;
  const parts = [`${selected ?? 0} selected`];
  if (c.queued) parts.push(`${c.queued} queued for export`);
  if (c.rendering) parts.push(`${c.rendering} rendering now`);
  parts.push(`${c.rendered} exported`);
  if (c.failed) parts.push(`${c.failed} failed`);
  if (c.previews) parts.push(`${c.previews} preview draft(s)`);
  out.textContent = parts.join(" · ");
}

// ------------------------------------------------------------------ AI
const CAPABILITY_LABELS = {
  transcribe: "Transcription",
  generate: "Text & JSON (clip judging, titles)",
  vision: "Image analysis",
};
function aiShow(text, error = false) {
  const out = $("#ai-result");
  out.hidden = false;
  out.textContent = text;
  out.classList.toggle("error", error);
}
function aiModelOptions(provider, capability, current) {
  const models = (provider?.models || []).filter((m) =>
    m.capabilities.includes(capability),
  );
  const ids = models.map((m) => m.id);
  if (current && !ids.includes(current)) ids.push(current);
  return ids
    .map((id) => {
      const m = models.find((x) => x.id === id);
      const price = m ? ` · ${m.pricing}` : "";
      return `<option value="${escapeHTML(id)}" ${id === current ? "selected" : ""}>${escapeHTML(id + price)}</option>`;
    })
    .join("");
}
function renderAI(data) {
  state.ai = data;
  const s = data.settings;
  const form = $("#ai-mode-form");
  form.elements.free_only.checked = s.free_only;
  form.elements.paid_calls.checked = s.paid_calls;
  form.elements.daily_paid_budget_usd.value = s.daily_paid_budget_usd;
  form.elements.monthly_paid_budget_usd.value = s.monthly_paid_budget_usd;
  form.elements.local_fallback.checked = s.local_fallback;
  const env = data.env;
  const lim = env.limits;
  // .env sets the ceilings: Studio can be stricter, never looser.
  form.elements.free_only.disabled = lim.free_only;
  form.elements.paid_calls.disabled = !lim.paid_calls;
  form.elements.daily_paid_budget_usd.max = lim.daily_paid_budget_usd;
  form.elements.monthly_paid_budget_usd.max = lim.monthly_paid_budget_usd;
  form.elements.daily_paid_budget_usd.disabled = lim.daily_paid_budget_usd <= 0;
  form.elements.monthly_paid_budget_usd.disabled = lim.monthly_paid_budget_usd <= 0;
  const locks = [
    lim.free_only ? `free-only is required by ${env.vars.free_only}=true` : "",
    lim.paid_calls ? "" : `paid calls are off by ${env.vars.paid_calls}=false`,
    `budgets at most $${lim.daily_paid_budget_usd}/day and $${lim.monthly_paid_budget_usd}/month`,
  ].filter(Boolean);
  const legacy = data.legacy_keys.exists
    ? `<p class="error">An older encrypted key file from Phase 1 exists (${escapeHTML(data.legacy_keys.path)}${data.legacy_keys.providers.length ? `, holding keys for ${escapeHTML(data.legacy_keys.providers.join(", "))}` : ""}). It is no longer read: put those keys in .env. The file was not changed or deleted.</p>`
    : "";
  $("#ai-env").innerHTML = `<p class="hint"><strong>API keys and spending limits come from</strong> <code>${escapeHTML(env.path)}</code> ${env.exists ? "(found)" : "(not found: copy .env.example to .env)"}. Studio never shows or stores full keys. <strong>Restart Studio after editing .env.</strong> Windows environment variables with the same name win over .env.</p>
    <p class="hint">From .env: ${escapeHTML(locks.join("; "))}.</p>
    ${env.error ? `<p class="error">${escapeHTML(env.error)}</p>` : ""}
    ${env.bad_lines.length ? `<p class="error">.env lines ${escapeHTML(env.bad_lines.join(", "))} could not be read and were ignored.</p>` : ""}
    ${legacy}`;
  $("#ai-summary").textContent = s.free_only
    ? "FREE-ONLY"
    : s.paid_calls
      ? `PAID ALLOWED · $${data.spent.day.toFixed(2)} TODAY`
      : "PAID CALLS OFF";
  const byName = Object.fromEntries(data.providers.map((p) => [p.name, p]));
  $("#ai-routes").innerHTML = data.capabilities
    .map((cap) => {
      const route = s.routes[cap] || {};
      const usable = data.providers.filter((p) =>
        p.name === "openrouter"
          ? cap !== "transcribe"
          : p.name === "local"
            ? cap !== "vision"
            : p.models.some((m) => m.capabilities.includes(cap)),
      );
      const providerSelect = `<select data-route-provider="${cap}"><option value="">None</option>${usable
        .map(
          (p) =>
            `<option value="${p.name}" ${p.name === route.provider ? "selected" : ""}>${escapeHTML(p.title)}</option>`,
        )
        .join("")}</select>`;
      const chosen = byName[route.provider];
      const modelField =
        route.provider === "openrouter" || route.provider === "local" && cap === "generate"
          ? `<input data-route-model="${cap}" value="${escapeHTML(route.model || "")}" placeholder="model id" />`
          : `<select data-route-model="${cap}">${aiModelOptions(chosen, cap, route.model)}</select>`;
      return `<label>${CAPABILITY_LABELS[cap]}${providerSelect}${modelField}</label>`;
    })
    .join("");
  $("#ai-providers").innerHTML = data.providers
    .map((p) => {
      const key =
        p.name === "local"
          ? "No key needed"
          : p.key.configured
            ? `Key configured · …${escapeHTML(p.key.last4)} (${p.key.source === "environment" ? `Windows environment variable ${escapeHTML(p.key.env)}` : `${escapeHTML(p.key.env)} in .env`})`
            : `No key: add ${escapeHTML(p.key.env)}=… to .env, then restart Studio`;
      const rl = p.rate_limits;
      const limits = rl
        ? `<p class="hint">Account limits (from ${escapeHTML(p.title)}'s last response): ${rl.remaining_requests ?? "?"}/${rl.limit_requests ?? "?"} requests, ${rl.remaining_tokens ?? "?"}/${rl.limit_tokens ?? "?"} tokens left.</p>`
        : "";
      const limited = p.last_limited
        ? `<p class="error">Paused by a rate/free-tier limit ${relTime(p.last_limited.ts)}: ${escapeHTML(p.last_limited.error || "")}</p>`
        : "";
      const health = p.health
        ? `${p.health.ok ? "Healthy" : "Problem"}: ${escapeHTML(p.health.detail)} · ${relTime(p.health.checked)}`
        : "Not checked yet";
      const extra =
        p.name === "groq"
          ? `<p class="hint" data-groq-plan="${escapeHTML(p.settings.billing)}">Groq plan: <strong>${p.settings.billing === "free_plan" ? "Free (no charges; free-tier rate limits apply)" : p.settings.billing === "paid" ? "Developer (paid, blocked while free-only is on)" : "Unknown (treated as paid and blocked)"}</strong> · set by ${escapeHTML(data.env.vars.groq_plan)} in .env. Chopify never upgrades your plan.</p>
             ${p.settings.zdr_confirmed_at ? `<p class="hint">Zero Data Retention confirmed ${relTime(p.settings.zdr_confirmed_at)}. <button type="button" class="link" data-ai-revoke="groq:zdr">Withdraw</button></p>` : `<label class="check"><input type="checkbox" data-zdr-check /> I turned on Zero Data Retention in Groq Console &gt; Settings &gt; Data Controls (Groq has no API to check this)</label><button type="button" data-ai-confirm="groq:zdr">Confirm for real recordings</button>`}`
          : p.name === "gemini"
            ? `<label>Gemini tier<select data-gemini-tier><option value="free" ${p.settings.tier === "free" ? "selected" : ""}>Free (synthetic test data only)</option><option value="paid" ${p.settings.tier === "paid" ? "selected" : ""}>Paid</option></select></label>`
            : p.name === "nvidia"
              ? `<p class="hint">Trial: internal evaluation with synthetic data only. Never used for projects or as a fallback.</p><label>Approved trial models (comma-separated, only after reading each model card's terms)<input data-ai-models value="${escapeHTML((p.settings.models || []).join(", "))}" placeholder="none approved" /></label>`
              : "";
      return `<article class="ai-provider" data-provider="${p.name}">
        <header><strong>${escapeHTML(p.title)}</strong>
          <label class="check"><input type="checkbox" data-ai-enabled ${p.enabled ? "checked" : ""} ${p.name === "local" ? "" : ""}/> Enabled</label></header>
        <p class="hint">${escapeHTML(p.role)}</p>
        <p class="ai-privacy" data-state="${escapeHTML(p.privacy.real_media)}">Privacy: real recordings ${escapeHTML(p.privacy.real_media)} — ${escapeHTML(p.privacy.detail)} <a href="${escapeHTML(p.terms_url || "#")}" target="_blank" rel="noopener">terms</a></p>
        <p class="hint" data-ai-key-status>${key}</p>
        ${extra}${limits}${limited}
        <div class="dash-inline"><button type="button" data-ai-health>Check health</button><small>${health}</small></div>
      </article>`;
    })
    .join("");
  $("#ai-usage-rows").innerHTML = data.usage.length
    ? data.usage
        .map(
          (u) =>
            `<tr><td>${escapeHTML(u.provider)}</td><td>${escapeHTML(u.model)}</td><td>${escapeHTML(u.capability)}</td><td>${u.requests}</td><td>${u.failed} / ${u.blocked} / ${u.limited}</td><td>${u.audio_minutes}</td><td>${u.input_tokens} / ${u.output_tokens}</td><td>$${Number(u.cost_usd).toFixed(4)}</td><td>${u.avg_latency_ms ?? "–"} ms</td><td>${u.free ? "free" : "paid"}</td></tr>`,
        )
        .join("")
    : '<tr><td colspan="10" class="muted">No AI requests yet.</td></tr>';
}
async function loadAI() {
  renderAI(await api("/api/ai"));
}
async function aiPost(action, body) {
  const result = await api(`/api/ai/${action}`, body, { timeout: 60000 });
  if (result.providers) renderAI(result);
  else await loadAI();
  return result;
}
$("#ai-mode-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    const form = event.target;
    const routes = {};
    for (const cap of state.ai.capabilities) {
      const provider = form.querySelector(`[data-route-provider="${cap}"]`).value || null;
      const model = form.querySelector(`[data-route-model="${cap}"]`)?.value || null;
      routes[cap] = { provider, model: provider ? model : null };
    }
    const providers = {};
    for (const card of document.querySelectorAll("#ai-providers [data-provider]")) {
      const name = card.dataset.provider;
      providers[name] = { enabled: card.querySelector("[data-ai-enabled]").checked };
      const models = card.querySelector("[data-ai-models]");
      if (models)
        providers[name].models = models.value
          .split(",")
          .map((m) => m.trim())
          .filter(Boolean);
      const tier = card.querySelector("[data-gemini-tier]");
      if (tier) providers[name].tier = tier.value;
    }
    await aiPost("settings", {
      free_only: form.elements.free_only.disabled || form.elements.free_only.checked,
      paid_calls: !form.elements.paid_calls.disabled && form.elements.paid_calls.checked,
      daily_paid_budget_usd: Number(form.elements.daily_paid_budget_usd.value) || 0,
      monthly_paid_budget_usd: Number(form.elements.monthly_paid_budget_usd.value) || 0,
      local_fallback: form.elements.local_fallback.checked,
      routes,
      providers,
    });
    aiShow("AI settings saved. New projects use these routes; existing projects keep theirs.");
  }),
);
$("#ai-routes").addEventListener("change", (event) => {
  const cap = event.target.dataset.routeProvider;
  if (!cap) return;
  state.ai.settings.routes[cap] = { provider: event.target.value || null, model: null };
  renderAI(state.ai);
});
$("#ai-providers").addEventListener(
  "click",
  safe(async (event) => {
    const card = event.target.closest("[data-provider]");
    if (!card) return;
    const provider = card.dataset.provider;
    if (event.target.matches("[data-ai-health]")) {
      const result = await aiPost("health", { provider });
      aiShow(`${provider}: ${result.ok ? "healthy" : "problem"} — ${result.detail}`, !result.ok);
    } else if (event.target.matches("[data-ai-confirm]")) {
      if (!card.querySelector("[data-zdr-check]")?.checked)
        throw new Error("Tick the box first: confirm you turned on Zero Data Retention.");
      const [p, what] = event.target.dataset.aiConfirm.split(":");
      await aiPost("confirm", { provider: p, what, confirm: true });
      aiShow("Groq Zero Data Retention confirmed. Real recordings may now be sent to Groq.");
    } else if (event.target.matches("[data-ai-revoke]")) {
      const [p, what] = event.target.dataset.aiRevoke.split(":");
      await aiPost("confirm", { provider: p, what, revoke: true });
      aiShow("Confirmation withdrawn: real recordings are no longer sent there.");
    }
  }),
);

// ------------------------------------------- project administration (Phase 3)
async function askCancelJob() {
  return askDialog({
    title: "Cancel this project's job?",
    body: "A recording is stopped and kept. Clips already made are kept.",
    confirmText: "Cancel job",
    danger: true,
  });
}
function openProjectMenu(projectId, anchor) {
  const menu = $("#project-menu");
  state.menuProject = projectId;
  const box = anchor.getBoundingClientRect();
  menu.style.top = `${Math.round(box.bottom + window.scrollY + 4)}px`;
  menu.style.left = `${Math.round(Math.max(8, Math.min(box.left, window.innerWidth - 230)))}px`;
  menu.hidden = false;
  menu.querySelector("button").focus();
}
function closeProjectMenu() {
  $("#project-menu").hidden = true;
}
document.addEventListener("click", (event) => {
  if (!event.target.closest("#project-menu, [data-menu-for], #project-actions"))
    closeProjectMenu();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeProjectMenu();
});
$("#project-actions").addEventListener("click", (event) => {
  if (state.id) openProjectMenu(state.id, event.currentTarget);
});
function projectTitleFor(id) {
  if (state.detail?.project?.id === id) return state.detail.display_title;
  const row = document.querySelector(`#projects [data-id="${id}"] strong`);
  return row ? row.textContent : "this project";
}
$("#project-menu").addEventListener(
  "click",
  safe(async (event) => {
    const item = event.target.closest("[data-menu]");
    if (!item) return;
    const id = state.menuProject;
    closeProjectMenu();
    const action = item.dataset.menu;
    if (action === "rename") {
      const title = await askDialog({
        title: "Rename project",
        input: { label: "Project name", value: projectTitleFor(id) },
        confirmText: "Rename",
        validate: (v) => (!v ? "Enter a name." : v.length > 120 ? "Use at most 120 characters." : ""),
      });
      if (!title) return;
      await api(`/api/projects/${id}/rename`, { title });
      notice(`Renamed to "${title}".`);
      state.projectMarkup = "";
      await refreshProjects();
      if (state.id === id) await selectProject(id);
    } else if (action === "open-folder") {
      const result = await api(`/api/projects/${id}/open-folder`, {});
      notice(`Opened ${result.opened}`);
    } else if (action === "storage") {
      const plan = await api(`/api/projects/${id}/storage`);
      const rows = Object.entries(plan.totals || {})
        .filter(([, bytes]) => bytes > 0)
        .map(([name, bytes]) => `<li>${escapeHTML(STORAGE_LABELS[name] || name)}: <strong>${formatSize(bytes)}</strong></li>`)
        .join("");
      await askDialog({
        title: `Storage · ${projectTitleFor(id)}`,
        html: `<p>Total ${formatSize(plan.total_bytes || 0)} on disk.</p><ul class="delete-list">${rows || "<li>No files.</li>"}</ul><p class="hint">Final clips are never deleted automatically. Supporting files can be cleaned up from Dashboard → Storage after the clips are uploaded.</p>`,
        confirmText: "Close",
      });
    } else if (action === "delete") {
      await deleteProjectFlow(id);
    }
  }),
);
const STORAGE_LABELS = {
  final_clips: "Final clips",
  kept_metadata: "Project metadata and history",
  source_media: "Source media and downloads",
  previews: "Previews",
  transcripts_ai: "Transcripts, captions and AI caches",
  temporary: "Temporary and intermediate files",
};
async function deleteProjectFlow(id) {
  const plan = await api(`/api/projects/${id}/delete-plan`, {});
  if (plan.blockers.length) throw new Error(plan.blockers.join(" "));
  const clips = plan.final_clips.length;
  const name = projectTitleFor(id);
  const keep = await askDialog({
    title: `Delete "${name}"?`,
    html: `<p>This removes the project from Studio: source media, transcripts, previews and caches (${plan.other_files} file(s), ${formatSize(plan.other_bytes)}).</p>
      <p><strong>Your ${clips} final clip(s) (${formatSize(plan.final_bytes)}) are kept</strong> in <code>${escapeHTML(plan.folder)}\\clips</code>. Upload history is kept.</p>
      ${plan.links.length ? `<p class="hint">${plan.links.length} link(s) inside the folder are removed as links only; their targets are not touched.</p>` : ""}
      ${clips ? `<p class="hint">To delete the final clips too, choose "Delete clips too".</p>` : ""}`,
    confirmText: "Delete project, keep clips",
    danger: true,
  });
  if (keep) {
    const result = await api(`/api/projects/${id}/delete`, { mode: "project", confirm: true });
    return afterDelete(id, `Project deleted. ${result.final_clips_kept} final clip(s) kept.`);
  }
  if (!clips) return notice("Nothing was deleted.");
  const typed = await askDialog({
    title: `Also delete ${clips} final clip(s)?`,
    html: `<p class="error">This permanently deletes the finished MP4 files too. It cannot be undone. Videos already on YouTube are not changed.</p>`,
    input: { label: `Type ${clips} to confirm`, placeholder: String(clips) },
    confirmText: "Delete clips too",
    danger: true,
    validate: (v) => (v === String(clips) ? "" : `Type ${clips} to confirm.`),
  });
  if (!typed) return notice("Nothing was deleted.");
  const result = await api(`/api/projects/${id}/delete`, {
    mode: "everything", confirm: true, confirm_clips: Number(typed),
  });
  return afterDelete(id, `Project and ${clips} clip(s) deleted (${formatSize(result.bytes_freed)}).`);
}
async function afterDelete(id, message) {
  notice(message);
  state.projectMarkup = "";
  if (state.id === id) {
    state.id = null;
    $("#project-view").hidden = true;
    $("#new-project").click();
  }
  await refreshProjects();
}
function renderSourceSlots(info) {
  const out = $("#source-slots");
  if (!info) {
    out.hidden = true;
    return;
  }
  const s = info.settings;
  out.hidden = false;
  out.textContent = `Sources ${info.active}/${s.max_sources} active${info.waiting ? ` · ${info.waiting} waiting` : ""} · render ${info.stages.render.running}/${info.stages.render.slots}${info.paused_reason ? ` · paused: ${info.paused_reason}` : ""}`;
}

// --------------------------------------------------------- live sessions (Phase 5)
const LIVE_KINDS = { obs_folder: "OBS folder", srt: "SRT", url: "Direct URL" };
function renderLive(data) {
  const active = data.sessions.filter((s) => s.state === "active").length;
  $("#live-summary").textContent = `${active} ACTIVE`;
  $("#live-sessions").innerHTML = data.sessions.length
    ? data.sessions
        .map(
          (s) => `<article class="live-session" data-live="${s.project_id}" data-state="${escapeHTML(s.state)}">
        <strong>${escapeHTML(LIVE_KINDS[s.kind] || s.kind)} · ${escapeHTML(s.source)}</strong>
        <span class="status">${escapeHTML(s.state)}</span>
        <small>${escapeHTML(s.message || "")} · buffer ${formatDuration(s.buffered_seconds || 0)} (${s.segments} segments, ${formatSize(s.bytes || 0)}) · ${s.pins} pinned · ${s.reconnects || 0} reconnects</small>
        ${(s.candidates || []).length ? `<ul class="live-candidates">${s.candidates.map((c) => `<li data-state="${escapeHTML(c.state)}">${formatDuration(c.peak)} · ${escapeHTML(c.state.replace("_", " "))}${c.score != null ? ` · ${Number(c.score).toFixed(1)}/10` : ""}${c.latency_seconds ? ` · ready in ${formatDuration(c.latency_seconds)}` : ""}${c.error && c.state === "ai_waiting" ? ` · ${escapeHTML(c.error)}` : ""}</li>`).join("")}</ul>` : ""}
        ${s.state === "active" ? `<button type="button" data-live-stop="${s.project_id}">Stop</button>` : ""}
      </article>`,
        )
        .join("")
    : '<p class="muted">No live sessions yet.</p>';
}
async function loadLive() {
  renderLive(await api("/api/live"));
}
$("#live-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    const form = event.target;
    const result = await api("/api/live/start", {
      kind: form.elements.kind.value,
      source: form.elements.source.value.trim(),
      title: form.elements.title.value.trim() || null,
    });
    notice(`Live session started: ${result.title || result.source}.`);
    await loadLive();
    await refreshProjects();
  }),
);
$("#live-sessions").addEventListener(
  "click",
  safe(async (event) => {
    const stop = event.target.closest("[data-live-stop]");
    if (!stop) return;
    const ok = await askDialog({
      title: "Stop this live session?",
      body: "Recording stops. Buffered footage, marked moments and finished clips are kept.",
      confirmText: "Stop session",
      danger: true,
    });
    if (!ok) return;
    await api(`/api/live/${stop.dataset.liveStop}/stop`, {});
    notice("Live session stopped.");
    await loadLive();
  }),
);

// -------------------------------------------- consent-based publishing (Phase 7)
async function loadConsent() {
  if (!state.id) return null;
  const data = await api(`/api/projects/${state.id}/consent`);
  const c = data.consent;
  $("#autopublish-status").textContent = c
    ? `On: ${c.visibility.toUpperCase()} to ${c.channel_title || c.channel_id}, up to ${c.daily_limit}/day.`
    : "Off: clips are uploaded only when you approve each one.";
  $("#autopublish-open").textContent = c ? "Turn off auto-publish" : "Auto-publish…";
  $("#autopublish-open").dataset.on = c ? "1" : "";
  state.consent = data;
  return data;
}
function consentChoices(form) {
  const visibility = form.querySelector("[name=visibility]:checked")?.value;
  const kids = form.querySelector("[name=made_for_kids]:checked")?.value;
  return {
    channel_id: form.elements.channel_id.value,
    visibility,
    public_confirmed: visibility === "public" && form.elements.public_word.value.trim() === "PUBLIC",
    made_for_kids: kids === "yes" ? true : kids === "no" ? false : null,
    region_code: form.elements.region_code.value.trim().toUpperCase(),
    game: form.elements.game.value.trim(),
    title_template: form.elements.title_template.value,
    description_template: form.elements.description_template.value,
    daily_limit: Number(form.elements.daily_limit.value),
    min_score: Number(form.elements.min_score.value),
    category_id: "20",
    confirm: form.elements.confirm.checked,
  };
}
$("#autopublish-open").addEventListener(
  "click",
  safe(async (event) => {
    const data = state.consent || (await loadConsent());
    if (event.currentTarget.dataset.on) {
      const ok = await askDialog({
        title: "Turn off auto-publish?",
        body: "Clips queued by it that have not started uploading go back to draft. Videos already on YouTube are not changed.",
        confirmText: "Turn off",
        danger: true,
      });
      if (!ok) return;
      const result = await api(`/api/projects/${state.id}/consent-revoke`, {});
      notice(`Auto-publish turned off. ${result.withdrawn} queued upload(s) withdrawn.`);
      return loadConsent();
    }
    if (!data.channel) throw new Error("Connect your YouTube channel first (Publish > Connect).");
    const dialog = $("#consent-dialog");
    const form = $("#consent-form");
    form.elements.channel_id.innerHTML = `<option value="">Choose…</option><option value="${escapeHTML(data.channel.id)}">${escapeHTML(data.channel.title || data.channel.id)}</option>`;
    form.querySelectorAll("[type=radio]").forEach((r) => (r.checked = false));
    form.elements.confirm.checked = false;
    form.elements.public_word.value = "";
    $("#consent-public").hidden = true;
    $("#consent-error").hidden = true;
    $("#consent-preview").textContent = "";
    $("#consent-note").textContent = data.note;
    dialog.showModal();
  }),
);
$("#consent-form").addEventListener("change", (event) => {
  if (event.target.name === "visibility")
    $("#consent-public").hidden = event.target.value !== "public";
});
$("#consent-form").addEventListener(
  "submit",
  safe(async (event) => {
    const action = event.submitter?.value;
    if (action === "cancel") return;
    event.preventDefault();
    const form = event.target;
    const choices = consentChoices(form);
    const error = $("#consent-error");
    error.hidden = true;
    try {
      if (action === "preview") {
        const p = await api(`/api/projects/${state.id}/consent-preview`, choices);
        $("#consent-preview").textContent = `Example: "${p.title}" — ${p.description}`;
        return;
      }
      await api(`/api/projects/${state.id}/consent`, choices);
    } catch (problem) {
      error.textContent = problem.message;
      error.hidden = false;
      return;
    }
    $("#consent-dialog").close("save");
    notice(`Auto-publish is on (${choices.visibility}).`);
    await loadConsent();
  }),
);

// ------------------------------------------------------ discovery (Phase 8)
function discoverCard(item, inList) {
  return `<article class="discover-item" data-video="${escapeHTML(item.id)}">
    <a href="${escapeHTML(item.url)}" target="_blank" rel="noopener">${item.thumbnail ? `<img src="${escapeHTML(item.thumbnail)}" alt="" loading="lazy">` : ""}</a>
    <div><a href="${escapeHTML(item.url)}" target="_blank" rel="noopener"><strong>${escapeHTML(item.title)}</strong></a>
    <small><a href="https://www.youtube.com/channel/${escapeHTML(item.channel_id)}" target="_blank" rel="noopener">${escapeHTML(item.channel_title)}</a>${item.live === "live" ? " · LIVE" : ""} · on YouTube${item.has_project ? " · already a project" : ""}</small>
    ${inList ? `<button type="button" data-discover-remove="${escapeHTML(item.id)}">Remove</button>` : `<button type="button" data-discover-add="${escapeHTML(item.id)}">Add to list</button>`}</div>
  </article>`;
}
function renderDiscover(result) {
  state.discover = result;
  $("#discover-usage").textContent = `${result.usage.search_left} SEARCHES LEFT TODAY`;
  const attribution = $("#discover-attribution");
  attribution.hidden = false;
  attribution.textContent = `${result.attribution}${result.cached ? " (cached under 1 hour)" : ""}`;
  $("#discover-results").innerHTML = result.items.length
    ? result.items.map((item) => discoverCard(item, false)).join("")
    : '<p class="muted">No results.</p>';
}
async function loadWatchlist() {
  const result = await api("/api/discover/watch");
  state.watch = result.items;
  $("#discover-watch").innerHTML = result.items.length
    ? result.items.map((item) => discoverCard(item, true)).join("")
    : '<p class="muted">Nothing saved yet.</p>';
}
$("#discover-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    const form = event.target;
    const params = new URLSearchParams({
      q: form.elements.q.value.trim(),
      order: form.elements.order.value,
      region: form.elements.region.value.trim().toUpperCase(),
    });
    if (form.elements.live.checked) params.set("live", "1");
    renderDiscover(await api(`/api/discover?${params}`));
  }),
);
$("#discover-popular").addEventListener(
  "click",
  safe(async () => {
    const region = $("#discover-form").elements.region.value.trim().toUpperCase();
    renderDiscover(await api(`/api/discover/popular?region=${encodeURIComponent(region)}`));
  }),
);
document.addEventListener(
  "click",
  safe(async (event) => {
    const add = event.target.closest("[data-discover-add]");
    const remove = event.target.closest("[data-discover-remove]");
    if (add) {
      const item = (state.discover?.items || []).find((i) => i.id === add.dataset.discoverAdd);
      await api("/api/discover/add", { item });
      notice("Saved to your list (link only; nothing is downloaded).");
      await loadWatchlist();
    } else if (remove) {
      await api("/api/discover/remove", { id: remove.dataset.discoverRemove });
      await loadWatchlist();
    }
  }),
);
