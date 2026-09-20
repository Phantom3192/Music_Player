const API_BASE = ""; // same origin as the backend that serves this page

const $ = (id) => document.getElementById(id);
const audio = $("audio");
const resultsEl = $("results");
const queueListEl = $("queue-list");
const queueCountEl = $("queue-count");
const toastEl = $("toast");

const npArtwork = $("np-artwork");
const npTitle = $("np-title");
const npAuthor = $("np-author");
const playBtn = $("play-btn");
const prevBtn = $("prev-btn");
const nextBtn = $("next-btn");
const progress = $("progress");
const progressFill = $("progress-fill");
const timeLabel = $("time-label");
const volume = $("volume");

let queue = [];       // array of track objects
let currentIndex = -1;
let toastTimer = null;

// ---- Helpers ------------------------------------------------------------

function fmtTime(sec) {
  if (!isFinite(sec)) return "0:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

function fmtDuration(ms) {
  if (!ms) return "";
  return fmtTime(ms / 1000);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function toast(message, isError = false) {
  clearTimeout(toastTimer);
  toastEl.textContent = message;
  toastEl.classList.toggle("is-error", isError);
  toastEl.hidden = false;
  toastTimer = setTimeout(() => (toastEl.hidden = true), 3500);
}

function makeThumb(track) {
  const img = el("img", "music-track-art");
  img.alt = "";
  img.loading = "lazy";
  if (track.artwork) img.src = track.artwork;
  img.addEventListener("error", () => (img.removeAttribute("src")));
  return img;
}

// ---- Sidebar views ------------------------------------------------------

document.querySelectorAll(".music-sidebar-link").forEach((link) => {
  link.addEventListener("click", (e) => {
    e.preventDefault();
    const target = link.dataset.panel;
    document.querySelectorAll(".music-sidebar-link").forEach((l) =>
      l.classList.toggle("active", l === link));
    document.querySelectorAll(".music-view").forEach((view) => {
      view.hidden = view.id !== `panel-${target}`;
    });
  });
});

// ---- Search -------------------------------------------------------------

$("search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("search-input").value.trim();
  if (!q) return;

  resultsEl.replaceChildren(el("li", "music-queue-empty", "Searching…"));
  try {
    const res = await fetch(`${API_BASE}/api/search?q=${encodeURIComponent(q)}`);
    if (!res.ok) {
      const j = await res.json().catch(() => null);
      throw new Error((j && j.detail) || `HTTP ${res.status}`);
    }
    const { results } = await res.json();
    renderResults(results);
  } catch (err) {
    resultsEl.replaceChildren(el("li", "music-queue-empty", `Search failed: ${err.message}`));
  }
});

function renderResults(tracks) {
  if (!tracks.length) {
    resultsEl.replaceChildren(el("li", "music-queue-empty", "No results."));
    return;
  }
  resultsEl.replaceChildren(...tracks.map(makeResultRow));
}

function makeResultRow(track) {
  const noLink = track.playable === false;       // JioSaavn gave no audio URL at all
  const restricted = track.available === false;  // flagged unavailable by JioSaavn

  const li = el("li", "music-track");
  if (noLink) li.classList.add("is-disabled");
  else if (restricted) li.classList.add("is-unavailable");

  const meta = el("div", "music-track-meta");
  meta.append(el("span", "qi-title", track.title || "Untitled"),
              el("span", "qi-author", track.author || ""));

  li.append(makeThumb(track), meta);

  if (noLink) {
    li.append(el("span", "music-soon-tag", "Not available"));
  } else {
    li.append(restricted
      ? el("span", "music-soon-tag", "Unavailable")
      : el("span", "music-track-time", fmtDuration(track.duration_ms)));
    li.append(makeTrackMenu(track));
    li.addEventListener("click", (e) => {
      if (e.target.closest(".music-track-menu")) return;
      playNow(track);
    });
  }
  return li;
}

// ---- "⋮" track menu -----------------------------------------------------

function closeMenus() {
  document.querySelectorAll(".music-track-menu-dropdown").forEach((d) => (d.hidden = true));
  document.querySelectorAll(".music-track-menu-btn").forEach((b) => b.classList.remove("is-open"));
}
document.addEventListener("click", closeMenus);

function makeTrackMenu(track) {
  const wrap = el("div", "music-track-menu");
  const btn = el("button", "music-track-menu-btn", "⋮");
  btn.type = "button";
  btn.setAttribute("aria-label", "Track options");

  const dropdown = el("div", "music-track-menu-dropdown");
  dropdown.hidden = true;

  const items = [
    ["▶", "Play now", () => playNow(track)],
    ["＋", "Add to queue", () => enqueue(track)],
  ];
  for (const [icon, label, action] of items) {
    const item = el("button", "music-track-menu-item");
    item.type = "button";
    item.append(el("span", null, icon), el("span", null, label));
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      closeMenus();
      action();
    });
    dropdown.append(item);
  }

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const wasHidden = dropdown.hidden;
    closeMenus();
    dropdown.hidden = !wasHidden;
    btn.classList.toggle("is-open", wasHidden);
  });

  wrap.append(btn, dropdown);
  return wrap;
}

