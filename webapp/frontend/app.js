/* Recipe Swipe - vanilla JS, no build step.
 *
 * Gestures (card):   drag right = approve, left = reject, up = detail sheet.
 * Gestures (sheet):  drag right = approve, left = reject, down = back to deck.
 * Every gesture also has a button, because a gesture that silently does
 * nothing is indistinguishable from a broken app - which is exactly how the
 * first version read.
 *
 * Two things this rewrite fixes structurally, not incidentally:
 *
 *  - Drag transforms are written to their own element, never to one that CSS
 *    also positions. A media query centring the old detail panel with
 *    translateX(-50%) overwrote the translateY(100%) hiding it, so swipe-up
 *    appeared to do nothing on desktop. Wrapper positions, inner transforms.
 *
 *  - A failed swipe no longer rewinds the deck. The old code decremented the
 *    cursor on any error, so a 500 from a double-submit put the card straight
 *    back in front of you - the "it keeps showing ones I already swiped"
 *    report. Swipes are now idempotent server-side and single-flight here.
 */

const $ = (id) => document.getElementById(id);

const deckEl = $("deck");
const emptyEl = $("empty-state");
const loadingEl = $("loading");
const detailEl = $("detail");
const detailBody = $("detail-body");
const toastEl = $("toast");

const VISIBLE = 3;             // cards kept in the DOM at once
const DEADZONE = 8;            // px before the gesture commits to an axis
const JUDGE_DIST = 95;         // px of horizontal travel that counts as a decision
const JUDGE_VEL = 0.5;         // px/ms flick that counts even without the distance
const REVEAL_DIST = 80;
const REVEAL_VEL = 0.5;

let cards = [];                // whole deck, oldest-staged first
let cursor = 0;                // index of the front card
let detailCard = null;         // card object shown in the sheet, or null
let busy = false;              // single-flight guard: one commit animation at a time

/* Ids this session has successfully swiped. Belt-and-braces against a card
   reappearing if the deck is reloaded before the backend's move lands. */
const swiped = new Set();

// ---------------------------------------------------------------- sounds

const sounds = {
  approve: new Audio("/sounds/approved.mp3"),
  reject: new Audio("/sounds/reject.mp3"),
  reveal: new Audio("/sounds/reveal.mp3"),
  excellent: new Audio("/sounds/excellent.mp3"),
};
for (const a of Object.values(sounds)) a.preload = "auto";

function play(name) {
  const a = sounds[name];
  if (!a) return;
  try {
    a.currentTime = 0;
    a.play().catch(() => {});   // autoplay policy may block until first tap
  } catch (_) {}
}

// ---------------------------------------------------------------- helpers

function toast(msg, ms = 2600) {
  toastEl.textContent = msg;
  toastEl.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (toastEl.hidden = true), ms);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function timeLabel(minutes) {
  if (!minutes) return null;
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60), m = minutes % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}

function metaBits(card) {
  const p = card.preview || {};
  const bits = [];
  const t = timeLabel(p.total_minutes);
  if (t) bits.push(t);
  if (p.servings) bits.push(`Serves ${p.servings}`);
  if (p.ingredient_count) bits.push(`${p.ingredient_count} ingredients`);
  return bits.map(esc).join('<span class="dot">·</span>');
}

function tagsHtml(card) {
  const out = [];
  if (card.diet) out.push(`<span class="tag diet">${esc(card.diet)}</span>`);
  for (const c of (card.preview.course || []).slice(0, 3)) {
    out.push(`<span class="tag">${esc(c)}</span>`);
  }
  return out.join("");
}

// ---------------------------------------------------------------- rendering

function cardHtml(card) {
  const peek = (card.preview.ingredients_peek || []).slice(0, 5);
  return `
    <div class="photo ${card.has_image ? "" : "no-image"}">
      ${card.has_image ? `<img src="/api/deck/${card.id}/image" alt="" draggable="false">` : ""}
    </div>
    <div class="scrim"></div>
    <div class="stamp like">Yes</div>
    <div class="stamp nope">Nope</div>
    <div class="stamp up">Info</div>
    <div class="overlay">
      ${card.source_name ? `<div class="eyebrow">${esc(card.source_name)}</div>` : ""}
      <h2 class="title">${esc(card.preview.title || "Untitled recipe")}</h2>
      <div class="meta">${metaBits(card)}</div>
      ${tagsHtml(card) ? `<div class="tags">${tagsHtml(card)}</div>` : ""}
      ${peek.length ? `<div class="peek">${peek.map((i) => `<span>${esc(i)}</span>`).join("")}</div>` : ""}
      <div class="hint">↑ swipe up for the full recipe</div>
    </div>`;
}

