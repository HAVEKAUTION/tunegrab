"use strict";
// ============================================================== helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name, cls = "") => `<svg class="i ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const fmtTime = (s) => { s = Math.round(s || 0); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${r}` : `${m}:${r}`; };
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const when = (iso) => { if (!iso) return ""; const d = new Date(iso); return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); };

class ApiError extends Error { constructor(message, body = {}) { super(message); this.body = body; } }
async function api(path, options = {}) {
  let res;
  try { res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options }); }
  catch { throw new ApiError("TuneGrab isn't responding. Is the TuneGrab window still open?"); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail;
    throw new ApiError(detail || `Request failed (${res.status})`, body);
  }
  return body;
}

// ============================================================== toasts
function toast(message, { kind = "ok", action = null, timeout = 4500 } = {}) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.setAttribute("role", kind === "bad" ? "alert" : "status");
  el.innerHTML = `${icon(kind === "bad" ? "alert" : kind === "warn" ? "info" : "check")}<div class="grow small">${esc(message)}</div>
    ${action ? `<button class="btn sm">${esc(action.label)}</button>` : ""}
    <button class="btn ghost icon sm" aria-label="Dismiss">${icon("x")}</button>`;
  const close = () => el.remove();
  if (action) $(".btn.sm", el).addEventListener("click", () => { action.run(); close(); });
  $(".btn.icon", el).addEventListener("click", close);
  $("#toasts").append(el);
  if (timeout) setTimeout(close, timeout);
}

// Cover art can expire or refuse a size: show the placeholder instead of a broken image.
document.addEventListener("error", (e) => {
  const img = e.target;
  if (!(img instanceof HTMLImageElement)) return;
  const holder = img.closest(".cover");
  if (holder) { holder.innerHTML = icon("music"); return; }
  const ph = document.createElement("span");
  ph.className = img.closest(".artist-head, .qrow") ? "ph" : "img-missing";
  img.replaceWith(ph);
}, true);

