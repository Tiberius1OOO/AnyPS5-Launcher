(() => {
  const $ = (id) => document.getElementById(id);
  const GITHUB_FALLBACK = "https://github.com/boykopovar/AnyPS5";
  const LAUNCHER_GITHUB_FALLBACK = "https://github.com/Tiberius1OOO/AnyPS5-Launcher";
  let lastLineCount = 0;
  let pollTimer = null;
  let progressTimer = null;
  let githubUrl = GITHUB_FALLBACK;
  let launcherGithubUrl = LAUNCHER_GITHUB_FALLBACK;
  let readmeLoaded = false;
  let inputProfile = { bindings: {} };
  let selectedAction = "Cross";
  let listenMode = false;
  let keyActions = [];
  let adminWhyCopy = null;
  let pendingInstallMode = null; // "admin" | "plain" | null
  let lastSetupAudit = null;
  let launcherUpdatePrompted = false; // once per page/runtime
  let launcherUpdateBusy = false;
  let launcherUpdateResolve = null; // unblock boot after Update / Keep
  let libraryProgressTimer = null;
  let libraryData = { converted: [], unconverted: [] };
  let audioCtx = null;
  let libraryScannedOnce = false;
  let jobRunning = false;
  let jobKind = "";
  let libraryScanRunning = false;
  let suppressConvertFill = false;
  let settingsSaveTimer = null;
  let backdropUrl = "";
  let backdropLayer = "A";
  let backdropTimer = null;
  let gamePollTimer = null;
  let playInFlight = false;

  function clientLog(level, message, detail) {
    try {
      api("/api/log", {
        method: "POST",
        body: JSON.stringify({ level: level || "info", message, detail: detail || "" }),
      }).catch(() => {});
    } catch (_) {}
  }

  function isHeavyJobKind(kind) {
    return /build|rebuild|convert|setup|update|pull|git/i.test(String(kind || ""));
  }

  function updateJobLocks(running, kind = "") {
    jobRunning = !!running;
    jobKind = running ? (kind || jobKind || "Job") : "";
    const block = jobRunning;
    [
      "btnConvert", "btnRebuild", "btnPull", "btnStart2",
      "btnLibraryScan", "btnSetupInstall", "btnMarkBuild",
    ].forEach((id) => {
      const el = $(id);
      if (el) el.disabled = block;
    });
    // Keep Settings, Library browse, tabs, update-check usable
    if (!block && $("jobStatus") && !$("jobStatus").dataset.sticky) {
      $("jobStatus").textContent = "Ready";
    }
  }

  function guardHeavyAction(actionLabel) {
    if (libraryScanRunning) {
      toast("Wait until the library scan finishes.", "error");
      clientLog("warning", `Blocked ${actionLabel} — library scan running`);
      return false;
    }
    if (!jobRunning) return true;
    toast(`Wait until ${jobKind || "the current job"} finishes before ${actionLabel}.`, "error");
    clientLog("warning", `Blocked ${actionLabel} — job running`, jobKind);
    return false;
  }

  function toast(msg, kind = "") {
    const el = $("toast");
    el.hidden = false;
    el.textContent = msg;
    el.className = "toast" + (kind ? ` ${kind}` : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, 4200);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    let data = null;
    try { data = await res.json(); } catch { data = {}; }
    if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
    return data;
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function setBusy(busy, kind = "") {
    // Only lock heavy actions — Settings / Library browsing stay available
    updateJobLocks(busy, kind || jobKind || "Job");
    if ($("jobStatus")) {
      $("jobStatus").textContent = busy ? `Running: ${kind || jobKind || "Job"}` : "Ready";
    }
  }

  function on(id, event, handler) {
    const el = $(id);
    if (!el) return;
    el.addEventListener(event, handler);
  }

  function renderKeyGrid() {
    const grid = $("keyGrid");
    if (!grid) return;
    const actions = keyActions.length
      ? keyActions
      : Object.keys(inputProfile.bindings || {});
    grid.innerHTML = actions.map((action) => {
      const keys = (inputProfile.bindings[action] || []).join(", ") || "click to assign";
      const active = action === selectedAction && listenMode ? " active" : "";
      return `<button type="button" class="key-cell${active}" data-key-action="${escapeHtml(action)}">
        <span class="act">${escapeHtml(action)}</span>
        <span class="keys">${escapeHtml(keys)}</span>
      </button>`;
    }).join("");
    grid.querySelectorAll("[data-key-action]").forEach((btn) => {
      btn.addEventListener("click", () => {
        selectedAction = btn.getAttribute("data-key-action");
        listenMode = true;
        const hint = $("listenHint");
        if (hint) {
          hint.hidden = false;
          hint.textContent = `Press a key or mouse button for ${selectedAction} (Esc cancels, Backspace clears)`;
        }
        renderKeyGrid();
        toast(`Assign key: ${selectedAction}`);
      });
    });
  }

  async function loadInput() {
    const data = await api("/api/input");
    keyActions = data.actions || [];
    inputProfile = { bindings: (data.profile && data.profile.bindings) || {} };
    renderKeyGrid();
  }

  async function saveInput() {
    await api("/api/input", {
      method: "POST",
      body: JSON.stringify({ bindings: inputProfile.bindings }),
    });
    toast("Controls saved", "ok");
  }

  function renderCommits(el, list, emptyText) {
    if (!list || !list.length) {
      el.innerHTML = `<p class="empty">${emptyText}</p>`;
      return;
    }
    el.innerHTML = list.map((c) => `
      <article class="commit">
        <div class="meta">${c.hash} · ${c.date} · ${escapeHtml(c.author)}</div>
        <div class="subj">${escapeHtml(c.subject)}</div>
      </article>
    `).join("");
  }

  function fillConfig(cfg) {
    if (!cfg) return;
    if (cfg.anyps5Root != null && $("anyps5Root")) $("anyps5Root").value = cfg.anyps5Root || "";
    // Settings always sync
    if ($("unconvertedGamesRoot")) $("unconvertedGamesRoot").value = cfg.unconvertedGamesRoot || "";
    if ($("convertedGamesRoot")) $("convertedGamesRoot").value = cfg.convertedGamesRoot || "";
    if ($("settingsOutputName")) $("settingsOutputName").value = cfg.outputName || "app.exe";
    if ($("settingsGpuFilter")) $("settingsGpuFilter").value = cfg.gpuFilter || "";
    if ($("settingsUnusedFilter")) $("settingsUnusedFilter").value = String(cfg.unusedFilter ?? "0");
    if ($("settingsToIntel")) $("settingsToIntel").checked = !!cfg.toIntel;
    if ($("settingsWindowsGui")) $("settingsWindowsGui").checked = !!cfg.windowsGui;
    if ($("settingsHideGameConsole")) $("settingsHideGameConsole").checked = !!cfg.hideGameConsole;
    if ($("settingsLogLevel")) $("settingsLogLevel").value = cfg.logLevel || "info";
    // Convert form — skip while library prefill is applying
    if (!suppressConvertFill) {
      if (cfg.lastGameRoot && $("gameRoot")) $("gameRoot").value = cfg.lastGameRoot;
      if (cfg.lastOutputDir && $("outputDir")) $("outputDir").value = cfg.lastOutputDir;
      if (cfg.outputName && $("outputName")) $("outputName").value = cfg.outputName;
      if (cfg.gpuFilter != null && $("gpuFilter")) $("gpuFilter").value = cfg.gpuFilter;
      if (cfg.unusedFilter != null && $("unusedFilter")) $("unusedFilter").value = String(cfg.unusedFilter);
      if ($("toIntel")) $("toIntel").checked = !!cfg.toIntel;
      if ($("windowsGui")) $("windowsGui").checked = !!cfg.windowsGui;
    }
  }

  function playConvertChime() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      if (!audioCtx) audioCtx = new Ctx();
      const ctx = audioCtx;
      if (ctx.state === "suspended") ctx.resume().catch(() => {});
      const now = ctx.currentTime;
      const notes = [523.25, 659.25, 783.99, 1046.5]; // C5 E5 G5 C6
      notes.forEach((freq, i) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        const t0 = now + i * 0.12;
        gain.gain.setValueAtTime(0.0001, t0);
        gain.gain.exponentialRampToValueAtTime(0.09, t0 + 0.04);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.55);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(t0);
        osc.stop(t0 + 0.6);
      });
    } catch (_) {}
  }

  function setLibraryBackdrop(url) {
    if (!url || url === backdropUrl) return;
    const next = url;
    const curId = backdropLayer === "A" ? "bgGameA" : "bgGameB";
    const nextId = backdropLayer === "A" ? "bgGameB" : "bgGameA";
    const cur = $(curId);
    const nxt = $(nextId);
    if (!nxt) return;
    if (backdropTimer) {
      clearTimeout(backdropTimer);
      backdropTimer = null;
    }
    nxt.style.backgroundImage = `url("${next}")`;
    // Half-second fade out of current, half-second fade in of next (1s total)
    if (cur && cur.classList.contains("visible")) {
      cur.classList.remove("visible");
      backdropTimer = window.setTimeout(() => {
        nxt.classList.add("visible");
        backdropLayer = backdropLayer === "A" ? "B" : "A";
        backdropUrl = next;
        backdropTimer = null;
      }, 500);
    } else {
      nxt.classList.add("visible");
      if (cur) cur.classList.remove("visible");
      backdropLayer = backdropLayer === "A" ? "B" : "A";
      backdropUrl = next;
    }
  }

  function renderLibraryProgress(progress) {
    const wrap = $("libraryProgressWrap");
    const label = $("libraryProgressLabel");
    const pctEl = $("libraryProgressPct");
    const detail = $("libraryProgressDetail");
    const fill = $("libraryProgressFill");
    if (!wrap) return;
    const running = !!(progress && progress.running);
    libraryScanRunning = running;
    // Disable heavy actions while scanning art
    if ($("btnConvert")) $("btnConvert").disabled = jobRunning || libraryScanRunning;
    if ($("btnRebuild")) $("btnRebuild").disabled = jobRunning || libraryScanRunning;
    if ($("btnLibraryScan")) $("btnLibraryScan").disabled = jobRunning || libraryScanRunning;
    const phaseLabel = (progress && (progress.phaseLabel || progress.phase)) || "";
    if (!running && !(progress && progress.phase === "done" && phaseLabel)) {
      // Keep brief "ready" flash only while running or just finished with label during poll
      if (!running) {
        wrap.hidden = true;
        if (fill) fill.classList.remove("busy", "indeterminate");
      }
      return;
    }
    wrap.hidden = false;
    const pct = progress && progress.percent != null ? Number(progress.percent) : null;
    const rate = (progress && progress.rate) ? String(progress.rate).trim() : "";
    if (label) label.textContent = phaseLabel || "Scanning game folders";
    if (pctEl) {
      const pctBit = pct != null && !Number.isNaN(pct) ? `${Math.round(pct)}%` : (running ? "…" : "");
      pctEl.textContent = rate && pctBit ? `${pctBit} · ${rate}` : pctBit;
    }
    if (detail) detail.textContent = (progress && progress.detail) || "";
    applyBarFill(fill, pct, running);
  }

  function libraryCardHtml(game, kind) {
    const fallbackText = escapeHtml(game.title || game.folderName || "Game");
    const cover = game.coverUrl
      ? `<img src="${escapeHtml(game.coverUrl)}" alt="${fallbackText}" loading="lazy" data-fallback="${fallbackText}" onerror="this.onerror=null;var t=this.getAttribute('data-fallback')||'Game';var d=document.createElement('div');d.className='library-cover-fallback';d.textContent=t;this.replaceWith(d);" />`
      : `<div class="library-cover-fallback">${fallbackText}</div>`;
    const badge = kind === "converted"
      ? `<span class="library-badge playable">Play</span>`
      : `<span class="library-badge needs-convert">Convert</span>`;
    const overlay = kind === "converted"
      ? `<div class="library-play"><span>Play</span></div>`
      : `<div class="library-play"><span>Convert</span></div>`;
    const meta = escapeHtml(game.titleId || game.folderName || "");
    const backdrop = escapeHtml(game.backdropUrl || game.coverUrl || "");
    const pathAttr = encodeURIComponent(game.path || "");
    const titleAttr = escapeHtml(game.title || "");
    const backdropAttr = encodeURIComponent(game.backdropUrl || game.coverUrl || "");
    return `
      <button type="button" class="library-card" data-id="${escapeHtml(game.id)}" data-kind="${kind}"
        data-path="${pathAttr}" data-title="${titleAttr}"
        data-backdrop="${backdropAttr}" title="${titleAttr}">
        <div class="library-cover">
          ${badge}
          ${cover}
          ${overlay}
        </div>
        <div class="library-title">${escapeHtml(game.title || "Untitled")}</div>
        <div class="library-meta">${meta}</div>
      </button>`;
  }

  function renderLibrary(library) {
    libraryData = library || { converted: [], unconverted: [] };
    const conv = libraryData.converted || [];
    const unconv = libraryData.unconverted || [];
    const convEl = $("libraryConverted");
    const unconvEl = $("libraryUnconverted");
    const convEmpty = $("libraryConvertedEmpty");
    const unconvEmpty = $("libraryUnconvertedEmpty");
    if ($("libraryConvertedCount")) $("libraryConvertedCount").textContent = `(${conv.length})`;
    if ($("libraryUnconvertedCount")) $("libraryUnconvertedCount").textContent = `(${unconv.length})`;
    if ($("libraryConvertedSize")) {
      $("libraryConvertedSize").textContent = libraryData.convertedTotalLabel
        ? `· ${libraryData.convertedTotalLabel}`
        : "";
    }
    if ($("libraryUnconvertedSize")) {
      $("libraryUnconvertedSize").textContent = libraryData.unconvertedTotalLabel
        ? `· ${libraryData.unconvertedTotalLabel}`
        : "";
    }
    if (convEl) {
      convEl.innerHTML = conv.map((g) => libraryCardHtml(g, "converted")).join("");
      if (convEmpty) convEmpty.hidden = conv.length > 0;
    }
    if (unconvEl) {
      unconvEl.innerHTML = unconv.map((g) => libraryCardHtml(g, "unconverted")).join("");
      if (unconvEmpty) unconvEmpty.hidden = unconv.length > 0;
    }
  }

  async function loadLibrary(quiet = true) {
    try {
      const data = await api("/api/library");
      renderLibrary(data.library || {});
      if (data.progress) renderLibraryProgress(data.progress);
      if (data.library && data.library.initialScanDone) libraryScannedOnce = true;
      return data;
    } catch (e) {
      if (!quiet) toast(e.message, "error");
      return null;
    }
  }

  function stopLibraryProgressPoll() {
    if (libraryProgressTimer) {
      clearInterval(libraryProgressTimer);
      libraryProgressTimer = null;
    }
  }

  function startLibraryProgressPoll(onDone) {
    stopLibraryProgressPoll();
    const tick = async () => {
      try {
        const data = await api("/api/library/progress");
        renderLibraryProgress(data.progress);
        if (data.progress && !data.progress.running) {
          stopLibraryProgressPoll();
          await loadLibrary(true);
          // Hide bar shortly after done
          window.setTimeout(() => {
            const wrap = $("libraryProgressWrap");
            if (wrap && data.progress && !data.progress.running) wrap.hidden = true;
          }, 1200);
          if (onDone) onDone(data);
        }
      } catch (_) {}
    };
    tick();
    libraryProgressTimer = setInterval(tick, 300);
  }

  async function scanLibraryFolders({
    toastOnStart = true,
    fetchRemote = true,
    mode = "incremental",
  } = {}) {
    if (!guardHeavyAction("scanning the library")) return;
    try {
      clientLog("info", "Library scan button / request", `mode=${mode} fetchRemote=${fetchRemote}`);
      const data = await api("/api/library/scan", {
        method: "POST",
        body: JSON.stringify({ fetchRemote: !!fetchRemote, mode }),
      });
      libraryScannedOnce = true;
      if (toastOnStart) {
        const startedMsg = mode === "first"
          ? "First time scan…"
          : (data.message || "Scanning for newly added games…");
        toast(data.started ? startedMsg : (data.message || "Scan running"), "ok");
      }
      renderLibraryProgress(data.progress);
      if (data.started || (data.progress && data.progress.running)) {
        startLibraryProgressPoll();
      } else {
        await loadLibrary(true);
      }
    } catch (e) {
      clientLog("error", "Library scan failed", e.message || String(e));
      toast(e.message, "error");
    }
  }

  async function ensureLibraryOnOpen() {
    bindLibraryGridClicks();
    const data = await loadLibrary(true);
    if (!data) return;
    const hasRoots = !!(($("convertedGamesRoot")?.value || "").trim()
      || ($("unconvertedGamesRoot")?.value || "").trim());
    if (hasRoots && data.needsFirstScan && !jobRunning && !libraryScanRunning) {
      clientLog("info", "First time library scan");
      await scanLibraryFolders({ toastOnStart: true, fetchRemote: true, mode: "first" });
    }
  }

  async function openUnconvertedForConvert(game) {
    if (!guardHeavyAction("starting a convert")) return;
    let path = (game && game.path) || "";
    const title = (game && game.title) || "";
    clientLog("info", "Library Convert clicked", path || title);
    if (!path) {
      clientLog("error", "Convert click missing path", title);
      toast("Missing game path. Use Scan for newly added games and try again.", "error");
      return;
    }
    suppressConvertFill = true;
    showTab("convert");
    try {
      const data = await api("/api/library/prepare-convert", {
        method: "POST",
        body: JSON.stringify({ path }),
      });
      const d = data.defaults || {};
      path = data.gameRoot || path;
      if ($("gameRoot")) $("gameRoot").value = path;
      if ($("outputDir")) $("outputDir").value = data.outputDir || "";
      // Game-named exe from prepare; settings for intel/gpu/cleanup/gui
      if ($("outputName")) {
        $("outputName").value = data.suggestedOutputName || d.outputName || "app.exe";
      }
      if ($("gpuFilter")) $("gpuFilter").value = d.gpuFilter != null ? String(d.gpuFilter) : "";
      if ($("unusedFilter")) $("unusedFilter").value = String(d.unusedFilter ?? "0");
      if ($("toIntel")) $("toIntel").checked = d.toIntel === true || d.toIntel === 1 || d.toIntel === "true";
      if ($("windowsGui")) $("windowsGui").checked = d.windowsGui === true || d.windowsGui === 1 || d.windowsGui === "true";
      await savePrefs();
      showTab("convert");
      clientLog(
        "info",
        "prepare-convert OK",
        `root=${path} out=${data.outputDir} exe=${$("outputName")?.value} intel=${$("toIntel")?.checked}`,
      );
      toast(`Ready to convert: ${data.title || title}`, "ok");
    } catch (e) {
      clientLog("error", "prepare-convert failed", e.message || String(e));
      toast(e.message, "error");
      if (/Converted Game Folder/i.test(e.message || "")) showTab("settings");
    } finally {
      window.setTimeout(() => { suppressConvertFill = false; }, 2500);
    }
  }

  function setGameLock(game) {
    const lock = $("gameLock");
    if (!lock) return;
    const running = !!(game && game.running);
    const starting = !!(game && game.starting) || (running && (game.pid == null || game.pid === "" || game.pid === "…"));
    lock.hidden = !running;
    lock.classList.toggle("is-starting", starting);
    document.body.classList.toggle("game-running", running);
    if ($("gameLockEyebrow")) {
      $("gameLockEyebrow").textContent = starting ? "Starting game" : "Game running";
    }
    if ($("gameLockTitle")) {
      if (!running) {
        $("gameLockTitle").textContent = "Started";
      } else if (starting) {
        $("gameLockTitle").textContent = `Starting ${game.title || "game"}…`;
      } else {
        $("gameLockTitle").textContent = `${game.title || "Game"} started and running`;
      }
    }
    if ($("gameLockDetail")) {
      if (!running) {
        $("gameLockDetail").textContent = "Close the game to unlock the launcher.";
      } else if (starting) {
        $("gameLockDetail").textContent = "Preparing runtimes and launching — please wait…";
      } else if (game.logPath) {
        $("gameLockDetail").textContent =
          `PID ${game.pid || "—"} · log: ${String(game.logPath).split(/[/\\\\]/).pop()}`;
      } else {
        $("gameLockDetail").textContent =
          `PID ${game.pid || "—"} · Close the game to unlock the launcher.`;
      }
    }
    if ($("jobStatus") && running) {
      $("jobStatus").textContent = starting
        ? `Starting ${game.title || "game"}…`
        : `Game ${game.title || ""} started and running`;
    }
  }

  function stopGamePoll() {
    if (gamePollTimer) {
      clearInterval(gamePollTimer);
      gamePollTimer = null;
    }
  }

  function startGamePoll() {
    stopGamePoll();
    const tick = async () => {
      try {
        const data = await api("/api/game/status");
        const g = data.game || {};
        setGameLock(g);
        if (!g.running) {
          stopGamePoll();
          playInFlight = false;
          if ($("jobStatus") && !jobRunning) $("jobStatus").textContent = "Ready";
          clientLog("info", "Game session ended", g.title || "");
          toast(g.title ? `${g.title} closed` : "Game closed", "ok");
        }
      } catch (_) {}
    };
    tick();
    gamePollTimer = setInterval(tick, 1000);
  }

  async function playLibraryGame(game) {
    if (playInFlight || document.body.classList.contains("game-running")) {
      toast("A game is already starting or running.", "error");
      return;
    }
    if (!guardHeavyAction("starting a game")) return;
    playInFlight = true;
    // Immediate lock so rapid clicks cannot queue more launches
    setGameLock({
      running: true,
      starting: true,
      title: game.title || "Game",
      pid: "…",
      logPath: "",
    });
    try {
      const data = await api("/api/start", {
        method: "POST",
        body: JSON.stringify({
          id: game.id,
          title: game.title || "",
          hideGameConsole: !!$("settingsHideGameConsole")?.checked,
        }),
      });
      const g = data.game || {
        running: true,
        title: game.title,
        exe: data.exe,
        pid: data.game && data.game.pid,
      };
      setGameLock(g);
      toast(data.message || `Game ${g.title || ""} started and running`, "ok");
      clientLog("info", "Game started from library", data.exe || "");
      startGamePoll();
    } catch (e) {
      playInFlight = false;
      setGameLock({ running: false });
      clientLog("error", "Game start failed", e.message || String(e));
      toast(e.message, "error");
    }
  }

  function bindLibraryGridClicks() {
    const root = $("tab-library");
    if (!root || root._libraryBound) return;
    root._libraryBound = true;
    root.addEventListener("click", (ev) => {
      const card = ev.target.closest && ev.target.closest(".library-card");
      if (!card) return;
      ev.preventDefault();
      const id = card.dataset.id || "";
      const kind = card.dataset.kind || "";
      let path = "";
      try { path = decodeURIComponent(card.dataset.path || ""); } catch (_) { path = card.dataset.path || ""; }
      const title = card.dataset.title || "";
      const fromList = [...(libraryData.converted || []), ...(libraryData.unconverted || [])]
        .find((g) => g.id === id);
      const game = fromList || { id, path, title, kind };
      if (!game.path) game.path = path;
      clientLog("debug", "Library card click", `${kind} ${path || id}`);
      if (kind === "converted") playLibraryGame(game);
      else openUnconvertedForConvert(game);
    });
    root.addEventListener("pointerover", (ev) => {
      const card = ev.target.closest && ev.target.closest(".library-card");
      if (!card) return;
      let url = "";
      try { url = decodeURIComponent(card.dataset.backdrop || ""); } catch (_) { url = card.dataset.backdrop || ""; }
      if (url) setLibraryBackdrop(url);
    });
    root.addEventListener("focusin", (ev) => {
      const card = ev.target.closest && ev.target.closest(".library-card");
      if (!card) return;
      let url = "";
      try { url = decodeURIComponent(card.dataset.backdrop || ""); } catch (_) { url = card.dataset.backdrop || ""; }
      if (url) setLibraryBackdrop(url);
    });
  }

  async function saveSettings({ quiet = true } = {}) {
    await api("/api/config", {
      method: "POST",
      body: JSON.stringify({
        unconvertedGamesRoot: ($("unconvertedGamesRoot")?.value || "").trim(),
        convertedGamesRoot: ($("convertedGamesRoot")?.value || "").trim(),
        outputName: ($("settingsOutputName")?.value || "").trim() || "app.exe",
        gpuFilter: ($("settingsGpuFilter")?.value || "").trim(),
        unusedFilter: $("settingsUnusedFilter")?.value || "0",
        toIntel: !!$("settingsToIntel")?.checked,
        windowsGui: !!$("settingsWindowsGui")?.checked,
        hideGameConsole: !!$("settingsHideGameConsole")?.checked,
        logLevel: $("settingsLogLevel")?.value || "info",
      }),
    });
    // Mirror converter defaults into Convert form (unless prefill lock)
    if (!suppressConvertFill) {
      if ($("gpuFilter")) $("gpuFilter").value = ($("settingsGpuFilter")?.value || "").trim();
      if ($("unusedFilter")) $("unusedFilter").value = $("settingsUnusedFilter")?.value || "0";
      if ($("toIntel")) $("toIntel").checked = !!$("settingsToIntel")?.checked;
      if ($("windowsGui")) $("windowsGui").checked = !!$("settingsWindowsGui")?.checked;
    }
    clientLog("debug", "Settings auto-saved", `logLevel=${$("settingsLogLevel")?.value || "info"}`);
    const badge = $("settingsSaveBadge");
    if (badge) {
      badge.hidden = false;
      badge.textContent = "Saved";
      badge.className = "badge ok";
      clearTimeout(saveSettings._badgeT);
      saveSettings._badgeT = setTimeout(() => { badge.hidden = true; }, 1200);
    }
    if (!quiet) toast("Settings saved", "ok");
  }

  function scheduleSettingsSave() {
    clearTimeout(settingsSaveTimer);
    settingsSaveTimer = setTimeout(() => {
      saveSettings({ quiet: true }).catch((e) => {
        clientLog("error", "Settings auto-save failed", e.message || String(e));
        toast(e.message, "error");
      });
    }, 350);
  }

  function bindSettingsAutosave() {
    const ids = [
      "unconvertedGamesRoot", "convertedGamesRoot",
      "settingsOutputName", "settingsGpuFilter", "settingsUnusedFilter",
      "settingsToIntel", "settingsWindowsGui", "settingsHideGameConsole", "settingsLogLevel",
    ];
    ids.forEach((id) => {
      const el = $(id);
      if (!el || el._autosaveBound) return;
      el._autosaveBound = true;
      el.addEventListener("change", scheduleSettingsSave);
      el.addEventListener("input", scheduleSettingsSave);
    });
  }

  function renderAlerts(g, b) {
    const box = $("alerts");
    if (!box) return;
    const items = [];
    if (g.updateAvailable) {
      items.push({
        kind: "warn",
        html: `<strong>Update available:</strong> ${g.behind} new change(s).
          <div class="alert-actions">
            <button class="btn compact primary" type="button" data-alert="updates">Updates</button>
            <button class="btn compact" type="button" data-alert="pull">Download</button>
          </div>`,
      });
    }
    if (b.syncState === "outdated") {
      items.push({
        kind: "danger",
        html: `<strong>Build is outdated.</strong> Run Build now after updating.
          <div class="alert-actions">
            <button class="btn compact primary" type="button" data-alert="rebuild">Build now</button>
          </div>`,
      });
    } else if (b.syncState === "unknown" && b.ready) {
      items.push({
        kind: "warn",
        html: `<strong>Build status unclear.</strong> Build again or mark as up to date.
          <div class="alert-actions">
            <button class="btn compact primary" type="button" data-alert="rebuild">Build now</button>
            <button class="btn compact" type="button" data-alert="mark">Mark as up to date</button>
          </div>`,
      });
    } else if (b.syncState === "need_setup") {
      items.push({
        kind: "warn",
        html: `<strong>AnyPS5 source not ready yet.</strong> Open Setup and run Install / repair (this clones the project). Build comes after that.
          <div class="alert-actions">
            <button class="btn compact primary" type="button" data-alert="setup">Open Setup</button>
          </div>`,
      });
    } else if (b.syncState === "missing") {
      items.push({
        kind: "warn",
        html: `<strong>Not built yet.</strong> Tools/source look ready. Open Build and run Build now.
          <div class="alert-actions">
            <button class="btn compact primary" type="button" data-alert="rebuild">Build now</button>
          </div>`,
      });
    } else if (b.syncState === "current" && !g.updateAvailable) {
      items.push({
        kind: "ok",
        html: `<strong>Everything is up to date.</strong>`,
      });
    }
    if (!items.length) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    box.innerHTML = items.map((i) => `<div class="alert ${i.kind}">${i.html}</div>`).join("");
    box.querySelectorAll("[data-alert]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const a = btn.getAttribute("data-alert");
        if (a === "updates") showTab("updates");
        if (a === "pull") { showTab("updates"); $("btnPull")?.click(); }
        if (a === "rebuild") { showTab("build"); $("btnRebuild")?.click(); }
        if (a === "mark") $("btnMarkBuild")?.click();
        if (a === "setup") showTab("setup");
      });
    });
  }

  function applyStatus(data) {
    const g = data.git || {};
    const b = data.build || {};
    if (data.githubUrl) githubUrl = data.githubUrl;
    if (data.launcherGithubUrl) launcherGithubUrl = data.launcherGithubUrl;
    $("statBranch").textContent = g.branch || "-";
    $("statHead").textContent = g.head || "-";
    $("repoPath").textContent = data.repoRoot || "";

    if (g.updateAvailable) {
      $("statUpdate").innerHTML = `<span class="warn-text">${g.behind} new</span>`;
      $("updateBadge").textContent = `${g.behind} update(s) available`;
      $("updateBadge").className = "badge warn";
    } else if (g.remoteOk !== false) {
      $("statUpdate").innerHTML = `<span class="ok-text">current</span>`;
      $("updateBadge").textContent = "Up to date";
      $("updateBadge").className = "badge ok";
    } else {
      $("statUpdate").innerHTML = `<span class="danger-text">offline?</span>`;
      $("updateBadge").textContent = "Fetch failed";
      $("updateBadge").className = "badge danger";
    }

    renderBuildStat(b, data.job);
    if ($("anyps5RootLabel") && data.repoRoot) {
      $("anyps5RootLabel").textContent = data.repoRoot;
    }
    if ($("launcherVerLabel")) {
      const ver = data.launcherVersion || (data.launcherUpdate && data.launcherUpdate.currentVersion) || "?";
      $("launcherVerLabel").textContent = `Launcher v${ver}`;
      $("launcherVerLabel").title = "Open launcher GitHub";
    }

    if ($("buildSyncLabel")) $("buildSyncLabel").textContent = b.syncLabel || "";
    $("bRelinker").textContent = b.relinker ? "OK" : "missing";
    $("bRelinker").className = b.relinker ? "ok-text" : "danger-text";
    $("bPrx").textContent = String(b.prxCount ?? 0);
    $("bGcc").textContent = b.winlibs ? "OK" : "missing";
    $("bGcc").className = b.winlibs ? "ok-text" : "danger-text";
    $("bCmake").textContent = b.cmake ? "OK" : "missing";
    $("bCmake").className = b.cmake ? "ok-text" : "danger-text";

    renderAlerts(g, b);
    renderCommits($("pendingLog"), g.pendingLog, g.updateAvailable ? "No details." : "No new commits.");
    renderCommits($("localLog"), g.localLog, "No log.");
    fillConfig(data.config);

    if (data.job && data.job.running) {
      setBusy(true, data.job.kind || "Job");
      startJobPoll();
    } else {
      updateJobLocks(false);
    }
  }

  function renderBuildStat(build, job) {
    const el = $("statBuild");
    const bar = $("statBuildProgress");
    const fill = $("statBuildProgressFill");
    if (!el) return;
    const running = !!(job && job.running);
    const kind = String((job && job.kind) || "");
    const isBuildJob = running && /build|rebuild/i.test(kind);
    if (isBuildJob) {
      const pct = job.progress != null ? Math.max(0, Math.min(100, Number(job.progress))) : null;
      const pctLabel = pct != null && !Number.isNaN(pct) ? `${Math.round(pct)}%` : "…";
      el.innerHTML = `<span class="warn-text">Building ${pctLabel}</span>`;
      if (bar) {
        bar.hidden = false;
        if (fill) {
          if (pct != null && !Number.isNaN(pct)) {
            fill.style.width = `${pct}%`;
            fill.classList.remove("indeterminate");
          } else {
            fill.style.width = "35%";
            fill.classList.add("indeterminate");
          }
        }
      }
      return;
    }
    if (bar) bar.hidden = true;
    if (fill) {
      fill.style.width = "0%";
      fill.classList.remove("indeterminate");
    }
    const b = build || {};
    if (b.syncState === "current") {
      el.innerHTML = `<span class="ok-text">ready</span>`;
    } else if (b.syncState === "outdated") {
      el.innerHTML = `<span class="danger-text">outdated</span>`;
    } else if (b.ready) {
      el.innerHTML = `<span class="warn-text">check needed</span>`;
    } else if (b.syncState === "need_setup") {
      el.innerHTML = `<span class="warn-text">need setup</span>`;
    } else {
      el.innerHTML = `<span class="warn-text">not built</span>`;
    }
  }

  async function refresh() {
    const data = await api("/api/status");
    applyStatus(data);
    return data;
  }

  async function updateCheck(quiet = false) {
    if (!quiet) toast("Checking GitHub...");
    await api("/api/update-check", { method: "POST", body: "{}" });
    const data = await refresh();
    const g = data.git || {};
    const b = data.build || {};
    if (g.updateAvailable) toast(`${g.behind} update(s) available`, "ok");
    else if (b.syncState === "outdated") toast("Updated, but build is outdated. Open Build.", "error");
    else if (g.remoteOk !== false) { if (!quiet) toast("Already up to date", "ok"); }
    else toast("Update check failed", "error");
    return data;
  }

  function openLauncherUpdateModal(info) {
    const modal = $("launcherUpdateModal");
    if (!modal) return Promise.resolve();
    const cur = info.currentVersion || "?";
    const latest = info.latestVersion || info.tagName || "?";
    const body = $("launcherUpdateBody");
    if (body) {
      body.textContent = `Version ${latest} is available (you have ${cur}). `
        + (info.canAutoUpdate
          ? "Update Now downloads the release, replaces the launcher files, and restarts automatically. Your AnyPS5 folder and settings are kept."
          : "Download the new zip from GitHub Releases and replace this launcher folder (or run the released .exe build to enable auto-update).");
    }
    const notes = $("launcherUpdateNotes");
    if (notes) {
      const text = (info.releaseNotes || "").trim();
      if (text) {
        notes.hidden = false;
        notes.textContent = text;
      } else {
        notes.hidden = true;
        notes.textContent = "";
      }
    }
    const status = $("launcherUpdateStatus");
    if (status) status.textContent = "Choose Update Now or Keep using Old version to continue.";
    const btnNow = $("btnLauncherUpdateNow");
    if (btnNow) {
      btnNow.disabled = false;
      btnNow.textContent = info.canAutoUpdate ? "Update Now" : "Open Releases";
      btnNow.dataset.mode = info.canAutoUpdate ? "auto" : "browser";
      btnNow.dataset.htmlUrl = info.htmlUrl || "";
    }
    const btnKeep = $("btnLauncherKeepOld");
    if (btnKeep) btnKeep.disabled = false;
    document.body.classList.add("update-gate");
    modal.hidden = false;
    // Force a decision before the rest of the UI continues
    return new Promise((resolve) => {
      launcherUpdateResolve = resolve;
    });
  }

  function closeLauncherUpdateModal() {
    const modal = $("launcherUpdateModal");
    if (modal) modal.hidden = true;
    document.body.classList.remove("update-gate");
    if (launcherUpdateResolve) {
      const done = launcherUpdateResolve;
      launcherUpdateResolve = null;
      done();
    }
  }

  async function checkLauncherUpdateOnce() {
    if (launcherUpdatePrompted) return;
    launcherUpdatePrompted = true;
    try {
      const data = await api("/api/launcher/update-check");
      if (data && data.updateAvailable) {
        await openLauncherUpdateModal(data);
      }
    } catch (_) {
      // Silent: offline / rate limit should not block the UI
    }
  }

  async function startLauncherUpdate() {
    if (launcherUpdateBusy) return;
    const btnNow = $("btnLauncherUpdateNow");
    const mode = (btnNow && btnNow.dataset.mode) || "auto";
    if (mode === "browser") {
      const url = (btnNow && btnNow.dataset.htmlUrl)
        || "https://github.com/Tiberius1OOO/AnyPS5-Launcher/releases";
      window.open(url, "_blank", "noopener");
      closeLauncherUpdateModal();
      return;
    }
    launcherUpdateBusy = true;
    if (btnNow) btnNow.disabled = true;
    const btnKeep = $("btnLauncherKeepOld");
    if (btnKeep) btnKeep.disabled = true;
    const status = $("launcherUpdateStatus");
    if (status) {
      status.textContent = "Starting download… watch the launcher console and the Updater window.";
    }
    setBusy(true);

    let poll = null;
    const finishFail = (msg) => {
      if (poll) window.clearInterval(poll);
      launcherUpdateBusy = false;
      if (btnNow) btnNow.disabled = false;
      if (btnKeep) btnKeep.disabled = false;
      if (status) status.textContent = msg || "";
      setBusy(false);
      toast(msg || "Update failed", "error");
    };

    const tick = async () => {
      try {
        const st = await api("/api/launcher/update-check?force=0");
        if (!st) return;
        if (status) {
          if (st.phase === "downloading") {
            const pct = (st.downloadPercent != null) ? ` ${Math.round(st.downloadPercent)}%` : "";
            status.textContent = `Downloading update${pct}… see console for details.`;
          } else if (st.phase === "extracting") {
            status.textContent = "Extracting update…";
          } else if (st.phase === "restarting") {
            status.textContent = "Updater window opened — replacing files and relaunching…";
          } else if (st.phase === "failed") {
            finishFail(st.error || "Update failed — see launcher console.");
            return;
          }
        }
        if (st.phase === "restarting") {
          toast("Updater running — launcher will restart", "ok");
        }
      } catch (_) {}
    };
    poll = window.setInterval(tick, 400);

    try {
      // Returns immediately; work continues in a background thread + updater console
      await api("/api/launcher/update", { method: "POST", body: "{}" });
      await tick();
    } catch (e) {
      finishFail(e.message || "Update failed");
    }
  }

  function showTab(name) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
    if (name === "readme") loadReadme();
    if (name === "setup") runSetupAudit(true).catch(() => {});
    if (name === "settings") {
      loadInput().catch((e) => toast(e.message, "error"));
    }
    if (name === "library") {
      ensureLibraryOnOpen().catch(() => {});
      clientLog("debug", "Opened Library tab");
    }
    if (name === "convert") {
      clientLog("debug", "Opened Convert tab");
    }
  }

  function applyBarFill(fill, pct, busy) {
    if (!fill) return;
    const known = pct != null && !Number.isNaN(Number(pct));
    fill.classList.toggle("indeterminate", !known);
    fill.classList.toggle("busy", !!(busy || !known));
    if (known) {
      fill.style.width = `${Math.max(0, Math.min(100, Number(pct)))}%`;
    } else {
      fill.style.width = "";
    }
  }

  function renderJobProgress(job) {
    const wrap = $("jobProgressWrap");
    const label = $("jobProgressLabel");
    const pctEl = $("jobProgressPct");
    const detail = $("jobProgressDetail");
    const fill = $("jobProgressFill");
    if (!wrap) return;
    const running = !!(job && job.running);
    const phaseLabel = (job && (job.phaseLabel || job.phase)) || "";
    const pct = job && job.progress != null ? Number(job.progress) : null;
    const rate = (job && job.progressRate) ? String(job.progressRate).trim() : "";
    const busy = !!(job && job.progressBusy);
    if (!running && !(job && job.ok === false && phaseLabel)) {
      wrap.hidden = true;
      if (fill) {
        fill.classList.remove("busy", "indeterminate");
      }
      return;
    }
    wrap.hidden = false;
    if (label) {
      label.textContent = phaseLabel
        || (running ? `Running: ${job.kind || "Job"}` : "Job");
    }
    if (pctEl) {
      const pctBit = pct != null && !Number.isNaN(pct)
        ? `${Math.round(pct)}%`
        : (running ? "…" : "");
      pctEl.textContent = rate && pctBit ? `${pctBit} · ${rate}` : pctBit;
    }
    if (detail) detail.textContent = (job && job.progressDetail) || "";
    applyBarFill(fill, pct, busy || (running && !!rate));
  }

  async function enrichJobWithSetupProgress(job) {
    // Setup has its own progress API; mirror it into the Console bar so it is not stuck.
    if (!job || !job.running) return job;
    const kind = String(job.kind || "");
    if (!/setup/i.test(kind)) return job;
    try {
      const data = await api("/api/setup/progress");
      const p = data && data.progress;
      if (!p) return job;
      const steps = p.steps || [];
      const total = p.total || steps.length || 0;
      const done = p.done || 0;
      const cur = steps.find((s) => s.status === "running");
      const transfer = p.transfer || null;
      let pct = total > 0 ? (done / total) * 100 : null;
      if (transfer && transfer.percent != null && total > 0) {
        // Blend in-step download/clone % into the overall step slice
        const slice = 100 / total;
        pct = Math.min(99, done * slice + (Number(transfer.percent) / 100) * slice);
      }
      const phaseBits = [];
      if (cur) phaseBits.push(cur.name);
      if (transfer && transfer.phase) phaseBits.push(transfer.phase);
      job.phaseLabel = phaseBits.length
        ? `Setup ${done}/${total}: ${phaseBits.join(" — ")}`
        : `Setup ${done}/${total}`;
      job.progress = pct;
      job.progressDetail = (transfer && transfer.label)
        || (transfer && transfer.received
          ? `${transfer.received}${transfer.total ? " / " + transfer.total : ""}${transfer.speed ? " · " + transfer.speed : ""}`
          : "")
        || (p.detail || "");
    } catch (_) {}
    return job;
  }

  async function pollJobOnce() {
    let job = await api("/api/job");
    job = await enrichJobWithSetupProgress(job);
    const lines = job.lines || [];
    $("console").textContent = lines.length ? lines.join("\n") : ($("console").textContent || "...");
    $("console").scrollTop = $("console").scrollHeight;
    lastLineCount = lines.length;
    renderJobProgress(job);
    updateJobLocks(!!job.running, job.kind || "");
    // Live Build chip in the hero while compiling
    if (job.running && /build|rebuild/i.test(String(job.kind || ""))) {
      renderBuildStat(null, job);
    } else if (!job.running) {
      const mini = $("statBuildProgress");
      if (mini && !mini.hidden && job.ok != null) {
        // leave chip until refresh() reloads real build status
      }
    }
    if (job.running) {
      const pctBit = job.progress != null ? ` ${Math.round(job.progress)}%` : "";
      const bit = job.phaseLabel
        ? `${job.phaseLabel}${job.phaseLabel.includes("%") ? "" : pctBit}`
        : `Running: ${job.kind || "Job"}${pctBit}`;
      $("jobStatus").textContent = bit;
    } else {
      $("jobStatus").textContent =
        job.ok === false ? "Error" : job.ok === true ? "Done" : "Ready";
    }
    if ($("logPathHint") && job.logPath) {
      const hist = job.logHistoryPath || "";
      const gameLog = job.logGamePath || "";
      if (hist) {
        $("logPathHint").textContent = gameLog
          ? `Convert log: ${hist}  |  beside game: ${gameLog}`
          : `Log file: ${hist}`;
      } else {
        $("logPathHint").textContent = `Log file: ${job.logPath}`;
      }
    }
    return job;
  }

  function startJobPoll() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    const tick = async () => {
      try {
        const job = await pollJobOnce();
        if (!job.running) {
          clearInterval(pollTimer);
          pollTimer = null;
          setBusy(false);
          await refresh();
          if (job.ok === true) {
            toast(`${job.kind || "Job"} finished`, "ok");
            if (/convert/i.test(String(job.kind || ""))) {
              playConvertChime();
              clientLog("info", "Convert job finished OK — refreshing library");
              showTab("library");
              scanLibraryFolders({ toastOnStart: false, fetchRemote: true, mode: "incremental" });
            }
          }
          if (job.ok === false) {
            const sendLog = job.logHistoryPath || job.logPath || "";
            const logHint = sendLog
              ? ` Send ${sendLog.split(/[/\\\\]/).pop()} if you need help.`
              : "";
            toast(`${job.kind || "Job"} failed. See Console (scroll up for the real error).${logHint}`, "error");
          }
        }
      } catch (e) {
        clearInterval(pollTimer);
        pollTimer = null;
        setBusy(false);
        toast("Job status error: " + e.message, "error");
      }
    };
    tick();
    pollTimer = setInterval(tick, 500);
  }

  async function beginJob(label, startFn, opts = {}) {
    if (!guardHeavyAction("starting another operation")) return;
    try {
      setBusy(true, opts.kind || label || "Job");
      lastLineCount = 0;
      if (!opts.stayOnTab) showTab("log");
      $("console").textContent = `${label}\n`;
      await startFn();
      startJobPoll();
    } catch (e) {
      setBusy(false);
      showTab("log");
      $("console").textContent += `\nERROR: ${e.message}\n`;
      toast(e.message, "error");
    }
  }

  function rewriteReadmeLinks(html) {
    const base = githubUrl.replace(/\/$/, "") + "/blob/main/";
    const rawBase = "https://raw.githubusercontent.com/boykopovar/AnyPS5/main/";
    const wrap = document.createElement("div");
    wrap.innerHTML = html;
    wrap.querySelectorAll("a[href]").forEach((a) => {
      const href = a.getAttribute("href") || "";
      if (/^https?:\/\//i.test(href) || href.startsWith("#") || href.startsWith("mailto:")) return;
      a.setAttribute("href", new URL(href, base).href);
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
    });
    wrap.querySelectorAll("img[src]").forEach((img) => {
      const src = img.getAttribute("src") || "";
      if (/^https?:\/\//i.test(src) || src.startsWith("data:")) return;
      img.setAttribute("src", new URL(src, rawBase).href);
    });
    return wrap.innerHTML;
  }

  async function loadReadme(force = false) {
    if (readmeLoaded && !force) return;
    $("readmeBody").innerHTML = `<p class="empty">Loading README from GitHub...</p>`;
    $("readmeMeta").textContent = "Loading...";
    try {
      const data = await api("/api/readme");
      const md = data.markdown || "";
      let html = (window.marked && window.marked.parse)
        ? window.marked.parse(md, { gfm: true, breaks: false })
        : `<pre>${escapeHtml(md)}</pre>`;
      $("readmeBody").innerHTML = rewriteReadmeLinks(html);
      const srcLabel = data.source === "github" ? "GitHub main" : "local fallback";
      $("readmeMeta").textContent = `Source: ${srcLabel}` + (data.warning ? ` (${data.warning})` : "");
      if (data.url) githubUrl = data.url;
      readmeLoaded = true;
    } catch (e) {
      $("readmeBody").innerHTML = `<p class="empty">Failed: ${escapeHtml(e.message)}</p>`;
      $("readmeMeta").textContent = "Error";
    }
  }

  function openGithub() {
    // Open once in the browser tab — server must not also call webbrowser.open
    window.open(githubUrl || GITHUB_FALLBACK, "_blank", "noopener,noreferrer");
  }

  function openLauncherGithub() {
    window.open(launcherGithubUrl || LAUNCHER_GITHUB_FALLBACK, "_blank", "noopener,noreferrer");
  }

  function renderSetupList(audit) {
    const box = $("setupList");
    const badge = $("setupBadge");
    if (!box) return;
    const checks = (audit && audit.checks) || [];
    if (!checks.length) {
      box.innerHTML = `<p class="empty">No data.</p>`;
      return;
    }
    box.innerHTML = checks.map((c) => `
      <div class="setup-item">
        <div class="st ${escapeHtml(c.status)}">${escapeHtml(c.status === "todo" ? "next" : c.status)}</div>
        <div>
          <div class="name">${escapeHtml(c.name)}${c.required ? "" : " (optional)"}</div>
          <div class="detail">${escapeHtml(c.detail || "")}</div>
        </div>
      </div>
    `).join("");
    if (badge) {
      const toolIssues = (audit.missingRequired || []).length;
      const nextSteps = (audit.nextSteps || []).length;
      if (toolIssues > 0) {
        badge.textContent = `${toolIssues} tool issue(s)`;
        badge.className = "badge danger";
      } else if (audit.allRequiredOk) {
        badge.textContent = "Ready. Build next";
        badge.className = "badge ok";
      } else if (nextSteps > 0) {
        badge.textContent = `${nextSteps} step(s) left`;
        badge.className = "badge warn";
      } else {
        badge.textContent = "Checked";
        badge.className = "badge";
      }
    }
  }

  function renderSetupProgress(progress) {
    const wrap = $("setupProgressWrap");
    const stepList = $("setupStepList");
    const label = $("setupProgressLabel");
    const detail = $("setupProgressDetail");
    const fill = $("setupProgressFill");
    const tWrap = $("setupTransferWrap");
    const tPhase = $("setupTransferPhase");
    const tPct = $("setupTransferPct");
    const tDetail = $("setupTransferDetail");
    const tFill = $("setupTransferFill");
    if (!wrap || !progress) return;

    const steps = progress.steps || [];
    const total = progress.total || steps.length || 0;
    const done = progress.done || 0;
    const transfer = progress.transfer || null;

    wrap.hidden = false;
    if (label) label.textContent = `${done} / ${total}`;
    // Overall line = which install step. Transfer block below = live clone/download detail.
    // Never copy transfer.label into both places (that showed the same path twice).
    if (detail) {
      const cur = steps.find((s) => s.status === "running");
      if (progress.running && cur) {
        const bite = transfer && transfer.phase ? ` — ${transfer.phase}` : "";
        detail.textContent = `Installing: ${cur.name}${bite}`;
      } else if (progress.detail) detail.textContent = progress.detail;
      else if (progress.ok === false) detail.textContent = "Failed. See Console";
      else if (progress.ok === true) detail.textContent = "Everything detected / install finished";
      else detail.textContent = "Checking this PC…";
    }
    if (fill) {
      const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
      fill.style.width = `${pct}%`;
    }

    if (tWrap) {
      if (progress.running && transfer) {
        tWrap.hidden = false;
        const pct = transfer.percent != null ? Math.max(0, Math.min(100, Number(transfer.percent))) : null;
        if (tPhase) tPhase.textContent = transfer.phase || "Transfer";
        if (tPct) tPct.textContent = pct != null ? `${Math.round(pct)}%` : "…";
        if (tDetail) {
          const bits = [];
          if (transfer.label) bits.push(transfer.label);
          if (transfer.received && transfer.total) bits.push(`${transfer.received} / ${transfer.total}`);
          else if (transfer.received) bits.push(transfer.received);
          if (transfer.speed) bits.push(transfer.speed);
          // If label already is the only content and equals phase, show nothing extra
          const joined = bits.join(" · ");
          tDetail.textContent =
            joined && joined !== (transfer.phase || "") ? joined : "";
        }
        applyBarFill(tFill, pct);
      } else {
        tWrap.hidden = true;
        if (tFill) {
          tFill.classList.remove("indeterminate");
          tFill.style.width = "0%";
        }
      }
    }

    if (stepList) {
      stepList.hidden = false;
      stepList.innerHTML = steps.length
        ? steps.map((s) => `
        <div class="setup-item">
          <div class="st ${escapeHtml(s.status)}">${escapeHtml(s.status === "todo" ? "next" : s.status)}</div>
          <div>
            <div class="name">${escapeHtml(s.name || s.id)}</div>
            <div class="detail">${escapeHtml(s.detail || "")}</div>
          </div>
        </div>
      `).join("")
        : `<p class="empty">No steps yet.</p>`;
    }
  }

  function stopProgressPoll() {
    if (progressTimer) {
      clearInterval(progressTimer);
      progressTimer = null;
    }
  }

  function startProgressPoll() {
    stopProgressPoll();
    let sawRunning = false;
    const tick = async () => {
      try {
        const data = await api("/api/setup/progress");
        renderSetupProgress(data.progress);
        if (data.progress && data.progress.running) sawRunning = true;
        // Only stop after we observed a run and it finished (avoid race on first tick)
        if (sawRunning && data.progress && !data.progress.running) {
          stopProgressPoll();
          try { await runSetupAudit(true); } catch (_) {}
        }
      } catch (_) {
        /* keep polling while job runs */
      }
    };
    tick();
    progressTimer = setInterval(tick, 250);
  }

  function openAdminWhyModal(mode) {
    pendingInstallMode = mode;
    const modal = $("adminWhyModal");
    if (!modal) return;
    if (adminWhyCopy) {
      const title = $("adminWhyTitle");
      const body = $("adminWhyBody");
      if (title && adminWhyCopy.title) title.textContent = adminWhyCopy.title;
      if (body && adminWhyCopy.body) body.textContent = adminWhyCopy.body;
    }
    modal.hidden = false;
  }

  function closeAdminWhyModal() {
    const modal = $("adminWhyModal");
    if (modal) modal.hidden = true;
    pendingInstallMode = null;
  }

  async function startSetupInstall({ elevate }) {
    closeAdminWhyModal();
    if (elevate) {
      try {
        const data = await api("/api/setup/install", {
          method: "POST",
          body: JSON.stringify({ elevate: true }),
        });
        toast(data.message || "Admin installer started. Click Re-check when it finishes.", "ok");
        // Poll audit so UI updates when elevated install finishes writing tools/repo
        let n = 0;
        const poll = setInterval(async () => {
          n += 1;
          try { await runSetupAudit(true); } catch (_) {}
          if (n >= 40) clearInterval(poll);
        }, 3000);
      } catch (e) {
        toast(e.message, "error");
      }
      return;
    }
    showTab("setup");
    beginJob(
      "Starting setup / install...",
      async () => {
        await api("/api/setup/install", { method: "POST", body: JSON.stringify({}) });
        startProgressPoll();
      },
      { stayOnTab: true }
    );
  }

  function requestSetupInstall() {
    // Tools already present → start install/clone immediately (no modal stall)
    if (lastSetupAudit && lastSetupAudit.toolsOk) {
      startSetupInstall({ elevate: false });
      return;
    }
    openAdminWhyModal("plain");
  }

  async function runSetupAudit(quiet = false) {
    if (!quiet) toast("Checking installation...");
    const data = await api("/api/setup/audit");
    lastSetupAudit = data.audit || null;
    renderSetupList(data.audit);
    if (data.adminWhy) adminWhyCopy = data.adminWhy;
    if (data.progress) renderSetupProgress(data.progress);
    const target = data.installTarget || (data.audit && data.audit.repoRoot) || "";
    if (target && $("anyps5RootLabel")) $("anyps5RootLabel").textContent = target;
    if (!quiet) {
      if (data.audit && data.audit.allRequiredOk) toast("Everything detected. You can Build next", "ok");
      else if (data.audit && data.audit.toolsOk) toast("Tools detected. Install will clone AnyPS5 if needed.", "ok");
      else toast("Some tools missing. Install can fix them (admin may be needed).", "error");
    }
    return data;
  }

  function initTipBubbles() {
    const bubble = $("tipBubble");
    if (!bubble) return;

    const hide = () => {
      bubble.classList.remove("visible");
      bubble.hidden = true;
      bubble.textContent = "";
    };

    const place = (btn) => {
      const text = btn.getAttribute("data-tip") || "";
      if (!text) return;
      bubble.textContent = text;
      bubble.hidden = false;
      bubble.classList.add("visible");

      const pad = 10;
      const rect = btn.getBoundingClientRect();
      // Measure after show
      const bw = bubble.offsetWidth || 280;
      const bh = bubble.offsetHeight || 60;
      let left = rect.left + rect.width / 2 - bw / 2;
      let top = rect.top - bh - 10;
      // Prefer above; if clipped, show below
      if (top < pad) top = rect.bottom + 10;
      left = Math.max(pad, Math.min(left, window.innerWidth - bw - pad));
      top = Math.max(pad, Math.min(top, window.innerHeight - bh - pad));
      bubble.style.left = `${Math.round(left)}px`;
      bubble.style.top = `${Math.round(top)}px`;
    };

    document.addEventListener("pointerover", (ev) => {
      const btn = ev.target.closest && ev.target.closest(".tip");
      if (btn) place(btn);
    });
    document.addEventListener("pointerout", (ev) => {
      const btn = ev.target.closest && ev.target.closest(".tip");
      if (!btn) return;
      const to = ev.relatedTarget;
      if (to && btn.contains(to)) return;
      hide();
    });
    document.addEventListener("focusin", (ev) => {
      const btn = ev.target.closest && ev.target.closest(".tip");
      if (btn) place(btn);
    });
    document.addEventListener("focusout", (ev) => {
      const btn = ev.target.closest && ev.target.closest(".tip");
      if (btn) hide();
    });
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
  }

  async function savePrefs() {
    await api("/api/config", {
      method: "POST",
      body: JSON.stringify({
        lastGameRoot: ($("gameRoot")?.value || "").trim(),
        lastOutputDir: ($("outputDir")?.value || "").trim(),
        outputName: ($("outputName")?.value || "").trim() || "app.exe",
        gpuFilter: ($("gpuFilter")?.value || "").trim(),
        unusedFilter: $("unusedFilter")?.value || "0",
        toIntel: !!$("toIntel")?.checked,
        windowsGui: !!$("windowsGui")?.checked,
        unconvertedGamesRoot: ($("unconvertedGamesRoot")?.value || "").trim(),
        convertedGamesRoot: ($("convertedGamesRoot")?.value || "").trim(),
      }),
    });
  }

  async function startGame() {
    if (playInFlight || document.body.classList.contains("game-running")) {
      toast("A game is already starting or running.", "error");
      return;
    }
    if (!guardHeavyAction("starting a game")) return;
    playInFlight = true;
    setGameLock({ running: true, starting: true, title: "Game", pid: "…", logPath: "" });
    try {
      await savePrefs();
      const data = await api("/api/start", {
        method: "POST",
        body: JSON.stringify({
          hideGameConsole: !!$("settingsHideGameConsole")?.checked,
        }),
      });
      const g = data.game || { running: true, title: "Game", exe: data.exe };
      setGameLock(g);
      toast(data.message || `Started: ${data.exe}`, "ok");
      startGamePoll();
    } catch (e) {
      playInFlight = false;
      setGameLock({ running: false });
      toast(e.message + " Use Library or Convert first.", "error");
      showTab("library");
    }
  }

  // tabs
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => showTab(tab.dataset.tab));
  });

  document.querySelectorAll("[data-browse]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const data = await api("/api/browse", {
          method: "POST",
          body: JSON.stringify({ kind: btn.dataset.kind || "folder" }),
        });
        if (data.path) {
          $(btn.dataset.browse).value = data.path;
          const target = btn.dataset.browse || "";
          if (target === "unconvertedGamesRoot" || target === "convertedGamesRoot") {
            await saveSettings({ quiet: true });
          } else {
            await savePrefs();
          }
        }
      } catch (e) {
        toast(e.message, "error");
      }
    });
  });

  on("btnStart2", "click", () => {
    if (!guardHeavyAction("starting a game from Convert")) return;
    startGame();
  });
  on("btnLibraryScan", "click", () => scanLibraryFolders({
    toastOnStart: true,
    fetchRemote: true,
    mode: "incremental",
  }));
  on("btnGameStop", "click", async () => {
    try {
      const data = await api("/api/game/stop", { method: "POST", body: "{}" });
      setGameLock(data.game || { running: false });
      playInFlight = false;
      stopGamePoll();
      toast("Game stop requested", "ok");
    } catch (e) {
      toast(e.message, "error");
    }
  });
  bindSettingsAutosave();
  // Resume lock UI if a game is already running (rare after refresh)
  api("/api/game/status").then((d) => {
    if (d.game && d.game.running) {
      setGameLock(d.game);
      startGamePoll();
    }
  }).catch(() => {});
  on("btnUpdateCheck", "click", () => updateCheck().catch((e) => toast(e.message, "error")));
  on("btnFetch", "click", () => updateCheck().catch((e) => toast(e.message, "error")));
  on("btnOpenGithub", "click", () => openGithub());
  on("btnOpenGithubReadme", "click", () => openGithub());
  on("brandLogo", "click", () => openLauncherGithub());
  on("brandLogo", "keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      openLauncherGithub();
    }
  });
  on("launcherVerLabel", "click", () => openLauncherGithub());
  on("launcherVerLabel", "keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      openLauncherGithub();
    }
  });
  on("btnReloadReadme", "click", () => loadReadme(true));
  on("btnPull", "click", () => {
    beginJob(
      "Starting GitHub update...",
      () => api("/api/update", { method: "POST", body: "{}" }),
      { kind: "Update" },
    );
  });
  on("btnRebuild", "click", () => {
    beginJob(
      "Starting build...",
      () => api("/api/rebuild", { method: "POST", body: "{}" }),
      { kind: "Build" },
    );
  });
  on("btnMarkBuild", "click", async () => {
    try {
      const data = await api("/api/build/mark-current", { method: "POST", body: "{}" });
      applyStatus({ ...(await api("/api/status")), build: data.build });
      await refresh();
      toast("Build marked as up to date", "ok");
    } catch (e) {
      toast(e.message, "error");
    }
  });
  on("btnConvert", "click", async () => {
    if (!guardHeavyAction("converting")) return;
    await savePrefs();
    const gameRoot = $("gameRoot").value.trim();
    if (!gameRoot) {
      toast("Please set a game folder.", "error");
      return;
    }
    try { await saveInput(); } catch (_) {}
    beginJob(
      "Starting convert...",
      () => api("/api/convert", {
        method: "POST",
        body: JSON.stringify({
          gameRoot,
          outputDir: $("outputDir").value.trim(),
          outputName: $("outputName").value.trim() || "app.exe",
          unusedFilter: $("unusedFilter").value,
          toIntel: $("toIntel").checked,
          windowsGui: $("windowsGui").checked,
        }),
      }),
      { kind: "Convert" },
    );
  });
  on("btnClearLog", "click", () => {
    $("console").textContent = "Ready.";
    lastLineCount = 0;
  });
  on("btnOpenLog", "click", async () => {
    try {
      const data = await api("/api/logs/open", { method: "POST", body: "{}" });
      toast(data.path ? `Opened ${data.path}` : "Opened log folder", "ok");
    } catch (e) {
      toast(e.message, "error");
    }
  });
  on("btnSetupAudit", "click", () => runSetupAudit().catch((e) => toast(e.message, "error")));
  on("btnSetupInstall", "click", () => requestSetupInstall());
  on("btnSetupElevate", "click", () => openAdminWhyModal("admin"));
  on("btnAdminContinue", "click", () => startSetupInstall({ elevate: true }));
  on("btnAdminWithout", "click", () => startSetupInstall({ elevate: false }));
  on("btnAdminCancel", "click", () => closeAdminWhyModal());
  on("adminWhyModal", "click", (ev) => {
    if (ev.target === $("adminWhyModal")) closeAdminWhyModal();
  });
  on("btnLauncherUpdateNow", "click", () => startLauncherUpdate());
  on("btnLauncherKeepOld", "click", () => closeLauncherUpdateModal());
  on("launcherUpdateModal", "click", (ev) => {
    // Do not dismiss by backdrop click — user must choose Update or Keep
    if (ev.target === $("launcherUpdateModal")) {
      ev.preventDefault();
      ev.stopPropagation();
    }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    const modal = $("launcherUpdateModal");
    if (modal && !modal.hidden) {
      ev.preventDefault();
      ev.stopPropagation();
    }
  }, true);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) runSetupAudit(true).catch(() => {});
  });
  on("btnInputSave", "click", () => saveInput().catch((e) => toast(e.message, "error")));
  on("btnInputExportJson", "click", () => {
    window.location.href = "/api/input/export";
  });
  on("btnInputExportIni", "click", () => {
    window.location.href = "/api/input/export.ini";
  });
  on("btnInputImport", "click", () => $("inputImportFile")?.click());
  on("inputImportFile", "change", async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      await api("/api/input/import", {
        method: "POST",
        body: JSON.stringify({ content: text, filename: file.name }),
      });
      await loadInput();
      toast(`Imported ${file.name}`, "ok");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      ev.target.value = "";
    }
  });
  on("btnInputReset", "click", async () => {
    try {
      await api("/api/input", {
        method: "POST",
        body: JSON.stringify({ bindings: null }),
      });
      await loadInput();
      toast("Keyboard bindings reset", "ok");
    } catch (e) {
      toast(e.message, "error");
    }
  });

  window.addEventListener("keydown", (ev) => {
    if (!listenMode) return;
    ev.preventDefault();
    const hint = $("listenHint");
    if (ev.key === "Escape") {
      listenMode = false;
      if (hint) { hint.hidden = true; hint.textContent = ""; }
      renderKeyGrid();
      return;
    }
    if (ev.key === "Backspace") {
      inputProfile.bindings[selectedAction] = [];
      listenMode = false;
      if (hint) {
        hint.hidden = false;
        hint.textContent = `Cleared ${selectedAction}`;
      }
      renderKeyGrid();
      return;
    }
    let name = ev.key;
    if (name === " ") name = "Space";
    if (name.length === 1) name = name.toUpperCase();
    const token = `KEY:${name}`;
    const cur = inputProfile.bindings[selectedAction] || [];
    if (!cur.includes(token)) cur.push(token);
    inputProfile.bindings[selectedAction] = cur;
    listenMode = false;
    if (hint) {
      hint.hidden = false;
      hint.textContent = `Assigned ${token} to ${selectedAction}`;
    }
    renderKeyGrid();
  });

  window.addEventListener("mousedown", (ev) => {
    if (!listenMode) return;
    // ignore clicks on the key grid itself (those select actions)
    if (ev.target.closest && ev.target.closest(".key-cell, .btn, .tab")) return;
    ev.preventDefault();
    const map = { 0: "Left", 1: "Middle", 2: "Right", 3: "X1", 4: "X2" };
    const name = map[ev.button];
    if (!name) return;
    const token = `MOUSE:${name}`;
    const cur = inputProfile.bindings[selectedAction] || [];
    if (!cur.includes(token)) cur.push(token);
    inputProfile.bindings[selectedAction] = cur;
    listenMode = false;
    const hint = $("listenHint");
    if (hint) {
      hint.hidden = false;
      hint.textContent = `Assigned ${token} to ${selectedAction}`;
    }
    renderKeyGrid();
  });

  function finishIntro() {
    const splash = $("bootSplash");
    if (splash) {
      splash.hidden = true;
      splash.setAttribute("aria-hidden", "true");
    }
    document.body.classList.remove("intro-active", "boot-pending");
    document.body.classList.add("ready");
    const brand = $("brandLogo");
    if (brand) brand.style.opacity = "1";
  }

  function runBootIntro() {
    const params = new URLSearchParams(window.location.search);
    const isBoot = params.get("boot") === "1";
    // Strip ?boot=1 immediately so F5 / Ctrl+F5 never replays the intro
    if (isBoot) {
      const clean = window.location.pathname || "/";
      window.history.replaceState({}, "", clean);
    }
    if (!isBoot) {
      finishIntro();
      return Promise.resolve();
    }

    const splash = $("bootSplash");
    const logo = $("bootLogo");
    const dock = $("brandDock");
    if (!splash || !logo || !dock) {
      finishIntro();
      return Promise.resolve();
    }

    document.body.classList.add("intro-active");
    document.body.classList.remove("boot-pending");
    splash.hidden = false;
    splash.setAttribute("aria-hidden", "false");

    return new Promise((resolve) => {
      const pulseMs = 1150;
      const flyMs = 780;
      window.setTimeout(() => {
        const from = logo.getBoundingClientRect();
        const to = dock.getBoundingClientRect();
        logo.classList.add("flying");
        logo.style.top = `${from.top}px`;
        logo.style.left = `${from.left}px`;
        logo.style.width = `${from.width}px`;
        // force layout, then animate to dock
        void logo.offsetWidth;
        logo.style.top = `${to.top}px`;
        logo.style.left = `${to.left}px`;
        logo.style.width = `${to.width}px`;

        window.setTimeout(() => {
          finishIntro();
          resolve();
        }, flyMs);
      }, pulseMs);
    });
  }

  // boot
  initTipBubbles();
  api("/api/setup/progress").then((d) => renderSetupProgress(d.progress)).catch(() => {});
  runBootIntro()
    .then(() => refresh())
    .then(async () => {
      // After logo lands: force update choice in the center before anything else
      try { await checkLauncherUpdateOnce(); } catch (_) {}
      toast("Launcher ready");
      let auditOk = false;
      try {
        const auditData = await runSetupAudit(true);
        auditOk = !!(auditData && auditData.audit && auditData.audit.allRequiredOk);
      } catch (_) {}
      try { await updateCheck(true); } catch (_) {}
      bindLibraryGridClicks();
      bindSettingsAutosave();
      if (auditOk) {
        // Instant cache load — first-time scan only when Library opens and never scanned
        showTab("library");
      } else {
        showTab("setup");
      }
    })
    .catch((e) => toast("Status error: " + e.message, "error"));
})();
