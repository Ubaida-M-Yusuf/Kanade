/* Kanade UI - vanilla JS, no build step. */
"use strict";

const TOKEN = document.querySelector('meta[name="kanade-token"]').content;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name) => `<svg><use href="#i-${name}"/></svg>`;
const collator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });
const tok = (url) => url + (url.includes("?") ? "&" : "?") + "k=" + encodeURIComponent(TOKEN);

const api = {
  async get(path) {
    const r = await fetch(path, { headers: { "X-Kanade-Token": TOKEN } });
    const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
    if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  },
  async post(path, body = {}) {
    const r = await fetch(path, { method: "POST", headers: { "X-Kanade-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
    if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  },
};

/* ------------------------------------------------------------------ state */
const S = {
  boot: null, settings: {}, videos: [], byId: new Map(), anime: [], animeById: new Map(), counts: {},
  view: "all", seriesId: null, history: [],
  q: "", searchIds: null, searchRank: null,
  filters: { type: "", year: "", series: "", num: "", fav: false, genres: new Set(), genreMode: "any",
             formats: new Set(), seasons: new Set(), quality: new Set(), lyrics: false, original: false,
             content: { sug: "", exp: "", nsfw: "" } },
  genreSearch: "", fTab: "type",
  genreIndex: new Map(),   // "Action" / "tag:Mecha" -> Set(anime ids)
  sort: "title", size: localStorage.getItem("kanade.size") || "m",
  sel: new Set(), focus: null, anchor: null, flat: [],
  version: null, loadingData: false, lastLoad: 0,
  mediaVersion: null, mediaTs: 0, mediaBusy: false, seriesSig: "",
  songs: new Map(),                 // song key -> every video of that song (versions, resolutions)
  stack: (() => { try { return localStorage.getItem("kanade.stack") !== "0"; } catch (e) { return true; } })(),
  fyTab: "rec",                     // For You: rec (recommended) | liked | hidden
  ratedSinceLearn: 0,
};

const VIEWS = {
  all: { title: "All Videos" }, series: { title: "Anime Series" }, op: { title: "Openings" }, ed: { title: "Endings" },
  fav: { title: "Favorites" }, year: { title: "By Year" }, recent: { title: "Recently Added" },
  review: { title: "Needs Review" }, settings: { title: "Settings" }, anime: { title: "" }, foryou: { title: "For You" },
};

/* ------------------------------------------------------------------ helpers */
const fmtDur = (s) => { if (!s) return ""; s = Math.round(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const themeLabel = (v) => `${v.type || "?"}${v.number || ""}${v.part || ""}`;
const fmtSize = (b) => b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(1) + " MB";
const fmtFormat = (f) => ({ TV_SHORT: "TV Short", MOVIE: "Movie", SPECIAL: "Special", ONA: "ONA", OVA: "OVA", TV: "TV" }[f] || (f || "").replace(/_/g, " "));
const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "";
/** One song = every version / resolution of one theme (same series, OP/ED, number and part). */
const songKey = (v) => (v.anime_id != null ? `${v.anime_id}|${v.type}|${v.number}|${v.part || ""}` : "v" + v.id);
function resLabel(h) {
  if (!h) return "";
  if (h >= 2000) return "4K";
  if (h >= 1400) return "1440p";
  if (h >= 1000) return "1080p";
  if (h >= 700) return "720p";
  if (h >= 560) return "576p";
  if (h >= 460) return "480p";
  if (h >= 340) return "360p";
  return h + "p";
}
/** "NCBD1080-Lyrics" -> "Creditless · Blu-ray · Lyrics" */
function tagLabel(tags) {
  const out = [];
  for (const t of String(tags || "").split("-").filter(Boolean)) {
    const m = /^(NC)?(BD|DVD|WEB)?(\d+)?$/i.exec(t);
    if (m && (m[1] || m[2])) {
      if (m[1]) out.push("Creditless");
      if (m[2]) out.push({ BD: "Blu-ray", DVD: "DVD", WEB: "Web" }[m[2].toUpperCase()]);
    } else out.push({ EN: "English subs", Trans: "Translated", Subbed: "Subbed" }[t] || t);
  }
  return out.join(" · ");
}
/** Which version a stack shows on its card: the best picture first, clean (no lyrics) before lyrics, v1 first. */
function versionQuality(a, b) {
  return (b.height || 0) - (a.height || 0) || (isLyrics(a) ? 1 : 0) - (isLyrics(b) ? 1 : 0) || (a.version || 1) - (b.version || 1) || a.id - b.id;
}
/** A rough franchise key from the title ("BlazBlue: Continuum Shift" and "BlazBlue: Calamity Trigger", or
    "Railgun S" and "Railgun T", share one), used to keep one franchise from filling the top of For You. */
function franchiseKey(v) {
  const full = (v.title || "").toLowerCase(), head = full.split(/[:：]| - /)[0].trim();
  let t = head.length >= 4 ? head : full;              // "Re:Zero" keeps its whole title
  t = t.replace(/[!?.,'’"()\[\]]/g, " ").replace(/\s+/g, " ").trim();
  for (let i = 0; i < 3; i++) t = t.replace(/\s+([a-z]{1,3}\d?|\d+|\d+(st|nd|rd|th)\s+season|season\s+\d+|the movie|movie|specials?|final season|zoku|shin)$/, "").trim();
  return t || "a" + v.anime_id;
}
/** Group a sorted list into one card per song (at the position of its first version in the list). */
function stackItems(items) {
  if (!S.stack || S.view === "review") return items;
  const groups = new Map(), order = [];
  for (const v of items) {
    const k = songKey(v);
    let g = groups.get(k);
    if (!g) { g = []; groups.set(k, g); order.push(k); }
    g.push(v);
  }
  if (order.length === items.length) return items;
  return order.map((k) => {
    const g = groups.get(k);
    if (g.length === 1) return g[0];
    const best = g.slice().sort(versionQuality)[0];
    const card = Object.create(best);       // reads fall through to the real video (favourites, ratings stay live)
    card._stack = g;
    return card;
  });
}
const stackOf = (v) => (v && v._stack) || [v];
/** Every video of the song a card stands for (also versions hidden by the current filters). */
const songVideos = (v) => S.songs.get(songKey(v)) || [v];
function toast(msg, err = false) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.transition = "opacity .3s"; el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, err ? 6000 : 2600);
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
const sourceLabel = (s) => ({ animethemes: "AnimeThemes (exact file)", "animethemes-prefix": "AnimeThemes (series)", "animethemes-fuzzy": "AnimeThemes (fuzzy title)", anilist: "AniList search", jikan: "MyAnimeList search", manual: "Manual" }[s] || s || "—");

/* ------------------------------------------------------------------ data */
async function loadData(keepScroll = true) {
  if (S.loadingData) return;
  S.loadingData = true;
  try {
    const [vd, ad] = await Promise.all([api.get("/api/videos"), api.get("/api/anime")]);
    const cols = vd.cols;
    S.videos = vd.rows.map((r) => { const o = {}; for (let i = 0; i < cols.length; i++) o[cols[i]] = r[i]; o._t = (o.title || "").toLowerCase(); return o; });
    S.byId = new Map(S.videos.map((v) => [v.id, v]));
    S.songs = new Map();
    for (const v of S.videos) { const k = songKey(v); if (!S.songs.has(k)) S.songs.set(k, []); S.songs.get(k).push(v); }
    S.anime = ad.rows;
    S.genreIndex = new Map();
    for (const a of S.anime) {
      try { a.genres = JSON.parse(a.genres || "[]"); } catch (e) { a.genres = []; }
      try { a.tags = JSON.parse(a.tags || "[]"); } catch (e) { a.tags = []; }
      for (const g of a.genres) { if (!S.genreIndex.has(g)) S.genreIndex.set(g, new Set()); S.genreIndex.get(g).add(a.id); }
      for (const t of a.tags) { const k = "tag:" + t; if (!S.genreIndex.has(k)) S.genreIndex.set(k, new Set()); S.genreIndex.get(k).add(a.id); }
    }
    S.animeById = new Map(S.anime.map((a) => [a.id, a]));
    S.counts = vd.counts;
    S.counts.foryou = S.rec ? forYouCount() : 0;
    S.version = vd.version;
    S.mediaVersion = vd.media_version;
    S.mediaTs = vd.media_ts;
    S.lastLoad = Date.now();
    for (const id of [...S.sel]) if (!S.byId.has(id)) S.sel.delete(id);
    updateCounts();
    fillFilterOptions();
    render(keepScroll);
    S.loadError = null;
  } catch (e) {
    S.loadError = e.message || "connection failed";
    if (!S.videos.length) render(false);   // show "can't reach" instead of an empty library
    else toast("Could not refresh library: " + S.loadError, true);
  } finally { S.loadingData = false; }
}

/** For You's sidebar number: strong matches (85 %+), not every ranked video. */
function forYouCount() {
  const rated = ratedSongs(), seen = new Set();
  for (const [id, r] of S.rec) {
    if ((S.recMode === "mix" ? r.m : r.a) < 85) continue;            // whole taste, current mode
    const v = S.byId.get(id); if (!v) continue;
    const k = songKey(v);
    if (!rated.has(k)) seen.add(k);
  }
  return seen.size;
}
/** Songs the user already rated (like / dislike), favourited or has in a liked folder: not recommended again. */
const inLikedFolder = (v) => !!(S.rec.get(v.id) || {}).lf;
function ratedSongs() {
  const out = new Set();
  for (const v of S.videos) if (v.rating || v.fav || inLikedFolder(v)) out.add(songKey(v));
  return out;
}
const isLiked = (x) => x.rating === 1 || ((x.fav || inLikedFolder(x)) && x.rating !== -1);
function updateCounts() {
  for (const b of $$("[data-count]")) {
    const n = S.counts[b.dataset.count] || 0;
    b.textContent = n ? n.toLocaleString() : (b.classList.contains("warn") ? "" : "0");
  }
}

/* ---------------- Filters panel: type, season, genres, tags, quality & more */
const gLabel = (k) => k.replace(/^tag:/, "");
const FORMAT_ORDER = ["TV", "TV_SHORT", "MOVIE", "OVA", "ONA", "SPECIAL", "Game", "Visual Novel"];
const SEASONS = [["Winter", "❄"], ["Spring", "🌸"], ["Summer", "☀"], ["Fall", "🍁"]];
const QUALITY = [
  ["1080", "1080p & up", (v) => (v.height || 0) >= 1080],
  ["720", "720p", (v) => (v.height || 0) >= 720 && v.height < 1080],
  ["sd", "Below 720p", (v) => (v.height || 0) > 0 && v.height < 720],
];
const LYRICS_RE = /Lyrics|(^|-)EN($|-)|Trans|Sub/i;
const isLyrics = (v) => LYRICS_RE.test(v.tags || "");

function genreAnimeSet() {
  /** anime ids allowed by the genre/tag selection, or null when none is selected */
  const keys = [...S.filters.genres];
  if (!keys.length) return null;
  const sets = keys.map((k) => S.genreIndex.get(k) || new Set());
  if (S.filters.genreMode === "all") {
    sets.sort((a, b) => a.size - b.size);
    return new Set([...sets[0]].filter((id) => sets.every((s) => s.has(id))));
  }
  const out = new Set();
  for (const s of sets) for (const id of s) out.add(id);
  return out;
}

function animeFacetSet() {
  /** anime ids allowed by all series-level filters (type, season, genres/tags); null = no restriction */
  const f = S.filters;
  let set = null;
  const narrow = (s) => { set = set ? new Set([...set].filter((x) => s.has(x))) : s; };
  if (f.formats.size) narrow(new Set(S.anime.filter((a) => f.formats.has(a.format || "")).map((a) => a.id)));
  if (f.seasons.size) narrow(new Set(S.anime.filter((a) => f.seasons.has(a.season || "")).map((a) => a.id)));
  const g = genreAnimeSet();
  if (g) narrow(g);
  const cf = f.content || {};
  for (const [k, col] of [["sug", "suggestive"], ["exp", "explicit"]]) {
    if (cf[k]) narrow(new Set(S.anime.filter((a) => (cf[k] === "only") === !!a[col]).map((a) => a.id)));
  }
  return set;
}

function videoFacetFilter(items) {
  const f = S.filters;
  let out = items;
  if (f.quality.size) { const tests = QUALITY.filter((q) => f.quality.has(q[0])).map((q) => q[2]); out = out.filter((v) => tests.some((t) => t(v))); }
  if (f.lyrics) out = out.filter(isLyrics);
  if (f.original) out = out.filter((v) => (v.version || 1) === 1);
  const cf = f.content || {};
  if (cf.nsfw) out = out.filter((v) => (cf.nsfw === "only") === !!v.nsfw);
  return out;
}

function panelFilterCount() {
  const f = S.filters;
  const cf = f.content || {};
  return f.genres.size + f.formats.size + f.seasons.size + f.quality.size + (f.lyrics ? 1 : 0) + (f.original ? 1 : 0) +
    ["sug", "exp", "nsfw"].filter((k) => cf[k]).length;
}

function activeFilterList() {
  const f = S.filters, out = [];
  for (const k of f.formats) out.push({ g: "format", k, label: fmtFormat(k) });
  for (const k of f.seasons) out.push({ g: "season", k, label: k });
  for (const k of f.genres) out.push({ g: "genre", k, label: k.startsWith("tag:") ? "#" + gLabel(k) : k });
  for (const k of f.quality) out.push({ g: "quality", k, label: (QUALITY.find((q) => q[0] === k) || [0, k])[1] });
  if (f.lyrics) out.push({ g: "lyrics", k: "1", label: "Lyrics / subtitles" });
  if (f.original) out.push({ g: "original", k: "1", label: "Original versions only" });
  const CL = { sug: ["Ecchi only", "No ecchi"], exp: ["18+ only", "No 18+"], nsfw: ["NSFW videos only", "No NSFW videos"] };
  for (const k of ["sug", "exp", "nsfw"]) {
    const val = (f.content || {})[k];
    if (val) out.push({ g: "content", k, label: CL[k][val === "only" ? 0 : 1] });
  }
  return out;
}

function removeFilter(g, k) {
  const f = S.filters;
  if (g === "format") f.formats.delete(k);
  else if (g === "season") f.seasons.delete(k);
  else if (g === "genre") f.genres.delete(k);
  else if (g === "quality") f.quality.delete(k);
  else if (g === "lyrics") f.lyrics = false;
  else if (g === "original") f.original = false;
  else if (g === "content") f.content[k] = "";
}

function toggleFilter(g, k, on) {
  const f = S.filters;
  const sets = { format: f.formats, season: f.seasons, genre: f.genres, quality: f.quality };
  if (sets[g]) { if (on) sets[g].add(k); else sets[g].delete(k); }
  else if (g === "lyrics") f.lyrics = on;
  else if (g === "original") f.original = on;
}

function anyFilterActive() {
  const f = S.filters;
  return panelFilterCount() > 0 || !!(S.q || f.type || f.year || f.series || f.num || f.fav);
}

function updateGenreButton() {
  const n = panelFilterCount();
  $("#filterCount").textContent = n ? String(n) : "";
  $("#filterBtn").classList.toggle("on", n > 0);
  $("#clearFilters").classList.toggle("hidden", !anyFilterActive());
  renderActiveChips();
}

function renderActiveChips() {
  const box = $("#activeChips");
  if (!box) return;
  const list = activeFilterList();
  box.classList.toggle("hidden", !list.length || !isGridView() || S.view === "anime");
  box.innerHTML = list.map((x) => `<button class="achip ${x.g}" data-unsel="${esc(x.g)}|${esc(x.k)}" title="Remove filter">${esc(x.label)}<span>✕</span></button>`).join("") +
    (list.length ? `<button class="achip-clear" data-clearall="1">Clear all</button>` : "");
}

function fillGenreOptions() { updateGenreButton(); if (!$("#filterPop").classList.contains("hidden")) renderFilterPanel(); }

function filterChip(g, k, label, n, on, extra = "") {
  return `<label class="gchk${on ? " on" : ""}${extra}"><input type="checkbox" data-fg="${esc(g)}" data-fk="${esc(k)}" ${on ? "checked" : ""}>
    <span class="box">${icon("check")}</span><span class="nm">${label}</span>${n != null ? `<span class="ct">${Number(n).toLocaleString()}</span>` : ""}</label>`;
}

function renderFilterPanel() {
  const pop = $("#filterPop"), f = S.filters, tab = S.fTab || "type";
  // counts: series for series-level facets, videos for video-level ones
  const fmtCount = new Map(), seasonCount = new Map();
  for (const a of S.anime) {
    fmtCount.set(a.format || "", (fmtCount.get(a.format || "") || 0) + 1);
    seasonCount.set(a.season || "", (seasonCount.get(a.season || "") || 0) + 1);
  }
  const genres = [], tags = [];
  for (const [k, ids] of S.genreIndex) (k.startsWith("tag:") ? tags : genres).push([k, ids.size]);
  genres.sort((a, b) => b[1] - a[1]);
  tags.sort((a, b) => b[1] - a[1]);
  const nTabs = {
    type: f.formats.size + f.seasons.size,
    genres: [...f.genres].filter((k) => !k.startsWith("tag:")).length,
    tags: [...f.genres].filter((k) => k.startsWith("tag:")).length,
    more: f.quality.size + (f.lyrics ? 1 : 0) + (f.original ? 1 : 0),
    content: ["sug", "exp", "nsfw"].filter((k) => (f.content || {})[k]).length,
  };
  const tabBtn = (id, label) => `<button class="ftab${tab === id ? " on" : ""}" data-tab="${id}">${label}${nTabs[id] ? `<b>${nTabs[id]}</b>` : ""}</button>`;
  const modeSeg = `<div class="seg" title="How to combine several genres/tags">
      <button data-mode="any" class="${f.genreMode === "any" ? "on" : ""}">Any</button>
      <button data-mode="all" class="${f.genreMode === "all" ? "on" : ""}">All</button></div>`;
  const modeHint = `<div class="gpop-hint">${f.genreMode === "all" ? "Series with <b>every</b> selected genre &amp; tag" : "Series with <b>at least one</b> selected genre or tag"}</div>`;
  let body = "";
  if (tab === "type") {
    const fmts = FORMAT_ORDER.filter((k) => fmtCount.get(k)).concat([...fmtCount.keys()].filter((k) => k && !FORMAT_ORDER.includes(k)));
    body = `<div class="gsec">Type <small>· series</small></div>
      <div class="ggrid g4">${fmts.map((k) => filterChip("format", k, esc(fmtFormat(k)), fmtCount.get(k), f.formats.has(k))).join("")}</div>
      <div class="gsec">Season <small>· when the series aired</small></div>
      <div class="ggrid g4">${SEASONS.map(([k, ic]) => filterChip("season", k, `<i class="sic">${ic}</i>${k}`, seasonCount.get(k) || 0, f.seasons.has(k))).join("")}</div>`;
  } else if (tab === "genres") {
    body = `<div class="ftools">${modeSeg}</div>${modeHint}
      <div class="ggrid">${genres.map(([k, n]) => filterChip("genre", k, esc(k), n, f.genres.has(k))).join("")}</div>`;
  } else if (tab === "tags") {
    const q = S.genreSearch.trim().toLowerCase();
    const shown = tags.filter(([k, n]) => n >= 3 && (!q || gLabel(k).toLowerCase().includes(q))).slice(0, q ? 300 : 150);
    body = `<div class="ftools"><div class="search mini">${icon("search")}<input id="gSearch" placeholder="Search tags (idol, mecha, isekai…)" value="${esc(S.genreSearch)}" autocomplete="off"></div>${modeSeg}</div>${modeHint}
      ${shown.length ? `<div class="ggrid">${shown.map(([k, n]) => filterChip("genre", k, esc(gLabel(k)), n, f.genres.has(k))).join("")}</div>` : `<div class="loading">No tag matches “${esc(S.genreSearch)}”.</div>`}`;
  } else if (tab === "content") {
    const nSug = S.anime.filter((a) => a.suggestive).length, nExp = S.anime.filter((a) => a.explicit).length;
    const nNsfw = S.videos.filter((v) => v.nsfw).length;
    const row = (k, title, sub, n) => {
      const val = (f.content || {})[k] || "";
      return `<div class="crow"><div><b>${title}</b><small>${sub} · ${n.toLocaleString()}</small></div>
        <div class="seg">${[["", "Any"], ["only", "Only"], ["hide", "Hide"]].map(([v, l]) => `<button data-content="${k}" data-cval="${v}" class="${val === v ? "on" : ""}">${l}</button>`).join("")}</div></div>`;
    };
    body = `<div class="gsec">Content <small>· combined from AniList, Kitsu and AnimeThemes</small></div>
      ${row("sug", "Ecchi / suggestive", "series with ecchi, fan service or sexual content", nSug)}
      ${row("exp", "18+ / explicit", "series rated adult (subset of the above)", nExp)}
      ${row("nsfw", "NSFW opening/ending video", "the video itself is marked NSFW (nudity, gore or flashing)", nNsfw)}`;
  } else {
    const qCount = QUALITY.map((q) => S.videos.filter(q[2]).length);
    const lyr = S.videos.filter(isLyrics).length, orig = S.videos.filter((v) => (v.version || 1) === 1).length;
    body = `<div class="gsec">Video quality <small>· videos</small></div>
      <div class="ggrid g3">${QUALITY.map((q, i) => filterChip("quality", q[0], esc(q[1]), qCount[i], f.quality.has(q[0]))).join("")}</div>
      <div class="gsec">Versions</div>
      <div class="ggrid g2">${filterChip("original", "1", "Original versions only <small>hide v2, v3…</small>", orig, f.original)}
        ${filterChip("lyrics", "1", "With lyrics / subtitles", lyr, f.lyrics)}</div>`;
  }
  const active = activeFilterList();
  pop.innerHTML = `
    <div class="ftabs">${tabBtn("type", "Type &amp; season")}${tabBtn("genres", "Genres")}${tabBtn("tags", "Tags")}${tabBtn("content", "Content")}${tabBtn("more", "Quality &amp; more")}</div>
    ${active.length ? `<div class="gsel">${active.map((x) => `<button class="achip ${x.g}" data-unsel="${esc(x.g)}|${esc(x.k)}">${esc(x.label)}<span>✕</span></button>`).join("")}</div>` : ""}
    <div class="gpop-body">${body}</div>
    <div class="gpop-foot"><span class="gres" id="gResult"></span>
      <button class="btn sm ghost" data-gact="clear"${active.length ? "" : " disabled"}>Clear filters</button><button class="btn sm primary" data-gact="done">Done</button></div>`;
  const items = applyFilters(baseItems());
  const nSeries = new Set(items.map((v) => v.anime_id)).size;
  $("#gResult").innerHTML = `<b>${items.length.toLocaleString()}</b> videos · <b>${nSeries.toLocaleString()}</b> series`;
  const inp = $("#gSearch");
  if (inp) inp.addEventListener("input", debounce(() => {
    S.genreSearch = inp.value; const pos = inp.selectionStart; renderFilterPanel();
    const ni = $("#gSearch"); if (ni) { ni.focus(); ni.setSelectionRange(pos, pos); }
  }, 120));
}

function toggleFilterPanel(force) {
  const pop = $("#filterPop");
  const open = force ?? pop.classList.contains("hidden");
  pop.classList.toggle("hidden", !open);
  $("#filterBtn").classList.toggle("open", open);
  if (open) { renderFilterPanel(); if (S.fTab === "tags") setTimeout(() => $("#gSearch")?.focus(), 30); }
}

function applyGenreChange() {
  updateGenreButton();
  if (!isGridView() || S.view === "anime") setView("all");
  else render(false);
}

function setGenre(g) {
  S.filters.genres = new Set(g ? [g] : []);
  S.filters.genreMode = "any";
  applyGenreChange();
  if (g) toast(`Showing ${gLabel(g)} · use Clear to show everything again`);
}

function clearAllFilters() {
  S.filters = { type: "", year: "", series: "", num: "", fav: false, genres: new Set(), genreMode: S.filters.genreMode,
                formats: new Set(), seasons: new Set(), quality: new Set(), lyrics: false, original: false,
                content: { sug: "", exp: "", nsfw: "" } };
  S.genreSearch = "";
  $("#q").value = ""; S.q = ""; S.searchIds = null; S.searchRank = null;
  if (S.sort === "relevance") setSort("title");
  for (const x of $$("#typeSeg button")) x.classList.toggle("on", x.dataset.type === "");
  $("#yearSel").value = ""; $("#numSel").value = ""; $("#seriesFilter").value = ""; $("#seriesFilter").classList.remove("on");
  $("#favOnly").classList.remove("on");
  toggleFilterPanel(false);
  updateGenreButton();
  render(false);
}

function onFilterClick(e) {
  const cb = e.target.closest("[data-content]");
  if (cb) { S.filters.content[cb.dataset.content] = cb.dataset.cval; renderFilterPanel(); applyGenreChange(); return true; }
  const t = e.target.closest("[data-tab]");
  if (t) { S.fTab = t.dataset.tab; renderFilterPanel(); if (S.fTab === "tags") $("#gSearch")?.focus(); return true; }
  const m = e.target.closest("[data-mode]");
  if (m) { S.filters.genreMode = m.dataset.mode; renderFilterPanel(); applyGenreChange(); return true; }
  const u = e.target.closest("[data-unsel]");
  if (u) { const [g, ...k] = u.dataset.unsel.split("|"); removeFilter(g, k.join("|")); if (!$("#filterPop").classList.contains("hidden")) renderFilterPanel(); applyGenreChange(); return true; }
  if (e.target.closest("[data-clearall]")) { clearAllFilters(); return true; }
  const a = e.target.closest("[data-gact]");
  if (a && a.dataset.gact === "clear") {
    const f = S.filters;
    f.genres.clear(); f.formats.clear(); f.seasons.clear(); f.quality.clear(); f.lyrics = false; f.original = false;
    f.content = { sug: "", exp: "", nsfw: "" };
    renderFilterPanel(); applyGenreChange(); return true;
  }
  if (a && a.dataset.gact === "done") { toggleFilterPanel(false); return true; }
  return false;
}

$("#filterBtn").addEventListener("click", (e) => { e.stopPropagation(); toggleFilterPanel(); });
$("#filterPop").addEventListener("click", (e) => { e.stopPropagation(); onFilterClick(e); });
$("#filterPop").addEventListener("change", (e) => {
  const cb = e.target.closest("[data-fg]"); if (!cb) return;
  toggleFilter(cb.dataset.fg, cb.dataset.fk, cb.checked);
  renderFilterPanel();
  applyGenreChange();
});
$("#activeChips").addEventListener("click", (e) => onFilterClick(e));
document.addEventListener("mousedown", (e) => { if (!e.target.closest(".gfilter")) { $("#filterPop").classList.add("hidden"); $("#filterBtn").classList.remove("open"); } });
$("#clearFilters").addEventListener("click", clearAllFilters);

function fillFilterOptions() {
  const years = [...new Set(S.videos.map((v) => v.year).filter(Boolean))].sort((a, b) => b - a);
  const ys = $("#yearSel"), cur = ys.value;
  ys.innerHTML = `<option value="">All years</option>` + years.map((y) => `<option ${String(y) === cur ? "selected" : ""}>${y}</option>`).join("");
  let maxN = 1;
  for (const v of S.videos) if ((v.number || 1) > maxN) maxN = v.number;
  maxN = Math.min(60, maxN);
  const ns = $("#numSel"), curN = ns.value;
  ns.innerHTML = `<option value="">Any OP/ED no.</option>` + Array.from({ length: maxN }, (_, i) => `<option value="${i + 1}" ${String(i + 1) === curN ? "selected" : ""}>OP/ED ${i + 1}</option>`).join("");
  S.seriesTitles = null;  // built lazily for the series filter suggestions
  fillGenreOptions();
}

/* ------------------------------------------------------------------ filtering & sorting */
function baseItems() {
  const v = S.videos;
  switch (S.view) {
    case "op": return v.filter((x) => x.type === "OP");
    case "ed": return v.filter((x) => x.type === "ED");
    case "fav": return v.filter((x) => x.fav);
    case "review": return v.filter((x) => x.status === "review");
    case "foryou": {
      if (S.fyTab === "liked") return v.filter(isLiked);
      if (S.fyTab === "hidden") return v.filter((x) => x.rating === -1);
      if (!S.rec.size) return [];
      const rated = ratedSongs();
      const g = S.recGroup >= 0 && S.recGroup < S.recGroups.length ? S.recGroup : -1;
      return v.filter((x) => S.rec.has(x.id) && !rated.has(songKey(x)) && (g < 0 || bestGroup(S.rec.get(x.id)) === g));
    }
    case "anime": return v.filter((x) => x.anime_id === S.seriesId);
    default: return v;
  }
}

function applyFilters(items) {
  const f = S.filters;
  let out = items;
  if (S.searchIds) out = out.filter((x) => S.searchIds.has(x.id));
  if (f.type && S.view !== "op" && S.view !== "ed") out = out.filter((x) => x.type === f.type);
  if (f.year) out = out.filter((x) => String(x.year) === f.year);
  if (f.num) out = out.filter((x) => String(x.number) === f.num);
  const aset = animeFacetSet();
  if (aset) out = out.filter((x) => aset.has(x.anime_id));
  out = videoFacetFilter(out);
  if (f.fav) out = out.filter((x) => x.fav);
  if (f.series) {
    const ids = new Set(S.anime.filter((a) => a.title === f.series).map((a) => a.id));
    if (ids.size) out = out.filter((x) => ids.has(x.anime_id));
  }
  return out;
}

const typeOrder = (t) => (t === "OP" ? 0 : t === "ED" ? 1 : 2);
function cmpTheme(a, b) { return typeOrder(a.type) - typeOrder(b.type) || (a.number || 0) - (b.number || 0) || (a.part || "").localeCompare(b.part || "") || (a.version || 1) - (b.version || 1); }
function sortItems(items) {
  const s = S.view === "fav" && S.sort === "title" && !S.q ? "fav" : S.sort;
  const arr = items.slice();
  const byTitle = (a, b) => collator.compare(a._t, b._t) || (a.anime_id || 0) - (b.anime_id || 0) || cmpTheme(a, b);
  if (s === "match") arr.sort((a, b) => recScore(S.rec.get(b.id)) - recScore(S.rec.get(a.id)) || byTitle(a, b));
  else if (s === "relevance" && S.searchRank) arr.sort((a, b) => (S.searchRank.get(a.id) ?? 1e9) - (S.searchRank.get(b.id) ?? 1e9));
  else if (s === "year_desc") arr.sort((a, b) => (b.year || 0) - (a.year || 0) || byTitle(a, b));
  else if (s === "year_asc") arr.sort((a, b) => (a.year || 9999) - (b.year || 9999) || byTitle(a, b));
  else if (s === "added") arr.sort((a, b) => (b.added || 0) - (a.added || 0) || byTitle(a, b));
  else if (s === "number") arr.sort((a, b) => cmpTheme(a, b) || byTitle(a, b));
  else if (s === "fav") arr.sort((a, b) => (b.fav_at || 0) - (a.fav_at || 0) || byTitle(a, b));
  else arr.sort(byTitle);
  return arr;
}

function folderOrder(f) {
  if (/^\d{4}$/.test(f)) return [0, -Number(f)];
  const d = /^(\d0)s$/i.exec(f);
  if (d) { const n = Number(d[1]); return [1, -(n >= 30 ? 1900 + n : 2000 + n)]; }
  return [2, f];
}
const SEASON_ORDER = { Winter: 0, Spring: 1, Summer: 2, Fall: 3 };

/* ------------------------------------------------------------------ virtual grid */
const V = { rows: [], offsets: [], total: 0, cols: 1, cardW: 280, live: new Map(), spare: new Map(), kind: "cards" };
const scroller = $("#scroller"), canvas = $("#canvas");
const SIZES = { xs: 160, s: 196, m: 236, l: 296, xl: 380 };
const SIZE_STEPS = ["xs", "s", "m", "l", "xl"];
if (!SIZES[S.size]) S.size = "m";
const GAP = 20;

function measure(kind) {
  const w = canvas.clientWidth || scroller.clientWidth - 56;
  const squeeze = $("#app").classList.contains("with-details") ? 0.8 : 1;
  const min = Math.round((kind === "posters" ? SIZES[S.size] * 0.66 : SIZES[S.size]) * squeeze);
  const cols = Math.max(1, Math.floor((w + GAP) / (min + GAP)));
  const cardW = (w - GAP * (cols - 1)) / cols;
  return { w, cols, cardW };
}

/** groups: [{header?, sub?, count?, anchor?, items:[...]}], kind 'cards' | 'posters', hero: {h, html} | null */
/** Re-flow the current rows for a new width/card size without re-filtering or re-sorting. */
function relayout() {
  if (!V.last || !isGridView()) return render(true);
  const top = scroller.scrollTop;
  buildRows(V.last.groups, V.last.kind, V.last.hero);
  scroller.scrollTop = top;
  paint();
}

function buildRows(groups, kind, hero = null) {
  V.last = { groups, kind, hero };
  const { cols, cardW } = measure(kind);
  V.cols = cols; V.cardW = cardW; V.kind = kind;
  const itemH = kind === "posters" ? Math.round(cardW * 1.5) + 62 : Math.round(cardW * 9 / 16) + 78;
  const rows = [];
  if (hero) rows.push({ type: "hero", h: hero.h, html: hero.html });
  S.flat = [];
  for (const g of groups) {
    if (g.header) rows.push({ type: "header", h: g.sub ? 44 : 58, label: g.header, count: g.count ?? g.items.length, sub: !!g.sub, anchor: g.anchor,
                              color: g.color, seeAll: g.seeAll, note: g.note });
    for (let i = 0; i < g.items.length; i += cols) {
      rows.push({ type: kind, h: itemH + GAP, items: g.items.slice(i, i + cols), start: S.flat.length + i });
    }
    S.flat.push(...g.items);
  }
  V.rows = rows;
  V.offsets = new Array(rows.length);
  let y = 0;
  for (let i = 0; i < rows.length; i++) { V.offsets[i] = y; y += rows[i].h; }
  V.total = y + 80;
  canvas.style.height = V.total + "px";
  // keep the old elements around: paint() reuses any row whose markup is unchanged,
  // so background refreshes don't flicker images or swallow clicks
  for (const el of V.live.values()) V.spare.set(el._sig, el);
  V.live.clear();
}

function paint() {
  const top = scroller.scrollTop, h = scroller.clientHeight;
  const over = h * 0.6;
  const lo = top - over, hi = top + h + over;
  let a = 0, b = V.rows.length - 1, first = V.rows.length;
  while (a <= b) { const m = (a + b) >> 1; if (V.offsets[m] + V.rows[m].h < lo) a = m + 1; else { first = m; b = m - 1; } }
  const want = new Set();
  for (let i = first; i < V.rows.length && V.offsets[i] < hi; i++) want.add(i);
  for (const [i, el] of V.live) if (!want.has(i)) { stopPreview(el); el.remove(); V.live.delete(i); }
  for (const i of want) {
    if (V.live.has(i)) continue;
    let el = renderRow(V.rows[i]);
    const old = V.spare.get(el._sig);
    if (old) {
      V.spare.delete(el._sig); el = old;
      for (const c of el.querySelectorAll(".card")) { const id = Number(c.dataset.id); c.classList.toggle("sel", S.focus === id); c.classList.toggle("multi", S.sel.size > 1 && S.sel.has(id)); }
    }
    else canvas.appendChild(el);
    el.style.transform = `translateY(${V.offsets[i]}px)`;
    V.live.set(i, el);
  }
  for (const el of V.spare.values()) { stopPreview(el); el.remove(); }
  V.spare.clear();
  updateYearBarActive();
}

function renderRow(r) {
  const el = renderRowInner(r);
  el._sig = el.className + "|" + el.style.cssText.replace(/transform:[^;]*;?/, "") + "|" + el.innerHTML;
  for (const img of el.querySelectorAll("img")) if (img.complete && img.naturalWidth) img.classList.add("loaded");
  return el;
}

function renderRowInner(r) {
  const el = document.createElement("div");
  if (r.type === "hero") { el.className = "vrow heroRow"; el.style.height = r.h + "px"; el.innerHTML = r.html; return el; }
  if (r.type === "header") {
    el.className = "vrow header" + (r.sub ? " sub" : "");
    el.style.height = r.h + "px";
    if (r.anchor) el.dataset.anchor = r.anchor;
    el.innerHTML = `${r.color ? `<i class="gdot" style="--g:${r.color}"></i>` : ""}<h2>${esc(r.label)}</h2><span class="count">${r.count.toLocaleString()}${r.sub ? "" : " videos"}${r.note ? ` · ${esc(r.note)}` : ""}</span><span class="line"></span>${r.seeAll != null ? `<button class="hlink" data-tg="${r.seeAll}">See all ${r.count.toLocaleString()} ›</button>` : ""}`;
    return el;
  }
  el.className = "vrow";
  el.style.gridTemplateColumns = `repeat(${V.cols}, minmax(0, 1fr))`;
  el.innerHTML = r.type === "posters" ? r.items.map(posterHTML).join("") : r.items.map((v, j) => cardHTML(v, r.start + j)).join("");
  return el;
}

/** Hover buttons + state marks shared by cards and version tiles. ids = the videos a click applies to. */
function rateState(vs) {
  const liked = vs.some((x) => x.rating === 1), disliked = vs.some((x) => x.rating === -1);
  return { liked, disliked: disliked && !liked, fav: vs.some((x) => x.fav), folder: vs.some(inLikedFolder) };
}
function cardHTML(v, idx) {
  const members = stackOf(v), many = members.length > 1;
  const rs = rateState(songVideos(v));
  const sel = S.focus === v.id ? " sel" : "";
  const multi = S.sel.size > 1 && S.sel.has(v.id) ? " multi" : "";
  const ph = v.thumb === 1 ? "" : (v.has_cover && v.anime_id ? `<div class="ph" style="background-image:url('${tok("/cover/" + v.anime_id)}')"></div>` : `<div class="ph shimmer"></div>`);
  const img = v.thumb === 1 ? `<img src="${tok("/thumb/" + v.id + "?v=" + (v.tv || 0))}" alt="" decoding="async">` : (v.thumb === -1 && !v.has_cover ? `<div class="noimg">${icon("op")}</div>` : "");
  const ver = v.version > 1 ? `<small>v${v.version}</small>` : "";
  const tagText = (v.tags || "").replace(/NCBD\d*|NCDVD\d*|BD\d*|DVD\d*/g, "").replace(/-+/g, " ").trim();
  const tags = tagText ? `<span class="tagpill">${esc(tagText)}</span>` : "";
  const res = resLabel(v.height);
  const marks = (rs.folder && !rs.disliked ? `<i class="mk fld" title="In your liked folders">${icon("folder")}</i>` : "") +
                (rs.fav ? `<i class="mk fav" title="Favourite">${icon("heart")}</i>` : "") +
                (rs.liked ? `<i class="mk up" title="You liked this song">${icon("up")}</i>` : rs.disliked ? `<i class="mk down" title="You disliked this song (hidden from For You)">${icon("down")}</i>` : "");
  const hidden = S.view === "foryou" && S.fyTab === "hidden";
  const acts = `<span class="acts">
        <button class="act${rs.liked ? " on" : ""}" data-rate="1" title="${rs.liked ? "Remove like" : "Like: more songs like this in For You (L)"}">${icon("up")}</button>
        <button class="act${rs.disliked ? " on" : ""}" data-rate="-1" title="${rs.disliked ? "Remove dislike (show it in For You again)" : "Dislike: hide from For You and learn to avoid songs like this (D)"}">${icon("down")}</button>
        <button class="act fav${v.fav ? " on" : ""}" data-fav="${v.id}" title="Favorite (F)">${icon("heart")}</button></span>`;
  const song = v.song ? `${esc(v.song)}${v.artists ? ` <span class="artist">– ${esc(v.artists)}</span>` : ""}` : `<span class="artist">${v.status === "review" ? "Unidentified" : "Unknown song"}</span>`;
  const an = S.animeById.get(v.anime_id);
  const flag = an && an.explicit ? `<span class="cflag x">18+</span>` : an && an.suggestive ? `<span class="cflag">Ecchi</span>` : "";
  let gtag = "";
  if (S.view === "foryou" && S.recGroups.length > 1) {
    const gi = bestGroup(S.rec.get(v.id));
    if (gi >= 0) gtag = `<span class="gtag" style="--g:${groupColor(gi)}" title="Song type, measured from the song's own sound (calm … intense)">${esc(S.recGroups[gi].name)}</span>`;
  }
  const sub = [v.year, v.folder_season || v.season].filter(Boolean).map(esc).join(`<span class="dot"></span>`) + flag + gtag;
  return `<div class="card${sel}${multi}${many ? " stack" : ""}" data-id="${v.id}" data-idx="${idx}"${many ? ` data-stack="${members.length}"` : ""}>
    <div class="thumb">${ph}${img}
      <span class="badge ${v.type || ""}">${esc(themeLabel(v))}${many ? "" : ver}</span>
      <span class="tr"><span class="info">${tags}${marks}${res ? `<span class="res r${res.replace(/\D/g, "") || "4"}" title="${v.width}×${v.height}">${res}</span>` : ""}</span>${acts}</span>
      ${v.status === "review" ? `<span class="review-flag">Review</span>` : ""}
      ${S.view === "foryou" && S.rec.has(v.id) && !hidden ? matchPill(S.rec.get(v.id)) : ""}
      ${hidden ? `<button class="unhide" data-rate="0" title="Show this song in For You again">${icon("eye")}Unhide</button>` : ""}
      <span class="br">${many ? `<span class="vcount" title="${members.length} versions of this song: click to see them all">${icon("stack")}${members.length}</span>` : ""}${v.duration ? `<span class="dur">${fmtDur(v.duration)}</span>` : ""}</span>
      <span class="check">${icon("check")}</span>
    </div>
    <div class="meta"><div class="title" title="${esc(v.title)}">${esc(v.title)}</div>
      <div class="song">${song}</div>
      <div class="sub">${sub}</div></div></div>`;
}

function posterHTML(a) {
  const img = a.has_cover ? `<img src="${tok("/cover/" + a.id)}" alt="" decoding="async">` : "";
  const initial = esc((a.title || "?").trim()[0] || "?");
  const sub = [a.year, a.season, fmtFormat(a.format)].filter(Boolean).map(esc).join(" · ");
  return `<div class="poster" data-anime="${a.id}">
    <div class="art"><div class="initial">${initial}</div>${img}
      <div class="counts">${a.ops ? `<span>${a.ops} OP</span>` : ""}${a.eds ? `<span class="ed">${a.eds} ED</span>` : ""}${a.favs ? `<span class="fv">♥ ${a.favs}</span>` : ""}</div></div>
    <div class="title" title="${esc(a.title)}">${esc(a.title)}</div>
    <div class="sub">${a.title_english && a.title_english !== a.title ? esc(a.title_english) : sub}</div></div>`;
}

let paintQueued = false, scrollIdle = null;
scroller.addEventListener("scroll", () => {
  if (!paintQueued) { paintQueued = true; requestAnimationFrame(() => { paintQueued = false; paint(); }); }
  // no hover previews while the list is moving
  clearTimeout(hoverTimer);
  if (!S.scrolling) { S.scrolling = true; if (hoverCard) stopPreview(hoverCard); }
  clearTimeout(scrollIdle);
  scrollIdle = setTimeout(() => { S.scrolling = false; }, 160);
}, { passive: true });

/** Re-render only the visible rows whose markup changed (after small data patches). */
function refreshVisible() {
  for (const [i, el] of V.live) {
    const fresh = renderRow(V.rows[i]);
    if (fresh._sig === el._sig) continue;
    fresh.style.transform = el.style.transform;
    stopPreview(el);
    el.replaceWith(fresh);
    V.live.set(i, fresh);
  }
}
canvas.addEventListener("load", (e) => { if (e.target.tagName === "IMG") e.target.classList.add("loaded"); }, true);
canvas.addEventListener("error", (e) => { if (e.target.tagName === "IMG") e.target.remove(); }, true);
let lastW = 0;
new ResizeObserver(debounce(() => { const w = canvas.clientWidth; if (w !== lastW && isGridView()) { lastW = w; relayout(); } }, 100)).observe(scroller);

/* ------------------------------------------------------------------ render views */
function isGridView() { return S.view !== "settings" && S.view !== "setup"; }

function setView(view, opts = {}) {
  if (!opts.noHistory) S.history.push({ view: S.view, seriesId: S.seriesId, scroll: scroller.scrollTop });
  const wasForYou = S.view === "foryou";
  if (wasForYou && view !== "foryou") {
    S.fyMemo = { top: scroller.scrollTop, sig: fySig() };       // come back to the same place if nothing changed
    if (S.ratedSinceLearn) { S.ratedSinceLearn = 0; api.post("/api/foryou/learn").catch(() => {}); }
  }
  if (view === "foryou" && !wasForYou) {
    if (S.pendingRec) { applyRecs(S.pendingRec); S.fyMemo = null; opts.scroll = 0; }    // new picks: start at the top
    else if (S.fyMemo && S.fyMemo.sig === fySig()) opts.scroll = S.fyMemo.top;
  }
  S.view = view;
  if (opts.seriesId !== undefined) S.seriesId = opts.seriesId;
  // For You sorts by "Best match" by default; other views go back to the normal default
  if (view === "foryou" && !wasForYou) { S.sortBeforeForYou = S.sort; setSort("match"); }
  else if (view !== "foryou" && wasForYou) { setSort(S.sort === "match" ? (S.sortBeforeForYou && S.sortBeforeForYou !== "match" ? S.sortBeforeForYou : "title") : S.sort); }
  S.sel.clear(); S.anchor = null;
  for (const a of $$("#nav a")) a.classList.toggle("active", a.dataset.view === (view === "anime" ? "series" : view));
  render(false);
  if (opts.scroll) { scroller.scrollTop = opts.scroll; paint(); }
}

function goBack() {
  const h = S.history.pop();
  if (!h) return setView("series", { noHistory: true });
  setView(h.view, { seriesId: h.seriesId, noHistory: true, scroll: h.scroll });
}

function render(keepScroll = true) {
  const view = S.view;
  if (typeof updateGenreButton === "function" && $("#filterCount")) updateGenreButton();
  const showPage = view === "settings" || view === "setup";
  $("#page").classList.toggle("hidden", !showPage);
  scroller.classList.toggle("hidden", showPage);
  $("#filters").classList.toggle("hidden", showPage);
  $("#backBtn").classList.toggle("hidden", view !== "anime");
  $("#yearBar").classList.add("hidden");
  if (typeof renderTasteBar === "function") renderTasteBar();
  $("#typeSeg").classList.toggle("hidden", view === "op" || view === "ed");
  $("#viewHead").classList.toggle("hidden", view === "setup" || view === "anime");
  $("#viewActions").innerHTML = "";
  if (showPage) { $("#empty").classList.add("hidden"); $("#bulkbar").classList.add("hidden"); return view === "settings" ? renderSettings() : renderSetup(); }
  const prevScroll = scroller.scrollTop;
  if (view === "series") renderSeriesGrid();
  else if (view === "anime") renderAnimePage();
  else renderVideoGrid();
  scroller.scrollTop = keepScroll ? prevScroll : 0;
  paint();
  renderBulkbar();
}

function setHead(title, sub) { $("#viewTitle").textContent = title; $("#viewSub").innerHTML = sub || ""; }

function showEmpty(n, html) {
  const e = $("#empty");
  e.classList.toggle("hidden", n > 0);
  if (!n) e.innerHTML = html;
}

function matchPill(r) {
  const p = recScore(r);
  const tier = p >= 97 ? "hot" : p >= 85 ? "warm" : "";
  return `<span class="match ${tier}" title="Match with your taste">${Math.round(p)}%</span>`;
}

function forYouEmptyHTML() {
  if (S.fyTab === "liked") return `<div><div class="big">👍</div><h3>No liked songs yet</h3><div>Hover any video and press 👍 (or select it and press L). Favourites count as likes too.</div></div>`;
  if (S.fyTab === "hidden") return `<div><div class="big">👁</div><h3>Nothing hidden</h3><div>Songs you dislike (👎 or D) disappear from For You and land here. For You learns to avoid songs like them.</div></div>`;
  const t = S.recStatus || {};
  const prog = S.recProgress || {};
  if (prog.state === "running") {
    const pct = prog.total ? Math.round(100 * prog.done / prog.total) : null;
    return `<div class="fy-empty"><div class="big">✨</div><h3>${esc(prog.phase || "Learning your taste…")}</h3>
      <div>${pct != null ? `${prog.done.toLocaleString()} / ${prog.total.toLocaleString()} (${pct}%)` : esc(prog.current || "")}</div>
      <div class="hint">You can keep using Kanade while it works (it only pauses while a video is playing). Results appear here as they're ready.</div></div>`;
  }
  if (!t.engine_ready) {
    return `<div class="fy-empty"><div class="big">✨</div><h3>Recommendations from your taste</h3>
      <div>Kanade can listen to (and look at) openings you love and find the most similar ones in your library.</div>
      <ol><li>Pick folders of videos you like in <b>Settings ▸ For You</b></li><li>Install the For You engine (one-time download, uses your graphics card if available)</li><li>Kanade analyses everything in the background</li></ol>
      <button class="btn primary" data-fy="settings">${icon("gear")}Set up For You</button></div>`;
  }
  if (!(t.folders || []).length && !S.counts.favs) {
    return `<div class="fy-empty"><div class="big">♡</div><h3>Tell Kanade what you like</h3>
      <div>Add folders of openings/endings you love in Settings, or favourite a few videos.</div>
      <button class="btn primary" data-fy="settings">${icon("gear")}Choose folders</button></div>`;
  }
  if (prog.state === "error") return `<div class="fy-empty"><div class="big">!</div><h3>For You analysis stopped</h3><div>${esc(prog.error || "")}</div><button class="btn" data-fy="analyze">Try again</button></div>`;
  if (!S.rec.size) return `<div class="fy-empty"><div class="big">✨</div><h3>Getting ready…</h3><div>Analysis will start shortly.</div><button class="btn" data-fy="analyze">Start analysis now</button></div>`;
  return `<div><div class="big">∅</div><h3>No matches</h3><div>Try a different search or clear the filters.</div></div>`;
}

function emptyHTML() {
  if (S.view === "foryou") return forYouEmptyHTML();
  if (!S.videos.length && S.loadError) {
    return `<div><div class="big">⟳</div><h3>Can't reach Kanade's library service</h3><div>Retrying automatically… If this stays, close Kanade and open it again.<br><small style="opacity:.6">${esc(S.loadError)}</small></div></div>`;
  }
  if (!S.videos.length) {
    const p = S.boot?.status?.pipeline;
    const running = p && p.state === "running";
    return `<div><div class="big">🎬</div><h3>${running ? "Building your library…" : "Your library is empty"}</h3><div>${running ? esc(p.phase) : "Pick your archive folder in Settings, then scan."}</div></div>`;
  }
  if (S.view === "fav" && !S.q) return `<div><div class="big">♡</div><h3>No favorites yet</h3><div>Hover a video and click the heart, or select it and press F.</div></div>`;
  if (S.view === "review" && !S.q) return `<div><div class="big">✓</div><h3>Nothing to review</h3><div>Every video was identified confidently.</div></div>`;
  return `<div><div class="big">∅</div><h3>No matches</h3><div>Try a different search or clear the filters.</div></div>`;
}

function renderVideoGrid() {
  const flatItems = sortItems(applyFilters(baseItems()));
  const items = stackItems(flatItems);
  const stacked = items.length !== flatItems.length;
  const parts = [stacked ? `${items.length.toLocaleString()} song${items.length === 1 ? "" : "s"} <span class="muted2">(${flatItems.length.toLocaleString()} videos)</span>`
                         : `${items.length.toLocaleString()} video${items.length === 1 ? "" : "s"}`];
  if (S.q) parts.push(`matching “${esc(S.q)}”`);
  if (S.view === "foryou") {
    // every analysed video is ranked here, best first; the % says how well each one fits
    const prog = S.recProgress || {};
    const unit = stacked ? "song" : "video";
    parts.length = 0;
    if (S.fyTab === "rec") {
      const strong = items.reduce((n, v) => n + (recScore(S.rec.get(v.id)) >= 85 ? 1 : 0), 0);
      parts.push(`<b>${strong.toLocaleString()}</b> strong matches (85%+)`);
      parts.push(`${items.length.toLocaleString()} ${unit}s you haven't rated, best first${S.q ? ` · matching “${esc(S.q)}”` : ""}`);
      if (prog.state === "running" && prog.total) parts.push(`analysis continues: ${(prog.total - prog.done).toLocaleString()} to go`);
    } else if (S.fyTab === "liked") {
      parts.push(`${items.length.toLocaleString()} ${unit}${items.length === 1 ? "" : "s"} you liked, favourited or have in your liked folders · For You learns from them`);
    } else {
      parts.push(`${items.length.toLocaleString()} ${unit}${items.length === 1 ? "" : "s"} you disliked · hidden from For You, and For You learns to avoid songs like them`);
    }
    if (S.pendingRec) parts.push(`<button class="hlink inline" data-fyapply="1">${icon("spark")}New picks from your likes · Show</button>`);
    setHead(VIEWS[S.view].title, parts.join(" · "));
  }
  if (S.view !== "foryou") setHead(VIEWS[S.view].title, parts.join(" "));
  let groups;
  if (S.view === "year") {
    const byF = new Map();
    for (const v of items) { const f = v.folder || "Other"; if (!byF.has(f)) byF.set(f, []); byF.get(f).push(v); }
    const folders = [...byF.keys()].sort((a, b) => { const x = folderOrder(a), y = folderOrder(b); return x[0] - y[0] || (x[1] < y[1] ? -1 : x[1] > y[1] ? 1 : 0); });
    groups = [];
    for (const f of folders) {
      const vs = byF.get(f);
      const seasons = new Map();
      for (const v of vs) { const s = v.folder_season || ""; if (!seasons.has(s)) seasons.set(s, []); seasons.get(s).push(v); }
      const label = f === "misc" ? "Misc & Games" : f;
      if (!(seasons.size === 1 && seasons.has(""))) {
        const keys = [...seasons.keys()].sort((a, b) => (SEASON_ORDER[a] ?? 9) - (SEASON_ORDER[b] ?? 9));
        groups.push({ header: label, count: vs.length, items: [], anchor: f });
        for (const k of keys) groups.push({ header: k || "Other", sub: true, items: seasons.get(k) });
      } else groups.push({ header: label, items: vs, anchor: f });
    }
    renderYearBar(folders);
  } else if (S.view === "recent") {
    const byD = new Map();
    const arr = items.slice().sort((a, b) => (b.added || 0) - (a.added || 0) || collator.compare(a._t, b._t));
    for (const v of arr) { const d = new Date((v.added || 0) * 1000).toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" }); if (!byD.has(d)) byD.set(d, []); byD.get(d).push(v); }
    groups = [...byD].map(([d, vs]) => ({ header: "Added " + d, items: vs }));
  } else if (S.view === "foryou" && S.fyTab === "rec" && S.recGroup === -1 && S.sort === "match" && S.recGroups.length > 1) {
    groups = forYouSections(items);
  } else {
    groups = [{ items }];
  }
  if (S.view === "foryou") {
    const t = S.recStatus || {};
    const based = t.taste_count ? `Based on ${t.taste_count.toLocaleString()} liked video${t.taste_count === 1 ? "" : "s"}` : "";
    const nLiked = new Set(S.videos.filter(isLiked).map(songKey)).size;
    const nHidden = new Set(S.videos.filter((x) => x.rating === -1).map(songKey)).size;
    $("#viewActions").innerHTML = `<span class="fy-meta">${esc(based)}</span>
      ${S.fyTab === "rec" && S.rec.size ? `<button class="btn teach" data-fyteach="1" title="Rate your current top picks one by one (with sound): the fastest way to teach For You">${icon("spark")}Teach For You</button>` : ""}
      <div class="seg fy-tabs" title="Recommendations, or the songs you rated">
        <button data-fytab="rec" class="${S.fyTab === "rec" ? "on" : ""}">${icon("spark")}For you</button>
        <button data-fytab="liked" class="${S.fyTab === "liked" ? "on" : ""}">${icon("up")}Liked<b>${nLiked || ""}</b></button>
        <button data-fytab="hidden" class="${S.fyTab === "hidden" ? "on" : ""}">${icon("down")}Hidden<b>${nHidden || ""}</b></button></div>
      <div class="seg fy-mode" title="What “similar” means">
        <button data-mode-fy="audio" class="${S.recMode !== "mix" ? "on" : ""}" title="Only how the songs sound">${icon("ed")}Sound</button>
        <button data-mode-fy="mix" class="${S.recMode === "mix" ? "on" : ""}" title="Sound, the look of the video and the anime's genres">${icon("op")}Sound + look + genre</button></div>`;
    $("#viewActions").onclick = (e) => {
      if (e.target.closest("[data-fyteach]")) return openTeach();
      const t = e.target.closest("[data-fytab]");
      if (t) { S.fyTab = t.dataset.fytab; S.fyMemo = null; scroller.scrollTop = 0; render(false); return; }
      const b = e.target.closest("[data-mode-fy]"); if (!b) return;
      S.recMode = b.dataset.modeFy;
      try { localStorage.setItem("kanade.recmode2", S.recMode); } catch (err) { /* ignore */ }
      S.counts.foryou = forYouCount(); updateCounts();
      render(false);
    };
  }
  if (S.view === "review") {
    $("#viewActions").innerHTML = `<button class="btn" id="acceptAll">${icon("check")}Accept all suggestions</button><button class="btn" id="rematchReview">${icon("refresh")}Re-run matching</button>`;
    $("#acceptAll").onclick = async () => {
      const ids = items.filter((v) => v.status === "review").map((v) => v.id);
      if (!ids.length) return;
      if (!confirm(`Accept the suggested anime for ${ids.length} video(s)? Videos without a suggestion stay in review. You can still edit them afterwards.`)) return;
      try { const r = await api.post("/api/accept", { ids }); toast(`Accepted ${r.accepted} suggestion(s)`); loadData(); } catch (e) { toast(e.message, true); }
    };
    $("#rematchReview").onclick = async () => { await api.post("/api/rematch", { ids: items.map((v) => v.id) }); toast("Matching queued"); };
  }
  buildRows(groups, "cards");
  showEmpty(items.length, emptyHTML());
}
$("#viewSub").addEventListener("click", (e) => { if (e.target.closest("[data-fyapply]")) applyPendingRecs(); });

function renderYearBar(folders) {
  const yb = $("#yearBar");
  yb.classList.remove("hidden");
  yb.innerHTML = folders.map((f) => `<button data-jump="${esc(f)}">${esc(f === "misc" ? "Misc" : f)}</button>`).join("");
}
function updateYearBarActive() {
  if (S.view !== "year") return;
  const top = scroller.scrollTop + 10;
  let cur = null;
  for (let i = 0; i < V.rows.length; i++) { const r = V.rows[i]; if (r.anchor) { if (V.offsets[i] <= top) cur = r.anchor; else break; } }
  for (const b of $$("#yearBar button")) b.classList.toggle("on", b.dataset.jump === cur);
}
$("#yearBar").addEventListener("click", (e) => {
  const b = e.target.closest("[data-jump]"); if (!b) return;
  const i = V.rows.findIndex((r) => r.anchor === b.dataset.jump);
  if (i >= 0) scroller.scrollTo({ top: V.offsets[i], behavior: "smooth" });
});

function renderSeriesGrid() {
  let list = S.anime.slice();
  if (S.searchIds) { const ids = new Set(); for (const id of S.searchIds) { const v = S.byId.get(id); if (v) ids.add(v.anime_id); } list = list.filter((a) => ids.has(a.id)); }
  const f = S.filters;
  if (f.year) list = list.filter((a) => String(a.year) === f.year || (a.folders || "").split(",").includes(f.year));
  if (f.fav) list = list.filter((a) => a.favs);
  if (f.type) list = list.filter((a) => (f.type === "OP" ? a.ops : a.eds));
  if (f.series) list = list.filter((a) => a.title === f.series);
  const aset = animeFacetSet();
  if (aset) list = list.filter((a) => aset.has(a.id));
  const s = S.sort;
  if (s === "year_desc") list.sort((a, b) => (b.year || 0) - (a.year || 0) || collator.compare(a.title, b.title));
  else if (s === "year_asc") list.sort((a, b) => (a.year || 9999) - (b.year || 9999) || collator.compare(a.title, b.title));
  else if (s === "added") list.sort((a, b) => (b.added || 0) - (a.added || 0));
  else if (s === "number") list.sort((a, b) => (b.videos || 0) - (a.videos || 0) || collator.compare(a.title, b.title));
  else list.sort((a, b) => collator.compare(a.title, b.title));
  setHead("Anime Series", `${list.length.toLocaleString()} series${S.q ? ` matching “${esc(S.q)}”` : ""}`);
  buildRows([{ items: list }], "posters");
  showEmpty(list.length, emptyHTML());
}

/* Series details (links, genres, related series) are cached per library version, and the neighbours in the
   series order are fetched ahead, so flipping between seasons is instant and the page doesn't jump. */
const HERO_H = 330, REL_H = 168;
const animeDetails = new Map();
let animeDetailsVersion = null;
function fetchAnimeDetail(id) {
  if (animeDetailsVersion !== S.version) { animeDetails.clear(); animeDetailsVersion = S.version; }
  let p = animeDetails.get(id);
  if (!p) {
    p = api.get("/api/anime/" + id).then((d) => { p.value = d; return d; });
    p.catch(() => animeDetails.delete(id));
    animeDetails.set(id, p);
  }
  return p;
}
function cachedAnimeDetail(id) {
  if (animeDetailsVersion !== S.version) return null;
  const p = animeDetails.get(id);
  return p && p.value ? p.value : null;
}
const hasRelated = (d) => !!(d && ((d.franchise && d.franchise.length > 1) || (d.related_missing && d.related_missing.length)));

function renderAnimePage() {
  const id = S.seriesId;
  const a = S.animeById.get(id);
  const items = applyFilters(baseItems()).sort(cmpTheme);
  const ops = items.filter((v) => v.type === "OP"), eds = items.filter((v) => v.type !== "OP");
  const d = cachedAnimeDetail(id);
  const rel = hasRelated(d);
  const hero = { h: HERO_H + (rel ? REL_H : 0), html: heroHTML(a, ops.length, eds.length, d) + (rel ? relatedHTML(d) : "") };
  const groups = [];
  if (ops.length) groups.push({ header: "Openings", items: ops });
  if (eds.length) groups.push({ header: "Endings", items: eds });
  buildRows(groups, "cards", hero);
  $("#empty").classList.add("hidden");
  if (d) { requestAnimationFrame(centerRelated); prefetchNeighbours(d); return; }
  // not loaded yet (rare: details are fetched on press): draw again once they arrive, at the same scroll position
  fetchAnimeDetail(id).then(() => {
    if (S.view !== "anime" || S.seriesId !== id) return;
    const top = scroller.scrollTop; renderAnimePage(); scroller.scrollTop = top; paint();
  }).catch(() => {});
}

function prefetchNeighbours(d) {
  for (const n of [d.prev, d.next]) if (n) fetchAnimeDetail(n.id).catch(() => {});
}

function heroExtrasHTML(d) {
  const links = [];
  if (d.mal_id) links.push(`<a href="#" data-ext="https://myanimelist.net/anime/${d.mal_id}">MyAnimeList</a>`);
  if (d.anilist_id) links.push(`<a href="#" data-ext="https://anilist.co/anime/${d.anilist_id}">AniList</a>`);
  if (d.at_slug) links.push(`<a href="#" data-ext="https://animethemes.moe/anime/${encodeURIComponent(d.at_slug)}">AnimeThemes</a>`);
  return {
    pills: (d.synonyms.length ? `<span class="pill" title="${esc(d.synonyms.join(" · "))}">aka ${esc(d.synonyms.slice(0, 2).join(" · "))}</span>` : "") +
      links.map((l) => `<span class="pill">${icon("link")}${l}</span>`).join(""),
    genres: genrePills(d.genres, d.tags, 8),
  };
}

function relatedHTML(d) {
  const list = d.franchise || [];
  const inLib = list.length;
  const meta = (x) => [fmtFormat(x.format), x.year].filter(Boolean).join(" · ");
  const items = list.map((x) => {
    const cover = x.has_cover ? `<img src="${tok("/cover/" + x.id)}" alt="" loading="lazy">` : "";
    return `<button class="rel-item${x.current ? " cur" : ""}" data-goanime="${x.id}" title="${esc(x.title)}${x.title_english && x.title_english !== x.title ? "\n" + esc(x.title_english) : ""}">
      <span class="rc">${cover}</span>
      <span class="rt"><span class="rk">${x.current ? "You are here" : esc(x.rel || "Same franchise")}</span>
        <span class="rn">${esc(x.title)}</span><span class="rm">${esc(meta(x))}</span></span></button>`;
  }).join("");
  const missing = (d.related_missing || []).map((m) => `<a href="#" class="rel-item ghost" data-ext="https://anilist.co/anime/${m.al}" title="${esc(m.title || "")} — not in your library (opens AniList)">
      <span class="rc">${icon("link")}</span>
      <span class="rt"><span class="rk">${esc(m.rel)} · not in library</span><span class="rn">${esc(m.title || "Unknown")}</span><span class="rm">${esc(meta(m))}</span></span></a>`).join("");
  const nav = (n, dir) => n ? `<button class="btn sm rel-go" data-goanime="${n.id}" title="${esc(n.title)}">${dir < 0 ? "‹ " : ""}${esc(n.label)}${dir > 0 ? " ›" : ""}</button>`
    : `<button class="btn sm rel-go" disabled>${dir < 0 ? "‹ Prequel" : "Sequel ›"}</button>`;
  return `<div class="rel" style="top:${HERO_H}px;height:${REL_H}px">
    <div class="rel-head"><span class="rel-title">${inLib > 1 ? "Series order" : "Related"}</span>
      <span class="rel-sub">${inLib > 1 ? `${inLib} in your library, by release date` : "not in your library"}</span>
      ${inLib > 1 ? `<span class="rel-nav">${nav(d.prev, -1)}${nav(d.next, 1)}</span>` : ""}</div>
    <div class="rel-strip">${items}${missing}</div></div>`;
}

function centerRelated() {
  const strip = canvas.querySelector(".rel-strip"), cur = strip && strip.querySelector(".rel-item.cur");
  if (!strip || !cur) return;
  strip.scrollLeft = Math.max(0, cur.offsetLeft - (strip.clientWidth - cur.offsetWidth) / 2);
}

function heroHTML(a, nops, neds, d = null) {
  const x = d ? heroExtrasHTML(d) : { pills: "", genres: "" };
  if (!a) return `<div class="hero"><div class="info"><h2>Unknown series</h2></div></div>`;
  const cover = a.has_cover ? tok("/cover/" + a.id) : "";
  const eyebrow = [fmtFormat(a.format), [a.season, a.year].filter(Boolean).join(" ")].filter(Boolean).join(" · ");
  return `<div class="hero">${cover ? `<div class="hero-bg" style="background-image:url('${cover}')"></div>` : ""}
    <div class="cover">${cover ? `<img src="${cover}" alt="">` : ""}</div>
    <div class="info">
      <div class="eyebrow">${esc(eyebrow || "Anime")}</div>
      <h2>${esc(a.title)}</h2>
      ${a.title_english && a.title_english !== a.title ? `<div class="alt">${esc(a.title_english)}</div>` : ""}
      ${a.title_japanese ? `<div class="jp">${esc(a.title_japanese)}</div>` : ""}
      <div class="facts"><span class="pill op">${nops} Opening${nops === 1 ? "" : "s"}</span><span class="pill ed">${neds} Ending${neds === 1 ? "" : "s"}</span>
        ${a.folders ? `<span class="pill">${icon("folder")}${esc(a.folders.split(",").join(", "))}</span>` : ""}<span id="heroExtra" style="display:contents">${x.pills}</span></div>
      <div class="genres" id="heroGenres">${x.genres}</div>
      <div class="actions"><button class="btn primary" data-act="play-first">${icon("play")}Play first</button>
        <button class="btn" data-act="edit-series">${icon("edit")}Edit series</button>
        <button class="btn" data-act="fav-series">${icon("heart")}Favorite all</button></div>
    </div></div>`;
}

/* ------------------------------------------------------------------ selection, details */
function cardEl(id) { return canvas.querySelector(`.card[data-id="${id}"]`); }
function refreshCard(id) {
  let el = cardEl(id);
  const v = S.byId.get(id);
  if (!el && v) {                                  // a version inside a stack: refresh the stack's card
    for (const m of songVideos(v)) { el = cardEl(m.id); if (el) break; }
  }
  if (!el) return;
  const idx = Number(el.dataset.idx);
  const item = S.flat[idx] && S.flat[idx].id === Number(el.dataset.id) ? S.flat[idx] : S.byId.get(Number(el.dataset.id));
  if (!item) return;
  const tmp = document.createElement("div"); tmp.innerHTML = cardHTML(item, idx);
  el.replaceWith(tmp.firstElementChild);
}
function refreshSelectionClasses() {
  for (const el of $$(".card", canvas)) {
    const id = Number(el.dataset.id);
    el.classList.toggle("sel", S.focus === id);
    el.classList.toggle("multi", S.sel.size > 1 && S.sel.has(id));
  }
  renderBulkbar();
}

function select(id, ev) {
  const idx = S.flat.findIndex((v) => v.id === id);
  if (ev && (ev.ctrlKey || ev.metaKey)) {
    if (S.sel.size === 0 && S.focus != null) S.sel.add(S.focus);
    if (S.sel.has(id)) S.sel.delete(id); else S.sel.add(id);
  } else if (ev && ev.shiftKey && S.anchor != null) {
    const a = S.flat.findIndex((v) => v.id === S.anchor);
    const [lo, hi] = a < idx ? [a, idx] : [idx, a];
    S.sel = new Set(S.flat.slice(lo, hi + 1).map((v) => v.id));
  } else {
    S.sel = new Set([id]);
  }
  if (!(ev && ev.shiftKey)) S.anchor = id;
  S.focus = id;
  refreshSelectionClasses();
  openDetails(id);
}

/**
 * Re-layout the grid with a smooth "glide": every visible card animates from its old position/size to its
 * new one (FLIP), new cards fade in, and the anchor card (selected, or first visible) stays put on screen.
 */
function animateLayout(change, anchorId = null) {
  if (!isGridView()) { change(); return; }
  const key = (el) => el.dataset.id ? "v" + el.dataset.id : "a" + el.dataset.anime;
  const first = new Map();
  for (const el of canvas.querySelectorAll(".card,.poster")) first.set(key(el), el.getBoundingClientRect());
  const scTop = scroller.getBoundingClientRect().top;
  let anchorKey = anchorId != null && first.has("v" + anchorId) ? "v" + anchorId : null;
  if (!anchorKey) {
    for (const [k, r] of first) if (r.bottom > scTop + 4) { anchorKey = k; break; }
  }
  const anchorRel = anchorKey ? first.get(anchorKey).top - scTop : null;

  change();
  lastW = canvas.clientWidth;          // the ResizeObserver must not re-render a second time
  relayout();
  if (anchorKey) {
    const flatIdx = anchorKey[0] === "v" ? S.flat.findIndex((x) => x.id === Number(anchorKey.slice(1)))
                                         : S.flat.findIndex((x) => x.id === Number(anchorKey.slice(1)));
    const row = V.rows.findIndex((r) => r.items && flatIdx >= r.start && flatIdx < r.start + r.items.length);
    if (row >= 0) {
      let top = Math.max(0, V.offsets[row] - anchorRel);
      const bottom = V.offsets[row] + V.rows[row].h;
      if (anchorId != null && bottom > top + scroller.clientHeight) top = bottom - scroller.clientHeight + 10;
      if (anchorId != null && V.offsets[row] < top) top = Math.max(0, V.offsets[row] - 10);
      scroller.scrollTop = top;
      paint();
    }
  }
  const vh = scroller.clientHeight;
  for (const el of canvas.querySelectorAll(".card,.poster")) {
    const f = first.get(key(el));
    const l = el.getBoundingClientRect();
    if (!f || Math.abs(f.top - l.top) > vh) {
      el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 200, easing: "ease-out" });
      continue;
    }
    const dx = f.left - l.left, dy = f.top - l.top, sx = f.width / l.width, sy = f.height / l.height;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1 && Math.abs(sx - 1) < 0.01) continue;
    el.animate([
      { transformOrigin: "0 0", transform: `translate(${dx}px, ${dy}px) scale(${sx}, ${sy})` },
      { transformOrigin: "0 0", transform: "none" },
    ], { duration: 240, easing: "cubic-bezier(.2, .7, .2, 1)" });
  }
}

function ensureVisible(idx) {
  const i = V.rows.findIndex((r) => r.type === "cards" && idx >= r.start && idx < r.start + r.items.length);
  if (i < 0) return;
  const top = V.offsets[i], bottom = top + V.rows[i].h;
  if (top < scroller.scrollTop) scroller.scrollTop = top - 10;
  else if (bottom > scroller.scrollTop + scroller.clientHeight) scroller.scrollTop = bottom - scroller.clientHeight + 10;
  paint();
}

let detailsReq = 0;
async function openDetails(id, prefetched = null) {
  const d = $("#details");
  const my = ++detailsReq;
  let v;
  try { v = (prefetched && await prefetched) || await api.get("/api/video/" + id); } catch (e) { return toast(e.message, true); }
  if (my !== detailsReq) return;
  const old = d.querySelector("video"); if (old) { old.pause(); old.removeAttribute("src"); old.load(); }
  d.innerHTML = detailsHTML(v);
  const wasOpen = d.classList.contains("open");
  d.classList.add("open"); d.setAttribute("aria-hidden", "false");
  if (!wasOpen && !$("#app").classList.contains("with-details")) {
    animateLayout(() => $("#app").classList.add("with-details"), S.focus);
  }
}
function closeDetails() {
  const d = $("#details");
  d.classList.remove("open"); d.setAttribute("aria-hidden", "true");
  const v = d.querySelector("video"); if (v) { v.pause(); v.removeAttribute("src"); v.load(); }
  const anchor = S.focus;
  S.focus = null;
  if ($("#app").classList.contains("with-details")) animateLayout(() => $("#app").classList.remove("with-details"), anchor);
  refreshSelectionClasses();
}

function genrePills(genres, tags, maxTags) {
  const g = (genres || []).map((x) => `<button class="gpill" data-genre="${esc(x)}">${esc(x)}</button>`);
  const t = (tags || []).slice(0, maxTags).map((x) => `<button class="gpill tag" data-genre="${esc("tag:" + x)}">${esc(x)}</button>`);
  return g.concat(t).join("");
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-genre]");
  if (b) { e.preventDefault(); e.stopPropagation(); setGenre(b.dataset.genre); }
}, true);

function detailsHTML(v) {
  const lv = S.byId.get(v.id) || v;
  const rs = rateState(songVideos(lv));
  const others = songVideos(lv).slice().sort((a, b) => (a.version || 1) - (b.version || 1) || versionQuality(a, b));
  const conf = Math.round((v.confidence || 0) * 100);
  const res = v.width ? `${v.width}×${v.height}` : "—";
  const sugg = v.suggestion && v.suggestion.candidate ? v.suggestion : null;
  const thumb = v.thumb_state === 1 ? `<img src="${tok("/thumb/" + v.id + "?v=" + (v.media_at || 0))}" alt="">` : (v.cover_path ? `<img src="${tok("/cover/" + v.anime_id)}" alt="" style="filter:blur(14px) brightness(.6)">` : "");
  return `<div class="d-media" id="dMedia">${thumb}
      <button class="playbig" data-act="play" title="Play in VLC (Enter)">${icon("play")}</button>
      <button class="close" data-act="close" title="Close (Esc)">${icon("x")}</button></div>
    <div class="d-body">
      <div class="d-badges"><span class="badge ${v.type || ""}">${esc(themeLabel(v))}${v.version > 1 ? `<small>v${v.version}</small>` : ""}</span>
        ${v.status === "review" ? `<span class="pill" style="color:var(--warn)">Needs review</span>` : ""}
        ${v.has_override ? `<span class="pill">${icon("edit")}Corrected</span>` : ""}
        ${v.duplicate_of ? `<span class="pill" style="color:var(--warn)">Duplicate</span>` : ""}
        ${v.p_tags ? `<span class="pill">${esc(v.p_tags)}</span>` : ""}</div>
      <div class="d-title" data-act="series" title="Open series">${esc(v.title)}</div>
      ${v.title_english && v.title_english !== v.title ? `<div class="d-alt">${esc(v.title_english)}</div>` : ""}
      ${v.title_japanese ? `<div class="d-jp">${esc(v.title_japanese)}</div>` : ""}
      <div class="d-song"><div class="s">${esc(v.song_title || "Unknown song")}</div><div class="a">${esc(v.artists || "Unknown artist")}</div></div>
      <div class="d-actions">
        <button class="btn primary" data-act="play">${icon("play")}Play in VLC</button>
        ${v.anime_id ? `<button class="btn" data-act="series" title="All openings and endings of this series, its seasons and related series">${icon("series")}Open series</button>` : ""}
        <button class="btn" data-act="fav">${icon("heart")}${v.fav ? "Unfavorite" : "Favorite"}</button>
        <button class="btn${rs.liked ? " on-up" : ""}" data-act="like" title="${rs.liked ? "Remove like" : "More songs like this in For You (L)"}">${icon("up")}${rs.liked ? "Liked" : "Like"}</button>
        <button class="btn${rs.disliked ? " on-down" : ""}" data-act="dislike" title="${rs.disliked ? "Show it in For You again" : "Hide from For You and learn to avoid songs like this (D)"}">${icon("down")}${rs.disliked ? "Disliked" : "Dislike"}</button>
        <button class="btn" data-act="reveal">${icon("folder")}Show in folder</button>
        <button class="btn" data-act="edit">${icon("edit")}Edit metadata</button>
        <button class="btn" data-act="preview">${icon("op")}Preview here</button>
      </div>
      ${sugg && v.status === "review" ? `<div class="sugg"><b>Suggested match</b> (${Math.round(sugg.confidence * 100)}% via ${esc(sourceLabel(sugg.source))})<br>${esc(sugg.candidate.title_romaji)}${sugg.candidate.year ? ` (${sugg.candidate.year})` : ""}
        <div class="row-actions" style="margin-top:10px"><button class="btn sm primary" data-act="accept">${icon("check")}Accept</button><button class="btn sm" data-act="find">Find another…</button></div></div>` : ""}
      ${others.length > 1 ? `<div class="d-sec"><h4>Versions of this song (${others.length})</h4><div class="dvers">${others.map((o) => `
        <div class="dver${o.id === v.id ? " cur" : ""}" data-dver="${o.id}" title="${o.id === v.id ? "Shown now" : "Show this version"}">
          <div class="dvt">${o.thumb === 1 ? `<img src="${tok("/thumb/" + o.id + "?v=" + (o.tv || 0))}" alt="">` : ""}</div>
          <div class="dvl"><b>${esc(themeLabel(o))}${o.version > 1 ? ` v${o.version}` : ""}</b><span>${esc([resLabel(o.height), tagLabel(o.tags), fmtDur(o.duration)].filter(Boolean).join(" · "))}</span></div>
          <button class="icon-btn" data-dplay="${o.id}" title="Play in VLC">${icon("play")}</button></div>`).join("")}</div></div>` : ""}
      <div class="d-sec"><h4>Theme</h4><dl class="kv">
        <dt>Type</dt><dd>${v.type === "ED" ? "Ending" : "Opening"} ${v.number || ""}${v.part || ""}${v.version > 1 ? `, version ${v.version}` : ""}</dd>
        ${v.episodes ? `<dt>Episodes</dt><dd>${esc(v.episodes)}</dd>` : ""}
        <dt>Year</dt><dd>${esc(v.year || "—")}${v.season ? " · " + esc(v.season) : ""}</dd>
        ${v.synonyms && v.synonyms.length ? `<dt>Also known as</dt><dd>${esc(v.synonyms.join(" · "))}</dd>` : ""}
        ${(v.content_reasons && v.content_reasons.length) || v.nsfw ? `<dt>Content</dt><dd>${v.explicit ? "<b>18+</b> · " : v.suggestive ? "<b>Ecchi / suggestive</b> · " : ""}${v.nsfw ? "this video is marked NSFW · " : ""}${esc((v.content_reasons || []).join(" · "))}</dd>` : ""}
      </dl>${(v.genres && v.genres.length) || (v.tags && v.tags.length) ? `<div class="genres" style="margin-top:10px">${genrePills(v.genres, v.tags, 6)}</div>` : ""}</div>
      <div class="d-sec"><h4>File</h4><dl class="kv">
        <dt>Filename</dt><dd class="path">${esc(v.filename)}</dd>
        <dt>Path</dt><dd class="path">${esc(v.path)}</dd>
        <dt>Folder</dt><dd>${esc([v.folder, v.folder_season].filter(Boolean).join(" / "))}</dd>
        <dt>Duration</dt><dd>${fmtDur(v.duration) || "—"}</dd>
        <dt>Resolution</dt><dd>${res}</dd>
        <dt>Size</dt><dd>${v.size ? fmtSize(v.size) : "—"}</dd>
        <dt>Added</dt><dd>${fmtDate(v.date_added)}</dd>
        ${v.exists ? "" : `<dt>Status</dt><dd style="color:var(--danger)">File missing on disk</dd>`}
        ${v.duplicate_path ? `<dt>Duplicate of</dt><dd class="path">${esc(v.duplicate_path)}</dd>` : ""}
        ${v.duplicates && v.duplicates.length ? `<dt>Copies</dt><dd class="path">${v.duplicates.map(esc).join("<br>")}</dd>` : ""}
      </dl></div>
      ${S.rec.has(v.id) ? (() => { const r = S.rec.get(v.id); return `<div class="d-sec"><h4>For You</h4><dl class="kv">
        <dt>Match</dt><dd><b>${Math.round(r.a)}%</b> by sound · <b>${Math.round(r.m)}%</b> by sound + look + genre</dd>
        ${r.ga && S.recGroups.length > 1 ? `<dt>Song type</dt><dd>${(() => { const i = bestGroup(r); return i >= 0 ? esc(S.recGroups[i]?.name || "") : "—"; })()}</dd>` : ""}
        ${r.wa ? `<dt>Sounds like</dt><dd>${esc(r.wa)}</dd>` : ""}${r.wv ? `<dt>Looks like</dt><dd>${esc(r.wv)}</dd>` : ""}</dl></div>`; })() : ""}
      <div class="d-sec"><h4>Identification</h4><dl class="kv">
        <dt>Source</dt><dd>${esc(sourceLabel(v.match_source))}</dd>
        <dt>Confidence</dt><dd><div class="conf ${conf < 85 ? "low" : ""}"><div class="bar"><i style="width:${conf}%"></i></div><span>${conf}%</span></div></dd>
        ${v.mal_id ? `<dt>MyAnimeList</dt><dd><a href="#" data-ext="https://myanimelist.net/anime/${v.mal_id}" style="color:var(--accent)">#${v.mal_id}</a></dd>` : ""}
        ${v.anilist_id ? `<dt>AniList</dt><dd><a href="#" data-ext="https://anilist.co/anime/${v.anilist_id}" style="color:var(--accent)">#${v.anilist_id}</a></dd>` : ""}
      </dl></div>
    </div>`;
}

$("#details").addEventListener("click", async (e) => {
  const ext = e.target.closest("[data-ext]");
  if (ext) { e.preventDefault(); return openExternal(ext.dataset.ext); }
  const dp = e.target.closest("[data-dplay]");
  if (dp) { e.stopPropagation(); return play(Number(dp.dataset.dplay)); }
  const dv = e.target.closest("[data-dver]");
  if (dv) { const nid = Number(dv.dataset.dver); if (nid !== S.focus) { S.focus = nid; refreshSelectionClasses(); openDetails(nid); } return; }
  const b = e.target.closest("[data-act]"); if (!b || S.focus == null) return;
  const id = S.focus, v = S.byId.get(id);
  try {
    switch (b.dataset.act) {
      case "play": return play(id);
      case "close": return closeDetails();
      case "fav": await toggleFav([id], !(v && v.fav)); return openDetails(id);
      case "reveal": return reveal(id);
      case "edit": return openEditor([id]);
      case "find": return openEditor([id], { tab: "find" });
      case "series": if (v && v.anime_id) setView("anime", { seriesId: v.anime_id }); return;
      case "like": return rateSongs([id], nextRating(id, 1));
      case "dislike": return rateSongs([id], nextRating(id, -1));
      case "accept": await api.post("/api/accept", { ids: [id] }); toast("Suggestion accepted"); await loadData(); return openDetails(id);
      case "preview": {
        const m = $("#dMedia");
        m.innerHTML = `<video src="${tok("/media/" + id)}" controls autoplay></video><button class="close" data-act="close" title="Close">${icon("x")}</button>`;
        return;
      }
    }
  } catch (err) { toast(err.message, true); }
});

/* ------------------------------------------------------------------ likes / dislikes */
/** Rate whole songs (all their versions). value: 1 like, -1 dislike (hidden from For You), 0 remove. */
async function rateSongs(cardIds, value, opts = {}) {
  const ids = [...new Set(cardIds.flatMap((id) => songVideos(S.byId.get(id) || { id }).map((x) => x.id)))];
  const before = ids.map((id) => [id, S.byId.get(id)?.rating || 0]);
  const hideNow = S.view === "foryou" && ((S.fyTab === "rec" && value !== 0) || (S.fyTab === "liked" && value !== 1) ||
                                          (S.fyTab === "hidden" && value !== -1));
  const apply = () => { for (const id of ids) { const v = S.byId.get(id); if (v) v.rating = value; } };
  if (hideNow) {
    animateLayout(() => { apply(); render(true); });
  } else {
    apply();
    for (const id of cardIds) refreshCard(id);
  }
  S.counts.foryou = forYouCount(); updateCounts();
  if (S.focus != null && ids.includes(S.focus) && $("#details").classList.contains("open")) openDetails(S.focus);
  S.ratedSinceLearn++;
  try { await api.post("/api/rate", { ids, value }); }
  catch (e) { toast("Could not save: " + e.message, true); return loadData(); }
  if (opts.quiet) return;
  const n = cardIds.length;
  const what = n > 1 ? `${n} songs` : "Song";
  const msg = value === 1 ? `${what} liked · For You learns from it` : value === -1 ? `${what} hidden from For You · it learns to avoid songs like ${n > 1 ? "these" : "this"}`
                          : `${what}: rating removed`;
  toastAction(msg, "Undo", async () => {
    const restore = new Map(before);
    animateLayout(() => { for (const [id, r] of restore) { const v = S.byId.get(id); if (v) v.rating = r; } render(true); });
    S.counts.foryou = forYouCount(); updateCounts();
    for (const val of [1, -1, 0]) {
      const grp = before.filter(([, r]) => r === val).map(([id]) => id);
      if (grp.length) await api.post("/api/rate", { ids: grp, value: val }).catch(() => {});
    }
  });
}
function toastAction(msg, label, fn) {
  const el = document.createElement("div");
  el.className = "toast withact";
  el.innerHTML = `<span>${esc(msg)}</span><button>${esc(label)}</button>`;
  el.querySelector("button").onclick = () => { el.remove(); fn(); };
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.transition = "opacity .3s"; el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, 5200);
}
/** The rating a click on a card's like/dislike button sets (clicking an active one removes it). */
function nextRating(cardId, value) {
  const st = rateState(songVideos(S.byId.get(cardId)));
  if (value === 1) return st.liked ? 0 : 1;
  if (value === -1) return st.disliked ? 0 : -1;
  return 0;
}

/* ------------------------------------------------------------------ Teach For You */
/** Rate the current top picks one at a time. Top picks the user doesn't like are exactly where the model is
    wrong, so these ratings teach it the most. One song per series; plays the best version with sound. */
function openTeach() {
  const rated = ratedSongs(), seenSeries = new Set(), queue = [];
  const ranked = S.videos.filter((v) => S.rec.has(v.id) && !rated.has(songKey(v)))
    .sort((a, b) => recScore(S.rec.get(b.id)) - recScore(S.rec.get(a.id)));
  const seenSong = new Set();
  for (const v of ranked) {
    const k = songKey(v);
    const f = franchiseKey(v);
    if (seenSong.has(k) || seenSeries.has(f)) continue;
    seenSong.add(k); seenSeries.add(f);
    queue.push(songVideos(v).slice().sort(versionQuality)[0]);
    if (queue.length >= 15) break;
  }
  if (!queue.length) return toast("Nothing left to rate here");
  const T = { queue, i: 0, rated: 0 };
  const m = $("#modal");
  const show = () => {
    const v = T.queue[T.i];
    if (!v) return finish();
    const r = S.rec.get(v.id);
    const old = m.querySelector("video"); if (old) { old.pause(); old.removeAttribute("src"); old.load(); }
    m.innerHTML = `<div class="modal-head"><div><h3>Teach For You</h3><div class="sub">${T.i + 1} of ${T.queue.length} · your current top picks · 👍 more like this · 👎 not for me</div></div>
        <button class="icon-btn" data-tclose title="Done (Esc)">${icon("x")}</button></div>
      <div class="modal-body teach-body">
        <div class="teach-video"><video src="${tok("/media/" + v.id)}#t=${(v.duration ? v.duration * 0.25 : 0).toFixed(1)}" autoplay playsinline controls></video>
          ${r ? matchPill(r) : ""}</div>
        <div class="teach-meta"><div class="t1">${esc(v.title)} <span class="badge ${v.type || ""}">${esc(themeLabel(v))}</span></div>
          <div class="t2">${esc(v.song || "Unknown song")}${v.artists ? ` – ${esc(v.artists)}` : ""}</div>
          <div class="t3">${esc([v.year, v.folder_season || v.season].filter(Boolean).join(" · "))}${r && r.wa ? ` · sounds like ${esc(r.wa)}` : ""}</div></div>
        <div class="teach-btns">
          <button class="btn tdown" data-trate="-1">${icon("down")}Not for me <kbd>D</kbd></button>
          <button class="btn" data-trate="0">Skip <kbd>S</kbd></button>
          <button class="btn tup" data-trate="1">${icon("up")}More like this <kbd>L</kbd></button></div></div>`;
    const vid = m.querySelector("video");
    vid.volume = 0.7;
    vid.play().catch(() => { vid.muted = true; vid.play().catch(() => {}); });
  };
  const rate = (val) => {
    const v = T.queue[T.i]; if (!v) return;
    if (val) { T.rated++; rateSongs([v.id], val, { quiet: true }); }
    T.i++;
    show();
  };
  const finish = () => {
    document.removeEventListener("keydown", keys, true);
    closeModal();
    if (T.rated) {
      api.post("/api/foryou/learn").catch(() => {});
      S.ratedSinceLearn = 0;
      toast(`Learning from your ${T.rated} rating${T.rated === 1 ? "" : "s"}… new picks appear in a few seconds (“Show” in the header)`);
    }
  };
  const keys = (e) => {
    if ($("#modalWrap").classList.contains("hidden")) { document.removeEventListener("keydown", keys, true); return; }
    const k = e.key.toLowerCase();
    if (k === "l" || e.key === "ArrowRight") { e.preventDefault(); e.stopPropagation(); rate(1); }
    else if (k === "d" || e.key === "ArrowLeft") { e.preventDefault(); e.stopPropagation(); rate(-1); }
    else if (k === "s" || e.key === "ArrowDown") { e.preventDefault(); e.stopPropagation(); rate(0); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(); }
  };
  document.addEventListener("keydown", keys, true);
  m.onclick = (e) => {
    if (e.target.closest("[data-tclose]")) return finish();
    const b = e.target.closest("[data-trate]"); if (b) rate(Number(b.dataset.trate));
  };
  $("#modalWrap").classList.remove("hidden");
  m.classList.add("teach");
  show();
}

/* ------------------------------------------------------------------ versions pop-up */
function versionTileHTML(v) {
  const res = resLabel(v.height);
  const tagText = tagLabel(v.tags);
  return `<div class="vtile" data-vid="${v.id}" title="Double-click to play in VLC · ⓘ for details">
    <div class="vthumb">${v.thumb === 1 ? `<img src="${tok("/thumb/" + v.id + "?v=" + (v.tv || 0))}" alt="" decoding="async">` : ""}
      <span class="badge ${v.type || ""}">${esc(themeLabel(v))}${v.version > 1 ? `<small>v${v.version}</small>` : ""}</span>
      ${res ? `<span class="res r${res.replace(/\D/g, "") || "4"}">${res}</span>` : ""}
      <span class="vplay">${icon("play")}</span>
      ${v.duration ? `<span class="dur">${fmtDur(v.duration)}</span>` : ""}</div>
    <div class="vmeta"><span>${esc([v.version > 1 ? `Version ${v.version}` : "Version 1", v.width ? `${v.width}×${v.height}` : "", tagText].filter(Boolean).join(" · "))}</span>
      <span class="vbtns"><button class="act fav${v.fav ? " on" : ""}" data-vfav="${v.id}" title="Favorite">${icon("heart")}</button>
        <button class="act" data-vinfo="${v.id}" title="Details">${icon("info")}</button></span></div></div>`;
}
function openVersions(cardId) {
  const card = S.byId.get(cardId); if (!card) return;
  const vs = songVideos(card).slice().sort((a, b) => (a.version || 1) - (b.version || 1) || versionQuality(a, b));
  const song = card.song ? `${card.song}${card.artists ? ` – ${card.artists}` : ""}` : "";
  openModal(`<div class="modal-head"><div><h3>${esc(card.title)} · ${esc(themeLabel(card))}</h3>
      <div class="sub">${esc(song)}${song ? " · " : ""}${vs.length} versions</div></div>
      <button class="icon-btn" data-vclose title="Close (Esc)">${icon("x")}</button></div>
    <div class="modal-body"><div class="vgrid">${vs.map(versionTileHTML).join("")}</div></div>`);
  $("#modal").classList.add("versions");
  const m = $("#modal");
  let clickTimer = null;
  m.onclick = (e) => {
    if (e.target.closest("[data-vclose]")) return closeModal();
    const f = e.target.closest("[data-vfav]");
    if (f) { const id = Number(f.dataset.vfav); const v = S.byId.get(id); toggleFav([id], !(v && v.fav)); f.classList.toggle("on", !!S.byId.get(id)?.fav); return; }
    const inf = e.target.closest("[data-vinfo]");
    if (inf) { const id = Number(inf.dataset.vinfo); closeModal(); S.focus = id; refreshSelectionClasses(); openDetails(id); return; }
    const t = e.target.closest(".vtile"); if (!t) return;
    const id = Number(t.dataset.vid);
    // one click selects a version, a double-click plays it (same as cards in the grid)
    if (clickTimer && clickTimer.id === id) { clearTimeout(clickTimer.t); clickTimer = null; play(id); return; }
    for (const x of m.querySelectorAll(".vtile.sel")) x.classList.remove("sel");
    t.classList.add("sel");
    if (clickTimer) clearTimeout(clickTimer.t);
    clickTimer = { id, t: setTimeout(() => { clickTimer = null; }, DOUBLE_CLICK_MS + 140) };
  };
  // hover previews inside the pop-up
  let hv = null;
  m.onmouseover = (e) => {
    const t = e.target.closest(".vtile");
    if (!t || t === hv) return;
    if (hv) stopPreview(hv);
    hv = t;
    clearTimeout(m._hoverT);
    if (!S.settings.hover_preview) return;
    m._hoverT = setTimeout(() => {
      const v = S.byId.get(Number(t.dataset.vid)); if (!v || !t.isConnected) return;
      const vid = document.createElement("video");
      vid.muted = true; vid.loop = true; vid.playsInline = true; vid.preload = "metadata"; vid.disablePictureInPicture = true;
      vid.src = tok("/media/" + v.id) + `#t=${(v.duration ? v.duration * 0.25 : 0).toFixed(1)}`;
      vid.addEventListener("playing", () => vid.classList.add("playing"), { once: true });
      t.querySelector(".vthumb").appendChild(vid);
      vid.play().catch(() => {});
    }, 450);
  };
  m.onmouseleave = () => { clearTimeout(m._hoverT); if (hv) stopPreview(hv); hv = null; };
}