function depthTransform(depth) {
  return `translateY(${depth * 26}px) scale(${1 - depth * 0.05})`;
}

function makeSlot(card, depth) {
  const slot = document.createElement("div");
  slot.className = "slot";
  slot.style.zIndex = String(VISIBLE - depth);
  slot.style.transform = depthTransform(depth);
  slot.dataset.id = card.id;

  const el = document.createElement("article");
  el.className = "card";
  el.innerHTML = cardHtml(card);
  slot.appendChild(el);

  if (depth === 0) attachCardGestures(el, card);
  return slot;
}

function buildStack() {
  deckEl.replaceChildren();
  const upcoming = cards.slice(cursor, cursor + VISIBLE);
  // Back-to-front so the front card is last in the DOM as well as on top.
  for (let d = upcoming.length - 1; d >= 0; d--) {
    deckEl.appendChild(makeSlot(upcoming[d], d));
  }
  syncChrome();
}

/** Front card leaves; everything behind promotes one step, a new back card
 *  slides in. Animating the survivors rather than rebuilding the whole stack
 *  is what makes the deck feel continuous. */
function promoteStack() {
  const slots = [...deckEl.querySelectorAll(".slot")];
  const front = slots[slots.length - 1];
  if (front) front.remove();

  const remaining = [...deckEl.querySelectorAll(".slot")];
  remaining.forEach((slot, i) => {
    const depth = remaining.length - 1 - i;
    slot.style.zIndex = String(VISIBLE - depth);
    slot.style.transform = depthTransform(depth);
    if (depth === 0) {
      const el = slot.querySelector(".card");
      const card = cards[cursor];
      if (el && card && !el.dataset.armed) attachCardGestures(el, card);
    }
  });

  const nextCard = cards[cursor + remaining.length];
  if (nextCard) deckEl.prepend(makeSlot(nextCard, remaining.length));
  syncChrome();
}

function syncChrome() {
  const done = Math.min(cursor, cards.length);
  $("count-current").textContent = String(Math.min(cursor + 1, cards.length));
  $("count-total").textContent = String(cards.length);
  $("progress-fill").style.width = cards.length ? `${(done / cards.length) * 100}%` : "0%";

  const out = cursor >= cards.length;
  emptyEl.hidden = !out;
  for (const b of ["btn-nope", "btn-info", "btn-like"]) $(b).disabled = out;
}

// ---------------------------------------------------------------- gestures

/** One drag engine, used by the card and by the detail sheet's grab handle.
 *  `el` receives the transform; `surface` is what the pointer is caught on. */
function draggable(el, surface, cfg) {
  let sx = 0, sy = 0, st = 0, dx = 0, dy = 0, axis = null, active = false;
  let lastX = 0, lastY = 0, lastT = 0, vx = 0, vy = 0;

  const clear = () => {
    el.classList.remove("dragging");
    cfg.onProgress?.(0, 0, null);
  };

  const down = (e) => {
    if (busy || e.button > 0) return;
    active = true; axis = null; dx = dy = vx = vy = 0;
    sx = lastX = e.clientX; sy = lastY = e.clientY;
    st = lastT = e.timeStamp;
    el.classList.remove("settling", "flying");
    el.classList.add("dragging");
    try { surface.setPointerCapture(e.pointerId); } catch (_) {}
  };

  const move = (e) => {
    if (!active) return;
    dx = e.clientX - sx;
    dy = e.clientY - sy;

    const dt = e.timeStamp - lastT;
    if (dt > 0) {
      vx = (e.clientX - lastX) / dt;
      vy = (e.clientY - lastY) / dt;
      lastX = e.clientX; lastY = e.clientY; lastT = e.timeStamp;
    }

    if (!axis && Math.hypot(dx, dy) > DEADZONE) {
      axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
    }
    if (!axis) return;
    if (axis === "y" && !cfg.allowY(dy)) return;

    cfg.onDrag(dx, dy, axis);
    cfg.onProgress?.(dx, dy, axis);
  };

  const up = (e) => {
    if (!active) return;
    active = false;
    try { surface.releasePointerCapture(e.pointerId); } catch (_) {}

    const quick = e.timeStamp - st < 450;
    if (axis === "x") {
      const flung = quick && Math.abs(vx) > JUDGE_VEL && Math.abs(dx) > 30;
      if (dx > JUDGE_DIST || (flung && dx > 0)) return cfg.commitX("right");
      if (dx < -JUDGE_DIST || (flung && dx < 0)) return cfg.commitX("left");
    } else if (axis === "y" && cfg.allowY(dy)) {
      const far = Math.abs(dy) > REVEAL_DIST;
      const flung = quick && Math.abs(vy) > REVEAL_VEL && Math.abs(dy) > 24;
      if (far || flung) return cfg.commitY();
    }
    cfg.settle();
    clear();
  };

  surface.addEventListener("pointerdown", down);
  surface.addEventListener("pointermove", move);
  surface.addEventListener("pointerup", up);
  surface.addEventListener("pointercancel", up);
}