// ============================================================== navigation
const TABS = ["get", "jobs", "library", "settings"];
function showTab(name, { focus = false } = {}) {
  if (!TABS.includes(name)) name = "get";
  for (const tab of TABS) $(`#tab-${tab}`).classList.toggle("hidden", tab !== name);
  $$(".nav-item").forEach((b) => b.dataset.tab === name ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"));
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  document.title = `${{ get: "Get music", jobs: "Downloads", library: "Library", settings: "Settings" }[name]} · TuneGrab`;
  $("#queuebar").classList.toggle("hidden", name !== "get" || !queue.size);
  if (name === "library") loadLibrary();
  if (name === "settings") { loadSettings(); renderSystem(); loadStorage(); }
  if (name === "jobs") loadHistory();
  if (focus) $(`#tab-${name} h1`).focus?.();
}
$$(".nav-item").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

// ============================================================== storage
const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : n < 1073741824 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1073741824).toFixed(2)} GB`;
async function loadStorage() {
  const uv = $("#st-uv").checked;
  try {
    const { items } = await api(`/api/storage${uv ? "?uv=1" : ""}`);
    $("#st-table").innerHTML = items.map((i) => `<tr><td><div class="t-title">${esc(i.label)}</div><div class="xs faint mono">${esc(i.path)}</div>
      ${i.note ? `<div class="xs faint">${esc(i.note)}</div>` : ""}</td><td class="c-end tnum">${fmtBytes(i.size)}</td></tr>`).join("");
  } catch (err) { $("#st-table").innerHTML = `<tr><td class="small error-text">${esc(err.message)}</td></tr>`; }
}
$("#st-uv").addEventListener("change", () => { $("#st-table").innerHTML = `<tr><td class="muted small">Measuring…</td></tr>`; loadStorage(); });
$("#st-clean").addEventListener("click", async () => {
  const btn = $("#st-clean"); btn.disabled = true;
  const keys = ["tmp", "logs", "database", "ytdlp_cache", ...($("#st-uv").checked ? ["uv_cache"] : [])];
  try {
    const r = await api("/api/storage/clean", { method: "POST", body: JSON.stringify({ keys }) });
    toast(r.freed ? `Freed ${fmtBytes(r.freed)}.` : "Nothing to free: already tidy.");
  } catch (err) { toast(err.message, { kind: "bad" }); }
  btn.disabled = false; loadStorage();
});

// ============================================================== system status
const SOURCE_URL = "https://github.com/HAVEKAUTION/tunegrab";
const state = { jobs: new Map(), tasks: new Map(), history: [], library: null, system: null };
async function loadSystem(force = false) {
  try {
    state.system = await api(`/api/system${force ? "?fresh=1" : ""}`);
  } catch (err) { state.system = null; renderSystemError(err.message); return; }
  renderSystem();
}
function renderSystemError(message) {
  $("#sys-status").innerHTML = `<span class="dot bad"></span><span>Couldn't check tools</span>`;
  $("#system-banner").innerHTML = `<div class="banner bad" role="alert">${icon("alert", "lg")}<div class="grow"><b>Can't check your setup.</b> <span class="muted">${esc(message)}</span></div></div>`;
}
function renderSystem() {
  const s = state.system;
  if (!s) return;
  $("#version").textContent = `Version ${s.version}`;
  const failing = s.checks.filter((c) => c.status === "fail");
  const warning = s.checks.filter((c) => c.status === "warn");
  $("#sys-status").innerHTML = failing.length
    ? `<span class="dot bad"></span><span>${plural(failing.length, "tool")} missing</span>`
    : `<span class="dot ok"></span><span>Tools ready</span>`;
  $("#system-banner").innerHTML = failing.length ? `<div class="banner bad" role="alert">${icon("alert", "lg")}<div class="grow">
      <b>Downloads won't work until this is fixed:</b>
      ${failing.map((c) => `<div class="small mt-1">${esc(c.name)}: ${esc(c.detail)}${c.hint ? ` — <span class="mono">${esc(c.hint)}</span>` : ""}</div>`).join("")}
      <div class="small muted mt-1">Then restart TuneGrab.</div></div>
      <button class="btn sm" data-recheck>${icon("refresh")}Check again</button></div>` : "";
  $("#system-banner").innerHTML += (s.warnings || []).map((w) => `<div class="banner warn" role="status">${icon("info", "lg")}<div class="grow small">${esc(w)}</div>
      <button class="btn sm ghost" data-dismiss-warnings>${icon("x")}Dismiss</button></div>`).join("");
  $("#system-banner").innerHTML += updateBanners(s.updates);
  $("#sys-table").innerHTML = s.checks.map((c) => {
    const kind = { ok: ["ok", "check", "Ready"], warn: ["warn", "info", "Optional"], fail: ["bad", "alert", "Missing"] }[c.status] || ["", "info", c.status];
    return `<tr><td>${icon(kind[1])}</td><td><div class="t-title">${esc(c.name)}</div><div class="t-sub">${esc(c.detail)}</div>
      ${c.hint ? `<div class="xs faint mono">${esc(c.hint)}</div>` : ""}</td><td class="c-end"><span class="pill ${kind[0]}">${kind[2]}</span></td></tr>`;
  }).join("");
  $("#sys-table").closest("table").setAttribute("aria-label", warning.length ? `${warning.length} optional items` : "System checks");
  const p = s.paths;
  $("#about").innerHTML = [["Version", s.version, null], ["Music folder", p.library, p.library], ["Log file", p.log, p.log],
    ["Settings file", p.config, p.config], ["Download history", p.archive, p.archive]]
    .map(([k, v, copy]) => `<dt>${k}</dt><dd class="${copy ? "mono" : ""}">${esc(v)}</dd><dd>${copy ? `<button class="btn ghost icon sm" data-copy="${esc(copy)}" aria-label="Copy ${k.toLowerCase()} path" title="Copy path">${icon("copy")}</button>` : ""}</dd>`).join("")
    + `<dt>Licence</dt><dd>GPL-3.0-or-later. Free software, shared as it is, with no warranty.</dd><dd></dd>
       <dt>Source</dt><dd><a href="${SOURCE_URL}" target="_blank" rel="noopener">${SOURCE_URL.replace("https://", "")} ${icon("external")}</a></dd><dd></dd>`;
  renderFirstRun();
}
// newer TuneGrab / yt-dlp (checked by the server once a day); each version can be dismissed
const dismissedUpdates = new Set(JSON.parse(localStorage.getItem("tg-dismissed-updates") || "[]"));
function updateBanners(u) {
  if (!u) return "";
  const out = [];
  if (u.tunegrab && !dismissedUpdates.has(`tunegrab:${u.tunegrab}`)) {
    out.push(`<div class="banner" role="status">${icon("download", "lg")}<div class="grow small"><b>TuneGrab ${esc(u.tunegrab)} is available</b> (you have ${esc(u.tunegrab_current)}).
      To update, run the install line again (or <span class="mono">git pull</span>), then restart TuneGrab.
      ${u.tunegrab_url ? `<a href="${esc(u.tunegrab_url)}" target="_blank" rel="noopener">What's new</a>` : ""}</div>
      <button class="btn sm ghost" data-dismiss-update="tunegrab:${esc(u.tunegrab)}">${icon("x")}Dismiss</button></div>`);
  }
  if (u.ytdlp && !dismissedUpdates.has(`ytdlp:${u.ytdlp}`)) {
    out.push(`<div class="banner" role="status">${icon("download", "lg")}<div class="grow small"><b>yt-dlp ${esc(u.ytdlp)} is available</b> (you have ${esc(u.ytdlp_current || "an older one")}).
      YouTube changes often, and a current yt-dlp keeps downloads working.</div>
      <button class="btn sm" data-update-ytdlp>${icon("download")}Update yt-dlp</button>
      <button class="btn sm ghost" data-dismiss-update="ytdlp:${esc(u.ytdlp)}">${icon("x")}Dismiss</button></div>`);
  }
  return out.join("");
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-dismiss-update]");
  if (!b) return;
  dismissedUpdates.add(b.dataset.dismissUpdate);
  localStorage.setItem("tg-dismissed-updates", JSON.stringify([...dismissedUpdates]));
  renderSystem();
});
document.addEventListener("click", async (e) => {
  const recheck = e.target.closest("[data-recheck], #sys-recheck");
  if (recheck) { recheck.disabled = true; await loadSystem(true); recheck.disabled = false; toast("Checked your setup again."); }
  const update = e.target.closest("[data-update-ytdlp]");
  if (update) {
    update.disabled = true;
    const label = update.innerHTML;
    update.innerHTML = `${icon("refresh", "spin")}Updating…`;
    try {
      await api("/api/system/update-ytdlp", { method: "POST" });
      toast("yt-dlp updated. Restart TuneGrab to use the new version.", { timeout: 10000 });
    } catch (err) { toast(err.message, { kind: "bad", timeout: 12000 }); }
    update.innerHTML = label; update.disabled = false;
    loadSystem(true);
  }
  const dismiss = e.target.closest("[data-dismiss-warnings]");
  if (dismiss) {
    dismiss.disabled = true;
    try { await api("/api/system/warnings", { method: "DELETE" }); if (state.system) state.system.warnings = []; renderSystem(); }
    catch (err) { dismiss.disabled = false; toast(err.message, { kind: "bad" }); }
  }
  const copy = e.target.closest("[data-copy]");
  if (copy) {
    try { await navigator.clipboard.writeText(copy.dataset.copy); toast("Path copied."); }
    catch { toast("Couldn't copy. Select the path and copy it manually.", { kind: "warn" }); }
  }
});

// ============================================================== get music: browse
// queue: Map key -> {key, kind: "spotify"|"ytmusic"|"url"|"tracks", id, url, name, artist, cover, year, total, trackIds}
const queue = new Map(JSON.parse(localStorage.getItem("tg-queue") || "[]"));
const view = { data: null, filter: "all", sheetKey: null, releaseCache: new Map(), loading: false };
const saveQueue = () => localStorage.setItem("tg-queue", JSON.stringify([...queue]));
const relKey = (r) => `${r.source}:${r.id}`;
const findRelease = (key) => (view.data?.releases || []).find((r) => relKey(r) === key);
const KIND_LABEL = { album: "Album", single: "Single / EP", compilation: "Compilation" };

$("#format").value = localStorage.getItem("tg-format") || "";
$("#format").addEventListener("change", () => localStorage.setItem("tg-format", $("#format").value));

function setFinderError(message) {
  $("#finder-error").textContent = message || "";
  $("#url").setAttribute("aria-invalid", message ? "true" : "false");
}

async function browse(text) {
  text = (text ?? $("#url").value).trim();
  if (!text) { setFinderError("Paste a Spotify or YouTube link, or type an artist, album or song."); $("#url").focus(); return; }
  $("#url").value = text;
  setFinderError("");
  view.loading = true; view.error = false;
  const btn = $("#browse-btn"); btn.disabled = true; btn.innerHTML = `${icon("loader", "spin")}Browsing…`;
  $("#browse").innerHTML = skeletonGrid();
  try {
    const data = await api(`/api/browse?url=${encodeURIComponent(text)}`);
    data.input = text;
    view.data = data; view.filter = "all";
    renderBrowse();
  } catch (err) {
    view.data = null; view.error = true;
    $("#browse").innerHTML = `<div class="card empty">${icon("alert")}<h3>Couldn't load that</h3><p>${esc(err.message)}</p>
      <p class="small faint">For a single song, type “artist - title” and choose Download now.</p></div>`;
  } finally {
    view.loading = false; btn.disabled = false; btn.innerHTML = `${icon("search")}Browse`;
  }
}
$("#finder").addEventListener("submit", (e) => { e.preventDefault(); browse(); });

async function downloadNow() {
  const url = $("#url").value.trim();
  if (!url) { setFinderError("Type a link or “artist - title” first."); $("#url").focus(); return; }
  const btn = $("#now-btn"); btn.disabled = true; btn.innerHTML = `${icon("loader", "spin")}Finding…`;
  try {
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify({ url, format: $("#format").value || null }) });
    upsertJob(job);
    // a typed search is named after the song it picked; a link is named once it's read
    toast(job.name !== url ? `Downloading “${job.name}”.` : `Downloading “${url}”.`, { action: { label: "View", run: () => showTab("jobs") } });
    $("#url").value = "";
  } catch (err) {
    if (err.body && err.body.browse) { toast(err.message, { kind: "warn" }); browse(err.body.browse); }
    else setFinderError(err.message);
  } finally { btn.disabled = false; btn.innerHTML = `${icon("download")}Download now`; }
}
$("#now-btn").addEventListener("click", downloadNow);

// CSV import (Exportify etc.): the browser reads the file and sends its text.
const CSV_MAX_BYTES = 20e6;
$("#csv-btn").addEventListener("click", () => $("#csv-file").click());
$("#csv-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (file.size > CSV_MAX_BYTES) { setFinderError("That file is over 20 MB. Split it into smaller lists."); return; }
  const name = file.name.replace(/\.csv$/i, "").replace(/_/g, " ").trim() || "Imported songs";
  const btn = $("#csv-btn"); btn.disabled = true; setFinderError("");
  try {
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify({ csv_text: await file.text(), name, format: $("#format").value || null }) });
    upsertJob(job);
    toast(`Importing “${name}”.`, { action: { label: "View", run: () => showTab("jobs") } });
  } catch (err) { setFinderError(err.message); }
  finally { btn.disabled = false; }
});

function skeletonGrid() {
  return `<div class="row loading-head"><div class="skeleton avatar"></div>
    <div class="stack tight"><div class="skeleton line-lg"></div><div class="skeleton line"></div></div></div>
    <div class="grid" aria-busy="true" aria-label="Loading">${Array.from({ length: 10 }, () => `<div><div class="skeleton sq"></div>
    <div class="skeleton caption"></div></div>`).join("")}</div>`;
}

function renderFirstRun() {
  if (view.data || view.loading || view.error) return;
  const s = state.system;
  $("#browse").innerHTML = `<div class="card empty">${icon("music")}
    <h3>Start with an artist</h3>
    <p>Type an artist to see their albums and singles, or an album or song to find it. Spotify links work too.</p>
    <div class="examples">${["Daft Punk", "Fleetwood Mac", "Tame Impala"].map((n) => `<button class="chip" data-example="${esc(n)}">${esc(n)}</button>`).join("")}</div>
    ${s ? `<p class="small faint mt-3">Music is saved to <span class="mono">${esc(s.paths.library)}</span>
      ${s.default_folder ? ` · <button class="linkish link" data-goto="settings">Choose another folder</button>` : ""}</p>` : ""}
  </div>`;
}

async function openArtist(btn) {
  const d = view.data, hit = d.artist;
  btn.disabled = true; btn.innerHTML = `${icon("loader", "spin")}Opening ${esc(hit.name)}…`;
  try {
    const data = await api(`/api/browse/ytmusic?artist=${encodeURIComponent(hit.name)}&channel=${encodeURIComponent(hit.id)}`);
    data.input = hit.name; $("#url").value = hit.name;
    view.data = data; view.filter = "all"; renderBrowse();
  } catch (err) {
    btn.disabled = false; btn.innerHTML = `${icon("user")}${esc(hit.name)}`;
    toast(`Couldn't open ${hit.name}: ${err.message}`, { kind: "bad", timeout: 8000 });
  }
}

async function fullDiscography(btn) {
  btn.disabled = true; btn.innerHTML = `${icon("loader", "spin")}Loading every release…`;
  try {
    const yt = await api(`/api/browse/ytmusic?artist=${encodeURIComponent(view.data.name)}`);
    yt.input = view.data.input; yt.top_tracks = view.data.top_tracks;
    view.data = yt; view.filter = "all"; renderBrowse();
    toast(`Loaded ${plural(yt.releases.length, "release")} from YouTube Music.`);
  } catch (err) {
    btn.disabled = false; btn.innerHTML = `${icon("refresh")}Full discography`;
    toast(`Couldn't load the full discography: ${err.message}`, { kind: "bad", timeout: 8000 });
  }
}

function renderBrowse() {
  const d = view.data;
  if (!d) return;
  $("#browse").innerHTML = d.type === "artist" ? artistHtml(d) : d.type === "video" ? videoHtml(d)
    : d.type === "search" ? searchHtml(d) : collectionHtml(d);
  if (view.sheetKey && $("#sheet").open) renderSheet();
}

// ============================================================== get music: video links
const VQ_LABEL = { best: "Best available", 2160: "4K (2160p)", 1440: "1440p", 1080: "1080p", 720: "720p", 480: "480p", 360: "360p" };
const videoForm = { mode: "video" };
function videoHtml(v) {
  const single = v.kind === "video";
  const top = v.heights && v.heights.length ? v.heights[0] : null;
  const qualities = ["best", "2160", "1440", "1080", "720", "480", "360"].filter((q) => q === "best" || !top || Number(q) <= top || q === v.default_quality);
  const quality = qualities.includes(v.default_quality) ? v.default_quality : "best";
  const meta = single
    ? [v.uploader, v.duration_s ? fmtTime(v.duration_s) : null, top ? `up to ${top}p` : null, v.site].filter(Boolean).join(" · ")
    : [v.uploader, plural(v.count, "video"), v.site].filter(Boolean).join(" · ");
  const list = !single && v.entries.length ? `<div class="card mt-4"><table><tbody>${v.entries.map((e, i) => `<tr><td class="c-num tnum">${i + 1}</td>
      <td><div class="t-title">${esc(e.title)}</div><div class="t-sub">${esc(e.uploader)}</div></td><td class="c-time tnum">${e.duration_s ? fmtTime(e.duration_s) : ""}</td></tr>`).join("")}</tbody></table>
      ${v.count > v.entries.length ? `<p class="small faint mt-2">and ${v.count - v.entries.length} more</p>` : ""}</div>` : "";
  return `<div class="video-head">${v.thumbnail ? `<img class="thumb" src="${esc(v.thumbnail)}" alt="">` : `<span class="thumb"></span>`}
      <div><div class="xs faint eyebrow">${single ? "Video" : "Playlist"}</div><h2>${esc(v.title)}</h2>
      <div class="small muted">${esc(meta)}</div></div></div>
    <form class="card" id="video-form" novalidate>
      <div class="video-form">
        <div class="field"><span class="label" id="vmode-label">Save as</span><div class="chips" role="group" aria-labelledby="vmode-label">
          <button type="button" class="chip" data-vmode="video" aria-pressed="${videoForm.mode === "video"}">Video (MP4)</button>
          <button type="button" class="chip" data-vmode="audio" aria-pressed="${videoForm.mode === "audio"}">Audio only</button></div></div>
        <label class="field" id="vq-field" ${videoForm.mode === "audio" ? "hidden" : ""}><span class="label">Quality</span><select id="v-quality" class="select">
          ${qualities.map((q) => `<option value="${q}" ${q === quality ? "selected" : ""}>${VQ_LABEL[q]}</option>`).join("")}</select></label>
        ${single ? `<label class="field"><span class="label">Clip from</span><input id="v-start" class="input time tnum" type="text" inputmode="decimal" placeholder="0:00" aria-describedby="v-help"></label>
        <label class="field"><span class="label">to</span><input id="v-end" class="input time tnum" type="text" inputmode="decimal" placeholder="${v.duration_s ? fmtTime(v.duration_s) : "end"}" aria-describedby="v-help"></label>` : ""}
        <button class="btn primary" type="submit" id="v-go">${icon("download")}Download</button>
      </div>
      <p class="small faint mt-3" id="v-help">${single ? "Leave the clip times empty for the whole video. " : ""}${videoForm.mode === "audio" ? `Audio goes to your music folder in the format chosen above.` : `Saved to <span class="mono">${esc(v.folder)}</span>. MP4 with H.264 where YouTube offers it, so it plays everywhere.`}</p>
      <div class="small error-text" id="v-error" role="alert"></div>
    </form>${list}`;
}
document.addEventListener("click", (e) => {
  const mode = e.target.closest("[data-vmode]");
  if (!mode || !view.data || view.data.type !== "video") return;
  const start = $("#v-start")?.value, end = $("#v-end")?.value, quality = $("#v-quality")?.value;
  videoForm.mode = mode.dataset.vmode;
  renderBrowse();
  if ($("#v-start")) { $("#v-start").value = start || ""; $("#v-end").value = end || ""; }
  if (quality) $("#v-quality").value = quality;
});
document.addEventListener("submit", async (e) => {
  if (e.target.id !== "video-form") return;
  e.preventDefault();
  const v = view.data, btn = $("#v-go");
  $("#v-error").textContent = "";
  const body = { url: v.url, mode: videoForm.mode, quality: $("#v-quality").value, start: $("#v-start")?.value.trim() || null, end: $("#v-end")?.value.trim() || null, format: $("#format").value || null };
  btn.disabled = true;
  try {
    const job = await api("/api/video", { method: "POST", body: JSON.stringify(body) });
    upsertJob(job);
    const what = body.start || body.end ? "clip" : videoForm.mode === "audio" ? "audio" : v.kind === "video" ? "video" : "videos";
    toast(`Downloading the ${what}: “${v.title}”.`, { action: { label: "View", run: () => showTab("jobs") } });
  } catch (err) { $("#v-error").textContent = err.message; }
  finally { btn.disabled = false; }
});

function artistHtml(d) {
  const counts = { album: 0, single: 0, compilation: 0 };
  d.releases.forEach((r) => counts[r.kind]++);
  const shown = d.releases.filter((r) => view.filter === "all" || r.kind === view.filter);
  const chip = (k, label, n) => n || k === "all" ? `<button class="chip" aria-pressed="${view.filter === k}" data-filter="${k}">${label}${k === "all" ? "" : ` <span class="faint">${n}</span>`}</button>` : "";
  const source = d.source === "ytmusic" ? "YouTube Music · complete" : d.source === "spotify-api" ? "Spotify · complete" : "Spotify · newest releases";
  return `<div class="artist-head">${d.image_url ? `<img src="${esc(d.image_url)}" alt="">` : `<span class="ph"></span>`}
      <div class="grow"><h2>${esc(d.name)}</h2><div class="small muted">${plural(d.releases.length, "release")} <span class="pill ml-2">${source}</span></div></div>
      ${!d.complete ? `<button class="btn" data-full>${icon("refresh")}Full discography</button>` : ""}</div>
    ${!d.complete ? `<div class="banner">${icon("info", "lg")}<div class="grow small">Spotify's public page only lists the newest 8 albums and 8 singles. <b>Full discography</b> loads every release from YouTube Music.</div></div>` : ""}
    <div class="toolbar"><div class="chips" role="group" aria-label="Filter releases">${chip("all", "All", d.releases.length)}${chip("album", "Albums", counts.album)}${chip("single", "Singles & EPs", counts.single)}${chip("compilation", "Compilations", counts.compilation)}</div>
      <div class="row"><button class="btn sm" data-tick-all>${icon("check")}Select all shown</button><button class="btn sm ghost" data-untick-all>Clear selection</button></div></div>
    ${shown.length ? `<div class="grid">${shown.map(releaseHtml).join("")}</div>` : `<div class="card empty"><p>No releases in this group.</p></div>`}
    ${(d.top_tracks || []).length ? topTracksHtml(d) : ""}`;
}

// plain text that isn't an artist's name: matching albums and songs from YouTube Music
const songsKey = (d) => `songs:${d.query}`;
function searchHtml(d) {
  const q = queue.get(songsKey(d));
  const picked = new Set(q ? q.trackIds : []);
  const all = d.songs.length && d.songs.every((s) => picked.has(s.id));
  const some = d.songs.some((s) => picked.has(s.id));
  const songs = d.songs.length ? `<div class="card section"><h3>Songs</h3><table>
    <thead><tr><th class="c-check"><input type="checkbox" data-song-all ${all ? "checked" : ""} ${!all && some ? "data-mixed" : ""} aria-label="Select all songs"></th>
      <th>Title</th><th class="c-time">Time</th></tr></thead><tbody>
    ${d.songs.map((s) => `<tr class="${picked.has(s.id) ? "on" : ""}"><td class="c-check"><input type="checkbox" data-song-pick="${esc(s.id)}" ${picked.has(s.id) ? "checked" : ""} aria-label="Queue ${esc(s.title)}"></td>
      <td><div class="t-title">${esc(s.title)}${s.explicit ? ` <span class="pill" title="Explicit">E</span>` : ""}</div>
        <div class="t-sub">${esc([s.artists.join(", "), s.album].filter(Boolean).join(" · "))}</div></td>
      <td class="c-time tnum">${s.duration_s ? fmtTime(s.duration_s) : ""}</td></tr>`).join("")}
    </tbody></table></div>` : "";
  const albums = d.releases.length ? `<h3 class="sub-head">Albums and singles</h3><div class="grid">${d.releases.map(releaseHtml).join("")}</div>` : "";
  return `<div class="artist-head"><div class="grow"><div class="xs faint eyebrow">Search</div>
      <h2>${esc(d.query)}</h2><div class="small muted">${[d.releases.length ? plural(d.releases.length, "album") : "", d.songs.length ? plural(d.songs.length, "song") : ""].filter(Boolean).join(" and ")}
      <span class="pill ml-2">YouTube Music</span></div></div>
      ${d.artist ? `<button class="btn" data-artist-open title="See every release by ${esc(d.artist.name)}">${icon("user")}${esc(d.artist.name)}</button>` : ""}</div>
    ${albums}${songs}`;
}
function toggleSong(id, on, all = false) {
  const d = view.data, key = songsKey(d);
  const q = queue.get(key);
  let picked = new Set(q ? q.trackIds : []);
  if (all) picked = on ? new Set(d.songs.map((s) => s.id)) : new Set(); else if (on) picked.add(id); else picked.delete(id);
  const songs = d.songs.filter((s) => picked.has(s.id));
  if (!songs.length) queue.delete(key);
  else queue.set(key, { key, kind: "songs", name: songs.length === 1 ? songs[0].title : `Songs for “${d.query}”`,
                        artist: [...new Set(songs.flatMap((s) => s.artists))].slice(0, 3).join(", "),
                        cover: songs[0].cover_url, total: songs.length, trackIds: songs.map((s) => s.id), songs });
  changed();
}

function releaseHtml(r) {
  const key = relKey(r), q = queue.get(key);
  const cls = q ? (q.trackIds ? "part" : "on") : "";
  const sub = [r.artist, r.year, KIND_LABEL[r.kind]].filter(Boolean).join(" · ");
  const state = q ? (q.trackIds ? `${q.trackIds.length} songs picked` : "whole release queued") : "not queued";
  return `<article class="rel ${cls}">
    <button class="pick" data-rel="${esc(key)}" aria-pressed="${q ? (q.trackIds ? "mixed" : "true") : "false"}" aria-label="${esc(r.name)}, ${esc(sub)}, ${state}">
      <div class="art">${r.cover_url ? `<img src="${esc(r.cover_url)}" alt="" loading="lazy">` : ""}
        <span class="tick" aria-hidden="true">${icon(q && q.trackIds ? "minus" : "check")}</span></div>
      <div class="meta"><div class="name">${esc(r.name)}</div><div class="sub">${esc(sub)}${q && q.trackIds ? ` · <span class="warn-text">${q.trackIds.length} picked</span>` : ""}</div></div>
    </button>
    <button class="songs" data-songs="${esc(key)}">${icon("list")}Choose songs</button>
  </article>`;
}

function topTracksHtml(d) {
  const key = `tracks:${d.id || d.name}`, q = queue.get(key);
  const picked = new Set(q ? q.trackIds : []);
  return `<div class="card section"><h3>Popular songs</h3><table><tbody>
    ${d.top_tracks.map((t, i) => `<tr class="${picked.has(t.spotify_id) ? "on" : ""}"><td class="c-check"><input type="checkbox" data-top="${esc(t.spotify_id)}" ${picked.has(t.spotify_id) ? "checked" : ""} aria-label="Queue ${esc(t.title)}"></td>
      <td class="c-num tnum">${i + 1}</td><td><span class="t-title">${esc(t.title)}</span></td><td class="t-sub">${esc(t.artists.join(", "))}</td></tr>`).join("")}
    </tbody></table></div>`;
}

function trackTable(tracks, picked, attr) {
  const ids = tracks.filter((t) => t.id);
  const all = ids.length && ids.every((t) => picked(t.id));
  const some = ids.some((t) => picked(t.id));
  return `<table><thead><tr><th class="c-check"><input type="checkbox" data-${attr}-all ${all ? "checked" : ""} ${!all && some ? "data-mixed" : ""} aria-label="Select all songs"></th>
    <th class="c-num">#</th><th>Title</th><th class="c-time">Time</th></tr></thead>
    <tbody>${tracks.map((t, i) => `<tr class="${picked(t.id) ? "on" : ""}"><td class="c-check">${t.id ? `<input type="checkbox" data-${attr}="${esc(t.id)}" ${picked(t.id) ? "checked" : ""} aria-label="Queue ${esc(t.title)}">` : ""}</td>
      <td class="c-num tnum">${t.track_number || i + 1}</td><td><div class="t-title">${esc(t.title)}</div><div class="t-sub">${esc(t.artists.join(", "))}${t.album && attr === "ct" ? ` · ${esc(t.album)}` : ""}</div></td>
      <td class="c-time tnum">${t.duration_ms ? fmtTime(t.duration_ms / 1000) : ""}</td></tr>`).join("")}</tbody></table>`;
}

function collectionHtml(c) {
  const key = `url:${c.input}`, q = queue.get(key);
  const picked = (id) => !!q && (!q.trackIds || q.trackIds.includes(id));
  const total = c.tracks.reduce((sum, t) => sum + t.duration_ms / 1000, 0);
  const cover = (c.tracks[0] || {}).cover_url;
  return `<div class="artist-head">${cover ? `<img src="${esc(cover)}" alt="" class="square">` : `<span class="ph square"></span>`}
      <div class="grow"><div class="xs faint eyebrow">${esc(c.kind)}</div><h2>${esc(c.name)}</h2>
      <div class="small muted">${c.owner ? `${esc(c.owner)} · ` : ""}${plural(c.tracks.length, "song")} · ${fmtTime(total)}</div></div></div>
    ${(c.warnings || []).map((w) => `<div class="banner">${icon("info", "lg")}<div class="grow small">${esc(w)}</div></div>`).join("")}
    <div class="card">${trackTable(c.tracks, picked, "ct")}</div>`;
}
function syncMixed(root) { $$("[data-mixed]", root).forEach((el) => { el.indeterminate = true; }); }

// ---- release songs sheet
async function openSheet(key) {
  view.sheetKey = key;
  const sheet = $("#sheet");
  const rel = findRelease(key);
  sheet.innerHTML = `<div class="d-head"><div><h3 id="sheet-title">${esc(rel.name)}</h3><div class="small muted">${esc([rel.year, KIND_LABEL[rel.kind]].filter(Boolean).join(" · "))}</div></div>
    <button class="btn ghost icon" data-close aria-label="Close">${icon("x")}</button></div>
    <div class="d-body" id="sheet-body"><div class="stack">${Array.from({ length: 8 }, () => `<div class="skeleton line-row"></div>`).join("")}</div></div>
    <div class="d-foot"><span class="small muted push" id="sheet-count"></span><button class="btn primary" data-close>Done</button></div>`;
  if (!sheet.open) sheet.showModal();
  try { await releaseTracks(rel); if (view.sheetKey === key) renderSheet(); }
  catch (err) { $("#sheet-body").innerHTML = `<div class="empty">${icon("alert")}<p>${esc(err.message)}</p><button class="btn" data-retry-sheet>${icon("refresh")}Try again</button></div>`; }
}
async function renderSheet() {
  const rel = findRelease(view.sheetKey);
  if (!rel || !view.releaseCache.has(view.sheetKey)) return;
  const c = await view.releaseCache.get(view.sheetKey);
  const q = queue.get(view.sheetKey);
  const picked = (id) => !!q && (!q.trackIds || q.trackIds.includes(id));
  const body = $("#sheet-body");
  const scroll = body.scrollTop;
  body.innerHTML = trackTable(c.tracks, picked, "rt");
  body.scrollTop = scroll;
  syncMixed(body);
  const n = q ? (q.trackIds ? q.trackIds.length : c.tracks.length) : 0;
  $("#sheet-count").textContent = n ? `${n} of ${plural(c.tracks.length, "song")} in the queue` : "Nothing from this release is queued";
}
function releaseTracks(rel) {
  const key = relKey(rel);
  if (!view.releaseCache.has(key)) {
    view.releaseCache.set(key, api(`/api/release?source=${rel.source}&id=${encodeURIComponent(rel.id)}`)
      .catch((err) => { view.releaseCache.delete(key); throw err; }));
  }
  return view.releaseCache.get(key);
}
$("#sheet").addEventListener("close", () => { view.sheetKey = null; });

// ---- queue edits
function releaseEntry(rel, trackIds = null, total = null) {
  return { key: relKey(rel), kind: rel.source, id: rel.id, name: rel.name, artist: rel.artist || view.data.name,
           cover: rel.cover_url, year: rel.year, total: total ?? rel.total_tracks, trackIds };
}
function fillTotal(rel) {
  const key = relKey(rel);
  if ((queue.get(key) || {}).total) return;
  releaseTracks(rel).then((c) => {
    const q = queue.get(key);
    if (q && !q.total) { q.total = c.tracks.length; saveQueue(); renderQueue(); }
  }).catch((err) => console.warn("song count unavailable for", rel.name, err.message));
}
function toggleRelease(key) {
  const rel = findRelease(key);
  if (!rel) return;
  if (queue.has(key)) queue.delete(key); else { queue.set(key, releaseEntry(rel)); fillTotal(rel); }
  changed();
}
async function toggleReleaseTrack(key, id, on, all = false) {
  const rel = findRelease(key);
  const c = await releaseTracks(rel);
  const ids = c.tracks.map((t) => t.id).filter(Boolean);
  const q = queue.get(key);
  let picked = new Set(q ? (q.trackIds || ids) : []);
  if (all) picked = on ? new Set(ids) : new Set(); else if (on) picked.add(id); else picked.delete(id);
  if (!picked.size) queue.delete(key);
  else queue.set(key, releaseEntry(rel, picked.size === ids.length ? null : [...picked], ids.length));
  changed();
}
function toggleCollectionTrack(id, on, all = false) {
  const c = view.data, key = `url:${c.input}`;
  const ids = c.tracks.map((t) => t.id).filter(Boolean);
  const q = queue.get(key);
  let picked = new Set(q ? (q.trackIds || ids) : []);
  if (all) picked = on ? new Set(ids) : new Set(); else if (on) picked.add(id); else picked.delete(id);
  if (!picked.size) queue.delete(key);
  else queue.set(key, { key, kind: "url", url: c.input, name: c.name, artist: c.owner || c.kind, cover: (c.tracks[0] || {}).cover_url,
                        total: ids.length, trackIds: picked.size === ids.length ? null : [...picked] });
  changed();
}
function toggleTop(id, on) {
  const d = view.data, key = `tracks:${d.id || d.name}`;
  const q = queue.get(key);
  const picked = new Set(q ? q.trackIds : []);
  if (on) picked.add(id); else picked.delete(id);
  if (!picked.size) queue.delete(key);
  else queue.set(key, { key, kind: "tracks", name: `${d.name}: popular songs`, artist: d.name, cover: d.image_url, total: picked.size, trackIds: [...picked] });
  changed();
}
function changed() {
  saveQueue(); renderQueue();
  const focusKey = document.activeElement?.dataset?.rel;
  renderBrowse(); syncMixed($("#browse"));
  if (focusKey) $(`[data-rel="${CSS.escape(focusKey)}"]`)?.focus();
}

$("#browse").addEventListener("click", (e) => {
  const t = e.target.closest("button, input");
  if (!t) return;
  if (t.dataset.example) { browse(t.dataset.example); return; }
  if (t.dataset.goto) { showTab(t.dataset.goto); return; }
  if (t.dataset.filter) { view.filter = t.dataset.filter; renderBrowse(); $(`[data-filter="${view.filter}"]`)?.focus(); return; }
  if (t.hasAttribute("data-full")) { fullDiscography(t); return; }
  if (t.hasAttribute("data-artist-open")) { openArtist(t); return; }
  if (t.hasAttribute("data-tick-all") || t.hasAttribute("data-untick-all")) {
    const on = t.hasAttribute("data-tick-all");
    const snapshot = new Map(queue);
    let n = 0;
    for (const r of view.data.releases) {
      if (view.filter !== "all" && r.kind !== view.filter) continue;
      const key = relKey(r);
      if (on && !queue.has(key)) { queue.set(key, releaseEntry(r)); fillTotal(r); n++; }
      if (!on && queue.delete(key)) n++;
    }
    changed();
    if (!on && n) toast(`Removed ${plural(n, "release")} from the queue.`, { action: { label: "Undo", run: () => restoreQueue(snapshot) } });
    return;
  }
  if (t.dataset.songs) { openSheet(t.dataset.songs); return; }
  if (t.dataset.rel) toggleRelease(t.dataset.rel);
});
$("#browse").addEventListener("change", (e) => {
  const t = e.target, on = t.checked;
  if (t.dataset.ct) toggleCollectionTrack(t.dataset.ct, on);
  else if (t.hasAttribute("data-ct-all")) toggleCollectionTrack(null, on, true);
  else if (t.dataset.top) toggleTop(t.dataset.top, on);
  else if (t.dataset.songPick) toggleSong(t.dataset.songPick, on);
  else if (t.hasAttribute("data-song-all")) toggleSong(null, on, true);
});
$("#sheet").addEventListener("change", (e) => {
  const t = e.target, on = t.checked;
  if (t.dataset.rt) toggleReleaseTrack(view.sheetKey, t.dataset.rt, on);
  else if (t.hasAttribute("data-rt-all")) toggleReleaseTrack(view.sheetKey, null, on, true);
});
$("#sheet").addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) $("#sheet").close();
  if (e.target.closest("[data-retry-sheet]")) openSheet(view.sheetKey);
  if (e.target === $("#sheet")) $("#sheet").close(); // backdrop
});

// ---- queue bar + review dialog
const entryCount = (q) => q.trackIds ? q.trackIds.length : q.total;
function restoreQueue(snapshot) { queue.clear(); for (const [k, v] of snapshot) queue.set(k, v); changed(); renderQueueDialog(); }
function renderQueue() {
  const bar = $("#queuebar");
  const onGet = !$("#tab-get").classList.contains("hidden");
  bar.classList.toggle("hidden", !queue.size || !onGet);
  if (!queue.size) { if ($("#queue-dialog").open) $("#queue-dialog").close(); return; }
  const entries = [...queue.values()];
  const songs = entries.reduce((n, q) => n + (entryCount(q) || 0), 0);
  const unknown = entries.some((q) => !entryCount(q));
  bar.innerHTML = `<div class="inner"><div class="covers" aria-hidden="true">${entries.slice(0, 5).map((q) => q.cover ? `<img src="${esc(q.cover)}" alt="">` : "<span></span>").join("")}</div>
    <div class="grow"><div class="t-title">${plural(entries.length, "item")} in the queue</div><div class="small muted tnum">${unknown ? `${songs}+ songs` : plural(songs, "song")}</div></div>
    <button class="btn ghost" data-q-review>${icon("list")}Review</button>
    <button class="btn primary" data-q-go title="Ctrl+Enter">${icon("download")}Download ${plural(entries.length, "item")}</button></div>`;
  if ($("#queue-dialog").open) renderQueueDialog();
}
function renderQueueDialog() {
  const entries = [...queue.values()];
  $("#queue-dialog").innerHTML = `<div class="d-head"><h3 id="queue-title">Download queue</h3><button class="btn ghost icon" data-close aria-label="Close">${icon("x")}</button></div>
    <div class="d-body qlist">${entries.length ? entries.map((q) => `<div class="qrow">
      ${q.cover ? `<img src="${esc(q.cover)}" alt="">` : `<span class="ph"></span>`}
      <div class="grow"><div class="t-title">${esc(q.name)}${q.year ? ` <span class="faint small">${esc(q.year)}</span>` : ""}</div>
        <div class="t-sub">${esc(q.artist || "")} · ${q.kind === "tracks" || q.kind === "songs" ? plural(q.trackIds.length, "song") : q.trackIds ? `${q.trackIds.length} of ${q.total || "?"} songs` : q.total ? `all ${q.total} songs` : "whole release"}${q.kind === "ytmusic" || q.kind === "songs" ? " · YouTube Music" : ""}</div></div>
      <button class="btn ghost icon" data-q-remove="${esc(q.key)}" aria-label="Remove ${esc(q.name)}" title="Remove">${icon("x")}</button></div>`).join("")
      : `<div class="empty"><p>The queue is empty.</p></div>`}</div>
    <div class="d-foot"><button class="btn ghost danger push" data-q-clear ${entries.length ? "" : "disabled"}>Clear queue</button>
      <button class="btn primary" data-q-go ${entries.length ? "" : "disabled"}>${icon("download")}Download all</button></div>`;
}
async function downloadQueue() {
  if (!queue.size) return;
  const format = $("#format").value || null;
  $$("[data-q-go]").forEach((b) => { b.disabled = true; b.innerHTML = `${icon("loader", "spin")}Starting…`; });
  const failed = [];
  let started = 0;
  for (const q of [...queue.values()]) {
    const body = q.kind === "ytmusic" ? { ytm_album: q.id, track_ids: q.trackIds, name: q.name }
      : q.kind === "spotify" ? { url: `spotify:album:${q.id}`, track_ids: q.trackIds }
      : q.kind === "tracks" ? { spotify_tracks: q.trackIds, name: q.name }
      : q.kind === "songs" ? { ytm_songs: q.songs, name: q.name }
      : { url: q.url, track_ids: q.trackIds };
    try { upsertJob(await api("/api/jobs", { method: "POST", body: JSON.stringify({ ...body, format }) })); queue.delete(q.key); started++; }
    catch (err) { failed.push(`${q.name}: ${err.message}`); }
  }
  changed();
  if ($("#queue-dialog").open) $("#queue-dialog").close();
  if (started) toast(`Started ${plural(started, "download")}.`, { action: { label: "View", run: () => showTab("jobs") } });
  for (const f of failed) toast(f, { kind: "bad", timeout: 10000 });
}
function onQueueClick(e) {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.hasAttribute("data-q-review")) { renderQueueDialog(); $("#queue-dialog").showModal(); }
  else if (t.hasAttribute("data-q-go")) downloadQueue();
  else if (t.hasAttribute("data-close")) $("#queue-dialog").close();
  else if (t.dataset.qRemove) {
    const snapshot = new Map(queue), entry = queue.get(t.dataset.qRemove);
    queue.delete(t.dataset.qRemove); changed(); renderQueueDialog();
    toast(`Removed “${entry.name}”.`, { action: { label: "Undo", run: () => restoreQueue(snapshot) } });
  } else if (t.hasAttribute("data-q-clear")) {
    const snapshot = new Map(queue);
    queue.clear(); changed(); $("#queue-dialog").close();
    toast(`Cleared ${plural(snapshot.size, "item")} from the queue.`, { action: { label: "Undo", run: () => restoreQueue(snapshot) }, timeout: 7000 });
  }
}
$("#queuebar").addEventListener("click", onQueueClick);
$("#queue-dialog").addEventListener("click", (e) => { if (e.target === $("#queue-dialog")) $("#queue-dialog").close(); else onQueueClick(e); });

// ============================================================== downloads (jobs)
const ACTIVE = ["resolving", "queued", "running"];
const STATUS = {
  queued: ["", "loader", "Waiting"], resolving: ["info", "loader", "Reading link"], matching: ["info", "search", "Finding audio"],
  downloading: ["info", "download", "Downloading"], converting: ["info", "loader", "Converting"], tagging: ["info", "loader", "Tagging"],
  done: ["ok", "check", "Done"], skipped: ["warn", "minus", "Skipped"], failed: ["bad", "alert", "Failed"],
};
const JOB_STATE = { resolving: ["info", "loader", "Reading link"], queued: ["", "minus", "Waiting"], running: ["info", "loader", "Downloading"],
                    finished: ["ok", "check", "Finished"], cancelled: ["warn", "stop", "Cancelled"], error: ["bad", "alert", "Couldn't start"] };
const jobPill = (state, running = false) => { const [kind, ic, label] = JOB_STATE[state] || ["", "info", state];
  return `<span class="pill ${kind}">${icon(ic, running ? "spin" : "")}${esc(label)}</span>`; };

function upsertJob(job) {
  const known = state.jobs.get(job.id);
  state.jobs.set(job.id, { ...(known || {}), ...job });
  scheduleRender();
  if (!known || known.state !== job.state || known.retryable !== job.retryable) refreshJob(job.id);
}
// Keep the newest snapshot of each song; an older one can arrive after a newer one.
function putTask(t) {
  const known = state.tasks.get(t.id);
  if (known && known.rev > t.rev) return;
  state.tasks.set(t.id, t); dirtyTasks.add(t.id);
}
async function refreshJob(id) {
  try {
    const full = await api(`/api/jobs/${id}`);
    for (const t of full.tasks) putTask(t);
    state.jobs.set(id, { ...state.jobs.get(id), ...full, tasks: undefined, taskIds: full.tasks.map((t) => t.id) });
    scheduleRender();
  } catch (err) {
    if (/no such job/i.test(err.message)) { state.jobs.delete(id); scheduleRender(); return; } // cleared or server restarted
    console.warn("could not refresh job", id, err.message);
  }
}
function renderJobs() {
  const jobs = [...state.jobs.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
  const active = jobs.filter((j) => ACTIVE.includes(j.state)).length;
  const badge = $("#active-count");
  badge.textContent = active; badge.classList.toggle("hidden", !active);
  $("#jobs-sub").textContent = active ? `${plural(active, "download")} in progress.` : "Progress for everything you've queued.";
  const failures = state.system?.failures || 0;
  const retry = $("#retry-failures");
  retry.hidden = !failures || active > 0;
  $("span", retry).textContent = `Retry ${plural(failures, "failed song")}`;
  const box = $("#jobs");
  if (!jobs.length) {
    jobViews.clear(); dirtyTasks.clear();
    box.innerHTML = state.history.length ? "" : `<div class="card empty">${icon("download")}<h3>Nothing downloading</h3><p>Queue some music on Get music and it shows up here.</p>
       <button class="btn" data-goto-get>${icon("search")}Find music</button></div>`;
    return;
  }
  for (const child of [...box.children]) if (!child.dataset.id) child.remove(); // the empty state
  const keep = new Set(jobs.map((j) => j.id));
  for (const [id, v] of jobViews) if (!keep.has(id)) { v.el.remove(); jobViews.delete(id); }
  let prev = null;
  for (const job of jobs) {
    const v = jobViews.get(job.id) || createJobView(job);
    updateJobView(v, job);
    const want = prev ? prev.nextElementSibling : box.firstElementChild;
    if (want !== v.el) box.insertBefore(v.el, want);
    prev = v.el;
  }
  dirtyTasks.clear();
}
// The Downloads tab is patched, not rebuilt: a 2,000-song job gets hundreds of updates a
// second, and rebuilding everything each frame made buttons unclickable and lost focus.
const jobViews = new Map(); // job id -> the card's elements and what they last showed
const dirtyTasks = new Set(); // songs that changed since the last frame
function setHtml(el, html) { if (el._html !== html) { el.innerHTML = html; el._html = html; } }
function createJobView(job) {
  const el = document.createElement("div");
  el.className = "card job"; el.dataset.id = job.id;
  el.innerHTML = `<div class="job-head"></div><div></div><div></div>
    <details class="tracks" data-job="${esc(job.id)}" hidden><summary>${icon("chevron")}<span></span></summary><table><tbody></tbody></table></details>`;
  const [head, notes, progress, details] = el.children;
  const v = { el, head, notes, progress, details, tbody: $("tbody", details), summary: $("summary span", details),
    rows: new Map(), open: null, state: null, rowIds: "" };
  details.addEventListener("toggle", () => {
    v.open = details.open;
    if (!details.open) { v.tbody.textContent = ""; v.rows.clear(); v.rowIds = ""; } // closed lists cost nothing
    else scheduleRender();
  });
  jobViews.set(job.id, v);
  return v;
}
function updateJobView(v, job) {
  const c = job.counts || {};
  const finished = (c.done || 0) + (c.skipped || 0) + (c.failed || 0);
  const pct = job.total ? Math.round(100 * finished / job.total) : 0;
  const running = ACTIVE.includes(job.state);
  v.el.setAttribute("aria-busy", String(running));
  setHtml(v.head, `<div class="grow"><h3>${esc(job.name)}</h3><div class="small faint">${esc(job.kind || "")}${job.created_at ? ` · ${when(job.created_at)}` : ""}</div></div>
      <div class="row">${jobPill(job.state, running)}
        ${running ? `<button class="btn sm" data-cancel="${job.id}">${icon("stop")}Cancel</button>` : ""}
        ${!running && job.retryable && job.taskIds ? `<button class="btn sm" data-retry="${job.id}">${icon("refresh")}Retry ${job.retryable} failed</button>` : ""}</div>`);
  const paused = running && job.paused_until ? new Date(job.paused_until) : null;
  setHtml(v.notes, `${paused ? `<div class="row mt-2"><p class="small warn-text">${icon("alert")} YouTube paused downloads from your connection. TuneGrab continues at ${paused.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.</p>
      <button class="btn sm" data-resume="${job.id}">${icon("refresh")}Try now</button></div>` : ""}
    ${job.error ? `<p class="small error-text mt-2">${esc(job.error)}</p>` : ""}
    ${(job.warnings || []).map((w) => `<p class="small warn-text mt-2">${esc(w)}</p>`).join("")}`);
  setHtml(v.progress, job.total ? `<div class="bar ${c.failed ? "warn" : ""}" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" aria-label="${esc(job.name)} progress"><i style="width:${pct}%"></i></div>
      <div class="job-stats tnum"><span>${finished} of ${job.total}</span><span>${c.done || 0} done</span>${c.skipped ? `<span>${c.skipped} already had</span>` : ""}${c.failed ? `<span class="error-text">${c.failed} failed</span>` : ""}</div>` : "");
  const ids = job.taskIds || [];
  v.details.hidden = !ids.length;
  if (!ids.length) return;
  v.summary.textContent = plural(ids.length, "song");
  if (v.open === null) v.open = running && ids.length <= 40; // small running jobs start open
  if (v.details.open !== v.open) v.details.open = v.open;
  const stateChanged = v.state !== job.state; // "Use a YouTube link" depends on the job state
  v.state = job.state;
  if (!v.open) return;
  const idKey = ids.join(",");
  if (idKey !== v.rowIds || stateChanged) { // new or reordered songs: rebuild this list once
    const html = ids.map((id) => state.tasks.get(id)).filter(Boolean).map(renderTask);
    v.tbody.innerHTML = html.join("");
    v.rows.clear();
    [...v.tbody.children].forEach((tr, i) => { tr._html = html[i]; v.rows.set(tr.dataset.task, tr); });
    v.rowIds = idKey;
    return;
  }
  for (const id of dirtyTasks) {
    const tr = v.rows.get(id), t = state.tasks.get(id);
    if (!tr || !t) continue;
    const html = renderTask(t);
    if (tr._html === html) continue;
    const tpl = document.createElement("template"); tpl.innerHTML = html;
    const next = tpl.content.firstElementChild; next._html = html;
    tr.replaceWith(next); v.rows.set(id, next);
  }
}
function renderTask(t) {
  const [kind, ic, label] = STATUS[t.status] || ["", "info", t.status];
  const busy = ["matching", "downloading", "converting", "tagging", "resolving"].includes(t.status);
  const job = state.jobs.get(t.job_id);
  // only songs matched by search can take another link; videos already came from one
  const canFix = t.status === "failed" && !t.retried && job && job.kind !== "video" && !ACTIVE.includes(job.state);
  const fixBtn = canFix ? `<button class="btn sm ghost mt-1" data-fix="${esc(t.job_id)}" data-task="${esc(t.id)}">${icon("external")}Use a YouTube link</button>` : "";
  const showBtn = (t.status === "done" || t.status === "skipped") && t.path
    ? `<button class="btn sm ghost" data-show="${esc(t.path)}" aria-label="Show ${esc(t.title)} in folder">${icon("folder")}Show</button>` : "";
  const detail = t.status === "failed" ? `<div class="small error-text">${esc(t.error)}</div>${fixBtn}`
    : t.status === "skipped" && t.message ? `<div class="xs faint">${esc(t.message)}</div>`
    : t.status === "done" ? `${t.source ? `<div class="xs ${t.low_quality ? "warn-text" : "faint"}" title="What YouTube served, before conversion">${t.low_quality ? `${icon("alert")} ` : ""}Source: ${esc(t.source)}</div>` : ""}${reviewHtml(t)}`
    : busy && t.message ? `<div class="xs faint">${esc(t.message)}</div>` : "";
  const match = t.match ? `<a class="xs" href="${esc(t.match.url)}" target="_blank" rel="noopener" title="${esc(t.match.reason)}">YouTube ${icon("external")} <span class="tnum">match ${t.match.score}</span></a>` : "";
  return `<tr data-task="${esc(t.id)}"><td class="c-num tnum">${t.position}</td>
    <td><div class="t-title">${esc(t.title)}</div><div class="t-sub">${esc(t.artists.join(", "))}</div>${detail}
      ${t.status === "downloading" ? `<div class="bar thin"><i style="width:${Math.round(t.progress * 100)}%"></i></div>` : ""}</td>
    <td class="c-end">${match}${showBtn}</td><td class="c-time tnum">${fmtTime(t.duration_s)}</td>
    <td class="c-end"><span class="pill ${kind}">${icon(ic, busy ? "spin" : "")}${label}</span></td></tr>`;
}
// ============================================================== match review
// "Check this match": a song TuneGrab wasn't sure about lists the other videos it found.
// The same panel serves a Downloads row and a Library row; both carry { key, review }.
const openReviews = new Set(); // song keys whose panel is open
const busyReviews = new Set(); // song keys with a choice on its way to the server
function reviewHtml(item) {
  const r = item.review;
  if (!r) return "";
  const key = esc(item.key), open = openReviews.has(item.key), busy = busyReviews.has(item.key);
  const toggle = `<div><button class="btn sm review-btn" data-review="${key}" aria-expanded="${open}">${icon("alert")}Check this match${icon("chevron")}</button></div>`;
  if (!open) return toggle;
  const line = (m, saved) => `<li><div class="grow"><div class="t-title">${esc(m.title)}</div>
      <div class="xs faint">${esc(m.channel || "Unknown channel")} · <span class="tnum">${m.duration_s ? fmtTime(m.duration_s) : "length unknown"}</span> · <span class="tnum">match ${m.score}</span> · ${esc(m.reason)}</div></div>
    <a class="btn sm ghost" href="${esc(m.url)}" target="_blank" rel="noopener" aria-label="Listen to ${esc(m.title)} on YouTube">${icon("external")}Listen</a>
    ${saved ? `<span class="pill">Saved now</span>` : `<button class="btn sm" data-choose="${esc(m.video_id)}" data-key="${key}" ${busy ? "disabled" : ""}>${busy ? icon("loader", "spin") : ""}Use this one</button>`}</li>`;
  const others = r.alternatives || [];
  return `${toggle}<div class="review" role="group" aria-label="Check the match for ${esc(item.title)}">
    <p class="small"><span class="warn-text">${esc(r.note)}</span>${item.duration_s ? ` <span class="faint">The song is <span class="tnum">${fmtTime(item.duration_s)}</span> long.</span>` : ""}</p>
    <ul>${r.match ? line(r.match, true) : ""}${others.map((m) => line(m, false)).join("")}</ul>
    ${others.length ? "" : `<p class="small faint">TuneGrab didn't find another video for this song.</p>`}
    <div class="row"><button class="btn sm" data-keep="${key}" ${busy ? "disabled" : ""}>${icon("check")}Keep the saved one</button>
      ${others.length ? `<span class="xs faint">Using another downloads it, then replaces the file once it's ready.</span>` : ""}</div></div>`;
}
// Redraw every row of this song (it can be on Downloads and in Library at once).
function refreshReview(key, { focus = false } = {}) {
  for (const t of state.tasks.values()) if (t.key === key) dirtyTasks.add(t.id);
  renderJobs();
  if (!$("#tab-library").classList.contains("hidden")) renderLibrary();
  if (focus) $$("[data-review]").find((el) => el.dataset.review === key && el.offsetParent)?.focus();
}
function toggleReview(key) {
  if (!openReviews.delete(key)) openReviews.add(key);
  refreshReview(key, { focus: true });
}
function forgetReview(key) {
  openReviews.delete(key);
  for (const t of state.tasks.values()) if (t.key === key) t.review = null;
  for (const t of state.library?.tracks || []) if (t.key === key) t.review = null;
}
async function chooseMatch(key, videoId) {
  if (busyReviews.has(key)) return;
  busyReviews.add(key); refreshReview(key);
  try {
    const job = await api("/api/review/choose", { method: "POST", body: JSON.stringify({ key, video_id: videoId }) });
    openReviews.delete(key);
    upsertJob(job);
    const elsewhere = $("#tab-jobs").classList.contains("hidden");
    toast("Downloading that version. Your current file stays until the new one is ready.",
      { action: elsewhere ? { label: "View", run: () => showTab("jobs") } : null });
  } catch (err) { toast(err.message, { kind: "bad" }); }
  finally { busyReviews.delete(key); refreshReview(key, { focus: true }); }
}
async function keepMatch(key) {
  if (busyReviews.has(key)) return;
  busyReviews.add(key); refreshReview(key);
  try {
    await api("/api/review/keep", { method: "POST", body: JSON.stringify({ key }) });
    forgetReview(key);
    toast("Kept. TuneGrab won't ask about this song again.");
  } catch (err) { toast(err.message, { kind: "bad" }); }
  finally { busyReviews.delete(key); refreshReview(key); }
}
// Clicks on the panel, from either tab. True when the click was one of ours.
function onReviewClick(b) {
  if (b.dataset.review) toggleReview(b.dataset.review);
  else if (b.dataset.choose) chooseMatch(b.dataset.key, b.dataset.choose);
  else if (b.dataset.keep) keepMatch(b.dataset.keep);
  else return false;
  return true;
}

// "Use a YouTube link" for a song TuneGrab couldn't find (or found wrongly and failed).
let fixTarget = null;
function openFix(jobId, taskId) {
  const t = state.tasks.get(taskId);
  if (!t) return;
  fixTarget = { jobId, taskId };
  const name = `${t.artists.join(", ")} - ${t.title}`;
  $("#fix-song").textContent = name;
  $("#fix-search").href = `https://music.youtube.com/search?q=${encodeURIComponent(name)}`;
  $("#fix-url").value = ""; $("#fix-error").textContent = "";
  $("#fix-submit").disabled = false;
  $("#fix-dialog").showModal(); $("#fix-url").focus();
}
$("#fix-dialog").addEventListener("click", (e) => {
  if (e.target.closest("[data-close]") || e.target === $("#fix-dialog")) $("#fix-dialog").close();
});
$("#fix-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#fix-url").value.trim();
  if (!url) { $("#fix-error").textContent = "Paste a YouTube link first."; $("#fix-url").focus(); return; }
  const btn = $("#fix-submit"); btn.disabled = true; $("#fix-error").textContent = "";
  try {
    const job = await api(`/api/jobs/${fixTarget.jobId}/tasks/${fixTarget.taskId}/fix`, { method: "POST", body: JSON.stringify({ youtube_url: url }) });
    upsertJob(job); $("#fix-dialog").close();
    toast("Saved. Downloading it from that link now.");
  } catch (err) { btn.disabled = false; $("#fix-error").textContent = err.message; }
});