/* ------------------------------------------------------------------ actions */
async function play(id) {
  try { await api.post("/api/play", { id }); toast("Opening in VLC…"); }
  catch (e) { toast(e.message, true); }
}
async function reveal(id) { try { await api.post("/api/reveal", { id }); } catch (e) { toast(e.message, true); } }
async function openExternal(url) { try { await api.post("/api/open-url", { url }); } catch (e) { toast(e.message, true); } }
async function toggleFav(ids, on) {
  for (const id of ids) { const v = S.byId.get(id); if (v) { v.fav = on ? 1 : 0; v.fav_at = on ? Date.now() / 1000 : null; } }
  for (const id of ids) {
    refreshCard(id);
    const b = cardEl(id)?.querySelector(".fav"); if (b && on) b.classList.add("pop");
  }
  S.counts.favs = S.videos.filter((v) => v.fav).length; updateCounts();
  try { await api.post("/api/favorite", { ids, on }); }
  catch (e) { toast("Could not save favorite: " + e.message, true); loadData(); }
  if (S.view === "fav" || S.filters.fav) setTimeout(() => render(true), 250);
}

/* ------------------------------------------------------------------ grid events */
// A single click waits this long for a possible second click before opening the details panel,
// so a double-click plays the video without the panel popping open first.
const DOUBLE_CLICK_MS = 260;
let pendingClick = null;
function highlightOnly(id) {
  S.sel = new Set([id]); S.anchor = id; S.focus = id;
  refreshSelectionClasses();
}
// start loading a series' details on press, so the page opens complete (no jump when the related strip arrives)
canvas.addEventListener("pointerdown", (e) => {
  const t = e.target.closest("[data-goanime], .poster");
  if (t) fetchAnimeDetail(Number(t.dataset.goanime || t.dataset.anime)).catch(() => {});
});
canvas.addEventListener("click", (e) => {
  const rb = e.target.closest("[data-rate]");
  if (rb) {
    e.stopPropagation();
    const id = Number(rb.closest(".card").dataset.id);
    const val = Number(rb.dataset.rate);
    rateSongs([id], val === 0 ? 0 : nextRating(id, val));
    return;
  }
  const vc = e.target.closest(".vcount");
  if (vc) { e.stopPropagation(); if (pendingClick) { clearTimeout(pendingClick.timer); pendingClick = null; } return openVersions(Number(vc.closest(".card").dataset.id)); }
  const fb = e.target.closest("[data-fav]");
  if (fb) { e.stopPropagation(); const id = Number(fb.dataset.fav); const v = S.byId.get(id); toggleFav([id], !(v && v.fav)); return; }
  const ext = e.target.closest("[data-ext]");
  if (ext) { e.preventDefault(); return openExternal(ext.dataset.ext); }
  const act = e.target.closest("[data-act]");
  if (act) return heroAction(act.dataset.act);
  const tg = e.target.closest(".hlink[data-tg]");
  if (tg) { S.recGroup = Number(tg.dataset.tg); S.groupExpanded = false; try { localStorage.setItem("kanade.recgroup2", String(S.recGroup)); } catch (err) { /* ignore */ } scroller.scrollTop = 0; render(false); return; }
  const go = e.target.closest("[data-goanime]");
  if (go) { const gid = Number(go.dataset.goanime); if (gid !== S.seriesId) setView("anime", { seriesId: gid }); return; }
  const p = e.target.closest(".poster");
  if (p) return setView("anime", { seriesId: Number(p.dataset.anime) });
  const c = e.target.closest(".card");
  if (!c) return;
  const id = Number(c.dataset.id);
  const plain = !(e.ctrlKey || e.metaKey || e.shiftKey);
  const panelOpen = $("#details").classList.contains("open");
  if (!plain) {                                   // Ctrl/Shift: multi-select, immediate
    if (pendingClick) { clearTimeout(pendingClick.timer); pendingClick = null; }
    select(id, e);
    return;
  }
  if (pendingClick && pendingClick.id === id) {   // second click on the same card: double-click
    clearTimeout(pendingClick.timer); pendingClick = null;
    if (c.dataset.stack) openVersions(id);        // a song with several versions: pick the one to play
    else play(id);                                // plays; never opens the panel
    return;
  }
  if (pendingClick) clearTimeout(pendingClick.timer);
  highlightOnly(id);                              // instant visual feedback
  if (panelOpen) {                                // panel already open: just show this video in it
    openDetails(id);
    pendingClick = { id, timer: setTimeout(() => { pendingClick = null; }, DOUBLE_CLICK_MS) };
  } else {
    const pre = api.get("/api/video/" + id).catch(() => null);   // fetch while we wait
    pendingClick = { id, timer: setTimeout(() => { pendingClick = null; openDetails(id, pre); }, DOUBLE_CLICK_MS) };
  }
});
// Native dblclick is unreliable here: the first click can redraw/reflow the grid, so the two clicks
// may land on different elements. Double-clicks are detected in the click handler instead.
canvas.addEventListener("dblclick", (e) => e.preventDefault());
canvas.addEventListener("contextmenu", (e) => {
  const c = e.target.closest(".card");
  const p = e.target.closest(".poster");
  if (!c && !p) return;
  e.preventDefault();
  if (p) return showCtx(e.clientX, e.clientY, [{ label: "Open series", icon: "series", fn: () => setView("anime", { seriesId: Number(p.dataset.anime) }) }]);
  const id = Number(c.dataset.id);
  if (!S.sel.has(id)) { S.sel = new Set([id]); S.focus = id; S.anchor = id; refreshSelectionClasses(); }
  const ids = [...S.sel];
  const v = S.byId.get(id);
  const many = ids.length > 1;
  const allFav = ids.every((i) => S.byId.get(i)?.fav);
  const items = [];
  if (!many) items.push({ label: "Play", icon: "play", k: "Enter", fn: () => play(id) });
  if (!many && v && v.anime_id && S.view !== "anime") items.push({ label: "Open series page", icon: "series", fn: () => setView("anime", { seriesId: v.anime_id }) });
  if (!many && songVideos(v).length > 1) items.push({ label: `Show all ${songVideos(v).length} versions`, icon: "stack", fn: () => openVersions(id) });
  if (!many) items.push({ label: "View details", icon: "info", fn: () => { S.focus = id; refreshSelectionClasses(); openDetails(id); } });
  if (!many) items.push({ label: "Open containing folder", icon: "folder", fn: () => reveal(id) });
  items.push({ sep: true });
  const rs = rateState(ids.flatMap((i) => songVideos(S.byId.get(i))));
  items.push(rs.liked ? { label: "Remove like", icon: "up", k: "L", fn: () => rateSongs(ids, 0) }
                      : { label: many ? `Like ${ids.length} songs` : "Like (more like this in For You)", icon: "up", k: "L", fn: () => rateSongs(ids, 1) });
  items.push(rs.disliked ? { label: "Remove dislike (show in For You again)", icon: "eye", k: "D", fn: () => rateSongs(ids, 0) }
                         : { label: many ? `Dislike ${ids.length} songs` : "Dislike (hide from For You)", icon: "down", k: "D", fn: () => rateSongs(ids, -1) });
  items.push({ sep: true });
  items.push({ label: many ? `Edit ${ids.length} videos…` : "Edit metadata…", icon: "edit", k: "E", fn: () => openEditor(ids) });
  items.push({ label: allFav ? "Remove from favorites" : "Add to favorites", icon: "heart", k: "F", fn: () => toggleFav(ids, !allFav) });
  if (ids.some((i) => S.byId.get(i)?.status === "review")) items.push({ label: "Accept suggested match", icon: "check", fn: async () => { const r = await api.post("/api/accept", { ids }); toast(`Accepted ${r.accepted}`); loadData(); } });
  if (ids.some((i) => S.byId.get(i)?.override)) items.push({ label: "Reset corrections", icon: "refresh", fn: async () => { await api.post("/api/override/clear", { ids }); toast("Corrections removed"); loadData(); } });
  showCtx(e.clientX, e.clientY, items);
});

