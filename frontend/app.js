(() => {
  const byId = (id) => document.getElementById(id);
  const dom = {
    form: byId("search-form"),
    urlInput: byId("track-url"),
    limit: byId("limit"),
    reloadBtn: byId("reload-btn"),
    discoverMoreBtn: byId("discover-more-btn"),
    results: byId("results"),
    loading: byId("loading"),
    error: byId("error"),
    seedTrack: byId("seed-track"),
    filterPanel: byId("filter-panel"),
    quickFilters: byId("quick-filters"),
    tagSections: byId("tag-sections"),
    activeFilterCount: byId("active-filter-count"),
    popularityMin: byId("popularity-min"),
    popularityMax: byId("popularity-max"),
    releaseYearMin: byId("release-year-min"),
    releaseYearMax: byId("release-year-max"),
    bpmFilter: byId("bpm-filter"),
    bpmTolerance: byId("bpm-tolerance"),
    bpmLabel: byId("bpm-label"),
    instrumentalOnly: byId("filter-instrumental-only"),
    vocalOnly: byId("filter-vocal-only"),
    advancedFilters: byId("advanced-filters"),
    resetFiltersBtn: byId("reset-filters-btn"),
    loginBtn: byId("spotify-login-btn"),
    logoutBtn: byId("spotify-logout-btn"),
    spotifyUser: byId("spotify-user"),
    addQueueBtn: byId("add-queue-btn"),
    actionStatus: byId("action-status"),
    actionsBar: byId("actions-bar"),
    boardPanel: byId("memory-board-panel"),
    boardCount: byId("memory-board-count"),
    boardList: byId("memory-board-list"),
    boardStatus: byId("memory-board-status"),
    boardAddVisibleBtn: byId("board-add-visible-btn"),
    boardClearBtn: byId("board-clear-btn"),
    boardCopyBtn: byId("board-copy-btn"),
    boardPlaylistTarget: byId("board-playlist-target"),
    boardPlaylistName: byId("board-playlist-name"),
    boardExistingPlaylist: byId("board-existing-playlist"),
    boardCreatePlaylistBtn: byId("board-create-playlist-btn"),
    drillPanel: byId("drill-panel"),
    drillCloseBtn: byId("drill-close-btn"),
    drillBreadcrumbs: byId("drill-breadcrumbs")
  };

  const STORAGE_KEYS = {
    board: "catid.memoryBoard.v1",
    uriCache: "catid.uriCache.v1"
  };

  const CORE_ADVANCED_EXCLUDES = new Set([
    "tempo", "bpm", "popularity", "release_year",
    "instrumentalness", "energy", "danceability",
    "valence", "acousticness"
  ]);

  const state = {
    lastQueryUrl: "",
    seed: null,
    tracks: [],
    breadcrumbs: [],
    seenTrackKeys: new Set(),
    spotifyConnected: false,
    playlists: [],
    board: loadJson(STORAGE_KEYS.board, []),
    uriCache: loadJson(STORAGE_KEYS.uriCache, {}),
    tagCategories: {},
    filters: {
      selectedTags: new Set(),
      instrumentalOnly: false,
      vocalOnly: false,
      advanced: {}
    },
    advancedSchema: {}
  };

  function loadJson(key, fallback) {
    try {
      const raw = window.localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (_) {
      return fallback;
    }
  }

  function saveJson(key, value) {
    window.localStorage.setItem(key, JSON.stringify(value));
  }

  function esc(value) {
    const div = document.createElement("div");
    div.textContent = String(value ?? "");
    return div.innerHTML;
  }

  function normalizeTag(tag) {
    return String(tag || "").trim().toLowerCase();
  }

  function trackKey(track) {
    return `${(track.artists?.[0] || "").trim().toLowerCase()}::${(track.name || "").trim().toLowerCase()}`;
  }

  function boardKey(item) {
    return `${(item.artist || "").trim().toLowerCase()}::${(item.title || "").trim().toLowerCase()}`;
  }

  function getWeights() {
    const weights = {
      tempo: 0.5,
      energy: 0.5,
      valence: 0.5,
      danceability: 0.5,
      acousticness: 0.5,
      instrumentalness: 0.5
    };
    document.querySelectorAll(".weight-row").forEach((row) => {
      const key = row.dataset.key;
      const input = row.querySelector("input[type='range']");
      const label = row.querySelector(".weight-val");
      const value = Number(input?.value || "50");
      if (key && Object.prototype.hasOwnProperty.call(weights, key)) {
        weights[key] = Math.max(0, Math.min(1, value / 100));
      }
      if (label) label.textContent = `${value}%`;
    });
    return weights;
  }

  function getCoreFilterValues() {
    const popMin = Number(dom.popularityMin?.value ?? 0);
    const popMax = Number(dom.popularityMax?.value ?? 100);
    const yearMin = Number(dom.releaseYearMin?.value ?? 1900);
    const yearMax = Number(dom.releaseYearMax?.value ?? 2100);
    return {
      popMin: Number.isFinite(popMin) ? popMin : 0,
      popMax: Number.isFinite(popMax) ? popMax : 100,
      yearMin: Number.isFinite(yearMin) ? yearMin : 1900,
      yearMax: Number.isFinite(yearMax) ? yearMax : 2100,
      bpmTolerancePct: Number(dom.bpmTolerance?.value ?? 100),
      instrumentalOnly: Boolean(dom.instrumentalOnly?.checked),
      vocalOnly: Boolean(dom.vocalOnly?.checked)
    };
  }

  function getBackendFiltersPayload() {
    const core = getCoreFilterValues();
    const payload = {
      bpm_min: null,
      bpm_max: null,
      popularity_min: core.popMin,
      popularity_max: core.popMax,
      release_year_min: core.yearMin,
      release_year_max: core.yearMax,
      tags_any: Array.from(state.filters.selectedTags),
      require_instrumental: core.instrumentalOnly && !core.vocalOnly ? true : (core.vocalOnly && !core.instrumentalOnly ? false : null)
    };
    if (state.seed?.bpm != null && core.bpmTolerancePct < 100) {
      const tolerance = (state.seed.bpm * core.bpmTolerancePct) / 100;
      payload.bpm_min = Math.max(0, state.seed.bpm - tolerance);
      payload.bpm_max = state.seed.bpm + tolerance;
    }
    return payload;
  }

  function activeFilterCount() {
    const core = getCoreFilterValues();
    let count = 0;
    if (core.popMin > 0 || core.popMax < 100) count += 1;
    if (core.yearMin > 1900 || core.yearMax < 2100) count += 1;
    if (core.bpmTolerancePct < 100) count += 1;
    if (state.filters.selectedTags.size > 0) count += 1;
    if (core.instrumentalOnly || core.vocalOnly) count += 1;
    Object.values(state.filters.advanced).forEach((value) => {
      if (value == null) return;
      if (typeof value === "object" && ("min" in value || "max" in value)) {
        if (value.min != null || value.max != null) count += 1;
      } else if (value !== "") {
        count += 1;
      }
    });
    return count;
  }

  function renderActiveFilterCount() {
    const count = activeFilterCount();
    dom.activeFilterCount.textContent = count > 0 ? `(${count} active)` : "";
  }

  function renderSeed() {
    if (!state.seed) {
      dom.seedTrack.classList.add("hidden");
      return;
    }
    const artists = (state.seed.artists || []).join(", ");
    const bpm = state.seed.bpm != null ? ` · ${Math.round(state.seed.bpm)} BPM` : "";
    dom.seedTrack.innerHTML = `<strong>Seed:</strong> ${esc(artists)} - ${esc(state.seed.name || "")}${esc(bpm)}`;
    dom.seedTrack.classList.remove("hidden");
  }

  function renderBreadcrumbs() {
    const hasTrail = state.breadcrumbs.length > 0;
    dom.drillPanel.classList.toggle("hidden", !hasTrail);
    if (!hasTrail) {
      dom.drillBreadcrumbs.innerHTML = "";
      return;
    }
    dom.drillBreadcrumbs.innerHTML = state.breadcrumbs
      .map((crumb, idx) => `<button type="button" class="crumb-btn" data-crumb-index="${idx}">${esc(crumb.label)}</button>`)
      .join(" <span>→</span> ");
  }

  function renderTagFilters() {
    const tagsByCategory = {};
    const uncategorized = new Set();
    const all = new Set();
    state.tracks.forEach((track) => {
      (track.tags || []).forEach((tagRaw) => {
        const tag = normalizeTag(tagRaw);
        if (!tag) return;
        all.add(tag);
        const category = state.tagCategories[tag];
        if (!category) {
          uncategorized.add(tag);
          return;
        }
        if (!tagsByCategory[category]) tagsByCategory[category] = new Set();
        tagsByCategory[category].add(tag);
      });
    });
    const allTags = Array.from(all).sort();
    dom.quickFilters.innerHTML = allTags
      .slice(0, 20)
      .map((tag) => `<button type="button" class="tag-chip ${state.filters.selectedTags.has(tag) ? "active" : ""}" data-tag="${esc(tag)}">${esc(tag)}</button>`)
      .join("");
    const sections = Object.entries(tagsByCategory)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([category, values]) => {
        const chips = Array.from(values).sort().map((tag) => `<button type="button" class="tag-chip ${state.filters.selectedTags.has(tag) ? "active" : ""}" data-tag="${esc(tag)}">${esc(tag)}</button>`).join("");
        return `<div class="tag-section"><div class="tag-section-label">${esc(category)}</div><div class="tag-chips">${chips}</div></div>`;
      });
    if (uncategorized.size > 0) {
      const chips = Array.from(uncategorized).sort().map((tag) => `<button type="button" class="tag-chip ${state.filters.selectedTags.has(tag) ? "active" : ""}" data-tag="${esc(tag)}">${esc(tag)}</button>`).join("");
      sections.push(`<div class="tag-section"><div class="tag-section-label">other</div><div class="tag-chips">${chips}</div></div>`);
    }
    dom.tagSections.innerHTML = sections.join("");
    byId("tag-filter")?.classList.toggle("hidden", allTags.length === 0);
  }

  function numericValue(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  function readTrackMetric(track, key) {
    if (key === "bpm" || key === "tempo") return numericValue(track.bpm) ?? numericValue(track.analysis_metrics?.tempo) ?? numericValue(track.audio_features?.tempo);
    if (Object.prototype.hasOwnProperty.call(track, key)) return track[key];
    if (track.audio_features && Object.prototype.hasOwnProperty.call(track.audio_features, key)) return track.audio_features[key];
    if (track.analysis_metrics && Object.prototype.hasOwnProperty.call(track.analysis_metrics, key)) return track.analysis_metrics[key];
    return null;
  }

  function buildAdvancedSchema() {
    const schema = {};
    state.tracks.forEach((track) => {
      const metrics = track.analysis_metrics || {};
      Object.keys(metrics).forEach((key) => {
        if (CORE_ADVANCED_EXCLUDES.has(key)) return;
        const value = metrics[key];
        if (value == null) return;
        if (!schema[key]) {
          schema[key] = { type: null, min: null, max: null, options: new Set() };
        }
        if (typeof value === "number" && Number.isFinite(value)) {
          schema[key].type = "number";
          schema[key].min = schema[key].min == null ? value : Math.min(schema[key].min, value);
          schema[key].max = schema[key].max == null ? value : Math.max(schema[key].max, value);
        } else if (typeof value === "boolean") {
          schema[key].type = "boolean";
        } else if (typeof value === "string") {
          schema[key].type = "string";
          schema[key].options.add(value);
        }
      });
    });
    Object.keys(schema).forEach((key) => {
      const definition = schema[key];
      if (definition.type === "string" && definition.options.size > 20) {
        delete schema[key];
      }
    });
    state.advancedSchema = schema;
    Object.keys(schema).forEach((key) => {
      if (state.filters.advanced[key] != null) return;
      if (schema[key].type === "number") state.filters.advanced[key] = { min: null, max: null };
      else if (schema[key].type === "boolean") state.filters.advanced[key] = null;
      else state.filters.advanced[key] = "";
    });
  }

  function renderAdvancedFilters() {
    const keys = Object.keys(state.advancedSchema).sort();
    if (keys.length === 0) {
      dom.advancedFilters.innerHTML = "<p class=\"action-status\">No additional metadata fields found for this result set.</p>";
      return;
    }
    dom.advancedFilters.innerHTML = keys.map((key) => {
      const def = state.advancedSchema[key];
      const label = key.replace(/_/g, " ");
      const current = state.filters.advanced[key];
      if (def.type === "number") {
        const minVal = current?.min == null ? "" : String(current.min);
        const maxVal = current?.max == null ? "" : String(current.max);
        return `<div class="advanced-item" data-adv-key="${esc(key)}"><label>${esc(label)}</label><div class="advanced-range"><input type="number" data-adv-role="min" placeholder="min (${esc(def.min)})" value="${esc(minVal)}" /><input type="number" data-adv-role="max" placeholder="max (${esc(def.max)})" value="${esc(maxVal)}" /></div></div>`;
      }
      if (def.type === "boolean") {
        return `<div class="advanced-item" data-adv-key="${esc(key)}"><label>${esc(label)}</label><select data-adv-role="bool"><option value="" ${current == null ? "selected" : ""}>Any</option><option value="true" ${current === true ? "selected" : ""}>True</option><option value="false" ${current === false ? "selected" : ""}>False</option></select></div>`;
      }
      const options = Array.from(def.options).sort().map((opt) => `<option value="${esc(opt)}" ${current === opt ? "selected" : ""}>${esc(opt)}</option>`).join("");
      return `<div class="advanced-item" data-adv-key="${esc(key)}"><label>${esc(label)}</label><select data-adv-role="string"><option value="">Any</option>${options}</select></div>`;
    }).join("");
  }

  function passesCoreFilters(track) {
    const core = getCoreFilterValues();
    if (track.popularity != null && (track.popularity < core.popMin || track.popularity > core.popMax)) return false;
    if (track.release_year != null && (track.release_year < core.yearMin || track.release_year > core.yearMax)) return false;
    if (state.filters.selectedTags.size > 0) {
      const tags = new Set((track.tags || []).map((tag) => normalizeTag(tag)));
      const matches = Array.from(state.filters.selectedTags).some((tag) => tags.has(tag));
      if (!matches) return false;
    }
    if (state.seed?.bpm != null && track.bpm != null && core.bpmTolerancePct < 100) {
      const tolerance = (state.seed.bpm * core.bpmTolerancePct) / 100;
      if (Math.abs(track.bpm - state.seed.bpm) > tolerance) return false;
    }
    const inst = numericValue(track.audio_features?.instrumentalness)
      ?? numericValue(track.analysis_metrics?.instrumentalness);
    const trackTags = new Set((track.tags || []).map((tag) => normalizeTag(tag)));
    const tagSaysInstrumental = trackTags.has("instrumental") || trackTags.has("ambient");
    const tagSaysVocal = trackTags.has("vocal") || trackTags.has("vocals");
    if (core.instrumentalOnly && !core.vocalOnly) {
      const pass = (inst != null && inst >= 0.6) || tagSaysInstrumental;
      if (!pass) return false;
    }
    if (core.vocalOnly && !core.instrumentalOnly) {
      const pass = (inst != null && inst <= 0.4) || tagSaysVocal;
      if (!pass) return false;
    }
    return true;
  }

  function passesAdvancedFilters(track) {
    return Object.entries(state.advancedSchema).every(([key, def]) => {
      const selected = state.filters.advanced[key];
      if (def.type === "number") {
        if (selected?.min == null && selected?.max == null) return true;
        const value = readTrackMetric(track, key);
        if (typeof value !== "number" || !Number.isFinite(value)) return false;
        if (selected.min != null && value < selected.min) return false;
        if (selected.max != null && value > selected.max) return false;
        return true;
      }
      if (def.type === "boolean") {
        if (selected == null) return true;
        const value = readTrackMetric(track, key);
        return value === selected;
      }
      if (!selected) return true;
      return String(readTrackMetric(track, key) || "") === selected;
    });
  }

  function filteredTracks() {
    return state.tracks.filter((track) => passesCoreFilters(track) && passesAdvancedFilters(track));
  }

  function renderResults() {
    const tracks = filteredTracks();
    dom.actionsBar.classList.toggle("hidden", tracks.length === 0);
    dom.results.innerHTML = tracks.length > 0
      ? tracks.map((track, index) => {
        const artists = (track.artists || []).join(", ");
        const key = trackKey(track);
        return `<article class="track-card"><div class="track-info"><div class="name">${index + 1}. ${esc(track.name || "")}</div><div class="detail">${esc(artists)}</div><div class="detail">${esc(track.album || "")}</div></div><div class="track-actions-inline"><button type="button" class="action-btn" data-action="queue" data-track-key="${esc(key)}">+ Queue</button><button type="button" class="action-btn" data-action="board" data-track-key="${esc(key)}">+ Board</button><button type="button" class="action-btn" data-action="drill" data-track-key="${esc(key)}">Drill Down</button>${track.spotify_url ? `<a class="action-btn" href="${esc(track.spotify_url)}" target="_blank" rel="noopener noreferrer">Open</a>` : ""}</div></article>`;
      }).join("")
      : "<p>No results found with current filters.</p>";
    dom.discoverMoreBtn.classList.toggle("hidden", !state.lastQueryUrl || state.tracks.length === 0);
  }

  function toBoardItem(track) {
    return {
      title: track.name || "",
      artist: (track.artists || [])[0] || "",
      spotifyUri: track.spotify_id ? `spotify:track:${track.spotify_id}` : null,
      source: "similarity_search",
      addedAt: new Date().toISOString(),
      metadata: { album: track.album || null }
    };
  }

  function renderBoard() {
    dom.boardPanel.classList.remove("hidden");
    dom.boardCount.textContent = String(state.board.length);
    dom.boardList.innerHTML = state.board.length > 0
      ? state.board.map((item) => `<div class="queue-item"><div><strong>${esc(item.artist)} - ${esc(item.title)}</strong></div><button type="button" class="action-btn" data-board-remove="${esc(boardKey(item))}">Remove</button></div>`).join("")
      : "<p>Memory Board is empty.</p>";
  }

  function addBoardItems(items) {
    const byKey = new Map(state.board.map((item) => [boardKey(item), item]));
    items.forEach((item) => byKey.set(boardKey(item), item));
    state.board = Array.from(byKey.values());
    saveJson(STORAGE_KEYS.board, state.board);
    renderBoard();
  }

  async function checkSpotify() {
    try {
      const response = await fetch("/api/spotify/status");
      const data = await response.json();
      state.spotifyConnected = data.connected === true;
      dom.loginBtn.classList.toggle("hidden", state.spotifyConnected);
      dom.logoutBtn.classList.toggle("hidden", !state.spotifyConnected);
      dom.spotifyUser.classList.toggle("hidden", !state.spotifyConnected);
      dom.spotifyUser.textContent = state.spotifyConnected ? data.user || "Spotify connected" : "";
      if (state.spotifyConnected) await loadPlaylists();
    } catch (_) {
      state.spotifyConnected = false;
    }
  }

  async function loadPlaylists() {
    if (!state.spotifyConnected) return;
    try {
      const response = await fetch("/api/spotify/playlists");
      if (!response.ok) return;
      const data = await response.json();
      state.playlists = Array.isArray(data) ? data : [];
      dom.boardExistingPlaylist.innerHTML = state.playlists.map((playlist) => `<option value="${esc(playlist.id)}">${esc(playlist.name)}</option>`).join("");
    } catch (_) {}
  }

  function setError(message) {
    dom.error.textContent = message;
    dom.error.classList.remove("hidden");
  }

  function clearError() {
    dom.error.classList.add("hidden");
    dom.error.textContent = "";
  }

  async function fetchUnified(queryUrl, options = {}) {
    const payload = {
      url: queryUrl,
      limit: Number(dom.limit?.value || 20),
      exclude: options.exclude ?? [],
      strict_mapped_only: false,
      use_metadata_fallback: true,
      weights: getWeights(),
      filters: getBackendFiltersPayload()
    };
    const response = await fetch("/api/similar/unified", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.detail?.message || data.detail || `Request failed (${response.status})`);
    }
    return data;
  }

  async function runSearch(queryUrl, options = {}) {
    if (!queryUrl) return;
    clearError();
    dom.loading.classList.remove("hidden");
    try {
      const data = await fetchUnified(queryUrl, options);
      const incoming = Array.isArray(data.similar_tracks) ? data.similar_tracks : [];
      if (options.append) {
        const byKey = new Map(state.tracks.map((track) => [trackKey(track), track]));
        incoming.forEach((track) => byKey.set(trackKey(track), track));
        state.tracks = Array.from(byKey.values());
      } else {
        state.tracks = incoming;
      }
      state.seed = data.seed_track || state.seed;
      state.tagCategories = data.tag_categories || {};
      state.lastQueryUrl = queryUrl;
      state.tracks.forEach((track) => state.seenTrackKeys.add(trackKey(track)));
      if (!options.skipBreadcrumbPush && state.seed) {
        const seedLabel = `${(state.seed.artists || []).join(", ")} - ${state.seed.name || ""}`;
        const last = state.breadcrumbs[state.breadcrumbs.length - 1];
        if (!last || last.label !== seedLabel) state.breadcrumbs.push({ url: queryUrl, label: seedLabel });
      }
      dom.filterPanel.classList.remove("hidden");
      dom.bpmFilter.classList.toggle("hidden", state.seed?.bpm == null);
      dom.bpmLabel.textContent = dom.bpmTolerance.value === "100" ? "Any" : `±${dom.bpmTolerance.value}%`;
      buildAdvancedSchema();
      renderSeed();
      renderTagFilters();
      renderAdvancedFilters();
      renderBreadcrumbs();
      renderActiveFilterCount();
      renderResults();
    } catch (error) {
      setError(error.message || "Search failed");
    } finally {
      dom.loading.classList.add("hidden");
    }
  }

  async function queueTrack(uri) {
    if (!uri) {
      dom.actionStatus.textContent = "Track has no Spotify URI.";
      return;
    }
    try {
      const response = await fetch("/api/spotify/queue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ track_uris: [uri] })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail?.message || data.detail || "Queue failed");
      dom.actionStatus.textContent = data.message || "Added to queue.";
    } catch (error) {
      dom.actionStatus.textContent = error.message || "Queue failed";
    }
  }

  async function queueVisible() {
    const uris = filteredTracks().map((track) => (track.spotify_id ? `spotify:track:${track.spotify_id}` : null)).filter(Boolean);
    if (!uris.length) {
      dom.actionStatus.textContent = "No mappable tracks in current view.";
      return;
    }
    try {
      const response = await fetch("/api/spotify/queue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ track_uris: uris })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail?.message || data.detail || "Queue failed");
      dom.actionStatus.textContent = data.message || `Queued ${uris.length} track(s).`;
    } catch (error) {
      dom.actionStatus.textContent = error.message || "Queue failed";
    }
  }

  async function resolveUri(item) {
    if (item.spotifyUri) return item.spotifyUri;
    const key = boardKey(item);
    if (state.uriCache[key]) return state.uriCache[key];
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const query = encodeURIComponent(`artist:${item.artist} track:${item.title}`);
      const response = await fetch(`/api/spotify/search-track?q=${query}`);
      if (response.status === 429) {
        const retryAfter = Number(response.headers.get("Retry-After") || "1");
        dom.boardStatus.textContent = `Rate limited, waiting ${retryAfter}s...`;
        await new Promise((resolve) => window.setTimeout(resolve, retryAfter * 1000 * (attempt + 1)));
        continue;
      }
      if (!response.ok) return null;
      const data = await response.json().catch(() => ({}));
      if (!data.spotify_uri) return null;
      state.uriCache[key] = data.spotify_uri;
      saveJson(STORAGE_KEYS.uriCache, state.uriCache);
      return data.spotify_uri;
    }
    return null;
  }

  async function createPlaylistFromBoard() {
    if (!state.spotifyConnected) {
      dom.boardStatus.textContent = "Connect Spotify first.";
      return;
    }
    if (state.board.length === 0) {
      dom.boardStatus.textContent = "Memory Board is empty.";
      return;
    }
    const resolved = [];
    const unresolved = [];
    for (let i = 0; i < state.board.length; i += 1) {
      const item = state.board[i];
      dom.boardStatus.textContent = `Resolving track ${i + 1} of ${state.board.length}...`;
      const uri = await resolveUri(item);
      if (uri) resolved.push(uri);
      else unresolved.push(`${item.artist} - ${item.title}`);
    }
    if (resolved.length === 0) {
      dom.boardStatus.textContent = "No tracks could be resolved to Spotify URIs.";
      return;
    }
    const target = dom.boardPlaylistTarget.value;
    try {
      let playlistUrl = "";
      if (target === "existing") {
        const playlistId = dom.boardExistingPlaylist.value;
        const response = await fetch(`/api/spotify/playlists/${playlistId}/tracks`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ track_uris: resolved })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail?.message || data.detail || "Could not add tracks");
        playlistUrl = playlistId ? `https://open.spotify.com/playlist/${playlistId}` : "";
      } else {
        const response = await fetch("/api/spotify/playlist", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: (dom.boardPlaylistName.value || "Cat-ID Memory Board").trim(),
            track_uris: resolved,
            public: false
          })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail?.message || data.detail || "Could not create playlist");
        playlistUrl = data.playlist_url || "";
      }
      const message = unresolved.length > 0
        ? `Playlist updated (${resolved.length} added, ${unresolved.length} unresolved).`
        : `Playlist updated (${resolved.length} added).`;
      dom.boardStatus.textContent = message;
      if (playlistUrl) {
        dom.boardStatus.innerHTML = `${esc(message)} <a href="${esc(playlistUrl)}" target="_blank" rel="noopener noreferrer">Open in Spotify</a>`;
      }
    } catch (error) {
      dom.boardStatus.textContent = error.message || "Playlist operation failed.";
    }
  }

  async function copyBoardText() {
    const text = state.board.map((item) => `${item.artist} - ${item.title}`).join("\n");
    if (!text) {
      dom.boardStatus.textContent = "Nothing to copy.";
      return;
    }
    await navigator.clipboard.writeText(text);
    dom.boardStatus.textContent = "Copied board tracks as text.";
  }

  function resetFilters() {
    dom.popularityMin.value = "0";
    dom.popularityMax.value = "100";
    dom.releaseYearMin.value = "1900";
    dom.releaseYearMax.value = "2100";
    dom.bpmTolerance.value = "100";
    dom.bpmLabel.textContent = "Any";
    dom.instrumentalOnly.checked = false;
    dom.vocalOnly.checked = false;
    state.filters.selectedTags.clear();
    Object.keys(state.filters.advanced).forEach((key) => {
      const def = state.advancedSchema[key];
      state.filters.advanced[key] = def?.type === "number" ? { min: null, max: null } : (def?.type === "boolean" ? null : "");
    });
    renderTagFilters();
    renderAdvancedFilters();
    renderActiveFilterCount();
    renderResults();
  }

  function onResultsClick(event) {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    const track = state.tracks.find((item) => trackKey(item) === target.dataset.trackKey);
    if (!track) return;
    if (target.dataset.action === "queue") {
      queueTrack(track.spotify_id ? `spotify:track:${track.spotify_id}` : "");
      return;
    }
    if (target.dataset.action === "board") {
      addBoardItems([toBoardItem(track)]);
      dom.boardStatus.textContent = "Added to Memory Board.";
      return;
    }
    if (target.dataset.action === "drill") {
      const nextSeed = track.spotify_url || (track.spotify_id ? `https://open.spotify.com/track/${track.spotify_id}` : "");
      if (!nextSeed) {
        setError("Cannot drill down on a track without Spotify ID.");
        return;
      }
      runSearch(nextSeed);
    }
  }

  dom.form.addEventListener("submit", (event) => {
    event.preventDefault();
    const url = dom.urlInput.value.trim();
    state.breadcrumbs = [];
    state.seenTrackKeys.clear();
    runSearch(url, { exclude: [] });
  });
  dom.reloadBtn.addEventListener("click", () => runSearch(state.lastQueryUrl || dom.urlInput.value.trim(), { skipBreadcrumbPush: true }));
  dom.discoverMoreBtn.addEventListener("click", () => runSearch(state.lastQueryUrl || dom.urlInput.value.trim(), {
    append: true,
    skipBreadcrumbPush: true,
    exclude: Array.from(state.seenTrackKeys)
  }));
  dom.results.addEventListener("click", onResultsClick);
  dom.quickFilters.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-tag]");
    if (!chip) return;
    const tag = normalizeTag(chip.dataset.tag);
    if (!tag) return;
    if (state.filters.selectedTags.has(tag)) state.filters.selectedTags.delete(tag);
    else state.filters.selectedTags.add(tag);
    renderTagFilters();
    renderActiveFilterCount();
    renderResults();
  });
  dom.tagSections.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-tag]");
    if (!chip) return;
    const tag = normalizeTag(chip.dataset.tag);
    if (!tag) return;
    if (state.filters.selectedTags.has(tag)) state.filters.selectedTags.delete(tag);
    else state.filters.selectedTags.add(tag);
    renderTagFilters();
    renderActiveFilterCount();
    renderResults();
  });
  [dom.popularityMin, dom.popularityMax, dom.releaseYearMin, dom.releaseYearMax, dom.instrumentalOnly, dom.vocalOnly].forEach((element) => {
    element?.addEventListener("input", () => {
      renderActiveFilterCount();
      renderResults();
    });
    element?.addEventListener("change", () => {
      renderActiveFilterCount();
      renderResults();
    });
  });
  dom.bpmTolerance.addEventListener("input", () => {
    dom.bpmLabel.textContent = dom.bpmTolerance.value === "100" ? "Any" : `±${dom.bpmTolerance.value}%`;
    renderActiveFilterCount();
    renderResults();
  });
  dom.advancedFilters.addEventListener("input", (event) => {
    const container = event.target.closest("[data-adv-key]");
    if (!container) return;
    const key = container.dataset.advKey;
    const def = state.advancedSchema[key];
    if (!def) return;
    if (def.type === "number") {
      const minInput = container.querySelector("[data-adv-role='min']");
      const maxInput = container.querySelector("[data-adv-role='max']");
      const min = minInput?.value === "" ? null : Number(minInput.value);
      const max = maxInput?.value === "" ? null : Number(maxInput.value);
      state.filters.advanced[key] = {
        min: Number.isFinite(min) ? min : null,
        max: Number.isFinite(max) ? max : null
      };
    } else if (def.type === "boolean") {
      const value = container.querySelector("[data-adv-role='bool']")?.value || "";
      state.filters.advanced[key] = value === "" ? null : value === "true";
    } else {
      state.filters.advanced[key] = container.querySelector("[data-adv-role='string']")?.value || "";
    }
    renderActiveFilterCount();
    renderResults();
  });
  dom.resetFiltersBtn.addEventListener("click", resetFilters);
  dom.loginBtn.addEventListener("click", () => { window.location.href = "/api/spotify/login"; });
  dom.logoutBtn.addEventListener("click", async () => {
    await fetch("/api/spotify/logout", { method: "POST" });
    await checkSpotify();
  });
  dom.addQueueBtn.addEventListener("click", queueVisible);

  dom.boardAddVisibleBtn.addEventListener("click", () => {
    addBoardItems(filteredTracks().map(toBoardItem));
    dom.boardStatus.textContent = "Added visible tracks to Memory Board.";
  });
  dom.boardClearBtn.addEventListener("click", () => {
    state.board = [];
    saveJson(STORAGE_KEYS.board, state.board);
    renderBoard();
    dom.boardStatus.textContent = "Memory Board cleared.";
  });
  dom.boardCopyBtn.addEventListener("click", () => {
    copyBoardText().catch(() => { dom.boardStatus.textContent = "Clipboard copy failed."; });
  });
  dom.boardCreatePlaylistBtn.addEventListener("click", () => { createPlaylistFromBoard(); });
  dom.boardPlaylistTarget.addEventListener("change", () => {
    const useExisting = dom.boardPlaylistTarget.value === "existing";
    dom.boardExistingPlaylist.classList.toggle("hidden", !useExisting);
    dom.boardPlaylistName.classList.toggle("hidden", useExisting);
  });
  dom.boardList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-board-remove]");
    if (!button) return;
    state.board = state.board.filter((item) => boardKey(item) !== button.dataset.boardRemove);
    saveJson(STORAGE_KEYS.board, state.board);
    renderBoard();
  });
  dom.drillBreadcrumbs.addEventListener("click", (event) => {
    const button = event.target.closest("[data-crumb-index]");
    if (!button) return;
    const idx = Number(button.dataset.crumbIndex);
    if (!Number.isInteger(idx) || idx < 0 || idx >= state.breadcrumbs.length) return;
    const crumb = state.breadcrumbs[idx];
    state.breadcrumbs = state.breadcrumbs.slice(0, idx + 1);
    runSearch(crumb.url, { skipBreadcrumbPush: true });
  });
  dom.drillCloseBtn.addEventListener("click", () => {
    state.breadcrumbs = [];
    renderBreadcrumbs();
  });

  renderBoard();
  renderActiveFilterCount();
  checkSpotify();
})();
