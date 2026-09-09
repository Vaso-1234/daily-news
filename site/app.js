(() => {
  "use strict";

  const DATA_URL = "./data.json";
  const CLIENT_POLL_MS = 5 * 60 * 1000;
  const STATUS_TICK_MS = 30 * 1000;
  const STALE_AFTER_MS = 90 * 60 * 1000;

  const state = {
    payload: null,
    activeSource: "All",
    lastFetchOk: null,
  };

  const el = {
    feed: document.getElementById("feed"),
    sourceFilters: document.getElementById("source-filters"),
    statusDot: document.getElementById("status-dot"),
    statusText: document.getElementById("status-text"),
    refreshBtn: document.getElementById("refresh-btn"),
    todayDate: document.getElementById("today-date"),
    briefingSummary: document.getElementById("briefing-summary"),
  };

  el.todayDate.textContent = new Date().toLocaleDateString(undefined, {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
  });

  function formatRelative(iso) {
    if (!iso) return "";
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "";
    const diffSec = Math.round((Date.now() - then) / 1000);
    if (diffSec < 45) return "just now";
    if (diffSec < 90) return "1 min ago";
    const diffMin = Math.round(diffSec / 60);
    if (diffMin < 60) return `${diffMin} min ago`;
    const diffHr = Math.round(diffMin / 60);
    if (diffHr < 24) return `${diffHr} hr ago`;
    const diffDay = Math.round(diffHr / 24);
    if (diffDay < 7) return `${diffDay} d ago`;
    return new Date(iso).toLocaleDateString();
  }

  function updateStatus() {
    if (!state.payload) {
      if (state.lastFetchOk === false) {
        el.statusDot.className = "dot error";
        el.statusText.textContent = "Failed to load";
      } else {
        el.statusDot.className = "dot";
        el.statusText.textContent = "Loading...";
      }
      return;
    }
    const genAt = new Date(state.payload.generated_at).getTime();
    const ageMs = Date.now() - genAt;
    const relative = formatRelative(state.payload.generated_at) || "just now";
    if (ageMs > STALE_AFTER_MS) {
      el.statusDot.className = "dot stale";
      el.statusText.textContent = `Updated ${relative} (stale)`;
    } else {
      el.statusDot.className = "dot live";
      el.statusText.textContent = `Updated ${relative}`;
    }
  }

  function renderChip(label, active, onClick) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip" + (active ? " active" : "");
    btn.textContent = label;
    btn.addEventListener("click", onClick);
    return btn;
  }

  function renderFilters() {
    if (!state.payload) return;
    const sources = ["All", ...state.payload.sources];
    el.sourceFilters.replaceChildren(
      ...sources.map((s) =>
        renderChip(s, s === state.activeSource, () => {
          state.activeSource = s;
          renderFilters();
          renderFeed();
        })
      )
    );
  }

  function renderBriefingSummary() {
    if (!el.briefingSummary) return;
    if (!state.payload) {
      el.briefingSummary.innerHTML = "&nbsp;";
      return;
    }
    const total = state.payload.items.length;
    const counts = state.payload.counts || {};
    const win = state.payload.window_hours || 24;
    const perSource = Object.entries(counts)
      .filter(([, n]) => n > 0)
      .map(([src, n]) => `${n} ${src}`)
      .join(" \u00b7 ");
    const noun = total === 1 ? "story" : "stories";
    el.briefingSummary.textContent = perSource
      ? `${total} ${noun} in the last ${win}h  \u2014  ${perSource}`.replace(
          "\u2014",
          "\u00b7"
        )
      : `${total} ${noun} in the last ${win}h`;
  }

  function renderCard(item) {
    const card = document.createElement("article");
    card.className = "card";

    const meta = document.createElement("div");
    meta.className = "card-meta";

    const badge = document.createElement("span");
    badge.className = "badge";
    badge.dataset.source = item.source;
    badge.textContent = item.source;
    meta.appendChild(badge);

    if (item.section) {
      const sep = document.createElement("span");
      sep.className = "dot-sep";
      sep.textContent = "\u00b7";
      meta.appendChild(sep);
      const sec = document.createElement("span");
      sec.className = "section-tag";
      sec.textContent = item.section;
      meta.appendChild(sec);
    }

    const rel = formatRelative(item.published);
    if (rel) {
      const sep2 = document.createElement("span");
      sep2.className = "dot-sep";
      sep2.textContent = "\u00b7";
      meta.appendChild(sep2);
      const t = document.createElement("span");
      t.className = "time";
      t.textContent = rel;
      if (item.published) t.title = new Date(item.published).toLocaleString();
      meta.appendChild(t);
    }
    card.appendChild(meta);

    const h = document.createElement("h2");
    h.className = "card-title";
    const a = document.createElement("a");
    a.href = item.link;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = item.title;
    h.appendChild(a);
    card.appendChild(h);

    if (item.summary) {
      const p = document.createElement("p");
      p.className = "card-summary";
      p.textContent = item.summary;
      card.appendChild(p);
    }

    const more = document.createElement("a");
    more.className = "card-readmore";
    more.href = item.link;
    more.target = "_blank";
    more.rel = "noopener noreferrer";
    more.textContent = "Read at " + item.source + " ...";
    card.appendChild(more);

    return card;
  }

  function renderFeed() {
    if (!state.payload) return;
    const items = state.payload.items.filter(
      (item) =>
        state.activeSource === "All" || item.source === state.activeSource
    );

    if (items.length === 0) {
      el.feed.innerHTML =
        '<p class="empty">No stories match this filter yet...</p>';
      return;
    }

    const frag = document.createDocumentFragment();
    for (const item of items) frag.appendChild(renderCard(item));
    el.feed.replaceChildren(frag);
  }

  async function loadData({ userInitiated = false } = {}) {
    if (userInitiated) el.refreshBtn.classList.add("spinning");
    try {
      const bust = Date.now();
      const res = await fetch(`${DATA_URL}?t=${bust}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();
      state.payload = payload;
      state.lastFetchOk = true;
      renderFilters();
      renderBriefingSummary();
      renderFeed();
      updateStatus();
    } catch (err) {
      state.lastFetchOk = false;
      console.error("Failed to load data.json:", err);
      if (!state.payload) {
        el.feed.innerHTML = `<p class="error">Couldn't load the briefing: ${err.message}... the cron may not have produced data.json yet...</p>`;
      }
      updateStatus();
    } finally {
      if (userInitiated) {
        setTimeout(() => el.refreshBtn.classList.remove("spinning"), 400);
      }
    }
  }

  el.refreshBtn.addEventListener("click", () =>
    loadData({ userInitiated: true })
  );

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") loadData();
  });

  loadData();
  setInterval(loadData, CLIENT_POLL_MS);
  setInterval(updateStatus, STATUS_TICK_MS);
})();
