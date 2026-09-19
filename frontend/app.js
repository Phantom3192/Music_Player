const API_BASE = ""; // same origin as the backend that serves this page

const audio = document.getElementById("audio");
const resultsEl = document.getElementById("results");
const queueListEl = document.getElementById("queue-list");

const npArtwork = document.getElementById("np-artwork");
const npTitle = document.getElementById("np-title");
const npAuthor = document.getElementById("np-author");
const playBtn = document.getElementById("play-btn");
const seek = document.getElementById("seek");
const timeLabel = document.getElementById("time-label");
const volume = document.getElementById("volume");

let queue = [];       // array of track objects
let currentIndex = -1;

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

// ---- Search -----------------------------------------------------------

document.getElementById("search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = document.getElementById("search-input").value.trim();
  const source = document.getElementById("source-select").value;
  if (!q) return;

  resultsEl.innerHTML = `<p class="hint">Searching…</p>`;
  try {
    const res = await fetch(`${API_BASE}/api/search?q=${encodeURIComponent(q)}&source=${source}`);
    if (!res.ok) throw new Error(await res.text());
    const { results } = await res.json();
    renderResults(results);
  } catch (err) {
    resultsEl.innerHTML = `<p class="hint">Search failed: ${err.message}</p>`;
  }
});

function renderResults(tracks) {
  if (!tracks.length) {
    resultsEl.innerHTML = `<p class="hint">No results.</p>`;
    return;
  }
  resultsEl.innerHTML = "";
  tracks.forEach((track) => {
    const row = document.createElement("div");
    row.className = "track-row";
    row.innerHTML = `
      <img src="${track.artwork || ""}" alt="" />
      <div class="track-meta">
        <span class="title">${escapeHtml(track.title || "Untitled")}</span>
        <span class="author">${escapeHtml(track.author || "")}</span>
      </div>
      <span class="track-duration">${fmtDuration(track.duration_ms)}</span>
    `;
    row.addEventListener("click", () => addToQueueAndMaybePlay(track));
    resultsEl.appendChild(row);
  });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ---- Queue --------------------------------------------------------------

function addToQueueAndMaybePlay(track) {
  queue.push(track);
  renderQueue();
  if (currentIndex === -1) {
    playIndex(queue.length - 1);
  }
}

function renderQueue() {
  queueListEl.innerHTML = "";
  queue.forEach((track, i) => {
    const li = document.createElement("li");
    li.textContent = track.title || "Untitled";
    if (i === currentIndex) li.classList.add("active");
    li.addEventListener("click", () => playIndex(i));
    queueListEl.appendChild(li);
  });
}

function playIndex(i) {
  if (i < 0 || i >= queue.length) return;
  currentIndex = i;
  const track = queue[i];

  npArtwork.src = track.artwork || "";
  npTitle.textContent = track.title || "Untitled";
  npAuthor.textContent = track.author || "";

  audio.src = `${API_BASE}/api/stream?url=${encodeURIComponent(track.uri)}&source=${encodeURIComponent(track.source)}`;
  audio.play();
  renderQueue();
}

// ---- Transport controls ---------------------------------------------------

playBtn.addEventListener("click", () => {
  if (audio.paused) audio.play();
  else audio.pause();
});

document.getElementById("next-btn").addEventListener("click", () => playIndex(currentIndex + 1));
document.getElementById("prev-btn").addEventListener("click", () => playIndex(currentIndex - 1));

audio.addEventListener("play", () => (playBtn.textContent = "⏸"));
audio.addEventListener("pause", () => (playBtn.textContent = "▶"));
audio.addEventListener("ended", () => playIndex(currentIndex + 1));

audio.addEventListener("timeupdate", () => {
  if (!isFinite(audio.duration)) return;
  seek.value = (audio.currentTime / audio.duration) * 100;
  timeLabel.textContent = `${fmtTime(audio.currentTime)} / ${fmtTime(audio.duration)}`;
});

seek.addEventListener("input", () => {
  if (!isFinite(audio.duration)) return;
  audio.currentTime = (seek.value / 100) * audio.duration;
});

volume.addEventListener("input", () => {
  audio.volume = parseFloat(volume.value);
});
