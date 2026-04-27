(() => {
  const form = document.getElementById("search-form");
  const urlInput = document.getElementById("track-url");
  const searchBtn = document.getElementById("search-btn");
  const seedEl = document.getElementById("seed-track");
  const resultsEl = document.getElementById("results");
  const errorEl = document.getElementById("error");
  const loadingEl = document.getElementById("loading");
  const filterPanel = document.getElementById("filter-panel");
  const bpmFilter = document.getElementById("bpm-filter");
  const bpmSlider = document.getElementById("bpm-tolerance");
  const bpmLabel = document.getElementById("bpm-label");
  const tagFilter = document.getElementById("tag-filter");
  const tagSections = document.getElementById("tag-sections");
  const quickFiltersEl = document.getElementById("quick-filters");
  const actionsBar = document.getElementById("actions-bar");
  const savePlaylistBtn = document.getElementById("save-playlist-btn");
  const addQueueBtn = document.getElementById("add-queue-btn");
  const actionStatus = document.getElementById("action-status");
  const spotifyLoginBtn = document.getElementById("spotify-login-btn");
  const spotifyUserEl = document.getElementById("spotify-user");
  const spotifyLogoutBtn = document.getElementById("spotify-logout-btn");
  const reloadBtn = document.getElementById("reload-btn");
  const audioWeightsPanel = document.getElementById("audio-weights");
  const weightRows = audioWeightsPanel.querySelectorAll(".weight-row");
  const modeBtns = document.querySelectorAll(".mode-btn");
  const drillPanel = document.getElementById("drill-panel");
  const drillBreadcrumbs = document.getElementById("drill-breadcrumbs");
  const drillProfile = document.getElementById("drill-profile");
  const drillCloseBtn = document.getElementById("drill-close-btn");
  const drillSidePanel = document.getElementById("drill-side-panel");
  const drillSideContent = document.getElementById("drill-side-content");

  let currentAudio = null;
  let spotifyConnected = false;
  let currentMode = "audio";

  let allTracks = [];
  let seedTrack = null;
  let seedTags = [];
  let tagCategories = {};
  let selectedTags = new Set();
  let displayLimit = 50;
  let approximated = false;
  let seenTrackKeys = new Set();
  let exhausted = false;
  let softFiltering = false;
  let traceHistory = [];
  let visibleTracks = [];
  let mappedCount = 0;
  let unmappedCount = 0;
  let mappingDegradedReason = null;
  let activeRequestId = 0;
  let activeController = null;
  let isRequestInFlight = false;
  let lastSeedKey = "";

  const discoverMoreBtn = document.getElementById("discover-more-btn");

  const CATEGORY_LABELS = {
    mood: "Mood",
    genre: "Style",
    vocals_instrumentals: "Vocals / Instrumentation",
  };

  const QUICK_FILTERS = [
    { label: "Chill mood", tags: ["chill", "chillout", "relaxing", "calm", "ambient", "peaceful", "dreamy", "atmospheric"] },
    { label: "More instrumental", tags: ["instrumental", "no vocal", "no vocals"] },
    { label: "More vocal", tags: ["female vocal", "male vocal", "vocals", "vocal", "female vocals", "male vocals"] },
  ];
  const TAG_ALIASES = {
    "female vocals": "female vocal",
    "male vocals": "male vocal",
    "no vocals": "no vocal",
  };
  const INSTRUMENTAL_TAGS = new Set(["instrumental", "no vocal"]);
  const VOCAL_TAGS = new Set(["vocal", "vocals", "female vocal", "male vocal"]);

  const AUDIO_DIMENSION_LABELS = {
    tempo: "BPM",
    energy: "Energy",
    valence: "Valence",
    danceability: "Dance",
    acousticness: "Acoustic",
    instrumentalness: "Instrumental",
  };

  function normalizeTag(tag) {
    const normalized = String(tag || "").trim().toLowerCase().replace(/\s+/g, " ");
    return TAG_ALIASES[normalized] || normalized;
  }

  function normalizedTagSet(tags) {
    return new Set((tags || []).map(normalizeTag));
  }

  function countTagOverlap(trackTags, selected) {
    let overlap = 0;
    for (const tag of selected) {
      if (trackTags.has(tag)) overlap++;
    }
    return overlap;
  }

  function instrumentalPenalty(trackTags, selectedHasInstrumental, selectedHasVocal) {
    if (!selectedHasInstrumental && !selectedHasVocal) return 1;
    const trackHasInstrumental = [...trackTags].some((tag) => INSTRUMENTAL_TAGS.has(tag));
    const trackHasVocal = [...trackTags].some((tag) => VOCAL_TAGS.has(tag));
    if (selectedHasInstrumental && trackHasVocal && !trackHasInstrumental) return 0.3;
    if (selectedHasInstrumental && trackHasVocal && trackHasInstrumental) return 0.7;
    if (selectedHasVocal && trackHasInstrumental && !trackHasVocal) return 0.5;
    return 1;
  }

  // --- Mode toggle ---
  modeBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.mode;
      if (mode === currentMode) return;
      currentMode = mode;
      modeBtns.forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
      audioWeightsPanel.classList.toggle("hidden", mode !== "audio");
      filterPanel.classList.add("hidden");
      traceHistory = [];
      renderDrillPanel();
    });
  });
  audioWeightsPanel.classList.toggle("hidden", currentMode !== "audio");

  // --- Weight sliders ---
  weightRows.forEach((row) => {
    const slider = row.querySelector("input[type=range]");
    const valSpan = row.querySelector(".weight-val");
    slider.addEventListener("input", () => {
      valSpan.textContent = slider.value + "%";
    });
  });

  function getWeights() {
    const weights = {};
    weightRows.forEach((row) => {
      const key = row.dataset.key;
      const slider = row.querySelector("input[type=range]");
      weights[key] = parseInt(slider.value, 10) / 100;
    });
    return weights;
  }

  // --- Form ---
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    await search();
  });
  reloadBtn.addEventListener("click", async () => {
    if (traceHistory.length > 0) {
      const pivot = traceHistory[traceHistory.length - 1];
      await runDrillSearch(pivot.track, { keepTrace: true });
      return;
    }
    await search();
  });

  bpmSlider.addEventListener("input", () => {
    updateBpmLabel();
    softFiltering = false;
    renderFiltered();
  });

  discoverMoreBtn.addEventListener("click", async () => {
    stopAudio();
    discoverMoreBtn.disabled = true;
    discoverMoreBtn.textContent = "Searching\u2026";
    try {
      let data;
      const url = urlInput.value.trim();
      const excludeList = [...seenTrackKeys];
      const request = beginRequest();
      if (currentMode === "audio") {
        data = await fetchAudioSimilar(url, excludeList, request);
      } else {
        data = await fetchLastfmSimilar(url, excludeList, request);
      }
      if (!isCurrentRequest(request.requestId)) return;
      if (data.similar_tracks.length === 0) {
        exhausted = true;
        renderFiltered();
        return;
      }
      allTracks = mergeTracksByKey(allTracks, data.similar_tracks);
      seedTags = data.seed_tags || seedTags;
      tagCategories = data.tag_categories || tagCategories;
      approximated = data.approximated || false;
      mappedCount = data.mapped_count || 0;
      unmappedCount = data.unmapped_count || 0;
      mappingDegradedReason = data.mapping_degraded_reason || null;
      addToSeen(allTracks);
      softFiltering = false;
      buildFilters();
      if (rankTracks().length === 0 && allTracks.length > 0) {
        softFiltering = true;
      }
      renderFiltered();
      updateActionsBar();
    } catch (err) {
      if (err.name !== "AbortError") {
        errorEl.textContent = err.message;
        errorEl.classList.remove("hidden");
      }
    } finally {
      endRequest();
      discoverMoreBtn.disabled = false;
      discoverMoreBtn.textContent = "Discover More";
      updateActionsBar();
    }
  });

  drillCloseBtn.addEventListener("click", () => {
    traceHistory = [];
    renderDrillPanel();
  });

  function updateBpmLabel() {
    const val = parseInt(bpmSlider.value, 10);
    if (val >= 100) {
      bpmLabel.textContent = "Any";
    } else if (val === 0) {
      bpmLabel.textContent = "Exact";
    } else {
      bpmLabel.textContent = "\u00b1" + val + "%";
    }
  }

  function trackKey(t) {
    return `${(t.artists || [])[0] || ""}::${t.name}`.toLowerCase();
  }

  function addToSeen(tracks) {
    for (const t of tracks) seenTrackKeys.add(trackKey(t));
  }

  function averageFeatures(entries) {
    const keys = ["tempo", "energy", "valence", "danceability", "acousticness", "instrumentalness"];
    const sums = Object.fromEntries(keys.map((k) => [k, 0]));
    const counts = Object.fromEntries(keys.map((k) => [k, 0]));
    for (const entry of entries) {
      const af = entry.audioFeatures || {};
      for (const key of keys) {
        const val = af[key];
        if (typeof val === "number") {
          sums[key] += val;
          counts[key] += 1;
        }
      }
      if (typeof entry.bpm === "number") {
        sums.tempo += entry.bpm;
        counts.tempo += 1;
      }
    }
    const out = {};
    for (const key of keys) {
      out[key] = counts[key] > 0 ? sums[key] / counts[key] : null;
    }
    return out;
  }

  function intersectTags(entries) {
    if (!entries.length) return [];
    let inter = new Set(entries[0].tags || []);
    for (let i = 1; i < entries.length; i++) {
      const s = new Set(entries[i].tags || []);
      inter = new Set([...inter].filter((t) => s.has(t)));
    }
    return [...inter];
  }

  function setWeightSlidersFromProfile(avg) {
    weightRows.forEach((row) => {
      const key = row.dataset.key;
      if (key === "tempo") return;
      const val = avg[key];
      if (typeof val !== "number") return;
      const slider = row.querySelector("input[type=range]");
      const pct = Math.max(0, Math.min(100, Math.round(val * 100)));
      slider.value = String(pct);
      row.querySelector(".weight-val").textContent = `${pct}%`;
    });
  }

  function renderDrillPanel() {
    if (!traceHistory.length) {
      drillBreadcrumbs.innerHTML = "";
      drillProfile.textContent = "Select a result track using Drill to start narrowing.";
      drillSideContent.innerHTML = "";
      drillPanel.classList.add("hidden");
      drillSidePanel.classList.add("hidden");
      return;
    }
    drillPanel.classList.remove("hidden");
    drillSidePanel.classList.remove("hidden");
    const avg = averageFeatures(traceHistory);
    const tags = intersectTags(traceHistory);

    drillBreadcrumbs.innerHTML = traceHistory
      .map((t, i) => `<button class="crumb-btn ${i === traceHistory.length - 1 ? "active" : ""}" data-crumb="${i}">${esc(t.track.name)}</button><button class="crumb-btn" data-remove-crumb="${i}" title="Remove step">×</button>`)
      .join("");
    drillProfile.innerHTML = `
      <div><strong>Narrowed Tags:</strong> ${tags.length ? tags.map((t) => esc(t)).join(", ") : "None"}</div>
      <div><strong>Avg Features:</strong>
        Energy ${fmtPct(avg.energy)} | Valence ${fmtPct(avg.valence)} | Dance ${fmtPct(avg.danceability)} | Acoustic ${fmtPct(avg.acousticness)} | Instrumental ${fmtPct(avg.instrumentalness)} | BPM ${fmtTempo(avg.tempo)}
      </div>
    `;
    drillSideContent.innerHTML = `
      <div class="drill-profile">${drillProfile.innerHTML}</div>
      <div class="tag-sections">
        ${traceHistory.map((entry, idx) => `
          <div class="tag-section">
            <label class="tag-section-label">Step ${idx + 1}</label>
            <div class="track-card-tags">
              <span class="track-tag">${esc(entry.track.name)}</span>
            </div>
          </div>
        `).join("")}
      </div>
    `;
    drillBreadcrumbs.querySelectorAll(".crumb-btn").forEach((btn) => {
      if (btn.dataset.crumb == null) return;
      btn.addEventListener("click", async () => {
        const idx = parseInt(btn.dataset.crumb, 10);
        traceHistory = traceHistory.slice(0, idx + 1);
        await runDrillSearch(traceHistory[idx].track);
      });
    });
    drillBreadcrumbs.querySelectorAll("[data-remove-crumb]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const idx = parseInt(btn.dataset.removeCrumb, 10);
        traceHistory = traceHistory.slice(0, idx);
        if (traceHistory.length === 0) {
          await search();
          return;
        }
        const pivot = traceHistory[traceHistory.length - 1];
        await runDrillSearch(pivot.track);
      });
    });
  }

  function fmtPct(v) {
    return typeof v === "number" ? `${Math.round(v * 100)}%` : "-";
  }
  function fmtTempo(v) {
    return typeof v === "number" ? `${Math.round(v)}` : "-";
  }

  async function runDrillSearch(track, options = {}) {
    const { keepTrace = false } = options;
    if (!track || !track.spotify_url) return;
    const avg = averageFeatures(traceHistory);
    const interTags = intersectTags(traceHistory);
    selectedTags = new Set(interTags);
    setWeightSlidersFromProfile(avg);

    const request = beginRequest();
    try {
      let data;
      if (currentMode === "audio") {
        data = await fetchAudioSimilar(track.spotify_url, [], request);
      } else {
        data = await fetchLastfmSimilar(track.spotify_url, [], request);
      }
      if (!isCurrentRequest(request.requestId)) return;
      seedTrack = data.seed_track;
      allTracks = data.similar_tracks;
      seedTags = data.seed_tags || [];
      tagCategories = data.tag_categories || {};
      approximated = data.approximated || false;
      mappedCount = data.mapped_count || 0;
      unmappedCount = data.unmapped_count || 0;
      mappingDegradedReason = data.mapping_degraded_reason || null;
      addToSeen(allTracks);
      renderSeed(seedTrack);
      buildFilters();
      renderFiltered();
      updateActionsBar();
      if (!keepTrace) {
        traceHistory = traceHistory.slice();
      }
      renderDrillPanel();
    } catch (err) {
      if (err.name !== "AbortError") {
        errorEl.textContent = err.message || "Drill search failed.";
        errorEl.classList.remove("hidden");
      }
    } finally {
      endRequest();
      updateActionsBar();
    }
  }

  function currentRequestLimit(excludeCount = 0) {
    const poolLimit = Math.max(displayLimit * 2, 25);
    const base = Math.min(poolLimit, 80);
    if (excludeCount > 0) {
      return Math.min(base + 20, 100);
    }
    return base;
  }

  async function search(options = {}) {
    const { preserveTrace = false, urlOverride = null } = options;
    const url = urlInput.value.trim();
    const effectiveUrl = urlOverride || url;
    if (!effectiveUrl) return;
    displayLimit = parseInt(document.getElementById("limit").value, 10);

    const request = beginRequest();
    stopAudio();
    seedEl.classList.add("hidden");
    filterPanel.classList.add("hidden");
    discoverMoreBtn.classList.add("hidden");
    resultsEl.innerHTML = "";
    errorEl.classList.add("hidden");
    loadingEl.classList.remove("hidden");
    searchBtn.disabled = true;
    allTracks = [];
    seedTrack = null;
    seedTags = [];
    mappedCount = 0;
    unmappedCount = 0;
    mappingDegradedReason = null;
    selectedTags.clear();
    seenTrackKeys.clear();
    exhausted = false;
    softFiltering = false;
    if (!preserveTrace) traceHistory = [];
    updateActionsBar();

    try {
      let data;
      if (currentMode === "audio") {
        data = await fetchAudioSimilar(effectiveUrl, [], request);
      } else {
        data = await fetchLastfmSimilar(effectiveUrl, [], request);
      }
      if (!isCurrentRequest(request.requestId)) return;

      seedTrack = data.seed_track;
      allTracks = data.similar_tracks;
      seedTags = data.seed_tags || [];
      tagCategories = data.tag_categories || {};
      approximated = data.approximated || false;
      mappedCount = data.mapped_count || 0;
      unmappedCount = data.unmapped_count || 0;
      mappingDegradedReason = data.mapping_degraded_reason || null;
      addToSeen(allTracks);

      renderSeed(seedTrack);
      buildFilters();
      renderFiltered();
      updateActionsBar();
      renderDrillPanel();
    } catch (err) {
      if (err.name !== "AbortError") {
        errorEl.textContent = err.message;
        errorEl.classList.remove("hidden");
      }
    } finally {
      endRequest();
      loadingEl.classList.add("hidden");
      searchBtn.disabled = false;
      updateActionsBar();
    }
  }

  function beginRequest() {
    if (activeController) activeController.abort();
    activeRequestId += 1;
    activeController = new AbortController();
    isRequestInFlight = true;
    return { requestId: activeRequestId, signal: activeController.signal };
  }

  function isCurrentRequest(requestId) {
    return requestId === activeRequestId;
  }

  function endRequest() {
    isRequestInFlight = false;
  }

  async function parseErrorPayload(resp) {
    const maybeJson = await resp.json().catch(() => null);
    if (maybeJson) return maybeJson;
    const text = await resp.text().catch(() => "");
    return { detail: text || `Request failed (${resp.status})` };
  }

  async function fetchLastfmSimilar(url, exclude = [], request = null) {
    const limit = currentRequestLimit(exclude.length);
    const resp = await fetch("/api/similar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, limit, exclude }),
      signal: request?.signal,
    });
    if (!resp.ok) {
      const data = await parseErrorPayload(resp);
      throw new Error(data.detail || `Request failed (${resp.status})`);
    }
    return resp.json();
  }

  async function fetchAudioSimilar(url, exclude = [], request = null) {
    const limit = currentRequestLimit(exclude.length);
    const resp = await fetch("/api/similar/audio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, limit, weights: getWeights(), exclude }),
      signal: request?.signal,
    });
    if (!resp.ok) {
      const data = await parseErrorPayload(resp);
      throw new Error(data.detail || `Request failed (${resp.status})`);
    }
    return resp.json();
  }

  // --- Tag filter chips ---
  function addChipListeners(chip, tag) {
    chip.addEventListener("click", () => {
      const normalized = normalizeTag(tag);
      if (selectedTags.has(normalized)) {
        selectedTags.delete(normalized);
        chip.classList.remove("active");
      } else {
        selectedTags.add(normalized);
        chip.classList.add("active");
      }
      softFiltering = false;
      renderFiltered();
    });
  }

  function buildFilters() {
    const tagCounts = {};
    for (const tag of seedTags) {
      const normalized = normalizeTag(tag);
      tagCounts[normalized] = (tagCounts[normalized] || 0) + 5;
    }
    for (const t of allTracks) {
      for (const tag of t.tags || []) {
        const normalized = normalizeTag(tag);
        tagCounts[normalized] = (tagCounts[normalized] || 0) + 1;
      }
    }

    const allPoolTags = Object.keys(tagCounts);
    const top30 = Object.entries(tagCounts)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 30)
      .map(([tag]) => tag);

    const sortedTags = [...new Set([...top30, ...selectedTags].filter((t) => t in tagCounts))];

    const seedKey = seedTrack ? trackKey(seedTrack) : "";
    if (seedTrack && seedTrack.bpm) {
      bpmFilter.classList.remove("hidden");
      if (seedKey !== lastSeedKey) {
        bpmSlider.value = 100;
      }
      updateBpmLabel();
    } else {
      bpmFilter.classList.add("hidden");
    }
    lastSeedKey = seedKey;

    if (sortedTags.length > 0) {
      tagFilter.classList.remove("hidden");

      quickFiltersEl.innerHTML = "";
      for (const preset of QUICK_FILTERS) {
        const matchingTags = allPoolTags.filter((st) => {
          const normalizedTag = normalizeTag(st);
          return preset.tags.some((pt) => normalizeTag(pt) === normalizedTag);
        });
        if (matchingTags.length === 0) continue;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "quick-filter-btn";
        btn.textContent = preset.label;
        btn.addEventListener("click", () => {
          matchingTags.forEach((t) => selectedTags.add(normalizeTag(t)));
          buildFilters();
          renderFiltered();
        });
        quickFiltersEl.appendChild(btn);
      }

      const byCategory = { mood: [], genre: [], vocals_instrumentals: [], other: [] };
      for (const tag of sortedTags) {
        const cat = tagCategories[tag] || "genre";
        (byCategory[cat] || byCategory.other).push(tag);
      }

      const categoryOrder = ["mood", "vocals_instrumentals", "genre", "other"];
      tagSections.innerHTML = "";
      for (const catKey of categoryOrder) {
        const tagsInCat = byCategory[catKey];
        if (tagsInCat.length === 0) continue;
        const label = CATEGORY_LABELS[catKey] || "Other";
        const section = document.createElement("div");
        section.className = "tag-section";
        section.innerHTML = `<label class="tag-section-label">${esc(label)}</label>`;
        const chipsWrap = document.createElement("div");
        chipsWrap.className = "tag-chips";
        for (const tag of tagsInCat) {
          const chip = document.createElement("button");
          chip.type = "button";
          chip.className = "tag-chip";
          chip.textContent = tag;
          if (selectedTags.has(normalizeTag(tag))) chip.classList.add("active");
          addChipListeners(chip, tag);
          chipsWrap.appendChild(chip);
        }
        section.appendChild(chipsWrap);
        tagSections.appendChild(section);
      }
    } else {
      tagFilter.classList.add("hidden");
    }

    filterPanel.classList.remove("hidden");
  }

  // --- Ranking ---
  function rankTracks() {
    const bpmTol = parseInt(bpmSlider.value, 10);
    const hasBpmFilter = !softFiltering && seedTrack && seedTrack.bpm && bpmTol < 100;
    const hasTagFilter = selectedTags.size > 0;
    const selectedHasInstrumental = [...selectedTags].some((tag) => INSTRUMENTAL_TAGS.has(tag));
    const selectedHasVocal = [...selectedTags].some((tag) => VOCAL_TAGS.has(tag));

    return allTracks
      .map((t) => {
        let score = t.match_score || 0;

        if (hasBpmFilter && t.bpm) {
          const pctDiff = (Math.abs(t.bpm - seedTrack.bpm) / seedTrack.bpm) * 100;
          if (bpmTol === 0) {
            score *= pctDiff <= 2 ? 1.0 : 0;
          } else if (pctDiff > bpmTol) {
            score = 0;
          } else {
            score *= 1 - (pctDiff / bpmTol) * 0.4;
          }
        }

        if (hasTagFilter) {
          const trackTags = normalizedTagSet(t.tags || []);
          const overlap = countTagOverlap(trackTags, selectedTags);
          if (overlap === 0) {
            score *= softFiltering ? 0.5 : 0;
          } else {
            score *= overlap / selectedTags.size;
          }
          score *= instrumentalPenalty(trackTags, selectedHasInstrumental, selectedHasVocal);
        }

        return { ...t, _score: score };
      })
      .filter((t) => t._score > 0)
      .sort((a, b) => b._score - a._score);
  }

  function renderFiltered() {
    const ranked = rankTracks();
    const shown = ranked.slice(0, displayLimit);
    renderResults(shown, ranked.length);
    discoverMoreBtn.classList.toggle("hidden", shown.length === 0 || exhausted);
  }

  // --- Badges ---
  function matchBadge(score) {
    if (score == null) return "";
    const pct = Math.round(score * 100);
    return `<span class="match-badge">${pct}% match</span>`;
  }

  function bpmBadge(bpm) {
    if (!bpm) return "";
    return `<span class="bpm-badge">${Math.round(bpm)} BPM</span>`;
  }

  function audioFeatureBadges(af) {
    if (!af) return "";
    const badges = [];
    for (const [key, label] of Object.entries(AUDIO_DIMENSION_LABELS)) {
      const val = af[key];
      if (val == null) continue;
      const display = key === "tempo" ? Math.round(val) : Math.round(val * 100) + "%";
      badges.push(`<span class="af-badge" title="${label}">${label} ${display}</span>`);
    }
    return badges.join("");
  }

  const SPOTIFY_ICON = `<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/></svg>`;

  function extractSpotifyTrackId(value) {
    if (!value || typeof value !== "string") return null;
    if (value.startsWith("spotify:track:")) return value.split(":")[2] || null;
    const match = value.match(/spotify\.com\/track\/([a-zA-Z0-9]+)/);
    return match ? match[1] : null;
  }

  function getSpotifyTargets(spotifyUrl, spotifyId) {
    const trackId = spotifyId || extractSpotifyTrackId(spotifyUrl);
    if (!trackId) return { appUrl: spotifyUrl || "", webUrl: spotifyUrl || "" };
    return {
      appUrl: `spotify:track:${trackId}`,
      webUrl: `https://open.spotify.com/track/${trackId}`,
    };
  }

  function renderSpotifyLink(label, spotifyUrl, spotifyId) {
    const { appUrl, webUrl } = getSpotifyTargets(spotifyUrl, spotifyId);
    if (!appUrl && !webUrl) return esc(label);
    return `<a class="spotify-open-link" href="${appUrl || webUrl}" data-web-url="${webUrl}" rel="noopener">${esc(label)}</a>`;
  }

  function spotifyBtn(url, spotifyId) {
    const { appUrl, webUrl } = getSpotifyTargets(url, spotifyId);
    if (!appUrl && !webUrl) return "";
    return `<a class="spotify-btn spotify-open-link" href="${appUrl || webUrl}" data-web-url="${webUrl}" rel="noopener" title="Open in Spotify">${SPOTIFY_ICON}</a>`;
  }

  function bindSpotifyOpenLinks(container) {
    container.querySelectorAll(".spotify-open-link").forEach((link) => {
      link.addEventListener("click", (event) => {
        const webUrl = link.dataset.webUrl;
        if (!webUrl || link.href.startsWith("http")) return;
        event.preventDefault();

        let appOpened = false;
        const markOpened = () => {
          appOpened = true;
          window.removeEventListener("blur", markOpened);
          document.removeEventListener("visibilitychange", onVisibility);
        };
        const onVisibility = () => {
          if (document.hidden) markOpened();
        };

        window.addEventListener("blur", markOpened);
        document.addEventListener("visibilitychange", onVisibility);
        window.location.href = link.getAttribute("href");
        window.setTimeout(() => {
          if (!appOpened) window.open(webUrl, "_blank", "noopener");
          markOpened();
        }, 900);
      });
    });
  }

  function queueSummaryText(data) {
    if (typeof data?.message === "string" && data.message) return data.message;
    const added = data?.added || 0;
    const failed = data?.failed || 0;
    if (failed > 0) {
      return `Added ${added} track(s), failed ${failed}.`;
    }
    return `Added ${added} track(s) to your queue.`;
  }

  function queueErrorText(data, fallbackStatus) {
    if (typeof data?.detail === "string") return data.detail;
    const detail = typeof data?.detail === "object" && data?.detail ? data.detail : null;
    if (detail?.message) {
      const errors = Array.isArray(detail.errors) ? detail.errors : [];
      if (errors.length === 0) return detail.message;
      const list = errors
        .slice(0, 2)
        .map((e) => e?.message || e?.reason || "Unknown queue error")
        .join(" | ");
      const remaining = errors.length > 2 ? ` (+${errors.length - 2} more)` : "";
      return `${detail.message} ${list}${remaining}`;
    }
    if (Array.isArray(data?.errors) && data.errors.length > 0) {
      const list = data.errors
        .slice(0, 2)
        .map((e) => e?.message || e?.reason || "Unknown queue error")
        .join(" | ");
      const remaining = data.errors.length > 2 ? ` (+${data.errors.length - 2} more)` : "";
      return `${list}${remaining}`;
    }
    return `Failed (${fallbackStatus})`;
  }

  async function queueSingleTrack(uri, btn) {
    if (!spotifyConnected) return;
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "...";
    try {
      const resp = await fetch("/api/spotify/queue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ track_uris: [uri] }),
      });
      if (!resp.ok) {
        const data = await parseErrorPayload(resp);
        throw new Error(queueErrorText(data, resp.status));
      }
      const data = await resp.json();
      btn.textContent = data.failed ? "!" : "\u2713";
      actionStatus.textContent = queueSummaryText(data);
      setTimeout(() => { btn.textContent = original; }, 1200);
    } catch (err) {
      btn.textContent = "!";
      actionStatus.textContent = err.message || "Could not add to queue.";
      setTimeout(() => { btn.textContent = original; }, 1200);
    } finally {
      setTimeout(() => { btn.disabled = false; }, 150);
    }
  }

  // --- Rendering ---
  function renderSeed(track) {
    const spotifyLink = track.spotify_url
      ? renderSpotifyLink(track.name, track.spotify_url, track.spotify_id)
      : esc(track.name);
    const bpm = track.bpm ? `<span class="seed-bpm">${Math.round(track.bpm)} BPM</span>` : "";
    const tagsHtml =
      track.tags && track.tags.length > 0
        ? `<div class="seed-tags">${track.tags.slice(0, 12).map((t) => `<span class="seed-tag">${esc(t)}</span>`).join("")}</div>`
        : "";
    const afHtml =
      track.audio_features
        ? `<div class="seed-audio-features">${audioFeatureBadges(track.audio_features)}</div>`
        : "";
    seedEl.innerHTML = `
      ${track.album_art ? `<img src="${track.album_art}" alt="Album art" />` : ""}
      <div class="seed-meta">
        <h2>${spotifyLink} ${spotifyBtn(track.spotify_url, track.spotify_id)}</h2>
        <div class="artists">${esc(track.artists.join(", "))} &mdash; ${esc(track.album)}</div>
        ${bpm}
        ${tagsHtml}
        ${afHtml}
      </div>
    `;
    seedEl.classList.remove("hidden");
    bindSpotifyOpenLinks(seedEl);
  }

  function renderResults(tracks, totalAvailable) {
    visibleTracks = tracks;
    if (!tracks.length) {
      resultsEl.innerHTML = "<p>No tracks match your filters. Try loosening the constraints.</p>";
      return;
    }
    const countNote = totalAvailable > tracks.length
      ? ` (showing ${tracks.length} of ${totalAvailable})`
      : ` (${tracks.length})`;
    const approxNote = approximated
      ? `<div class="approx-notice">Audio features estimated from tags (Spotify audio-features API unavailable for this app)</div>`
      : "";
    let html = `<h3>Cat ID Matches${countNote}</h3>${approxNote}`;
    tracks.forEach((t, idx) => {
      const previewBtn = t.preview_url
        ? `<button class="play-btn" data-url="${t.preview_url}" title="Preview">&#9654;</button>`
        : "";
      const nameLink = t.spotify_url
        ? renderSpotifyLink(t.name, t.spotify_url, t.spotify_id)
        : esc(t.name);
      const trackTags =
        t.tags && t.tags.length > 0
          ? `<div class="track-card-tags">${t.tags.slice(0, 6).map((tag) => `<span class="track-tag">${esc(tag)}</span>`).join("")}</div>`
          : "";
      const afHtml =
        t.audio_features
          ? `<div class="track-card-tags">${audioFeatureBadges(t.audio_features)}</div>`
          : "";
      const queueBtn =
        spotifyConnected && t.spotify_id
          ? `<button class="queue-btn" data-uri="spotify:track:${t.spotify_id}" title="Add to queue" aria-label="Add ${esc(t.name)} to queue">+Q</button>`
          : "";
      const mappingNote = !t.spotify_id
        ? `<div class="detail">No Spotify match found for queue/playlist actions yet.</div>`
        : "";
      const drillBtn =
        t.spotify_url
          ? `<button class="drill-btn" data-drill-idx="${idx}" title="Drill down with this track" aria-label="Drill down with ${esc(t.name)}">Drill</button>`
          : "";
      html += `
        <div class="track-card">
          ${t.album_art ? `<img src="${t.album_art}" alt="" />` : '<div class="img-placeholder"></div>'}
          <div class="track-info">
            <div class="name">${nameLink}</div>
            <div class="detail">${esc(t.artists.join(", "))}${t.album ? " &mdash; " + esc(t.album) : ""}</div>
            ${mappingNote}
            ${trackTags}
            ${afHtml}
          </div>
          <div class="track-badges">
            ${matchBadge(t.match_score)}
            ${bpmBadge(t.bpm)}
          </div>
          <div class="track-actions">
            ${previewBtn}
            ${queueBtn}
            ${drillBtn}
            ${spotifyBtn(t.spotify_url, t.spotify_id)}
          </div>
        </div>
      `;
    });
    resultsEl.innerHTML = html;

    resultsEl.querySelectorAll(".play-btn").forEach((btn) => {
      btn.addEventListener("click", () => togglePreview(btn));
    });
    resultsEl.querySelectorAll(".queue-btn").forEach((btn) => {
      btn.addEventListener("click", () => queueSingleTrack(btn.dataset.uri, btn));
    });
    resultsEl.querySelectorAll(".drill-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const idx = parseInt(btn.dataset.drillIdx, 10);
        const track = visibleTracks[idx];
        if (!track) return;
        traceHistory.push({
          track,
          tags: track.tags || [],
          audioFeatures: track.audio_features || null,
          bpm: track.bpm || null,
        });
        await runDrillSearch(track);
      });
    });
    bindSpotifyOpenLinks(resultsEl);
  }

  // --- Audio preview ---
  function togglePreview(btn) {
    const url = btn.dataset.url;
    if (currentAudio && currentAudio.src === url) {
      stopAudio();
      return;
    }
    stopAudio();
    currentAudio = new Audio(url);
    currentAudio.volume = 0.5;
    currentAudio.play().catch(() => {
      btn.innerHTML = "&#9654;";
      currentAudio = null;
      actionStatus.textContent = "Preview could not be played in this browser context.";
    });
    btn.innerHTML = "&#9724;";
    currentAudio.addEventListener("ended", () => {
      btn.innerHTML = "&#9654;";
      currentAudio = null;
    });
  }

  function stopAudio() {
    if (currentAudio) {
      currentAudio.pause();
      currentAudio = null;
      document.querySelectorAll(".play-btn").forEach((b) => {
        b.innerHTML = "&#9654;";
      });
    }
  }

  function esc(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
  }

  // --- Spotify auth ---
  async function checkSpotifyStatus() {
    try {
      const resp = await fetch("/api/spotify/status");
      const data = await resp.json();
      spotifyConnected = data.connected;
      if (data.connected) {
        spotifyLoginBtn.classList.add("hidden");
        spotifyUserEl.textContent = data.user;
        spotifyUserEl.classList.remove("hidden");
        spotifyLogoutBtn.classList.remove("hidden");
      } else {
        spotifyLoginBtn.classList.remove("hidden");
        spotifyUserEl.classList.add("hidden");
        spotifyLogoutBtn.classList.add("hidden");
      }
      updateActionsBar();
    } catch (_) { /* ignore */ }
  }

  function updateActionsBar() {
    if (allTracks.length > 0 && spotifyConnected) {
      actionsBar.classList.remove("hidden");
      savePlaylistBtn.disabled = isRequestInFlight;
      addQueueBtn.disabled = isRequestInFlight;
      const degradeNote = mappingDegradedReason ? ` (${mappingDegradedReason.replaceAll("_", " ")})` : "";
      actionStatus.textContent = `${mappedCount}/${mappedCount + unmappedCount} tracks mapped to Spotify${degradeNote}. Queue/playlist use mapped tracks only.`;
    } else {
      actionsBar.classList.add("hidden");
    }
  }

  function mergeTracksByKey(existingTracks, incomingTracks) {
    const byKey = new Map();
    for (const track of existingTracks) byKey.set(trackKey(track), track);
    for (const track of incomingTracks) byKey.set(trackKey(track), track);
    return [...byKey.values()];
  }

  function getActionableTrackUris() {
    const ranked = rankTracks().slice(0, displayLimit);
    const mappable = ranked.filter((t) => t.spotify_id);
    const uris = mappable.map((t) => `spotify:track:${t.spotify_id}`);
    return { uris, shown: ranked.length, mappable: mappable.length, unmapped: ranked.length - mappable.length };
  }

  spotifyLoginBtn.addEventListener("click", () => {
    window.location.href = "/api/spotify/login";
  });

  spotifyLogoutBtn.addEventListener("click", async () => {
    await fetch("/api/spotify/logout", { method: "POST" });
    spotifyConnected = false;
    spotifyLoginBtn.classList.remove("hidden");
    spotifyUserEl.classList.add("hidden");
    spotifyLogoutBtn.classList.add("hidden");
    updateActionsBar();
  });

  function getFilteredTrackUris() {
    const ranked = rankTracks().slice(0, displayLimit);
    return ranked
      .filter((t) => t.spotify_id)
      .map((t) => `spotify:track:${t.spotify_id}`);
  }

  savePlaylistBtn.addEventListener("click", async () => {
    const actionData = getActionableTrackUris();
    const uris = actionData.uris;
    if (uris.length === 0) {
      actionStatus.textContent = "No Spotify tracks in current results.";
      return;
    }
    const name = seedTrack
      ? `Follow Your Cat ID - ${seedTrack.name} - ${seedTrack.artists[0]}`
      : "Follow Your Cat ID";
    actionStatus.textContent = `Creating playlist (${actionData.mappable} mappable, ${actionData.unmapped} skipped)...`;
    savePlaylistBtn.disabled = true;
    try {
      const resp = await fetch("/api/spotify/playlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, track_uris: uris }),
      });
      if (!resp.ok) {
        const data = await parseErrorPayload(resp);
        throw new Error(queueErrorText(data, resp.status));
      }
      const data = await resp.json();
      actionStatus.innerHTML = `Playlist created! <a href="${data.playlist_url}" target="_blank" rel="noopener">Open in Spotify</a>`;
    } catch (err) {
      actionStatus.textContent = err.message;
    } finally {
      savePlaylistBtn.disabled = false;
    }
  });

  addQueueBtn.addEventListener("click", async () => {
    const actionData = getActionableTrackUris();
    const uris = actionData.uris;
    if (uris.length === 0) {
      actionStatus.textContent = "No Spotify tracks in current results.";
      return;
    }
    actionStatus.textContent = `Adding to queue (${actionData.mappable} mappable, ${actionData.unmapped} skipped)...`;
    addQueueBtn.disabled = true;
    try {
      const resp = await fetch("/api/spotify/queue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ track_uris: uris }),
      });
      if (!resp.ok) {
        const data = await parseErrorPayload(resp);
        throw new Error(queueErrorText(data, resp.status));
      }
      const data = await resp.json();
      actionStatus.textContent = queueSummaryText(data);
    } catch (err) {
      actionStatus.textContent = err.message;
    } finally {
      addQueueBtn.disabled = false;
    }
  });

  checkSpotifyStatus();
})();