// ---- Queue --------------------------------------------------------------

function enqueue(track) {
  queue.push(track);
  renderQueue();
  toast(`Added “${track.title || "Untitled"}” to the queue`);
  if (currentIndex === -1) playIndex(queue.length - 1);
}

function playNow(track) {
  // Slot it in right after the current track so the rest of the queue continues.
  const at = currentIndex + 1;
  queue.splice(at, 0, track);
  playIndex(at);
}

function removeFromQueue(i) {
  const wasCurrent = i === currentIndex;
  queue.splice(i, 1);
  if (i < currentIndex) {
    currentIndex--;
  } else if (wasCurrent) {
    if (i < queue.length) {
      playIndex(i);
      return;
    }
    stopPlayback();
    return;
  }
  renderQueue();
}

function clearQueue() {
  queue = [];
  stopPlayback();
}

$("clear-btn").addEventListener("click", clearQueue);

function renderQueue() {
  queueCountEl.textContent = String(queue.length);

  if (!queue.length) {
    queueListEl.replaceChildren(el("li", "music-queue-empty", "Your queue is empty."));
  } else {
    queueListEl.replaceChildren(...queue.map((track, i) => {
      const li = el("li", "music-track");
      if (i === currentIndex) li.classList.add("is-active");

      const meta = el("div", "music-track-meta");
      meta.append(el("span", "qi-title", track.title || "Untitled"),
                  el("span", "qi-author", track.author || ""));

      const remove = el("button", "music-queue-remove", "✕");
      remove.type = "button";
      remove.title = "Remove from queue";
      remove.setAttribute("aria-label", "Remove from queue");
      remove.addEventListener("click", (e) => {
        e.stopPropagation();
        removeFromQueue(i);
      });

      li.append(makeThumb(track), meta,
                el("span", "music-track-time", fmtDuration(track.duration_ms)), remove);
      li.addEventListener("click", () => playIndex(i));
      return li;
    }));
  }
  updateControls();
}

function updateControls() {
  const hasQueue = queue.length > 0;
  playBtn.disabled = !hasQueue;
  prevBtn.disabled = !hasQueue;
  nextBtn.disabled = !hasQueue;
}

// ---- Playback -----------------------------------------------------------

function setNowPlaying(track) {
  npTitle.textContent = track ? (track.title || "Untitled") : "Nothing playing yet";
  npAuthor.textContent = track ? (track.author || "") : "Search for a song to get started";
  npAuthor.classList.remove("is-error");
  if (track && track.artwork) {
    npArtwork.src = track.artwork;
    npArtwork.hidden = false;
  } else {
    npArtwork.removeAttribute("src");
    npArtwork.hidden = true;
  }
}