function attachCardGestures(el, card) {
  el.dataset.armed = "1";
  const like = el.querySelector(".stamp.like");
  const nope = el.querySelector(".stamp.nope");
  const upSt = el.querySelector(".stamp.up");
  const clamp01 = (n) => Math.max(0, Math.min(1, n));

  draggable(el, el, {
    allowY: (dy) => dy < 0,          // the card only lifts, never drags down
    onDrag(dx, dy, axis) {
      if (axis === "x") {
        el.style.transform =
          `translate(${dx}px, ${dy * 0.12}px) rotate(${dx * 0.055}deg)`;
      } else {
        const lift = Math.max(dy, -window.innerHeight);
        el.style.transform = `translateY(${lift}px) scale(${1 + lift / 6000})`;
      }
    },
    onProgress(dx, dy, axis) {
      like.style.opacity = axis === "x" ? clamp01(dx / 78) : 0;
      nope.style.opacity = axis === "x" ? clamp01(-dx / 78) : 0;
      upSt.style.opacity = axis === "y" ? clamp01(-dy / 70) : 0;
    },
    settle() {
      el.classList.add("settling");
      el.style.transform = "";
    },
    commitX: (dir) => judgeFromCard(el, card, dir),
    commitY: () => openDetail(el, card),
  });
}

// ---------------------------------------------------------------- commits

function flyOff(el, dir) {
  const x = dir === "right" ? window.innerWidth * 1.4 : -window.innerWidth * 1.4;
  const rot = dir === "right" ? 26 : -26;
  el.classList.remove("dragging", "settling");
  el.classList.add("flying");
  el.style.transform = `translate(${x}px, ${window.innerHeight * 0.08}px) rotate(${rot}deg)`;
  el.style.opacity = "0";
}

function judgeFromCard(el, card, dir) {
  if (busy) return;
  busy = true;
  flyOff(el, dir);
  play(dir === "right" ? "approve" : "reject");
  setTimeout(() => {
    cursor += 1;
    promoteStack();
    busy = false;
  }, 300);
  submitSwipe(card, dir);
}

function judgeFromDetail(dir) {
  if (busy || !detailCard) return;
  busy = true;
  const card = detailCard;
  const panel = detailEl.querySelector(".detail-card");
  flyOff(panel, dir);
  detailEl.querySelector(".detail-scrim").style.opacity = "0";
  play(dir === "right" ? "excellent" : "reject");

  setTimeout(() => {
    closeDetailDom();
    cursor += 1;
    promoteStack();
    busy = false;
  }, 380);
  submitSwipe(card, dir);
}

/** Fire-and-report. The deck has already moved on: rewinding it on failure is
 *  what made swiped cards reappear, so a failure only ever raises a toast. */
async function submitSwipe(card, dir) {
  try {
    const res = await api(`/api/swipe/${card.id}`, {
      method: "POST",
      body: JSON.stringify({ direction: dir }),
    });
    swiped.add(card.id);
    if (res.result === "approved") {
      // Three outcomes worth telling apart. "already_in_mealie" means the card
      // was filed but nothing was imported, because that exact source URL is
      // in the library - saying "Saved to Mealie" there would be a lie.
      // "similar_to" is the fallible title match: the import DID happen, and
      // this is a heads-up, not a refusal.
      if (res.dry_run) toast(`Dry run · not sent to Mealie · ${card.preview.title}`);
      else if (res.already_in_mealie) toast(`Already in Mealie · filed without re-importing`);
      else if (res.similar_to) toast(`Saved · looks similar to "${res.similar_to}"`, 4500);
      else toast(`Saved to Mealie · ${card.preview.title}`);
    } else if (res.result === "already") { /* idempotent replay, nothing to say */ }
  } catch (err) {
    toast(`Couldn't ${dir === "right" ? "save" : "reject"}: ${err.message}`, 5000);
  }
}

