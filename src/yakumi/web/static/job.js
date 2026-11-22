// 任务页：显示进度、校对字幕、保存并重新生成。
(() => {
  let job = JSON.parse(document.getElementById("job-data").textContent);
  const $ = (sel) => document.querySelector(sel);
  const statusPanel = $("[data-status-box]");
  const statusTitle = $("[data-status-title]");
  const statusPct = $("[data-status-pct]");
  const statusDetail = $("[data-status-detail]");
  const progress = $("[data-progress]");
  const bar = $("[data-progress-bar]");
  const stepsBox = $("[data-steps-box]");
  const reportBox = $("[data-report-box]");
  const errorBox = $("[data-error]");
  const chip = $("[data-status-chip]");
  const renderBtn = $("[data-render]");
  const video = $("[data-video]");
  const cueList = $("[data-cues]");
  const emptyCues = $("[data-empty-cues]");
  const editedBadge = $("[data-edited]");
  const cueCount = $("[data-cue-count]");
  const formatGroup = $("[data-format]");
  const stepsList = $("[data-steps]");
  const restartBtn = $("[data-restart]");
  const deleteBtn = $("[data-delete-job]");

  const settings = { bilingual: job.bilingual, burn: job.burn, format: job.format };
  let segments = [];
  let dirty = false;
  let submitting = false;
  let pollTimer = null;
  let loadedFor = null;
  let rows = [];

  // ---- 设置 -----------------------------------------------------------
  function syncSettings() {
    document.querySelectorAll("[data-setting]").forEach((toggle) => {
      const on = Boolean(settings[toggle.dataset.setting]);
      toggle.classList.toggle("on", on);
      toggle.setAttribute("aria-checked", String(on));
    });
    formatGroup.querySelectorAll("button").forEach((btn) => {
      const selected = btn.dataset.value === settings.format;
      btn.classList.toggle("selected", selected);
      btn.setAttribute("aria-checked", String(selected));
    });
  }
  document.querySelectorAll("[data-setting]").forEach((toggle) =>
    toggle.addEventListener("click", () => {
      settings[toggle.dataset.setting] = !settings[toggle.dataset.setting];
      syncSettings();
    }),
  );
  formatGroup.addEventListener("click", (event) => {
    const btn = event.target.closest("button");
    if (!btn) return;
    settings.format = btn.dataset.value;
    syncSettings();
  });

  // ---- 状态 -----------------------------------------------------------
  const busy = () => job.busy || job.status === "running" || job.status === "pending";
  const scanBtn = $("[data-scan-terms]");

  function setEditorBusy() {
    const locked = submitting || busy();
    // 等待术语审核时，在“术语审核”里完成审核后继续，而不是重新生成
    renderBtn.disabled = locked || job.status === "awaiting_review";
    if (scanBtn) scanBtn.disabled = locked;
    if (retranslateBtn) retranslateBtn.disabled = locked;
    if (restartBtn) restartBtn.disabled = locked;
    deleteBtn.disabled = locked;
    cueList.querySelectorAll("textarea").forEach((el) => (el.readOnly = locked));
  }

  function showState() {
    chip.dataset.status = job.status;
    chip.textContent = job.status_label;
    showStatus();
    showSteps();
    showReport();
    errorBox.hidden = job.status !== "error" || !job.error;
    errorBox.textContent = job.error || "";
    setEditorBusy();
    renderBtn.querySelector("span").textContent = job.status === "error" ? "继续处理" : "保存并生成";
    for (const kind of ["subtitles", "video"]) {
      const link = document.querySelector(`[data-download="${kind}"]`);
      const url = job.files?.[kind];
      link.hidden = !url || busy();
      if (url) link.href = url;
    }
    document.dispatchEvent(new CustomEvent("yakumi:job-state", { detail: job }));
  }

  // 术语审核面板（terms.js）保存后换上新的任务版本
  document.addEventListener("yakumi:job-refresh", (event) => {
    // 保存审核不改动字幕，正在校对的字幕仍然基于最新版本
    if (loadedFor === job.revision) loadedFor = event.detail.revision;
    job = event.detail;
    showState();
  });

  // 术语审核面板（terms.js）提交后，任务重新开始处理
  document.addEventListener("yakumi:job-submitted", (event) => {
    job = event.detail;
    job.busy = true;
    setDirty(false);
    loadedFor = null;
    showState();
    poll();
  });

  const disableTermsBtn = $("[data-disable-terms]");
  disableTermsBtn?.addEventListener("click", async () => {
    if (submitting || busy()) return;
    if (!window.confirm("之后的翻译不再使用术语扫描和审核结果（审核数据会保留，可以重新扫描）。确定吗？")) return;
    submitting = true;
    setEditorBusy();
    try {
      const data = await post(`/api/jobs/${job.id}/terms`, { revision: job.revision, action: "disable" });
      disableTermsBtn.hidden = true;
      scanBtn.querySelector("span").textContent = "扫描术语";
      const panel = document.querySelector("[data-terms-panel]");
      if (panel) panel.hidden = true;
      if (data.status === "pending") document.dispatchEvent(new CustomEvent("yakumi:job-submitted", { detail: data }));
      else document.dispatchEvent(new CustomEvent("yakumi:job-refresh", { detail: data }));
    } catch (err) {
      errorBox.textContent = `操作失败：${err.message}`;
      errorBox.hidden = false;
    } finally {
      submitting = false;
      setEditorBusy();
    }
  });

  scanBtn?.addEventListener("click", async () => {
    if (submitting || busy()) return;
    const again = job.term_mode !== "off";
    const message = again
      ? "重新扫描术语会产生费用，已有的审核决定会保留。扫描完成后需要审核，然后重新翻译。确定继续吗？"
      : "扫描术语会产生费用。扫描完成后任务会暂停，等你审核术语后再重新翻译。确定继续吗？";
    if (!window.confirm(message)) return;
    submitting = true;
    setEditorBusy();
    try {
      const data = await post(`/api/jobs/${job.id}/terms`, { revision: job.revision, action: "scan", mode: "review" });
      scanBtn.querySelector("span").textContent = "重新扫描术语";
      if (disableTermsBtn) disableTermsBtn.hidden = false;
      document.dispatchEvent(new CustomEvent("yakumi:job-submitted", { detail: data }));
    } catch (err) {
      errorBox.textContent = `扫描术语失败：${err.message}`;
      errorBox.hidden = false;
    } finally {
      submitting = false;
      setEditorBusy();
    }
  });

  async function poll() {
    clearTimeout(pollTimer);
    try {
      const res = await fetch(`/api/jobs/${job.id}`);
      if (res.ok) job = await res.json();
    } catch {
      /* 网络波动时稍后重试 */
    }
    showState();
    // 链接视频下载完成后刷新页面，换上播放器
    if (job.has_video && document.querySelector("[data-awaiting-video]") && !dirty) {
      window.location.reload();
      return;
    }
    if (busy()) {
      pollTimer = setTimeout(poll, 1000);
    } else {
      loadSegments();
      refreshTrack();
    }
  }

  // 处理进度：一行标题说明现在在做什么，下面一行补充说明；步骤和统计收进可展开的区域
  function showStatus() {
    const steps = job.steps || [];
    const index = steps.findIndex((step) => step.status === "active" || step.status === "error");
    const current = steps[index];
    const pct = Math.round((job.progress || 0) * 100);
    const position = current && steps.length > 1 ? `第 ${index + 1}/${steps.length} 步` : "";
    let state, title, detail;
    let showBar = false;
    if (job.status === "running" && current) {
      state = "busy";
      title = job.message || current.label;
      // 消息不以步骤名开头时（例如“探测识别能力”），补上步骤名
      detail = job.message && !job.message.startsWith(current.label) ? `${position} · ${current.label}` : position;
      showBar = true;
    } else if (busy()) {
      state = "busy";
      title = "排队中";
      detail = "马上开始处理";
      showBar = true;
    } else if (job.status === "awaiting_review") {
      state = "wait";
      title = "等待术语审核";
      detail = job.message ? `${job.message}。审核后继续翻译` : "审核后继续翻译";
    } else if (job.status === "error") {
      state = "error";
      title = current ? `${current.label}失败` : "处理失败";
      detail = "点击“继续处理”从失败的步骤重试";
    } else {
      state = "done";
      title = "已完成";
      detail = "可以校对字幕并下载";
    }
    statusPanel.dataset.state = state;
    statusTitle.textContent = title;
    statusTitle.title = title;
    statusDetail.textContent = detail;
    statusPct.textContent = showBar && job.status === "running" ? `${pct}%` : "";
    progress.hidden = !showBar;
    bar.style.width = `${job.status === "running" ? pct : 0}%`;
  }

  const STEP_TEXT = { done: "已完成", active: "进行中", error: "失败", todo: "未开始" };
  function showSteps() {
    const steps = job.steps || [];
    // 全部完成时步骤没有参考价值，收起来
    stepsBox.hidden = steps.length < 2 || (job.status === "done" && !busy());
    const done = steps.filter((step) => step.status === "done").length;
    stepsBox.querySelector("[data-steps-summary]").textContent = `全部步骤（已完成 ${done}/${steps.length}）`;
    stepsList.replaceChildren(
      ...steps.map((step) => {
        const li = document.createElement("li");
        li.className = `step ${step.status}`;
        const label = document.createElement("span");
        label.textContent = step.label;
        li.append(label);
        if (step.status === "active" || step.status === "error") {
          const tag = document.createElement("span");
          tag.className = "step-state";
          tag.textContent = STEP_TEXT[step.status];
          li.append(tag);
          if (step.status === "active") li.setAttribute("aria-current", "step");
        } else {
          li.title = STEP_TEXT[step.status] || "";
        }
        return li;
      }),
    );
  }

  // 处理详情：每个阶段一行，费用单独一行
  function showReport() {
    const report = job.report;
    reportBox.hidden = !report || busy();
    if (!report) return;
    $("[data-report-cost]").textContent = report.cost;
    const list = $("[data-report]");
    const lines = report.sections.map((section) => [section.label, section.items, []]);
    lines.push(["费用", [{ text: report.cost }], report.cost_notes]);
    list.replaceChildren(
      ...lines.flatMap(([label, items, notes]) => {
        const dt = document.createElement("dt");
        dt.textContent = label;
        const dd = document.createElement("dd");
        for (const item of items) {
          const span = document.createElement("span");
          span.className = item.warn ? "report-item warn" : "report-item";
          span.textContent = item.text;
          dd.append(span);
        }
        for (const note of notes) {
          const small = document.createElement("small");
          small.className = "report-note";
          small.textContent = note;
          dd.append(small);
        }
        return [dt, dd];
      }),
    );
  }

  // ---- 字幕预览 -------------------------------------------------------
  function refreshTrack() {
    if (!video) return;
    const old = video.querySelector("track");
    if (!job.files?.preview) return;
    const track = document.createElement("track");
    track.kind = "subtitles";
    track.label = "字幕";
    track.srclang = old?.srclang || "und";
    track.default = true;
    track.src = `${job.files.preview}?v=${job.updated_at}`;
    old?.remove();
    video.appendChild(track);
    track.addEventListener("load", () => (track.track.mode = "showing"));
    track.track.mode = "showing";
  }

  // ---- 字幕校对 -------------------------------------------------------
  // 先取整到 0.1 秒，避免 59.96 显示成 00:60.0
  const fmtTime = (t) => {
    const tenths = Math.round(t * 10);
    return `${String(Math.floor(tenths / 600)).padStart(2, "0")}:${((tenths % 600) / 10).toFixed(1).padStart(4, "0")}`;
  };

  function autosize(el) {
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + 2}px`;
  }

  // 一次性调整很多输入框的高度：先全部重置、再统一读取、最后统一写入，只触发一次布局计算
  function autosizeAll(els) {
    els.forEach((el) => (el.style.height = "auto"));
    const heights = els.map((el) => el.scrollHeight);
    els.forEach((el, i) => (el.style.height = `${heights[i] + 2}px`));
  }

  function setDirty(value) {
    dirty = value;
    editedBadge.hidden = !value;
    if (!value) cueList.querySelectorAll(".cue.edited").forEach((c) => c.classList.remove("edited"));
  }

  function textarea(value, field, index, label) {
    const el = document.createElement("textarea");
    el.rows = 1;
    el.value = value;
    el.dataset.field = field;
    el.setAttribute("aria-label", `第 ${index + 1} 条${label}`);
    if (field === "text") el.className = "cue-source";
    return el;
  }

  // 同一句的字幕条放进同一个 .sentence 容器（左侧细线相连），句与句之间留空。
  // 未分组（group < 0，例如旧任务）的字幕条各自成一组。
  function renderCues() {
    cueList.replaceChildren();
    rows = [];
    const translated = Boolean(job.target_lang);
    let group = null;
    let lastGroup = null;
    segments.forEach((seg, i) => {
      const gid = seg.group ?? -1;
      if (!group || gid < 0 || gid !== lastGroup) {
        group = document.createElement("div");
        group.className = "sentence";
        cueList.append(group);
      }
      lastGroup = gid;
      const row = document.createElement("div");
      row.className = "cue";
      row.dataset.index = i;
      const time = document.createElement("button");
      time.type = "button";
      time.className = "cue-time";
      time.textContent = fmtTime(seg.start);
      time.title = video ? `${fmtTime(seg.start)} → ${fmtTime(seg.end)}，点击跳转` : `${fmtTime(seg.start)} → ${fmtTime(seg.end)}`;
      time.addEventListener("click", () => {
        if (!video) return;
        video.currentTime = seg.start + 0.01;
        video.play().catch(() => {});
      });
      row.append(time);
      if (translated) row.append(textarea(seg.translation, "translation", i, "译文"));
      row.append(textarea(seg.text, "text", i, "原文"));
      group.append(row);
      rows.push(row);
      group.classList.toggle("multi", group.children.length > 1);
    });
    autosizeAll([...cueList.querySelectorAll("textarea")]);
    cueCount.textContent = segments.length ? `${segments.length} 条` : "";
    emptyCues.hidden = segments.length > 0;
  }

  cueList.addEventListener("input", (event) => {
    const el = event.target;
    if (el.tagName !== "TEXTAREA") return;
    const row = el.closest(".cue");
    segments[row.dataset.index][el.dataset.field] = el.value;
    row.classList.add("edited");
    autosize(el);
    setDirty(true);
  });

  async function loadSegments() {
    if (dirty || loadedFor === job.revision) return;
    const revision = job.revision;
    try {
      const res = await fetch(`/api/jobs/${job.id}/segments`);
      if (!res.ok) return;
      const loaded = await res.json();
      if (dirty || job.revision !== revision) return;
      segments = loaded;
      loadedFor = revision;
      renderCues();
      setEditorBusy();
    } catch {
      /* 忽略，下次刷新时再加载 */
    }
    if (!segments.length && !busy()) {
      emptyCues.querySelector("p").textContent = "还没有字幕。处理完成后会显示在这里。";
      emptyCues.querySelector("svg")?.classList.remove("spin");
    }
  }

  let activeRow = null;
  video?.addEventListener("timeupdate", () => {
    const t = video.currentTime;
    const index = segments.findIndex((s) => t >= s.start && t < s.end);
    const row = index >= 0 ? rows[index] : null;
    if (row === activeRow) return;
    activeRow?.classList.remove("active");
    row?.classList.add("active");
    activeRow = row;
  });

  // ---- 保存并生成 -----------------------------------------------------
  renderBtn.addEventListener("click", async () => {
    if (submitting || busy()) return;
    submitting = true;
    setEditorBusy();
    const body = { ...settings, revision: dirty ? loadedFor : job.revision };
    if (dirty) body.segments = segments.map(({ text, translation }) => ({ text, translation }));
    try {
      const res = await fetch(`/api/jobs/${job.id}/render`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      job = data;
      job.busy = true;
      setDirty(false);
      loadedFor = null;
      showState();
      poll();
    } catch (err) {
      errorBox.textContent = `保存失败：${err.message}`;
      errorBox.hidden = false;
    } finally {
      submitting = false;
      setEditorBusy();
    }
  });

  // ---- 背景资料与补充说明 ------------------------------------------------
  const contextPanel = $("[data-context-panel]");
  const retranslateBtn = $("[data-retranslate]");
  if (contextPanel) {
    const allGlossaries = JSON.parse(document.getElementById("glossary-data").textContent);
    const picker = $("[data-job-glossaries]");
    const notesBox = $("[data-job-notes]");
    const known = new Set(allGlossaries.map((g) => g.id));
    // 已经被删除的背景资料也列出来（标记出来），方便取消勾选
    const items = [...allGlossaries, ...(job.glossaries || []).filter((id) => !known.has(id)).map((id) => ({ id, name: id, missing: true }))];
    for (const g of items) {
      const label = document.createElement("label");
      label.className = "chip-option";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = g.id;
      box.checked = (job.glossaries || []).includes(g.id);
      const name = document.createElement("span");
      name.textContent = g.missing ? `${g.name}（已删除）` : g.name;
      label.append(box, name);
      if (g.summary) label.title = g.summary;
      if (!g.missing && g.count) {
        const count = document.createElement("small");
        count.textContent = g.count;
        count.title = "固定译法";
        label.append(count);
      }
      picker.append(label);
    }
    $("[data-no-glossaries]").hidden = items.length > 0;
    notesBox.value = job.notes || "";
    autosize(notesBox);
    notesBox.addEventListener("input", () => autosize(notesBox));

    retranslateBtn.addEventListener("click", async () => {
      if (submitting || busy()) return;
      if (job.completed?.includes("translate") &&
          !window.confirm("重新翻译会覆盖校对过的译文，确定继续吗？")) return;
      submitting = true;
      setEditorBusy();
      const body = {
        revision: job.revision,
        glossaries: [...picker.querySelectorAll("input:checked")].map((b) => b.value),
        notes: notesBox.value,
      };
      try {
        const res = await fetch(`/api/jobs/${job.id}/retranslate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        job = data;
        job.busy = true;
        setDirty(false);
        loadedFor = null;
        showState();
        poll();
      } catch (err) {
        errorBox.textContent = `重新翻译失败：${err.message}`;
        errorBox.hidden = false;
      } finally {
        submitting = false;
        setEditorBusy();
      }
    });
  }

  // ---- 任务操作 -------------------------------------------------------
  async function post(url, body, method = "POST") {
    const res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  restartBtn?.addEventListener("click", async () => {
    if (submitting || busy()) return;
    const message = dirty
      ? "有未保存的修改，重新处理会丢弃它们，并覆盖校对过的字幕。确定从头重新处理吗？"
      : "会按当前设置重新识别、断句和翻译，覆盖校对过的字幕，并再次产生费用。确定继续吗？";
    if (!window.confirm(message)) return;
    submitting = true;
    setEditorBusy();
    try {
      job = await post(`/api/jobs/${job.id}/restart`, { revision: job.revision });
      job.busy = true;
      setDirty(false);
      loadedFor = null;
      showState();
      poll();
    } catch (err) {
      errorBox.textContent = `重新处理失败：${err.message}`;
      errorBox.hidden = false;
    } finally {
      submitting = false;
      setEditorBusy();
    }
  });

  deleteBtn.addEventListener("click", async () => {
    if (submitting || busy()) return;
    if (!window.confirm(`删除任务“${job.name}”？识别结果、字幕和网页上传的文件都会被删除，无法恢复。`)) return;
    submitting = true;
    setEditorBusy();
    try {
      const res = await fetch(`/api/jobs/${job.id}`, { method: "DELETE" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      dirty = false;
      window.location.href = "/";
    } catch (err) {
      errorBox.textContent = `删除失败：${err.message}`;
      errorBox.hidden = false;
      submitting = false;
      setEditorBusy();
    }
  });

  window.addEventListener("beforeunload", (event) => {
    if (dirty) event.preventDefault();
  });

  syncSettings();
  showState();
  poll();
})();