async function heroAction(act) {
  const a = S.animeById.get(S.seriesId); if (!a) return;
  const vids = S.flat;
  if (act === "play-first" && vids.length) play(vids[0].id);
  if (act === "edit-series") openEditor(vids.map((v) => v.id));
  if (act === "fav-series") { await toggleFav(vids.map((v) => v.id), true); toast("Added all to favorites"); }
}

/* hover previews */
let hoverTimer = null, hoverCard = null;
canvas.addEventListener("mouseover", (e) => {
  const c = e.target.closest(".card");
  if (c === hoverCard) return;
  if (hoverCard) stopPreview(hoverCard);
  hoverCard = c;
  clearTimeout(hoverTimer);
  if (!c || !S.settings.hover_preview) return;
  if (S.scrolling) return;
  hoverTimer = setTimeout(() => { if (!S.scrolling && !document.hidden) startPreview(c); }, 550);
});
canvas.addEventListener("mouseleave", () => { clearTimeout(hoverTimer); if (hoverCard) stopPreview(hoverCard); hoverCard = null; });
function startPreview(c) {
  if (!c.isConnected) return;
  const v = S.byId.get(Number(c.dataset.id)); if (!v) return;
  const th = c.querySelector(".thumb");
  if (th.querySelector("video")) return;
  const vid = document.createElement("video");
  vid.muted = true; vid.loop = true; vid.playsInline = true; vid.preload = "metadata"; vid.disablePictureInPicture = true;
  const start = v.duration ? Math.max(0, v.duration * 0.25) : 0;
  vid.src = tok("/media/" + v.id) + `#t=${start.toFixed(1)}`;
  vid.addEventListener("playing", () => vid.classList.add("playing"), { once: true });
  th.appendChild(vid);
  vid.play().catch(() => {});
}
function stopPreview(c) {
  for (const vid of c.querySelectorAll("video")) { vid.pause(); vid.removeAttribute("src"); vid.load(); vid.remove(); }
}