async function loadHistory() {
  try { state.history = await api("/api/history"); }
  catch (err) { state.history = []; toast(`Couldn't load earlier downloads: ${err.message}`, { kind: "bad" }); }
  renderHistory(); renderJobs();
}
function renderHistory() {
  const live = new Set(state.jobs.keys());
  const rows = state.history.filter((h) => !live.has(h.id));
  setHtml($("#history"), rows.length ? `<div class="card history mt-4"><h3>Earlier</h3><table><tbody>
    ${rows.map((h) => { const c = h.counts || {};
      return `<tr><td><div class="t-title">${esc(h.name)}</div><div class="xs faint">${when(h.finished_at || h.created_at)}</div></td>
        <td class="tnum muted">${c.done || 0} done${c.skipped ? ` · ${c.skipped} already had` : ""}${c.failed ? ` · <span class="error-text">${c.failed} failed</span>` : ""}</td>
        <td class="c-end">${jobPill(h.state)}</td></tr>`; }).join("")}
    </tbody></table></div>` : "");
}
$("#tab-jobs").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.hasAttribute("data-goto-get")) { showTab("get"); $("#url").focus(); return; }
  if (b.dataset.fix) { openFix(b.dataset.fix, b.dataset.task); return; }
  if (b.dataset.show) { openPath(b.dataset.show); return; }
  if (onReviewClick(b)) return;
  if (b.dataset.retry) {
    b.disabled = true;
    try { upsertJob(await api(`/api/jobs/${b.dataset.retry}/retry`, { method: "POST" })); toast("Retrying the failed songs."); }
    catch (err) { b.disabled = false; toast(err.message, { kind: "bad" }); }
  } else if (b.dataset.resume) {
    b.disabled = true;
    try { upsertJob(await api(`/api/jobs/${b.dataset.resume}/resume`, { method: "POST" })); toast("Trying again now. If YouTube still refuses, TuneGrab pauses again."); }
    catch (err) { b.disabled = false; toast(err.message, { kind: "bad" }); }
  } else if (b.dataset.cancel) {
    b.disabled = true;
    try { upsertJob(await api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST" })); toast("Cancelling. Songs already saved are kept."); }
    catch (err) { b.disabled = false; toast(err.message, { kind: "bad" }); }
  } else if (b.id === "clear-finished") {
    try {
      const { cleared } = await api("/api/jobs/finished", { method: "DELETE" });
      for (const [id, j] of state.jobs) if (!ACTIVE.includes(j.state)) state.jobs.delete(id);
      state.history = []; renderHistory(); renderJobs();
      toast(cleared ? "Cleared finished downloads. Your files are untouched." : "Nothing to clear.");
    } catch (err) { toast(err.message, { kind: "bad" }); }
  } else if (b.id === "retry-failures") {
    b.disabled = true;
    try { upsertJob(await api("/api/failures/retry", { method: "POST" })); toast("Retrying every song that failed before."); loadSystem(); }
    catch (err) { toast(err.message, { kind: "bad" }); }
    finally { b.disabled = false; }
  }
});

