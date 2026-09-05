/* Recipe Swipe - vanilla JS, no build step.
 *
 * Three-way gesture on the card: swipe right = approve, left = reject,
 * up = reveal the detail panel. The detail panel is itself swipeable via its
 * top handle: right = approve (different sound), left = reject (same sound
 * as the card), down = dismiss back to the card, unjudged.
 *
 * Auth: /api/* requires HTTP Basic. No credential handling here - a 401 with
 * WWW-Authenticate: Basic triggers the browser's own native prompt for
 * fetch(), same as a normal page load, cached by the browser afterward.
 */

const deckEl = document.getElementById("deck");
const emptyEl = document.getElementById("empty-state");
const loadingEl = document.getElementById("loading");
const countCurrentEl = document.getElementById("count-current");
const countTotalEl = document.getElementById("count-total");
const toastEl = document.getElementById("toast");
const detailPanel = document.getElementById("detail-panel");
const detailBody = document.getElementById("detail-body");

let cards = [];        // full deck, oldest-staged first
let cursor = 0;         // index of the top (frontmost) card
let detailOpenFor = null; // card object currently shown in the detail panel, or null

const sounds = {
  approve: new Audio("/sounds/approved.mp3"),
  reject: new Audio("/sounds/reject.mp3"),
  reveal: new Audio("/sounds/reveal.mp3"),
  excellent: new Audio("/sounds/excellent.mp3"),
};
function play(name) {
  const a = sounds[name];
  if (!a) return;
  try {
    a.currentTime = 0;
    a.play().catch(() => {}); // ignored - browser may block until first real gesture
  } catch (_) {}
}

function toast(msg, ms = 2200) {
  toastEl.textContent = msg;
  toastEl.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (toastEl.hidden = true), ms);
}

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

function minutesLabel(card) {
  const m = card.preview.total_minutes;
  if (!m) return null;
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60), rem = m % 60;
  return rem ? `${h}h ${rem}m` : `${h}h`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function metaHtml(card) {
  const time = minutesLabel(card);
  const bits = [];
  if (time) bits.push(`<span>⏱ ${time}</span>`);
  if (card.preview.servings) bits.push(`<span>🍽 ${card.preview.servings}</span>`);
  if (card.preview.ingredient_count) bits.push(`<span>${card.preview.ingredient_count} ingredients</span>`);
  return bits.join("");
}

// ---------- card rendering ----------

function renderCardEl(card, z) {
  const el = document.createElement("div");
  el.className = "card";
  el.style.zIndex = z;
  el.dataset.id = card.id;

  const tags = [card.diet, ...(card.preview.course || [])].filter(Boolean);
  // A partial ingredient peek, matching the sketch: name/time up top, then a
  // compact grid of *some* ingredients, not the full list.
  const peek = (card.preview.ingredients_peek || []).slice(0, 6);

  el.innerHTML = `
    <div class="card-image-wrap ${card.has_image ? "" : "no-image"}">
      ${card.has_image ? `<img loading="lazy" src="/api/deck/${card.id}/image" alt="">` : ""}
      <div class="stamp approve">Yes</div>
      <div class="stamp reject">No</div>
    </div>
    <div class="card-body">
      <div class="card-source">${escapeHtml(card.source_name || "")}</div>
      <h2 class="card-title">${escapeHtml(card.preview.title || "Untitled recipe")}</h2>
      <div class="card-meta">${metaHtml(card)}</div>
      <div class="card-tags">${tags.map(t => `<span class="pill">${escapeHtml(t)}</span>`).join("")}</div>
      ${peek.length ? `
        <div class="ingredient-peek">
          <div class="label">Some ingredients</div>
          <div class="ingredient-grid">${peek.map(i => `<span>${escapeHtml(i)}</span>`).join("")}</div>
        </div>` : ""}
    </div>
  `;
  return el;
}

function renderVisible() {
  deckEl.querySelectorAll(".card").forEach(c => c.remove());
  const visible = cards.slice(cursor, cursor + 3).reverse();
  visible.forEach((card, i) => {
    const el = renderCardEl(card, i);
    const depth = visible.length - 1 - i;
    if (depth > 0) {
      el.style.transform = `translateY(${depth * 10}px) scale(${1 - depth * 0.04})`;
      el.style.opacity = depth > 1 ? "0.6" : "1";
    } else {
      attachCardGestures(el, card);
    }
    deckEl.appendChild(el);
  });
  countCurrentEl.textContent = Math.min(cursor + 1, cards.length);
  countTotalEl.textContent = cards.length;
  emptyEl.hidden = cursor < cards.length;
}

// ---------- card gestures: left/right judge, up reveal ----------