/* ------------------------------------------------------------------ context menu */
function showCtx(x, y, items) {
  const m = $("#ctx");
  m.innerHTML = items.map((it, i) => it.sep ? "<hr>" : `<button data-i="${i}">${icon(it.icon || "info")}${esc(it.label)}${it.k ? `<span class="k">${it.k}</span>` : ""}</button>`).join("");
  m.classList.remove("hidden");
  const r = m.getBoundingClientRect();
  m.style.left = Math.min(x, innerWidth - r.width - 8) + "px";
  m.style.top = Math.min(y, innerHeight - r.height - 8) + "px";
  m.onclick = (e) => { const b = e.target.closest("[data-i]"); if (!b) return; hideCtx(); items[Number(b.dataset.i)].fn(); };
}
function hideCtx() { $("#ctx").classList.add("hidden"); }
document.addEventListener("mousedown", (e) => { if (!e.target.closest("#ctx")) hideCtx(); });
window.addEventListener("blur", hideCtx);
document.addEventListener("contextmenu", (e) => { if (!e.target.closest("input,textarea") && !e.defaultPrevented) e.preventDefault(); });

/* ------------------------------------------------------------------ bulk bar */
function renderBulkbar() {
  const bb = $("#bulkbar");
  const n = S.sel.size;
  bb.classList.toggle("hidden", n < 2 || !isGridView());
  if (n < 2) return;
  const ids = [...S.sel];
  const anyReview = ids.some((i) => S.byId.get(i)?.status === "review");
  bb.innerHTML = `<span class="n">${n} selected</span>
    <button class="btn sm" data-b="fav">${icon("heart")}Favorite</button>
    <button class="btn sm" data-b="unfav">Unfavorite</button>
    <button class="btn sm" data-b="like">${icon("up")}Like</button>
    <button class="btn sm" data-b="dislike">${icon("down")}Dislike</button>
    <button class="btn sm" data-b="edit">${icon("edit")}Edit / assign…</button>
    ${anyReview ? `<button class="btn sm" data-b="accept">${icon("check")}Accept suggestions</button>` : ""}
    <button class="btn sm ghost" data-b="clear" title="Clear selection">${icon("x")}</button>`;
  bb.onclick = async (e) => {
    const b = e.target.closest("[data-b]"); if (!b) return;
    const ids2 = [...S.sel];
    if (b.dataset.b === "fav") toggleFav(ids2, true);
    if (b.dataset.b === "unfav") toggleFav(ids2, false);
    if (b.dataset.b === "like") rateSongs(ids2, 1);
    if (b.dataset.b === "dislike") rateSongs(ids2, -1);
    if (b.dataset.b === "edit") openEditor(ids2);
    if (b.dataset.b === "accept") { const r = await api.post("/api/accept", { ids: ids2 }); toast(`Accepted ${r.accepted}`); loadData(); }
    if (b.dataset.b === "clear") { S.sel.clear(); refreshSelectionClasses(); }
  };
}