// ============================================================== library
async function loadLibrary() {
  if (!state.library) $("#library").innerHTML = `<div class="stack section">${Array.from({ length: 4 }, () => `<div class="skeleton block"></div>`).join("")}</div>`;
  try { state.library = await api("/api/library"); renderLibrary(); }
  catch (err) { $("#library").innerHTML = `<div class="card empty section">${icon("alert")}<h3>Couldn't load your library</h3><p>${esc(err.message)}</p></div>`; }
}
$("#lib-refresh").addEventListener("click", async (e) => { e.currentTarget.disabled = true; await loadLibrary(); e.currentTarget.disabled = false; });
let filterTimer = 0;
$("#lib-filter").addEventListener("input", () => { clearTimeout(filterTimer); filterTimer = setTimeout(renderLibrary, 120); });

function renderLibrary() {
  const lib = state.library;
  if (!lib) return;
  const q = $("#lib-filter").value.trim().toLowerCase();
  const tracks = lib.tracks.filter((t) => !q || [t.title, t.album, t.album_artist, ...t.artists].some((v) => (v || "").toLowerCase().includes(q)));
  const missing = lib.tracks.filter((t) => !t.exists).length;
  $("#lib-meta").innerHTML = `${plural(lib.tracks.length, "song")} in <span class="mono">${esc(lib.root)}</span>${missing ? ` · <span class="warn-text">${missing} moved or deleted</span>` : ""}`;
  $("#playlists").innerHTML = lib.playlists.length && !q ? `<div class="card section"><h3>Playlists</h3><table><tbody>
    ${lib.playlists.map(playlistRow).join("")}</tbody></table></div>` : "";
  const videos = (lib.videos || []).filter((v) => !q || [v.title, v.uploader].some((x) => (x || "").toLowerCase().includes(q)));
  $("#videos").innerHTML = videos.length ? `<div class="card section"><h3>Videos</h3><table><tbody>
    ${videos.map((v) => `<tr><td><div class="t-title">${esc(v.title)}</div><div class="t-sub">${esc(v.uploader)}</div></td>
      <td class="xs faint">${v.mode === "audio" ? `Audio${v.format ? ` · ${esc(v.format.toUpperCase())}` : ""}` : "Video"}${v.clip ? " · clip" : ""}${v.source ? ` · ${esc(v.source)}` : ""}</td>
      <td class="c-time tnum">${v.duration_s ? fmtTime(v.duration_s) : ""}</td>
      <td class="c-end">${v.exists ? `<button class="btn ghost sm" data-show="${esc(v.path)}" aria-label="Show ${esc(v.title)} in folder">${icon("folder")}Show</button>` : `<span class="xs warn-text">moved or deleted</span>`}</td></tr>`).join("")}
    </tbody></table></div>` : "";
  if (!tracks.length) {
    if (videos.length) { $("#library").innerHTML = ""; return; }
    $("#library").innerHTML = lib.tracks.length
      ? `<div class="card empty section">${icon("search")}<p>Nothing matches “${esc(q)}”.</p></div>`
      : `<div class="card empty section">${icon("library")}<h3>Your library is empty</h3><p>Songs you download appear here, grouped by artist and album.</p><button class="btn" data-goto-get>${icon("search")}Find music</button></div>`;
    return;
  }
  const byArtist = new Map();
  for (const t of tracks) {
    const artist = t.album_artist || "Unknown artist";
    if (!byArtist.has(artist)) byArtist.set(artist, new Map());
    const albums = byArtist.get(artist), album = `${t.album || "Unknown album"}${t.year ? ` (${t.year})` : ""}`;
    if (!albums.has(album)) albums.set(album, []);
    albums.get(album).push(t);
  }
  const artists = [...byArtist.keys()].sort((a, b) => a.localeCompare(b));
  $("#library").innerHTML = artists.map((artist) => {
    const albums = byArtist.get(artist);
    return `<section class="lib-artist"><h3>${esc(artist)}</h3>${[...albums.keys()].sort().map((album) => {
      const list = albums.get(album).sort((a, b) => (a.track_number || 0) - (b.track_number || 0));
      const cover = (list.find((t) => t.cover_url) || {}).cover_url;
      return `<div class="lib-album"><div class="cover">${cover ? `<img src="${esc(cover)}" alt="" loading="lazy">` : icon("music")}</div><div><h4>${esc(album)}</h4><table><tbody>${list.map((t) => `<tr>
        <td class="c-num tnum">${t.track_number || ""}</td>
        <td><span class="t-title">${esc(t.title)}</span>${t.exists ? "" : ` <span class="pill warn">Missing file</span>`}${t.low_quality ? ` <span class="pill warn" title="YouTube only had a low-quality version of this song">${icon("alert")}Low quality source</span>` : ""}<div class="t-sub">${esc(t.artists.join(", "))}</div>${reviewHtml(t)}</td>
        <td class="xs faint"${t.source ? ` title="What YouTube served, before TuneGrab saved it as ${esc((t.format || "audio").toUpperCase())}"` : ""}>${esc((t.format || "").toUpperCase())}${t.source ? ` · from ${esc(t.source)}` : ""}</td><td class="c-time tnum">${fmtTime(t.duration_s)}</td>
        <td class="c-end">${t.exists ? `<button class="btn ghost sm" data-show="${esc(t.path)}" aria-label="Show ${esc(t.title)} in folder">${icon("folder")}Show</button>` : ""}</td></tr>`).join("")}
        </tbody></table></div></div>`;
    }).join("")}</section>`;
  }).join("");
}
// ============================================================== playlist sync
// Library -> a playlist -> "Check for new songs": downloads what was added and lists
// what left. Files of songs that left are only deleted after the dialog below, which
// names each one and asks twice.
const syncing = new Set(); // playlist ids being checked
const quietJobs = new Set(); // sync jobs with nothing new: no "0 downloaded" toast
function playlistRow(p) {
  const id = esc(p.playlist_id), busy = syncing.has(p.playlist_id);
  const label = busy ? "Checking…" : p.csv ? "Check a new CSV file" : "Check for new songs";
  const hint = p.csv ? "Choose a newer export of this playlist: new songs are downloaded and ones that left are listed"
    : "Download songs added to this playlist and list the ones that left it";
  return `<tr><td class="t-title">${esc(p.name)}</td><td class="muted tnum">${plural(p.tracks, "song")}</td>
    <td>${p.removed ? `<button class="btn sm ghost warn-text" data-removed="${id}">${icon("alert")}${plural(p.removed, "song")} left this playlist</button>` : ""}</td>
    <td class="c-end xs faint">Updated ${when(p.synced_at)}</td>
    <td class="c-end"><button class="btn sm" data-sync="${id}" ${p.csv ? "data-csv" : ""} ${busy ? "disabled" : ""} title="${hint}">${icon(busy ? "loader" : "refresh", busy ? "spin" : "")}${label}</button></td></tr>`;
}
const focusSync = (id) => $$("[data-sync]").find((el) => el.dataset.sync === id)?.focus();
async function syncPlaylist(id, csvText = null) {
  if (syncing.has(id)) return;
  syncing.add(id); renderLibrary();
  let res = null;
  try {
    res = await api("/api/playlists/sync", { method: "POST", body: JSON.stringify({ id, csv_text: csvText }) });
    if (!res.new) quietJobs.add(res.job.id);
    upsertJob(res.job);
    toast(`${res.name}: ${res.new ? `${plural(res.new, "new song")} downloading` : "no new songs"}.`,
      { action: res.new ? { label: "View", run: () => showTab("jobs") } : null });
    if (!res.complete) toast(`Only ${res.listed} of ${res.total} songs in ${res.name} could be read, so TuneGrab can't tell which ones left it. Nothing was removed.`, { kind: "warn", timeout: 10000 });
  } catch (err) { toast(err.message, { kind: "bad" }); }
  syncing.delete(id);
  await loadLibrary();
  if (res && res.gone.some((g) => g.state === "file")) openRemoved(id, res.name, res.gone);
  else focusSync(id);
}
$("#sync-file").addEventListener("change", async (e) => {
  const file = e.target.files[0], id = e.target.dataset.playlist;
  e.target.value = "";
  if (!file || !id) return;
  if (file.size > CSV_MAX_BYTES) { toast("That file is over 20 MB. Split it into smaller lists.", { kind: "bad" }); return; }
  let text;
  try { text = await file.text(); }
  catch { toast("That file couldn't be read. Choose it again.", { kind: "bad" }); return; }
  syncPlaylist(id, text);
});