// ---------------------------------------------------------------- detail sheet

function openDetail(cardEl, card) {
  if (busy) return;
  busy = true;
  detailCard = card;

  cardEl.classList.remove("dragging", "settling");
  cardEl.classList.add("flying");
  cardEl.style.transform = "translateY(-135%) scale(0.94)";
  cardEl.style.opacity = "0";
  play("reveal");

  fillDetail(card);
  const panel = detailEl.querySelector(".detail-card");
  panel.classList.remove("flying", "dragging");
  panel.style.transform = "";
  panel.style.opacity = "";
  detailEl.querySelector(".detail-scrim").style.opacity = "";
  detailEl.hidden = false;
  detailBody.scrollTop = 0;

  requestAnimationFrame(() => {
    panel.classList.add("settling");
    detailEl.classList.add("open");
    busy = false;
  });
}

/** Back to the deck, unjudged - the card that flew up has to come back. */
function dismissDetail() {
  if (!detailCard) return;
  const panel = detailEl.querySelector(".detail-card");
  panel.classList.remove("dragging");
  panel.classList.add("settling");
  panel.style.transform = "translateY(100%)";
  detailEl.classList.remove("open");

  setTimeout(() => {
    closeDetailDom();
    const front = deckEl.querySelector(".slot:last-child .card");
    if (front) {
      front.classList.remove("flying");
      front.classList.add("settling");
      front.style.transform = "";
      front.style.opacity = "";
      // The "Info" stamp was ramped up by the swipe that opened the sheet;
      // without this it stays lit on the card you just came back to.
      front.querySelectorAll(".stamp").forEach((s) => (s.style.opacity = "0"));
    }
  }, 330);
}

function closeDetailDom() {
  detailEl.hidden = true;
  detailEl.classList.remove("open");
  const panel = detailEl.querySelector(".detail-card");
  panel.classList.remove("flying", "dragging", "settling");
  panel.style.transform = "";
  panel.style.opacity = "";
  detailEl.querySelector(".detail-scrim").style.opacity = "";
  detailCard = null;
}

function fillDetail(card) {
  const p = card.preview || {};
  const peek = p.ingredients_peek || [];
  detailBody.innerHTML = `
    ${card.has_image ? `<div class="detail-hero"><img src="/api/deck/${card.id}/image" alt=""></div>` : ""}
    ${card.source_name ? `<div class="eyebrow">${esc(card.source_name)}</div>` : ""}
    <h1>${esc(p.title || "Untitled recipe")}</h1>
    <div class="meta">${metaBits(card)}</div>
    ${tagsHtml(card) ? `<div class="tags">${tagsHtml(card)}</div>` : ""}
    ${p.description ? `<p class="description">${esc(p.description)}</p>` : ""}
    <div id="detail-extra"><p class="ing-more">Loading the full recipe…</p></div>
    <h3>Source</h3>
    <p><a href="${esc(card.canonical_url)}" target="_blank" rel="noopener">${esc(card.canonical_url)}</a></p>`;
  loadDetailExtra(card, peek);
}

/** The card payload only carries a 6-line ingredient peek. The sheet is where
 *  you decide, so it pulls the full record instead of guessing from the peek. */