/* ------------------------------------------------------------------ editor modal */
function openModal(html) { $("#modal").innerHTML = html; $("#modalWrap").classList.remove("hidden"); }
function closeModal() {
  const m = $("#modal");
  for (const vid of m.querySelectorAll("video")) { vid.pause(); vid.removeAttribute("src"); vid.load(); }
  clearTimeout(m._hoverT);
  m.onclick = m.onmouseover = m.onmouseleave = null;
  m.classList.remove("versions", "teach");
  $("#modalWrap").classList.add("hidden"); m.innerHTML = "";
}
$("#modalWrap").addEventListener("mousedown", (e) => { if (e.target.id === "modalWrap") closeModal(); });

async function openEditor(ids, opts = {}) {
  const many = ids.length > 1;
  let v = null;
  try { v = await api.get("/api/video/" + ids[0]); } catch (e) { return toast(e.message, true); }
  const o = v.override || {};
  const sameAnime = ids.every((i) => S.byId.get(i)?.anime_id === v.anime_id);
  const a = v.anime_id ? await api.get("/api/anime/" + v.anime_id).catch(() => null) : null;
  const title = many ? `Edit ${ids.length} videos` : "Edit metadata";
  const sub = many ? (sameAnime && a ? esc(a.display_title) : "Multiple series") : esc(v.filename);
  const startTab = opts.tab || (v.status === "review" && !many ? "find" : "details");
  openModal(`
    <div class="modal-head"><div><h3>${title}</h3><div class="sub">${sub}</div></div><button class="icon-btn" data-m="close">${icon("x")}</button></div>
    <div class="tabs"><button data-tab="details">Details</button><button data-tab="find">Find &amp; assign anime</button></div>
    <div class="modal-body">
      <div data-pane="details">
        <div class="form-grid">
          ${a && sameAnime ? `
          <div class="sec-title">Series — applies to every video of “${esc(a.display_title)}”</div>
          <div class="field"><label>Romaji title (displayed)</label><input id="e_romaji" value="${esc(a.o_romaji || "")}" placeholder="${esc(a.title_mal || a.title_romaji)}"></div>
          <div class="field half"><label>English title</label><input id="e_english" value="${esc(a.o_english || "")}" placeholder="${esc(a.title_english || "")}"></div>
          <div class="field half"><label>Japanese title</label><input id="e_japanese" value="${esc(a.o_japanese || "")}" placeholder="${esc(a.title_japanese || "")}"></div>
          <div class="field third"><label>Series year</label><input id="e_ayear" type="number" value="${esc(a.o_year || "")}" placeholder="${esc(a.year || "")}"></div>
          <div class="field third"><label>Season</label><select id="e_season"><option value="">${esc(a.season || "—")} (auto)</option>${["Winter", "Spring", "Summer", "Fall"].map((s) => `<option ${a.o_season === s ? "selected" : ""}>${s}</option>`).join("")}</select></div>
          <div class="field third"></div>` : `<div class="sec-title">${many ? "Selected videos belong to different series — use “Find & assign anime” to move them all to one series." : "This video is not linked to a series yet — use “Find & assign anime”."}</div>`}
          <div class="sec-title">${many ? `Theme — applies to all ${ids.length} selected videos (leave blank to keep)` : "Theme"}</div>
          <div class="field third"><label>Type</label><select id="e_type"><option value="">${many ? "Keep" : `${esc(v.type || "—")} (auto)`}</option><option value="OP" ${o.type === "OP" ? "selected" : ""}>Opening (OP)</option><option value="ED" ${o.type === "ED" ? "selected" : ""}>Ending (ED)</option></select></div>
          <div class="field third"><label>Number</label><input id="e_number" type="number" min="1" value="${esc(many ? "" : o.number || "")}" placeholder="${many ? "Keep" : esc(v.number || "")}"></div>
          <div class="field third"><label>Version</label><input id="e_version" type="number" min="1" value="${esc(many ? "" : o.version || "")}" placeholder="${many ? "Keep" : esc(v.version || 1)}"></div>
          <div class="field third"><label>Year</label><input id="e_year" type="number" value="${esc(many ? "" : o.year || "")}" placeholder="${many ? "Keep" : esc(v.year || "")}"></div>
          ${many ? "" : `<div class="field"><label>Song title</label><input id="e_song" value="${esc(o.song_title || "")}" placeholder="${esc(v.song_title || "")}"></div>
          <div class="field"><label>Artist(s)</label><input id="e_artists" value="${esc(o.artists || "")}" placeholder="${esc(v.artists || "")}"></div>`}
          <div class="field"><div class="hint">Corrections are stored separately and always win over automatic data, even after rescans. Leave a field empty to use the automatic value.</div></div>
        </div>
      </div>
      <div data-pane="find" class="hidden">
        <div class="finder-bar"><input id="f_q" placeholder="Search anime title…" value="${esc(v.status === "review" ? (v.p_title || "") : (v.title || ""))}"><button class="btn primary" id="f_go">${icon("search")}Search</button></div>
        <div class="src-seg" id="f_src"><button data-src="library" class="on">Library &amp; catalogue (offline)</button><button data-src="animethemes">AnimeThemes</button><button data-src="anilist">AniList</button><button data-src="mal">MyAnimeList</button></div>
        <div class="results" id="f_results"><div class="loading">Search, then click a result to assign ${many ? `all ${ids.length} videos` : "this video"} to it.</div></div>
      </div>
    </div>
    <div class="modal-foot">${!many && v.has_override ? `<button class="btn ghost danger" data-m="reset" style="margin-right:auto">${icon("refresh")}Reset corrections</button>` : ""}<button class="btn" data-m="close">Cancel</button><button class="btn primary" data-m="save">${icon("check")}Save</button></div>`);
  const m = $("#modal");
  const setTab = (t) => { for (const b of $$("[data-tab]", m)) b.classList.toggle("on", b.dataset.tab === t); for (const p of $$("[data-pane]", m)) p.classList.toggle("hidden", p.dataset.pane !== t); $("[data-m=save]", m).classList.toggle("hidden", t !== "details"); if (t === "find") $("#f_q").focus(); };
  setTab(startTab);
  let src = "library";
  const doSearch = async () => {
    const q = $("#f_q").value.trim(); if (!q) return;
    const box = $("#f_results"); box.innerHTML = `<div class="loading">Searching…</div>`;
    const r = await api.get(`/api/remote-search?source=${src}&q=${encodeURIComponent(q)}`).catch((e) => ({ results: [], error: e.message }));
    if (r.error && !r.results.length) { box.innerHTML = `<div class="loading" style="color:var(--danger)">${esc(r.error)}</div>`; return; }
    if (!r.results.length) { box.innerHTML = `<div class="loading">No results.</div>`; return; }
    box._results = r.results;
    box.innerHTML = r.results.map((c, i) => {
      const cov = c.cover ? (c.cover.startsWith("/") ? tok(c.cover) : c.cover) : "";
      return `<div class="result" data-r="${i}">${cov ? `<img src="${esc(cov)}" alt="" referrerpolicy="no-referrer">` : `<div class="noc"></div>`}
        <div><div class="t">${esc(c.title_romaji)}</div><div class="s">${esc([c.title_english, c.year, c.season, c.format].filter(Boolean).join(" · "))}</div></div>
        <span class="src">${esc(c.source === "library" ? "In library" : c.source)}${c.score ? ` · ${c.score}%` : ""}</span></div>`;
    }).join("");
  };
  m.onclick = async (e) => {
    const t = e.target.closest("[data-tab]"); if (t) return setTab(t.dataset.tab);
    const s = e.target.closest("[data-src]"); if (s) { src = s.dataset.src; for (const b of $$("[data-src]", m)) b.classList.toggle("on", b === s); return doSearch(); }
    if (e.target.closest("#f_go")) return doSearch();
    const r = e.target.closest("[data-r]");
    if (r) {
      const c = $("#f_results")._results[Number(r.dataset.r)];
      try {
        const res = await api.post("/api/assign", { ids, target: c });
        toast(`Assigned ${ids.length} video(s) to ${c.title_romaji}`);
        closeModal();
        if (S.view === "anime") S.seriesId = res.anime_id;
        await loadData();
        if (S.focus) openDetails(S.focus);
      } catch (err) { toast(err.message, true); }
      return;
    }
    const b = e.target.closest("[data-m]"); if (!b) return;
    if (b.dataset.m === "close") return closeModal();
    if (b.dataset.m === "reset") { await api.post("/api/override/clear", { ids }); toast("Corrections removed"); closeModal(); await loadData(); if (S.focus) openDetails(S.focus); return; }
    if (b.dataset.m === "save") {
      const val = (id) => { const el = $("#" + id, m); return el ? el.value.trim() : undefined; };
      const video = {};
      const put = (k, id) => { const x = val(id); if (x === undefined) return; if (many && x === "") return; video[k] = x === "" ? null : x; };
      put("type", "e_type"); put("number", "e_number"); put("version", "e_version"); put("year", "e_year");
      if (!many) { put("song_title", "e_song"); put("artists", "e_artists"); }
      const body = { ids, video };
      if ($("#e_romaji", m)) {
        body.anime_id = v.anime_id;
        body.anime = { title_romaji: val("e_romaji") || null, title_english: val("e_english") || null, title_japanese: val("e_japanese") || null, year: val("e_ayear") || null, season: val("e_season") || null };
      }
      try { await api.post("/api/override", body); toast("Saved"); closeModal(); await loadData(); if (S.focus) openDetails(S.focus); }
      catch (err) { toast(err.message, true); }
    }
  };
  $("#f_q").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  if (startTab === "find") doSearch();
}