// Songs that left a playlist. Step 1 lists them (tick which files to delete); step 2
// names every file again before anything is deleted.
let removed = null; // { id, name, gone, step, picked: Set of song keys, busy, failed }
const removedFiles = () => removed.gone.filter((g) => g.state === "file");
const removedPicked = () => removedFiles().filter((g) => removed.picked.has(g.key));
async function showRemoved(id) {
  try { const r = await api(`/api/playlists/removed?id=${encodeURIComponent(id)}`); openRemoved(id, r.name, r.gone); }
  catch (err) { toast(err.message, { kind: "bad" }); }
}
function openRemoved(id, name, gone) {
  removed = { id, name, gone, step: "list", busy: false, failed: [],
    picked: new Set(gone.filter((g) => g.state === "file").map((g) => g.key)) };
  renderRemoved();
  const d = $("#removed-dialog");
  if (!d.open) d.showModal();
}
function renderRemoved() {
  const r = removed, d = $("#removed-dialog");
  const picked = removedPicked();
  const song = (g) => `<span class="t-title">${esc(g.title)}</span> <span class="t-sub">${esc(g.artists.join(", "))}</span>`;
  const head = (title) => `<div class="d-head"><h3 id="removed-title">${esc(title)}</h3><button class="btn ghost icon" data-close aria-label="Close">${icon("x")}</button></div>`;
  if (r.step === "confirm") {
    d.innerHTML = `${head(`Delete ${plural(picked.length, "file")}?`)}
      <div class="d-body"><p class="small">These files are deleted from your computer for good. They are not moved to the Recycle Bin or Trash.</p>
        <ul class="file-list">${picked.map((g) => `<li><div>${song(g)}</div><div class="xs mono faint">${esc(g.path)}</div></li>`).join("")}</ul></div>
      <div class="d-foot"><button class="btn ghost push" data-back ${r.busy ? "disabled" : ""}>Back</button>
        <button class="btn danger armed" data-delete ${r.busy ? "disabled" : ""}>${r.busy ? icon("loader", "spin") : ""}Delete ${plural(picked.length, "file")}</button></div>`;
    $("[data-back]", d).focus(); // the safe choice has focus, so Enter can't delete
    return;
  }
  const NOTE = { shared: "Kept: another playlist has it", not_downloaded: "No file in your library" };
  d.innerHTML = `${head(`No longer in ${r.name}`)}
    <div class="d-body"><p class="small muted">${plural(r.gone.length, "song")} left this playlist. The files are still in your library; nothing is deleted unless you choose it here.</p>
      ${r.failed.map((f) => `<p class="small error-text" role="alert">Couldn't delete ${esc(f.path)}: ${esc(f.reason)}. Close any program using it and try again.</p>`).join("")}
      <ul class="file-list">${r.gone.map((g) => g.state === "file"
        ? `<li><label class="check"><input type="checkbox" data-pick="${esc(g.key)}" ${r.picked.has(g.key) ? "checked" : ""}><span><span>${song(g)}</span><span class="xs mono faint">${esc(g.path)}</span></span></label></li>`
        : `<li><div>${song(g)}</div><div class="xs faint">${NOTE[g.state] || ""}</div></li>`).join("")}</ul></div>
    <div class="d-foot"><button class="btn ghost push" data-keep-all ${r.busy ? "disabled" : ""}>Keep all files</button>
      <button class="btn danger" data-next ${picked.length && !r.busy ? "" : "disabled"}>Delete ${plural(picked.length, "file")}…</button></div>`;
}
$("#removed-dialog").addEventListener("change", (e) => {
  const key = e.target.dataset?.pick;
  if (!key) return;
  if (e.target.checked) removed.picked.add(key); else removed.picked.delete(key);
  const next = $("[data-next]", $("#removed-dialog")), n = removedPicked().length;
  next.disabled = !n; next.textContent = `Delete ${plural(n, "file")}…`;
});
$("#removed-dialog").addEventListener("click", async (e) => {
  const d = $("#removed-dialog"), r = removed;
  if (e.target === d || e.target.closest("[data-close]")) { d.close(); return; }
  const b = e.target.closest("button");
  if (!b || !r || r.busy) return;
  if (b.hasAttribute("data-next")) { r.step = "confirm"; renderRemoved(); return; }
  if (b.hasAttribute("data-back")) { r.step = "list"; renderRemoved(); $("[data-next]", d).focus(); return; }
  if (b.hasAttribute("data-keep-all")) {
    r.busy = true; renderRemoved();
    try {
      await api("/api/playlists/removed/keep", { method: "POST", body: JSON.stringify({ id: r.id }) });
      d.close(); toast("Kept. Those songs stay in your library."); loadLibrary();
    } catch (err) { r.busy = false; renderRemoved(); toast(err.message, { kind: "bad" }); }
    return;
  }
  if (b.hasAttribute("data-delete")) {
    r.busy = true; renderRemoved();
    try {
      const res = await api("/api/playlists/removed/delete", { method: "POST", body: JSON.stringify({ id: r.id, keys: removedPicked().map((g) => g.key), confirm: true }) });
      Object.assign(r, { gone: res.gone, failed: res.failed, step: "list", busy: false });
      r.picked = new Set(removedFiles().map((g) => g.key));
      if (res.deleted.length) toast(`Deleted ${plural(res.deleted.length, "file")}.`);
      if (removedFiles().length) renderRemoved(); else d.close();
      loadLibrary();
    } catch (err) { r.busy = false; r.step = "list"; renderRemoved(); toast(err.message, { kind: "bad" }); }
  }
});
$("#removed-dialog").addEventListener("close", () => { const id = removed?.id; removed = null; if (id) focusSync(id); });

async function openPath(path) {
  try { await api("/api/open", { method: "POST", body: JSON.stringify({ path }) }); }
  catch (err) { toast(err.message, { kind: "bad" }); }
}
$("#tab-library").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (onReviewClick(b)) return;
  if (b.dataset.sync) {
    if (!b.hasAttribute("data-csv")) { syncPlaylist(b.dataset.sync); return; }
    $("#sync-file").dataset.playlist = b.dataset.sync; $("#sync-file").click(); // asks for the new export
    return;
  }
  if (b.dataset.removed) { showRemoved(b.dataset.removed); return; }
  if (b.dataset.show) openPath(b.dataset.show);
  if (b.hasAttribute("data-goto-get")) { showTab("get"); $("#url").focus(); }
});
$("#lib-open").addEventListener("click", () => openPath(null));

// ============================================================== settings
const SETTING_KEYS = ["output_dir", "format", "bitrate", "threads", "playlist_mode", "lyrics", "lrc", "track_template", "playlist_template", "video_quality", "cookies_from_browser", "check_updates"];
let savedSettings = null;
function readForm() {
  const v = {};
  for (const key of SETTING_KEYS) { const el = $(`#s-${key}`); v[key] = el.type === "checkbox" ? el.checked : el.value; }
  return v;
}
function fillForm(values) {
  for (const key of SETTING_KEYS) {
    const el = $(`#s-${key}`);
    if (el.type === "checkbox") el.checked = !!values[key]; else el.value = values[key] ?? "";
  }
}
function setSettingsMsg(text, kind = "") {
  const m = $("#settings-msg"); m.textContent = text;
  m.className = `small ${kind === "bad" ? "error-text" : kind === "dirty" ? "warn-text" : "muted"}`;
}
function dirty() { return savedSettings && JSON.stringify(readForm()) !== JSON.stringify(savedSettings); }
async function loadSettings() {
  if (dirty()) return; // keep unsaved edits when switching tabs
  try {
    const { values } = await api("/api/settings");
    fillForm(values); savedSettings = readForm(); setSettingsMsg("No unsaved changes.");
  } catch (err) { setSettingsMsg(err.message, "bad"); }
}
$("#settings-form").addEventListener("input", () => setSettingsMsg(dirty() ? "Unsaved changes." : "No unsaved changes.", dirty() ? "dirty" : ""));
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#s-save"); btn.disabled = true;
  $$("#settings-form .input").forEach((el) => el.removeAttribute("aria-invalid"));
  try {
    const { values } = await api("/api/settings", { method: "POST", body: JSON.stringify(readForm()) });
    fillForm(values); savedSettings = readForm(); setSettingsMsg("Saved.");
    toast("Settings saved."); state.library = null; loadSystem(true);
  } catch (err) {
    setSettingsMsg(err.message, "bad");
    const key = SETTING_KEYS.find((k) => err.message.includes(k) || (k.endsWith("template") && /template/i.test(err.message)));
    if (key) $(`#s-${key}`).setAttribute("aria-invalid", "true");
  } finally { btn.disabled = false; }
});
$("#s-reset").addEventListener("click", async () => {
  const before = readForm();
  try {
    const { values } = await api("/api/settings", { method: "POST", body: JSON.stringify(Object.fromEntries(SETTING_KEYS.map((k) => [k, ""]))) });
    fillForm(values); savedSettings = readForm(); setSettingsMsg("Defaults restored.");
    toast("Settings reset to defaults.", { action: { label: "Undo", run: async () => {
      try { const r = await api("/api/settings", { method: "POST", body: JSON.stringify(before) }); fillForm(r.values); savedSettings = readForm(); setSettingsMsg("Restored your settings."); }
      catch (err) { toast(err.message, { kind: "bad" }); } } }, timeout: 8000 });
    loadSystem(true);
  } catch (err) { setSettingsMsg(err.message, "bad"); }
});
$("#s-browse").addEventListener("click", async (e) => {
  const btn = e.currentTarget; btn.disabled = true; setSettingsMsg("Choose a folder in the window that opened.");
  try {
    const { path } = await api("/api/pick-folder", { method: "POST" });
    if (path) { $("#s-output_dir").value = path; setSettingsMsg("Folder chosen. Save to use it.", "dirty"); }
    else setSettingsMsg(dirty() ? "Unsaved changes." : "No unsaved changes.", dirty() ? "dirty" : "");
  } catch (err) { setSettingsMsg(err.message, "bad"); }
  finally { btn.disabled = false; }
});
$("#s-open").addEventListener("click", () => openPath(null));
window.addEventListener("beforeunload", (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } });

