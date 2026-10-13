(() => {
  const $ = (id) => document.getElementById(id);
  const GITHUB_FALLBACK = "https://github.com/boykopovar/AnyPS5";
  let lastLineCount = 0;
  let pollTimer = null;
  let progressTimer = null;
  let githubUrl = GITHUB_FALLBACK;
  let readmeLoaded = false;
  let inputProfile = { bindings: {} };
  let selectedAction = "Cross";
  let listenMode = false;
  let keyActions = [];
  let adminWhyCopy = null;
  let pendingInstallMode = null; // "admin" | "plain" | null
  let lastSetupAudit = null;

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

  function setBusy(busy) {
    [
      "btnConvert", "btnRebuild", "btnPull", "btnStart", "btnStart2",
      "btnFetch", "btnUpdateCheck", "btnSetupInstall", "btnInputSave", "btnMarkBuild",
    ].forEach((id) => {
      const el = $(id);
      if (el) el.disabled = busy;
    });
    $("jobStatus").textContent = busy ? "Job running..." : "Ready";
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
    if (cfg.lastGameRoot) $("gameRoot").value = cfg.lastGameRoot;
    if (cfg.lastOutputDir) $("outputDir").value = cfg.lastOutputDir;
    if (cfg.outputName) $("outputName").value = cfg.outputName;
    if (cfg.gpuFilter != null) $("gpuFilter").value = cfg.gpuFilter;
    if (cfg.unusedFilter != null) $("unusedFilter").value = String(cfg.unusedFilter);
    $("toIntel").checked = !!cfg.toIntel;
    $("windowsGui").checked = !!cfg.windowsGui;
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
        html: `<strong>Not built yet.</strong> Tools/source look ready — open Build and run Build now.
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

    if (b.syncState === "current") {
      $("statBuild").innerHTML = `<span class="ok-text">ready</span>`;
    } else if (b.syncState === "outdated") {
      $("statBuild").innerHTML = `<span class="danger-text">outdated</span>`;
    } else if (b.ready) {
      $("statBuild").innerHTML = `<span class="warn-text">check needed</span>`;
    } else if (b.syncState === "need_setup") {
      $("statBuild").innerHTML = `<span class="warn-text">need setup</span>`;
    } else {
      $("statBuild").innerHTML = `<span class="warn-text">not built</span>`;
    }
    if ($("anyps5RootLabel") && data.repoRoot) {
      $("anyps5RootLabel").textContent = data.repoRoot;
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
      setBusy(true);
      startJobPoll();
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

  function showTab(name) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
    if (name === "readme") loadReadme();
    if (name === "setup") runSetupAudit(true).catch(() => {});
    if (name === "controls") {
      loadInput().catch((e) => toast(e.message, "error"));
    }
  }

  async function pollJobOnce() {
    const job = await api("/api/job");
    const lines = job.lines || [];
    $("console").textContent = lines.length ? lines.join("\n") : ($("console").textContent || "...");
    $("console").scrollTop = $("console").scrollHeight;
    lastLineCount = lines.length;
    $("jobStatus").textContent = job.running
      ? `Running: ${job.kind || "Job"}`
      : (job.ok === false ? "Error" : job.ok === true ? "Done" : "Ready");
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
          if (job.ok === true) toast(`${job.kind || "Job"} finished`, "ok");
          if (job.ok === false) toast(`${job.kind || "Job"} failed. See Console.`, "error");
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
    try {
      setBusy(true);
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

  async function openGithub() {
    try {
      const data = await api("/api/open-github", { method: "POST", body: "{}" });
      window.open(data.url || githubUrl, "_blank", "noopener,noreferrer");
    } catch {
      window.open(githubUrl, "_blank", "noopener,noreferrer");
    }
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
        badge.textContent = "Ready — Build next";
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
    if (!wrap || !progress) return;

    const steps = progress.steps || [];
    const total = progress.total || steps.length || 0;
    const done = progress.done || 0;

    wrap.hidden = false;
    if (label) label.textContent = `${done} / ${total}`;
    if (detail) {
      const cur = steps.find((s) => s.status === "running");
      if (progress.running && cur) detail.textContent = `Installing: ${cur.name}…`;
      else if (progress.detail) detail.textContent = progress.detail;
      else if (progress.ok === false) detail.textContent = "Failed — see Console";
      else if (progress.ok === true) detail.textContent = "Everything detected / install finished";
      else detail.textContent = "Checking this PC…";
    }
    if (fill) {
      const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
      fill.style.width = `${pct}%`;
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
    progressTimer = setInterval(tick, 400);
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
      if (data.audit && data.audit.allRequiredOk) toast("Everything detected — you can Build next", "ok");
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
        lastGameRoot: $("gameRoot").value.trim(),
        lastOutputDir: $("outputDir").value.trim(),
        outputName: $("outputName").value.trim() || "app.exe",
        gpuFilter: $("gpuFilter").value.trim(),
        unusedFilter: $("unusedFilter").value,
        toIntel: $("toIntel").checked,
        windowsGui: $("windowsGui").checked,
      }),
    });
  }

  async function startGame() {
    try {
      await savePrefs();
      const data = await api("/api/start", { method: "POST", body: "{}" });
      toast(`Started: ${data.exe}`, "ok");
    } catch (e) {
      toast(e.message + " Use Convert first.", "error");
      showTab("convert");
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
          await savePrefs();
        }
      } catch (e) {
        toast(e.message, "error");
      }
    });
  });

  on("btnStart", "click", startGame);
  on("btnStart2", "click", startGame);
  on("btnUpdateCheck", "click", () => updateCheck().catch((e) => toast(e.message, "error")));
  on("btnFetch", "click", () => updateCheck().catch((e) => toast(e.message, "error")));
  on("btnOpenGithub", "click", () => openGithub());
  on("btnOpenGithubReadme", "click", () => openGithub());
  on("btnReloadReadme", "click", () => loadReadme(true));
  on("btnPull", "click", () =>
    beginJob("Starting GitHub update...", () => api("/api/update", { method: "POST", body: "{}" }))
  );
  on("btnRebuild", "click", () =>
    beginJob("Starting build...", () => api("/api/rebuild", { method: "POST", body: "{}" }))
  );
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
    await savePrefs();
    const gameRoot = $("gameRoot").value.trim();
    if (!gameRoot) {
      toast("Please set a game folder.", "error");
      return;
    }
    // save input first so convert picks it up
    try { await saveInput(); } catch (_) {}
    beginJob("Starting convert...", () =>
      api("/api/convert", {
        method: "POST",
        body: JSON.stringify({
          gameRoot,
          outputDir: $("outputDir").value.trim(),
          outputName: $("outputName").value.trim() || "app.exe",
          unusedFilter: $("unusedFilter").value,
          toIntel: $("toIntel").checked,
          windowsGui: $("windowsGui").checked,
        }),
      })
    );
  });
  on("btnClearLog", "click", () => {
    $("console").textContent = "Ready.";
    lastLineCount = 0;
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
      toast("Launcher ready");
      try { await runSetupAudit(true); } catch (_) {}
      try { await updateCheck(true); } catch (_) {}
    })
    .catch((e) => toast("Status error: " + e.message, "error"));
})();