/* ------------------------------------------------------------------ settings & setup */
async function renderSettings() {
  setHead("Settings", "Library folder, playback, metadata sources and maintenance");
  const page = $("#page");
  const b = await api.get("/api/bootstrap").catch(() => S.boot);
  S.boot = b; S.settings = b.settings;
  const st = b.settings, c = b.counts, tools = b.tools;
  const ol = await api.get("/api/ollama").catch(() => ({ available: false, models: [] }));
  const cat = b.catalog;
  if (S.view !== "settings") return;
  page.innerHTML = `
  <div class="panel"><h3>Library</h3><p class="desc">Kanade only reads this folder. It never renames, moves or deletes your videos.</p>
    <div class="form-grid">
      <div class="field"><label>Archive folder</label><div class="inline"><input id="s_root" value="${esc(st.archive_root)}"><button class="btn" data-s="pick-root">${icon("folder")}Browse…</button></div></div>
    </div>
    <div class="stat-grid" style="margin-top:16px">
      <div class="stat"><b>${(c.total || 0).toLocaleString()}</b><span>videos</span></div>
      <div class="stat"><b>${(c.series || 0).toLocaleString()}</b><span>series</span></div>
      <div class="stat"><b>${(c.ops || 0).toLocaleString()}</b><span>openings</span></div>
      <div class="stat"><b>${(c.eds || 0).toLocaleString()}</b><span>endings</span></div>
      <div class="stat"><b>${(c.review || 0).toLocaleString()}</b><span>need review</span></div>
      <div class="stat"><b>${(c.missing || 0).toLocaleString()}</b><span>missing files</span></div>
    </div>
    <div class="row-actions" style="margin-top:16px">
      <button class="btn primary" data-s="scan">${icon("refresh")}Rescan library</button>
      <button class="btn" data-s="sync">${icon("refresh")}Re-sync AnimeThemes catalogue</button>
      <button class="btn" data-s="rematch">Re-identify all (keeps corrections)</button>
      <button class="btn" data-s="thumbs">Retry failed thumbnails</button>
    </div></div>
  <div class="panel"><h3>For You · your taste</h3><p class="desc">Folders of openings/endings you love: add as many as you like, from any drive (subfolders are included, any video format; Kanade only reads them). Kanade learns how they sound and look and ranks your library in <b>For You</b>; 👍 / 👎 on any video teach it more. Analysis runs in the background at low priority (it only pauses while a video is playing).</p>
    <div class="tlist" id="t_list"></div>
    <div class="inline" style="margin-top:10px"><input id="t_new" placeholder="Paste a folder path, or use Browse… (add as many folders as you like)"><button class="btn" data-s="t-browse">${icon("folder")}Browse…</button><button class="btn" data-s="t-add">Add</button></div>
    <label class="check-row" style="margin-top:12px"><input type="checkbox" id="t_fav" ${st.taste_use_favorites ? "checked" : ""}> Count my favourites as likes too</label>
    <div class="status-line" style="margin-top:12px"><span class="dotc ${b.taste && b.taste.engine_ready ? "" : "mid"}"></span>For You engine: ${b.taste && b.taste.engine_ready ? `installed · ${(b.taste.analysed || 0).toLocaleString()} library videos analysed` : "not installed (one-time download of about 4–5 GB: PyTorch + 2 AI models; uses your NVIDIA GPU if present)"}</div>
    <div class="row-actions" style="margin-top:12px">${b.taste && b.taste.engine_ready ? "" : `<button class="btn primary" data-s="engine-setup">Install For You engine</button>`}
      <button class="btn ${b.taste && b.taste.engine_ready ? "primary" : ""}" data-s="t-save">${icon("check")}Save folders &amp; analyse</button>
      ${b.taste && b.taste.engine_ready ? `<button class="btn" data-s="engine-analyze">${icon("refresh")}Re-analyse now</button>` : ""}</div></div>
  <div class="panel"><h3>Playback</h3><p class="desc">Double-click or Enter plays a video. Leave the VLC path empty to use the Windows default player for the file type.</p>
    <div class="form-grid">
      <div class="field"><label>VLC path (optional)</label><div class="inline"><input id="s_vlc" value="${esc(st.vlc_path)}" placeholder="C:\\Program Files\\VideoLAN\\VLC\\vlc.exe"><button class="btn" data-s="pick-vlc">Browse…</button></div></div>
      <label class="check-row"><input type="checkbox" id="s_hover" ${st.hover_preview ? "checked" : ""}> Muted video preview when hovering a card</label>
    </div></div>
  <div class="panel"><h3>Metadata sources</h3><p class="desc">AnimeThemes is matched first (exact filename), then AniList / MyAnimeList search, then the optional local Ollama model. Low-confidence matches go to Needs Review.</p>
    <div class="status-line"><span class="dotc ${cat.size ? "" : "bad"}"></span>AnimeThemes catalogue: ${cat.size ? `${cat.size.toLocaleString()} anime, synced ${cat.age_days < 1 ? "today" : Math.round(cat.age_days) + " days ago"}` : "not downloaded yet"}</div>
    <div class="status-line"><span class="dotc ${tools.ok ? "" : "bad"}"></span>FFmpeg: ${tools.ok ? esc(tools.ffmpeg) : esc(tools.help)}</div>
    <div class="status-line"><span class="dotc ${ol.available ? "" : "mid"}"></span>Ollama: ${ol.available ? `running (${ol.models.length} models)` : "not running (optional)"}</div>
    <div class="form-grid" style="margin-top:14px">
      <label class="check-row"><input type="checkbox" id="s_anilist" ${st.use_anilist ? "checked" : ""}> Use AniList for search and missing titles</label>
      <label class="check-row"><input type="checkbox" id="s_jikan" ${st.use_jikan ? "checked" : ""}> Use Jikan (MyAnimeList) for search</label>
      <label class="check-row"><input type="checkbox" id="s_ollama" ${st.ollama_enabled ? "checked" : ""}> Use Ollama for ambiguous filenames</label>
      <div class="field half"><label>Ollama URL</label><input id="s_ourl" value="${esc(st.ollama_url)}"></div>
      <div class="field half"><label>Ollama model</label>${ol.models.length ? `<select id="s_omodel"><option value="">First available</option>${ol.models.map((mm) => `<option ${mm === st.ollama_model ? "selected" : ""}>${esc(mm)}</option>`).join("")}</select>` : `<input id="s_omodel" value="${esc(st.ollama_model)}" placeholder="e.g. llama3.1:8b">`}</div>
      <div class="field third"><label>Auto-accept confidence</label><input id="s_accept" type="number" step="0.01" min="0.5" max="1" value="${esc(st.accept_confidence)}"></div>
      <div class="field third"><label>Worker threads</label><input id="s_workers" type="number" min="1" max="16" value="${esc(st.workers)}"></div>
      <div class="field third"><label>Thumbnail width (px)</label><input id="s_tw" type="number" min="240" max="960" step="40" value="${esc(st.thumb_width)}"></div>
      <div class="field third"><label>Check for new files every (min, 0 = off)</label><input id="s_rescan" type="number" min="0" max="1440" step="5" value="${esc(st.auto_rescan_minutes)}"></div>
    </div>
    <div class="row-actions" style="margin-top:16px"><button class="btn" data-s="anilist">Fill missing titles from AniList</button><button class="btn" data-s="mal">Fetch official MAL titles &amp; artists (Jikan)</button></div></div>
  <div class="panel"><h3>Data</h3><p class="desc">Database, thumbnails, covers, API cache and logs live in <code>${esc(b.data_dir)}</code>.</p>
    <div class="row-actions"><button class="btn" data-s="open-data">${icon("folder")}Open data folder</button></div></div>
  <div class="row-actions" style="max-width:920px;justify-content:flex-end"><button class="btn primary" data-s="save">${icon("check")}Save settings</button></div>`;
  S.tasteDraft = [...((b.taste && b.taste.folders) || st.taste_folders || [])];
  renderTasteList();
  page.onclick = async (e) => {
    const btn = e.target.closest("[data-s]"); if (!btn) return;
    const a = btn.dataset.s;
    try {
      if (a === "pick-root") { const r = await api.post("/api/pick-folder", { kind: "folder" }); if (r.path) $("#s_root").value = r.path; }
      if (a === "pick-vlc") { const r = await api.post("/api/pick-folder", { kind: "file" }); if (r.path) $("#s_vlc").value = r.path; }
      if (a === "scan") { await api.post("/api/scan"); toast("Scan started"); }
      if (a === "sync") { await api.post("/api/sync-catalog"); toast("Catalogue sync started"); }
      if (a === "rematch") { if (confirm("Re-identify every automatically matched video? Manual corrections are kept.")) { await api.post("/api/rematch", { all: true }); toast("Re-identification started"); } }
      if (a === "thumbs") { await api.post("/api/thumbs/retry"); toast("Retrying failed thumbnails"); }
      if (a === "anilist") { await api.post("/api/fill-anilist"); toast("AniList fill started"); }
      if (a === "mal") { await api.post("/api/enrich-mal"); toast("MyAnimeList enrichment started"); }
      if (a === "open-data") await api.post("/api/open-data-dir");
      if (a === "t-browse") {                      // a picked folder is added right away (pick again to add more)
        const r = await api.post("/api/pick-folder", { kind: "folder" });
        if (r.path) {
          if (!S.tasteDraft.includes(r.path)) { S.tasteDraft.push(r.path); renderTasteList(); toast("Folder added · press “Save folders & analyse” when done"); }
          $("#t_new").value = "";
        }
      }
      if (a === "t-add") { const v = $("#t_new").value.trim(); if (v && !S.tasteDraft.includes(v)) { S.tasteDraft.push(v); renderTasteList(); } $("#t_new").value = ""; }
      if (a === "t-del") { S.tasteDraft.splice(Number(btn.dataset.i), 1); renderTasteList(); }
      if (a === "t-save") { const nv = $("#t_new").value.trim(); if (nv && !S.tasteDraft.includes(nv)) S.tasteDraft.push(nv); await api.post("/api/taste", { folders: S.tasteDraft, use_favorites: $("#t_fav").checked }); toast("Saved · For You will update in the background"); renderSettings(); }
      if (a === "engine-setup") { await api.post("/api/taste", { folders: S.tasteDraft, use_favorites: $("#t_fav").checked }); await api.post("/api/engine/setup"); toast("Installing the For You engine in the background (large download)…"); }
      if (a === "engine-analyze") { await api.post("/api/engine/analyze"); toast("For You analysis started"); }
      if (a === "save") {
        const r = await api.post("/api/settings", {
          archive_root: $("#s_root").value.trim(), vlc_path: $("#s_vlc").value.trim(), hover_preview: $("#s_hover").checked,
          use_anilist: $("#s_anilist").checked, use_jikan: $("#s_jikan").checked, ollama_enabled: $("#s_ollama").checked,
          ollama_url: $("#s_ourl").value.trim(), ollama_model: $("#s_omodel").value.trim(), accept_confidence: Number($("#s_accept").value) || 0.85,
          workers: Number($("#s_workers").value) || 4, thumb_width: Number($("#s_tw").value) || 480,
          auto_rescan_minutes: Math.max(0, Number($("#s_rescan").value) || 0),
        });
        S.settings = r.settings; toast("Settings saved");
      }
    } catch (err) { toast(err.message, true); }
  };
}