// ============================================================== live events
let renderPending = false;
function scheduleRender() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => { renderPending = false; renderJobs(); renderHistory(); });
}
function setConn(kind, text) { $("#conn").innerHTML = `<span class="dot ${kind}"></span><span>${text}</span>`; }
// After a dropped connection (or when the server had to drop events for us) the page may
// have missed updates: reload jobs, their songs, history and system state from scratch.
let resyncing = false;
async function resync() {
  if (resyncing) return;
  resyncing = true;
  try {
    const jobs = await api("/api/jobs");
    const live = new Set(jobs.map((j) => j.id));
    for (const [id, job] of state.jobs) {
      if (live.has(id)) continue;
      for (const tid of job.taskIds || []) state.tasks.delete(tid);
      state.jobs.delete(id); // the server restarted; it's in history now
    }
    for (const job of jobs) { upsertJob(job); refreshJob(job.id); }
    loadSystem(true); loadHistory();
  } catch (err) { console.warn("resync failed", err.message); }
  finally { resyncing = false; }
}
function connect() {
  const source = new EventSource("/api/events");
  let lost = false;
  source.addEventListener("open", () => { setConn("ok", "Connected"); if (lost) { lost = false; resync(); } });
  source.addEventListener("error", () => { lost = true; setConn("warn", "Reconnecting…"); });
  source.addEventListener("resync", () => resync());
  source.addEventListener("job", (e) => {
    const job = JSON.parse(e.data), before = state.jobs.get(job.id);
    upsertJob(job);
    if (before && ACTIVE.includes(before.state) && job.state === "error") {
      toast(`Couldn't start ${job.name}${job.error ? `: ${job.error}` : "."}`, { kind: "bad", timeout: 8000 });
    } else if (before && ACTIVE.includes(before.state) && !ACTIVE.includes(job.state)) {
      const c = job.counts || {};
      // a playlist check that found nothing new already said so
      if (!(quietJobs.delete(job.id) && !c.failed && !c.done)) toast(`${job.name}: ${c.done || 0} downloaded${c.failed ? `, ${c.failed} failed` : ""}.`, { kind: c.failed ? "warn" : "ok" });
      state.library = null; loadSystem();
      if (!$("#tab-library").classList.contains("hidden")) loadLibrary();
    }
  });
  source.addEventListener("task", (e) => {
    const t = JSON.parse(e.data);
    putTask(t);
    const job = state.jobs.get(t.job_id);
    if (job && job.taskIds && !job.taskIds.includes(t.id)) job.taskIds.push(t.id);
    if (!job || !job.taskIds) refreshJob(t.job_id); else scheduleRender();
  });
}