function attachCardGestures(el, card) {
  let startX = 0, startY = 0, dx = 0, dy = 0, axis = null, dragging = false;
  const DEADZONE = 10;
  const JUDGE_THRESHOLD = 110;
  const REVEAL_THRESHOLD = 100;

  const reset = () => {
    el.classList.remove("dragging");
    el.querySelectorAll(".stamp").forEach(s => (s.style.opacity = 0));
  };

  const onDown = (e) => {
    dragging = true; axis = null; dx = 0; dy = 0;
    el.classList.add("dragging");
    startX = e.clientX; startY = e.clientY;
    el.setPointerCapture(e.pointerId);
  };

  const onMove = (e) => {
    if (!dragging) return;
    dx = e.clientX - startX; dy = e.clientY - startY;

    if (!axis && (Math.abs(dx) > DEADZONE || Math.abs(dy) > DEADZONE)) {
      axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
    }
    if (axis === "x") {
      const rot = dx * 0.06;
      el.style.transform = `translate(${dx}px, ${dy * 0.15}px) rotate(${rot}deg)`;
      el.querySelector(".stamp.approve").style.opacity = Math.max(0, Math.min(1, dx / 90));
      el.querySelector(".stamp.reject").style.opacity = Math.max(0, Math.min(1, -dx / 90));
    } else if (axis === "y" && dy < 0) {
      const clamped = Math.max(dy, -window.innerHeight);
      el.style.transform = `translateY(${clamped}px) scale(${1 - Math.min(0.06, -clamped / 2000)})`;
      showDetailBehind(card, Math.min(1, -clamped / REVEAL_THRESHOLD));
    }
  };

  const onUp = () => {
    if (!dragging) return;
    dragging = false;

    if (axis === "x") {
      if (dx > JUDGE_THRESHOLD) return commitJudge(el, card, "right");
      if (dx < -JUDGE_THRESHOLD) return commitJudge(el, card, "left");
      el.classList.add("snap-back");
      el.style.transform = "";
      reset();
    } else if (axis === "y") {
      if (-dy > REVEAL_THRESHOLD) return commitReveal(el, card);
      el.classList.add("snap-back");
      el.style.transform = "";
      hideDetailBehind();
    } else {
      reset();
    }
  };

  el.addEventListener("pointerdown", onDown);
  el.addEventListener("pointermove", onMove);
  el.addEventListener("pointerup", onUp);
  el.addEventListener("pointercancel", onUp);
}

function showDetailBehind(card, progress) {
  if (detailPanel.hidden) {
    fillDetail(card);
    detailPanel.hidden = false;
    detailPanel.classList.add("dragging");
  }
  detailPanel.style.transform = `translateY(${100 - progress * 100}%)`;
}
function hideDetailBehind() {
  detailPanel.classList.remove("dragging");
  detailPanel.classList.add("animating");
  detailPanel.style.transform = "translateY(100%)";
  setTimeout(() => {
    detailPanel.hidden = true;
    detailPanel.classList.remove("animating", "open");
    detailPanel.style.transform = "";
  }, 340);
}

function commitJudge(cardEl, card, direction) {
  cardEl.classList.add("flying-x");
  const flyX = direction === "right" ? window.innerWidth : -window.innerWidth;
  cardEl.style.transform = `translate(${flyX}px, ${window.innerHeight * 0.15}px) rotate(${direction === "right" ? 28 : -28}deg)`;
  cardEl.style.opacity = "0";
  play(direction === "right" ? "approve" : "reject");
  advanceAndSwipe(card, direction);
}

function commitReveal(cardEl, card) {
  cardEl.classList.add("flying-up");
  cardEl.style.transform = `translateY(-140%)`;
  cardEl.style.opacity = "0";
  play("reveal");

  detailPanel.classList.remove("dragging");
  detailPanel.classList.add("animating");
  detailPanel.classList.add("open");
  detailPanel.style.transform = "";
  detailOpenFor = card;
  attachDetailGestures();
}

async function advanceAndSwipe(card, direction) {
  cursor += 1;
  renderVisible();
  try {
    const result = await api(`/api/swipe/${card.id}`, { method: "POST", body: JSON.stringify({ direction }) });
    if (direction === "right") toast(`Added to Mealie: ${result.slug}`);
  } catch (err) {
    toast(`Swipe failed: ${err.message}`);
    cursor -= 1;
    renderVisible();
  }
}

// ---------- detail panel content ----------

function fillDetail(card) {
  const peek = card.preview.ingredients_peek || [];
  detailBody.innerHTML = `
    <div class="card-source">${escapeHtml(card.source_name || "")}</div>
    <h1>${escapeHtml(card.preview.title || "Untitled recipe")}</h1>
    <div class="card-meta">${metaHtml(card)}</div>
    ${card.preview.description ? `<p class="description">${escapeHtml(card.preview.description)}</p>` : ""}
    ${peek.length ? `<h3>Ingredients (${card.preview.ingredient_count || peek.length})</h3>
      <ul>${peek.map(i => `<li>${escapeHtml(i)}</li>`).join("")}</ul>` : ""}
    ${card.preview.keywords && card.preview.keywords.length
      ? `<h3>Keywords</h3><p class="description">${card.preview.keywords.map(escapeHtml).join(" · ")}</p>` : ""}
    <h3>Source</h3>
    <p class="description"><a href="${card.canonical_url}" target="_blank" rel="noopener">${escapeHtml(card.canonical_url)}</a></p>
  `;
}

// ---------- detail panel gestures: handle drag = judge / dismiss ----------