function renderSetup() {
  $("#page").innerHTML = `<div class="setup"><div class="logo">${$(".brand .logo").innerHTML}</div>
    <h1>Welcome to Kanade</h1><p>Choose the folder that holds your anime openings and endings (organised by year, like <code>2015\\Spring\\…</code>).<br>Kanade only reads it and never changes your files.</p>
    <div class="inline"><input id="setupRoot" placeholder="D:\\AnimeThemes" value="${esc(S.settings.archive_root || "")}"><button class="btn" id="setupPick">${icon("folder")}Browse…</button><button class="btn primary" id="setupGo">Start</button></div>
    ${S.boot && !S.boot.tools.ok ? `<p style="color:var(--warn);margin-top:20px">${esc(S.boot.tools.help)}</p>` : ""}</div>`;
  $("#setupPick").onclick = async () => { try { const r = await api.post("/api/pick-folder"); if (r.path) $("#setupRoot").value = r.path; } catch (e) { toast(e.message, true); } };
  $("#setupGo").onclick = async () => {
    const root = $("#setupRoot").value.trim(); if (!root) return;
    try { const r = await api.post("/api/settings", { archive_root: root }); S.settings = r.settings; toast("Scanning your archive…"); setView("all", { noHistory: true }); }
    catch (e) { toast(e.message, true); }
  };
}

/* ------------------------------------------------------------------ For You */
S.rec = new Map();          // video id -> {a: audio %, m: audio+visual %, wa, wv}
// default: sound + look + genre (ranked liked songs far higher than sound alone in the held-out test); the choice
// made with the old model ("kanade.recmode") is not carried over
S.recMode = (() => { try { return localStorage.getItem("kanade.recmode2") || "mix"; } catch (e) { return "mix"; } })();
S.tasteDraft = [];
S.recGroups = [];
S.recGroup = (() => { try { return Number(localStorage.getItem("kanade.recgroup2") ?? -1); } catch (e) { return -1; } })();
S.groupExpanded = false;

// song types in order: Intense, Energetic, Mellow, Calm
const GROUP_COLORS = ["#f87171", "#fbbf24", "#a78bfa", "#38bdf8", "#a3e635", "#2dd4bf", "#fb923c", "#f472b6"];
const groupColor = (i) => GROUP_COLORS[i % GROUP_COLORS.length];

/** The song type (taste group) a video sounds most like, in the current mode; -1 when unknown. */
function bestGroup(r) {
  const arr = r && (S.recMode === "mix" ? r.gm : r.ga);
  if (!arr || !arr.length) return -1;
  let bi = 0;
  for (let i = 1; i < arr.length; i++) if (arr[i] > arr[bi]) bi = i;
  return bi;
}