// ============================================================== keyboard
document.addEventListener("keydown", (e) => {
  const el = e.target instanceof Element ? e.target : document.body;
  const typing = el.matches("input, select, textarea") || el.isContentEditable;
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && queue.size) { e.preventDefault(); downloadQueue(); return; }
  if (typing || e.altKey || e.ctrlKey || e.metaKey || document.querySelector("dialog[open]")) return;
  if (e.key === "/") { e.preventDefault(); showTab("get"); $("#url").focus(); $("#url").select(); }
  else if (["1", "2", "3", "4"].includes(e.key)) showTab(TABS[Number(e.key) - 1]);
  else if (e.key === "?") $("#keys-dialog").showModal();
});
$("#shortcuts-btn").addEventListener("click", () => $("#keys-dialog").showModal());
$("#keys-dialog").addEventListener("click", (e) => { if (e.target.closest("[data-close]") || e.target === $("#keys-dialog")) $("#keys-dialog").close(); });

// ============================================================== start
(async function init() {
  showTab(location.hash.slice(1) || "get");
  renderQueue();
  renderFirstRun();
  connect();
  await loadSystem();
  try { for (const job of await api("/api/jobs")) upsertJob(job); }
  catch (err) { toast(`Couldn't load downloads: ${err.message}`, { kind: "bad" }); }
  loadHistory();
})();