let detailGesturesAttached = false;
function attachDetailGestures() {
  if (detailGesturesAttached) return;
  detailGesturesAttached = true;
  const handle = document.querySelector(".detail-drag-handle");

  let startX = 0, startY = 0, dx = 0, dy = 0, axis = null, dragging = false;
  const DEADZONE = 10;
  const JUDGE_THRESHOLD = 110;
  const DISMISS_THRESHOLD = 100;

  const onDown = (e) => {
    if (!detailOpenFor) return;
    dragging = true; axis = null; dx = 0; dy = 0;
    detailPanel.classList.add("dragging");
    startX = e.clientX; startY = e.clientY;
    handle.setPointerCapture(e.pointerId);
  };
  const onMove = (e) => {
    if (!dragging) return;
    dx = e.clientX - startX; dy = e.clientY - startY;
    if (!axis && (Math.abs(dx) > DEADZONE || Math.abs(dy) > DEADZONE)) {
      axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
    }
    if (axis === "x") {
      const rot = dx * 0.05;
      detailPanel.style.transform = `translate(${dx}px, ${dy * 0.1}px) rotate(${rot}deg)`;
    } else if (axis === "y" && dy > 0) {
      detailPanel.style.transform = `translateY(${dy}px)`;
    }
  };
  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    detailPanel.classList.remove("dragging");
    detailPanel.classList.add("animating");

    if (axis === "x" && dx > JUDGE_THRESHOLD) return commitDetailJudge("right");
    if (axis === "x" && dx < -JUDGE_THRESHOLD) return commitDetailJudge("left");
    if (axis === "y" && dy > DISMISS_THRESHOLD) return dismissDetail();

    detailPanel.style.transform = "";
    setTimeout(() => detailPanel.classList.remove("animating"), 340);
  };

  handle.addEventListener("pointerdown", onDown);
  handle.addEventListener("pointermove", onMove);
  handle.addEventListener("pointerup", onUp);
  handle.addEventListener("pointercancel", onUp);
}

function commitDetailJudge(direction) {
  const card = detailOpenFor;
  detailPanel.classList.add("flying-x");
  const flyX = direction === "right" ? window.innerWidth : -window.innerWidth;
  detailPanel.style.transform = `translate(${flyX}px, 0) rotate(${direction === "right" ? 20 : -20}deg)`;
  detailPanel.style.opacity = "0";
  play(direction === "right" ? "excellent" : "reject");

  setTimeout(() => {
    detailPanel.hidden = true;
    detailPanel.classList.remove("flying-x", "open");
    detailPanel.style.transform = "";
    detailPanel.style.opacity = "";
    detailOpenFor = null;
  }, 420);

  advanceAndSwipe(card, direction);
}

function dismissDetail() {
  detailPanel.style.transform = "translateY(100%)";
  setTimeout(() => {
    detailPanel.hidden = true;
    detailPanel.classList.remove("animating", "open");
    detailPanel.style.transform = "";
    detailOpenFor = null;
  }, 340);
}

// ---------- deck loading ----------

async function loadDeck() {
  loadingEl.hidden = false;
  emptyEl.hidden = true;
  try {
    const data = await api("/api/deck");
    cards = data.cards;
    cursor = 0;
    renderVisible();
  } catch (err) {
    toast(`Could not load deck: ${err.message}`, 4000);
  } finally {
    loadingEl.hidden = true;
  }
}

// ---------- pull-more sheet ----------

const pullSheet = document.getElementById("pull-sheet");
function openPullSheet() { pullSheet.hidden = false; }
document.getElementById("btn-pull").addEventListener("click", openPullSheet);
document.getElementById("btn-menu").addEventListener("click", openPullSheet);
document.getElementById("btn-pull-empty").addEventListener("click", openPullSheet);
pullSheet.querySelector(".sheet-backdrop").addEventListener("click", () => (pullSheet.hidden = true));

document.getElementById("btn-pull-go").addEventListener("click", async () => {
  const status = document.getElementById("pull-status");
  const body = {
    count: parseInt(document.getElementById("pull-count").value, 10) || 20,
    source: document.getElementById("pull-source").value.trim() || null,
    course: document.getElementById("pull-course").value.trim() || null,
    search: document.getElementById("pull-search").value.trim() || null,
  };
  status.textContent = "Pulling… this can take a minute.";
  try {
    const result = await api("/api/pull", { method: "POST", body: JSON.stringify(body) });
    status.textContent = `Staged ${result.staged} new recipe(s).`;
    await loadDeck();
    setTimeout(() => (pullSheet.hidden = true), 1200);
  } catch (err) {
    status.textContent = `Failed: ${err.message}`;
  }
});

// ---------- keyboard fallback (desktop testing) ----------

document.addEventListener("keydown", (e) => {
  const topCard = deckEl.querySelector(".card:last-child");
  if (!topCard) return;
  if (e.key === "ArrowRight") commitJudge(topCard, cards[cursor], "right");
  if (e.key === "ArrowLeft") commitJudge(topCard, cards[cursor], "left");
  if (e.key === "ArrowUp") commitReveal(topCard, cards[cursor]);
});

// ---------- PWA service worker (best-effort, safe to fail) ----------

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

loadDeck();