/** "All my taste" view: one section per song type with its best matches, then everything by overall match. */
function forYouSections(items) {
  const n = S.recGroups.length;
  const per = Array.from({ length: n }, () => []);
  for (const v of items) { const gi = bestGroup(S.rec.get(v.id)); if (gi >= 0) per[gi].push(v); }
  const show = Math.max(1, measure("cards").cols) * 2;          // two rows of the best matches per type
  const out = [];
  S.recGroups.forEach((g, i) => {
    const score = (v) => recScore(S.rec.get(v.id));      // the overall (learned) score: same % as on the card
    // best matches first, at most 2 songs per franchise, so one long-running show can't fill a whole section
    const top = [], perSeries = new Map();
    for (const v of per[i].sort((a, b) => score(b) - score(a))) {
      const f = franchiseKey(v), n = perSeries.get(f) || 0;
      if (n >= 2) continue;
      perSeries.set(f, n + 1);
      top.push(v);
      if (top.length >= show) break;
    }
    if (top.length) out.push({ header: g.name, count: per[i].length, items: top, color: groupColor(i), seeAll: i });
  });
  out.push({ header: "Everything, ranked best to least like your taste", count: items.length, items });
  return out;
}

/** The score the current mode and taste group give a video (0-100). */
function recScore(r) {
  if (!r) return -1;
  const g = S.recGroup;
  if (g >= 0 && r.ga) return (S.recMode === "mix" ? r.gm : r.ga)[g] ?? -1;
  return S.recMode === "mix" ? r.m : r.a;
}

function renderTasteBar() {
  const bar = $("#tasteBar");
  const show = S.view === "foryou" && S.fyTab === "rec" && S.recGroups.length > 1 && S.rec.size;
  bar.classList.toggle("hidden", !show);
  if (!show) return;
  const g = S.recGroup;
  const chip = (i, label, n) => `<button class="tchip${g === i ? " on" : ""}" data-tg="${i}" title="${i >= 0 ? "Show only songs of this type, best match first · ✎ to rename" : "All song types"}">${i >= 0 ? `<i class="gdot" style="--g:${groupColor(i)}"></i>` : ""}${label}${n != null ? `<b>${n}</b>` : ""}${i >= 0 && g === i ? `<span class="tedit" data-tedit="${i}" title="Rename this group">✎</span>` : ""}</button>`;
  const cur = g >= 0 ? S.recGroups[g] : null;
  const members = cur ? cur.members : [];
  const shown = S.groupExpanded ? members : members.slice(0, 6);
  bar.innerHTML = `<div class="tchips">${chip(-1, "✨ All my taste", null)}${S.recGroups.map((x) => chip(x.id, esc(x.name), x.size)).join("")}</div>
    ${cur ? `<div class="tmembers"><span>Your liked songs of this type:</span> ${shown.map((n) => `<i>${esc(n)}</i>`).join("")}
      ${members.length > 6 ? `<button class="tmore" data-tmore="1">${S.groupExpanded ? "show less" : `+${members.length - 6} more`}</button>` : ""}</div>`
          : `<div class="tmembers"><span>Every song is typed by its own sound, from calm to intense. ${S.sort === "match" ? "Below: your best matches of each type, then everything." : ""} Pick a type to see all its matches.</span></div>`}`;
}

$("#tasteBar").addEventListener("click", async (e) => {
  const ed = e.target.closest("[data-tedit]");
  if (ed) {
    e.stopPropagation();
    const grp = S.recGroups[Number(ed.dataset.tedit)];
    const name = prompt(`Name this group (leave empty for the automatic name “${grp.auto_name || grp.name}”):`, grp.name);
    if (name === null) return;
    try {
      await api.post("/api/taste-group-name", { key: grp.key, name });
      grp.name = name.trim() || grp.auto_name || grp.name;
      renderTasteBar();
    } catch (err) { toast(err.message, true); }
    return;
  }
  const c = e.target.closest("[data-tg]");
  if (c) {
    S.recGroup = Number(c.dataset.tg); S.groupExpanded = false;
    try { localStorage.setItem("kanade.recgroup2", String(S.recGroup)); } catch (err) { /* ignore */ }
    render(false); return;
  }
  if (e.target.closest("[data-tmore]")) { S.groupExpanded = !S.groupExpanded; renderTasteBar(); }
});

/** New results never reshuffle the page you're looking at: while For You is open they wait (a "Show" link
    appears) and are used the next time you open For You. */
async function loadRecs() {
  try {
    const d = await api.get("/api/foryou");
    S.recStatus = d.status || {};
    S.recProgress = d.progress || S.recProgress;
    if (d.rank_at && d.rank_at === S.recRankAt) return;
    if (S.view === "foryou" && isGridView() && S.rec.size) { S.pendingRec = d; render(true); return; }
    applyRecs(d);
    if (S.view === "foryou" && isGridView()) render(true);
  } catch (e) { /* engine not ready yet */ }
}
function applyRecs(d) {
  const m = new Map();
  const nums = (x) => (x ? x.split(",").map(Number) : null);
  for (const [id, a, mix, wa, wv, ga, gm, lf] of d.rows) m.set(id, { a, m: mix, wa, wv, ga: nums(ga), gm: nums(gm), lf: !!lf });
  S.rec = m;
  S.recRankAt = d.rank_at;
  S.pendingRec = null;
  S.recGroups = d.groups || [];
  if (S.recGroup >= S.recGroups.length) S.recGroup = -1;
  S.counts.foryou = forYouCount();
  updateCounts();
}
function applyPendingRecs() {
  if (!S.pendingRec) return;
  applyRecs(S.pendingRec);
  S.fyMemo = null;
  scroller.scrollTop = 0;
  render(false);
}
const fySig = () => [S.recRankAt, S.fyTab, S.recGroup, S.recMode, S.q, S.stack].join("|");

function renderTasteList() {
  const box = $("#t_list"); if (!box) return;
  box.innerHTML = S.tasteDraft.length ? S.tasteDraft.map((f, i) => `<div class="titem">${icon("folder")}<span title="${esc(f)}">${esc(f)}</span><button class="icon-btn" data-s="t-del" data-i="${i}" title="Remove">${icon("x")}</button></div>`).join("")
    : `<div class="tempty">No folders yet. Add one or more folders of videos you love.</div>`;
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-fy]"); if (!b) return;
  if (b.dataset.fy === "settings") setView("settings");
  if (b.dataset.fy === "analyze") api.post("/api/engine/analyze").then(() => toast("For You analysis started")).catch((err) => toast(err.message, true));
});

/* ------------------------------------------------------------------ jobs / polling */
function jobHTML(name, j) {
  if (!j || (j.state === "idle" && !j.error)) return "";
  const pct = j.total ? Math.min(100, (j.done / j.total) * 100) : 0;
  return `<div class="job ${j.state === "error" ? "err" : ""}"><div class="job-top"><b>${esc(j.phase || name)}</b><span>${j.total ? `${j.done.toLocaleString()}/${j.total.toLocaleString()}` : ""}</span></div>
    <div class="job-cur">${esc(j.state === "error" ? (j.error || j.message) : (j.current || j.message || ""))}</div>
    ${j.state === "running" ? `<div class="bar ${j.total ? "" : "indet"}"><i style="width:${pct}%"></i></div>` : ""}</div>`;
}
let lastMsg = "";
async function poll() {
  try {
    const st = await api.get("/api/status");
    $("#jobs").innerHTML = jobHTML("Library", st.pipeline) + jobHTML("Thumbnails", st.thumbs) + jobHTML("For You", st.recs);
    const recWasRunning = S.recProgress && S.recProgress.state === "running";
    S.recProgress = st.recs;
    if (st.recs_version !== S.recVersion || (recWasRunning && st.recs.state !== "running")) { S.recVersion = st.recs_version; loadRecs(); }
    else if (S.view === "foryou" && !S.rec.size && isGridView()) render(true);
    if (S.boot) S.boot.status = st;
    const msg = st.pipeline.message;
    if (msg && msg !== lastMsg && st.pipeline.state !== "running" && /unreachable|failed/i.test(msg)) toast(msg, true);
    lastMsg = msg;
    const busy = st.pipeline.state === "running";
    const minGap = busy ? (S.videos.length ? 20000 : 3000) : 0;
    const canReload = isGridView() && $("#modalWrap").classList.contains("hidden") && !document.hidden;
    if ((st.library_version !== S.version || S.loadError) && Date.now() - S.lastLoad > minGap && canReload) {
      await loadData(true);
    } else if (st.media_version !== S.mediaVersion && S.videos.length && !document.hidden) {
      await applyMediaUpdates();
    }
    if (!isGridView()) { S.counts = { ...st.counts, foryou: S.rec ? forYouCount() : 0 }; updateCounts(); }
    S.offline = false;
  } catch (e) {
    // server busy, restarting or gone
    if (!S.offline) { S.offline = true; $("#jobs").innerHTML = `<div class="job err"><div class="job-top"><b>Disconnected</b></div><div class="job-cur">Kanade's background service isn't responding. Retrying…</div></div>`; }
  }
  setTimeout(poll, document.hidden ? 10000 : 2000);
}

/** Patch thumbnails / durations / covers that became ready, without reloading the library. */
async function applyMediaUpdates() {
  if (S.mediaBusy) return;
  S.mediaBusy = true;
  try {
    const d = await api.get("/api/media-updates?since=" + S.mediaTs);
    let changed = false;
    for (const [id, thumb, dur, w, h, tv] of d.videos) {
      const v = S.byId.get(id); if (!v) continue;
      if (v.thumb !== thumb || v.duration !== dur || v.tv !== tv) { v.thumb = thumb; v.duration = dur; v.width = w; v.height = h; v.tv = tv; changed = true; }
    }
    if (d.anime.length) {
      const ids = new Set(d.anime);
      for (const id of ids) { const a = S.animeById.get(id); if (a && !a.has_cover) { a.has_cover = 1; changed = true; } }
      for (const v of S.videos) if (!v.has_cover && ids.has(v.anime_id)) { v.has_cover = 1; changed = true; }
    }
    S.mediaTs = d.now; S.mediaVersion = d.version;
    if (changed && isGridView()) refreshVisible();
  } finally { S.mediaBusy = false; }
}
document.addEventListener("visibilitychange", () => { if (document.hidden && hoverCard) stopPreview(hoverCard); });

/* ------------------------------------------------------------------ search & filters */
const runSearch = debounce(async () => {
  const q = $("#q").value.trim();
  S.q = q;
  if (!q) { S.searchIds = null; S.searchRank = null; if (S.sort === "relevance") setSort("title"); render(false); return; }
  try {
    const r = await api.get("/api/search?q=" + encodeURIComponent(q));
    if ($("#q").value.trim() !== q) return;
    S.searchIds = new Set(r.ids);
    S.searchRank = new Map(r.ids.map((id, i) => [id, i]));
    if (S.sort === "title") setSort("relevance");
    render(false);
  } catch (e) { toast(e.message, true); }
}, 110);

function setSort(s) {
  const sel = $("#sortSel");
  if (s === "match" && !sel.querySelector('[value="match"]')) sel.insertAdjacentHTML("afterbegin", `<option value="match">Best match</option>`);
  if (s !== "match" && S.view !== "foryou") sel.querySelector('[value="match"]')?.remove();
  if (s === "relevance" && !sel.querySelector('[value="relevance"]')) sel.insertAdjacentHTML("afterbegin", `<option value="relevance">Relevance</option>`);
  if (s !== "relevance") sel.querySelector('[value="relevance"]')?.remove();
  S.sort = s; sel.value = s;
}

$("#q").addEventListener("input", () => {
  if (S.view === "settings" || S.view === "setup") setView("all");
  runSearch();
});
$("#qClear").addEventListener("mousedown", (e) => e.preventDefault());   // keep focus where it is
$("#qClear").addEventListener("click", () => { $("#q").value = ""; runSearch(); $("#q").focus(); });
$("#q").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("#q").value = ""; runSearch(); $("#q").blur(); }
  if ((e.key === "Enter" || e.key === "ArrowDown") && S.view !== "series") { e.preventDefault(); $("#q").blur(); if (S.flat.length) { ensureVisible(0); select(S.flat[0].id); } }
});
$("#typeSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; S.filters.type = b.dataset.type; for (const x of $$("#typeSeg button")) x.classList.toggle("on", x === b); render(false); });
$("#yearSel").addEventListener("change", (e) => { S.filters.year = e.target.value; render(false); });

$("#numSel").addEventListener("change", (e) => { S.filters.num = e.target.value; render(false); });
// Series filter suggestions: only the ~40 best matches live in the DOM (not thousands of <option>s)
function fillSeriesSuggestions(text) {
  if (!S.seriesTitles) S.seriesTitles = [...new Set(S.anime.map((a) => a.title))].sort(collator.compare);
  const t = text.trim().toLowerCase();
  const hits = [];
  for (const title of S.seriesTitles) {
    if (!t || title.toLowerCase().includes(t)) { hits.push(title); if (hits.length >= 40) break; }
  }
  $("#seriesList").innerHTML = hits.map((x) => `<option value="${esc(x)}">`).join("");
}
$("#seriesFilter").addEventListener("focus", (e) => fillSeriesSuggestions(e.target.value));
$("#seriesFilter").addEventListener("blur", () => { $("#seriesList").innerHTML = ""; });
$("#seriesFilter").addEventListener("input", debounce((e) => {
  fillSeriesSuggestions(e.target.value);
  const v = e.target.value.trim();
  const exact = S.anime.some((a) => a.title === v);
  S.filters.series = exact ? v : "";
  e.target.classList.toggle("on", exact);
  if (exact || !v) render(false);
}, 150));
function clearSeriesFilter() {
  const inp = $("#seriesFilter");
  inp.value = ""; inp.classList.remove("on");
  fillSeriesSuggestions("");
  if (S.filters.series) { S.filters.series = ""; render(false); }
}
$("#seriesClear").addEventListener("mousedown", (e) => e.preventDefault());
$("#seriesClear").addEventListener("click", () => { clearSeriesFilter(); $("#seriesFilter").focus(); });
$("#seriesFilter").addEventListener("keydown", (e) => { if (e.key === "Escape" && e.target.value) { e.stopPropagation(); clearSeriesFilter(); } });
$("#stackBtn").classList.toggle("on", S.stack);
$("#stackBtn").addEventListener("click", () => {
  S.stack = !S.stack;
  $("#stackBtn").classList.toggle("on", S.stack);
  try { localStorage.setItem("kanade.stack", S.stack ? "1" : "0"); } catch (e) { /* ignore */ }
  toast(S.stack ? "Versions of the same song are grouped into one card" : "Every version shown as its own card");
  render(false);
});
$("#favOnly").addEventListener("click", () => { S.filters.fav = !S.filters.fav; $("#favOnly").classList.toggle("on", S.filters.fav); render(false); });
$("#sortSel").addEventListener("change", (e) => { S.sort = e.target.value; render(false); });
function setCardSize(step) {
  step = Math.max(0, Math.min(SIZE_STEPS.length - 1, step));
  const next = SIZE_STEPS[step];
  $("#sizeRange").value = String(step);
  if (next === S.size) return;
  animateLayout(() => {
    S.size = next;
    try { localStorage.setItem("kanade.size", S.size); } catch (e) { /* storage unavailable */ }
  }, S.focus);
}
$("#sizeRange").value = String(SIZE_STEPS.indexOf(S.size));
$("#sizeRange").addEventListener("input", (e) => setCardSize(Number(e.target.value)));
scroller.addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  setCardSize(SIZE_STEPS.indexOf(S.size) + (e.deltaY < 0 ? 1 : -1));
}, { passive: false });
$("#backBtn").addEventListener("click", goBack);
$("#nav").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-view]"); if (!a) return;
  S.history = [];
  setView(a.dataset.view, { noHistory: true });
});

/* ------------------------------------------------------------------ keyboard */
document.addEventListener("keydown", (e) => {
  const inField = e.target.closest("input, textarea, select");
  if (!$("#modalWrap").classList.contains("hidden")) { if (e.key === "Escape") closeModal(); return; }
  if (e.key === "/" && !inField) { e.preventDefault(); $("#q").focus(); $("#q").select(); return; }
  if (inField) return;
  if (e.key === "Escape") { hideCtx(); if (S.sel.size > 1) { S.sel = new Set(S.focus != null ? [S.focus] : []); refreshSelectionClasses(); } else if ($("#details").classList.contains("open")) closeDetails(); else if (S.view === "anime") goBack(); return; }
  if (e.key === "Backspace" && S.view === "anime") { goBack(); return; }
  if ((e.key === "+" || e.key === "=") && !e.ctrlKey) { setCardSize(SIZE_STEPS.indexOf(S.size) + 1); return; }
  if ((e.key === "-" || e.key === "_") && !e.ctrlKey) { setCardSize(SIZE_STEPS.indexOf(S.size) - 1); return; }
  if (!isGridView() || S.view === "series") return;
  const idx = S.focus != null ? S.flat.findIndex((v) => v.id === S.focus) : -1;
  if (e.key === "Enter" && S.focus != null) { e.preventDefault(); play(S.focus); return; }
  if ((e.key === "f" || e.key === "F") && S.focus != null) { const ids = S.sel.size ? [...S.sel] : [S.focus]; toggleFav(ids, !ids.every((i) => S.byId.get(i)?.fav)).then(() => openDetails(S.focus)); return; }
  if ((e.key === "e" || e.key === "E") && S.focus != null) { openEditor(S.sel.size ? [...S.sel] : [S.focus]); return; }
  if ((e.key === "l" || e.key === "L" || e.key === "d" || e.key === "D") && S.focus != null && !e.ctrlKey) {
    const ids = S.sel.size ? [...S.sel] : [S.focus];
    const want = e.key.toLowerCase() === "l" ? 1 : -1;
    const st = rateState(ids.flatMap((i) => songVideos(S.byId.get(i))));
    rateSongs(ids, want === 1 ? (st.liked ? 0 : 1) : (st.disliked ? 0 : -1));
    return;
  }
  if (e.key === "a" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); S.sel = new Set(S.flat.map((v) => v.id)); refreshSelectionClasses(); return; }
  const moves = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: V.cols, ArrowUp: -V.cols };
  if (moves[e.key] !== undefined && S.flat.length) {
    e.preventDefault();
    const n = Math.max(0, Math.min(S.flat.length - 1, idx < 0 ? 0 : idx + moves[e.key]));
    ensureVisible(n);
    select(S.flat[n].id, e.shiftKey ? { shiftKey: true } : null);
  }
});

/* ------------------------------------------------------------------ boot */
(async function boot() {
  for (const x of $$("#typeSeg button")) x.classList.toggle("on", x.dataset.type === "");
  try { S.boot = await api.get("/api/bootstrap"); }
  catch (e) { document.body.insertAdjacentHTML("beforeend", `<div class="toasts"><div class="toast err">Cannot reach the Kanade backend: ${esc(e.message)}</div></div>`); return; }
  S.settings = S.boot.settings;
  S.counts = S.boot.counts;
  updateCounts();
  if (!S.boot.tools.ok) toast(S.boot.tools.help, true);
  if (!S.settings.archive_root || !S.boot.root_exists) {
    S.view = "setup"; render(false);
  } else {
    setView("all", { noHistory: true });
    await loadData(false);
    loadRecs();
  }
  poll();
})();