function resetProgress() {
  progressFill.style.width = "0%";
  progress.setAttribute("aria-valuenow", "0");
  timeLabel.textContent = "0:00 / 0:00";
}

function playIndex(i) {
  if (i < 0 || i >= queue.length) return;
  currentIndex = i;
  const track = queue[i];

  setNowPlaying(track);
  resetProgress();
  audio.src = `${API_BASE}/api/stream?url=${encodeURIComponent(track.uri)}`;
  audio.play().catch(() => {});
  renderQueue();
}

function stopPlayback() {
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  currentIndex = -1;
  setNowPlaying(null);
  resetProgress();
  renderQueue();
}

// ---- Transport controls -------------------------------------------------

playBtn.addEventListener("click", () => {
  if (!audio.getAttribute("src")) {
    if (queue.length) playIndex(currentIndex >= 0 ? currentIndex : 0);
    return;
  }
  if (audio.paused) audio.play().catch(() => {});
  else audio.pause();
});

nextBtn.addEventListener("click", () => {
  if (currentIndex + 1 < queue.length) playIndex(currentIndex + 1);
  else toast("End of the queue");
});

prevBtn.addEventListener("click", () => {
  // Like most players: restart the track first, go back only if already near the start.
  if (audio.currentTime > 3 || currentIndex <= 0) audio.currentTime = 0;
  else playIndex(currentIndex - 1);
});

audio.addEventListener("play", () => (playBtn.textContent = "⏸"));
audio.addEventListener("pause", () => (playBtn.textContent = "▶"));
audio.addEventListener("ended", () => {
  if (currentIndex + 1 < queue.length) playIndex(currentIndex + 1);
});

function updateProgress() {
  if (!isFinite(audio.duration) || audio.duration <= 0) return;
  const pct = (audio.currentTime / audio.duration) * 100;
  progressFill.style.width = `${pct}%`;
  progress.setAttribute("aria-valuenow", String(Math.round(pct)));
  timeLabel.textContent = `${fmtTime(audio.currentTime)} / ${fmtTime(audio.duration)}`;
}
audio.addEventListener("timeupdate", updateProgress);
audio.addEventListener("durationchange", updateProgress);

function seekTo(fraction) {
  if (!isFinite(audio.duration) || audio.duration <= 0) return;
  audio.currentTime = Math.min(Math.max(fraction, 0), 1) * audio.duration;
}
progress.addEventListener("click", (e) => {
  const rect = progress.getBoundingClientRect();
  seekTo((e.clientX - rect.left) / rect.width);
});
progress.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight") audio.currentTime = Math.min(audio.currentTime + 5, audio.duration || 0);
  if (e.key === "ArrowLeft") audio.currentTime = Math.max(audio.currentTime - 5, 0);
});

volume.addEventListener("input", () => {
  audio.volume = parseFloat(volume.value);
  try { localStorage.setItem("eclipse.volume", volume.value); } catch (_) {}
});
try {
  const saved = localStorage.getItem("eclipse.volume");
  if (saved !== null) {
    volume.value = saved;
    audio.volume = parseFloat(saved);
  }
} catch (_) {}

// ---- Playback errors ----------------------------------------------------
// Without this a failed track just sits at 0:00 with no explanation.
audio.addEventListener("error", async () => {
  if (!audio.getAttribute("src")) return;
  console.error("Audio error", audio.error);
  npAuthor.classList.add("is-error");
  npAuthor.textContent = "⚠ Couldn't play this track";
  try {
    const r = await fetch(audio.src, { headers: { Range: "bytes=0-0" } });
    if (!r.ok) {
      const j = await r.json().catch(() => null);
      if (j && j.detail) npAuthor.textContent = "⚠ " + j.detail;
    }
  } catch (_) {}
});

renderQueue();