async function loadDetailExtra(card, peek) {
  const slot = $("detail-extra");
  const render = (ingredients, steps, keywords, truncated) => {
    if (!slot) return;
    slot.innerHTML = `
      ${ingredients.length ? `<h3>Ingredients</h3>
        <ul class="ing-list">${ingredients.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>
        ${truncated ? `<p class="ing-more">…and more on the source page.</p>` : ""}` : ""}
      ${steps.length ? `<h3>Method · ${steps.length} step${steps.length === 1 ? "" : "s"}</h3>
        <ul class="ing-list">${steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}
      ${keywords.length ? `<h3>Keywords</h3>
        <div class="tags">${keywords.map((k) => `<span class="tag">${esc(k)}</span>`).join("")}</div>` : ""}`;
  };

  try {
    const d = await api(`/api/deck/${card.id}/detail`);
    render(d.ingredients || [], d.steps || [], d.keywords || [], false);
  } catch (_) {
    render(peek, [], card.preview.keywords || [], peek.length >= 5);
  }
}

function attachDetailGestures() {
  const panel = detailEl.querySelector(".detail-card");
  const grab = $("detail-grab");
  draggable(panel, grab, {
    allowY: (dy) => dy > 0,          // the sheet only drags back down
    onDrag(dx, dy, axis) {
      panel.style.transform = axis === "x"
        ? `translate(${dx}px, ${dy * 0.1}px) rotate(${dx * 0.045}deg)`
        : `translateY(${dy}px)`;
    },
    settle() {
      panel.classList.add("settling");
      panel.style.transform = "";
    },
    commitX: (dir) => judgeFromDetail(dir),
    commitY: () => dismissDetail(),
  });
}

// ---------------------------------------------------------------- deck load

async function loadDeck() {
  loadingEl.hidden = false;
  emptyEl.hidden = true;
  try {
    const data = await api("/api/deck");
    cards = (data.cards || []).filter((c) => !swiped.has(c.id));
    cursor = 0;
    buildStack();
  } catch (err) {
    toast(`Couldn't load the deck: ${err.message}`, 6000);
  } finally {
    loadingEl.hidden = true;
  }
}

// ---------------------------------------------------------------- controls

function frontCardEl() {
  return deckEl.querySelector(".slot:last-child .card");
}
function frontCard() {
  return cards[cursor];
}

$("btn-like").addEventListener("click", () => {
  const el = frontCardEl(), c = frontCard();
  if (el && c) judgeFromCard(el, c, "right");
});
$("btn-nope").addEventListener("click", () => {
  const el = frontCardEl(), c = frontCard();
  if (el && c) judgeFromCard(el, c, "left");
});
$("btn-info").addEventListener("click", () => {
  const el = frontCardEl(), c = frontCard();
  if (el && c) openDetail(el, c);
});
$("btn-d-like").addEventListener("click", () => judgeFromDetail("right"));
$("btn-d-nope").addEventListener("click", () => judgeFromDetail("left"));
$("btn-d-back").addEventListener("click", dismissDetail);
detailEl.querySelector(".detail-scrim").addEventListener("click", dismissDetail);

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea")) return;
  if (!$("pull-sheet").hidden) {
    if (e.key === "Escape") $("pull-sheet").hidden = true;
    return;
  }
  if (detailCard) {
    if (e.key === "ArrowRight") judgeFromDetail("right");
    if (e.key === "ArrowLeft") judgeFromDetail("left");
    if (e.key === "ArrowDown" || e.key === "Escape") dismissDetail();
    return;
  }
  const el = frontCardEl(), c = frontCard();
  if (!el || !c) return;
  if (e.key === "ArrowRight") judgeFromCard(el, c, "right");
  if (e.key === "ArrowLeft") judgeFromCard(el, c, "left");
  if (e.key === "ArrowUp") openDetail(el, c);
});

// ---------------------------------------------------------------- harvest sheet

const pullSheet = $("pull-sheet");
const openPull = () => { pullSheet.hidden = false; $("pull-status").textContent = ""; };
$("btn-pull").addEventListener("click", openPull);
$("btn-pull-empty").addEventListener("click", openPull);
pullSheet.querySelector(".sheet-backdrop").addEventListener("click", () => (pullSheet.hidden = true));

$("btn-pull-go").addEventListener("click", async () => {
  const status = $("pull-status");
  const go = $("btn-pull-go");
  go.disabled = true;
  status.textContent = "Harvesting… this takes a minute.";
  try {
    const result = await api("/api/pull", {
      method: "POST",
      body: JSON.stringify({
        count: parseInt($("pull-count").value, 10) || 20,
        source: $("pull-source").value.trim() || null,
        course: $("pull-course").value.trim() || null,
        search: $("pull-search").value.trim() || null,
      }),
    });
    status.textContent = `Staged ${result.staged} new recipe${result.staged === 1 ? "" : "s"}.`;
    await loadDeck();
    setTimeout(() => (pullSheet.hidden = true), 1300);
  } catch (err) {
    status.textContent = `Failed: ${err.message}`;
  } finally {
    go.disabled = false;
  }
});

// ---------------------------------------------------------------- boot

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

/** Surface dry-run mode before the first swipe, not after it. */
async function showMode() {
  try {
    const h = await api("/api/health");
    if (h.dry_run) $("dry-badge").hidden = false;
  } catch (_) { /* the deck load will report anything that matters */ }
}

attachDetailGestures();
showMode();
loadDeck();
