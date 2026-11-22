// 首页：选择任务类型、上传视频或字幕并创建任务；刷新进行中任务的状态。
(() => {
  const form = document.querySelector("[data-upload-form]");
  const submit = form.querySelector("[data-submit]");
  const submitLabel = form.querySelector("[data-submit-label]");
  const target = form.querySelector("[data-target]");
  const source = form.querySelector("[data-source]");
  const fileMeta = form.querySelector("[data-file-meta]");
  const progressBox = form.querySelector("[data-upload-progress]");
  const bar = form.querySelector("[data-upload-bar]");
  const progressText = form.querySelector("[data-upload-text]");
  const errorBox = form.querySelector("[data-upload-error]");
  const modeSwitch = form.querySelector("[data-mode-switch]");
  const PREFS = "yakumi:upload";

  const formatSize = (bytes) =>
    bytes >= 1 << 30 ? `${(bytes / (1 << 30)).toFixed(1)} GB`
    : bytes >= 1 << 20 ? `${(bytes / (1 << 20)).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1024))} KB`;

  // ---- 记住上次的选择（只存在这个浏览器里） -----------------------------
  function loadPrefs() {
    try {
      return JSON.parse(localStorage.getItem(PREFS) || "{}") || {};
    } catch {
      return {};
    }
  }
  function savePrefs() {
    try {
      localStorage.setItem(PREFS, JSON.stringify({ mode: form.dataset.mode, target: target.value, source: source.value }));
    } catch {
      /* 浏览器不允许存储时忽略 */
    }
  }

  // ---- 文件选择 -------------------------------------------------------
  const activeInput = (name) => form.querySelector(`input[name="${name}"]:not(:disabled)`);

  function showFile(input) {
    const field = input.closest(".file-field");
    const label = field.querySelector(".file-name");
    const file = input.files[0];
    field.classList.toggle("has-file", Boolean(file));
    label.textContent = file ? file.name : label.dataset.placeholder;
    if (input === form.querySelector('[data-only="media"] input')) {
      fileMeta.textContent = file ? `${file.name} · ${formatSize(file.size)}` : "支持 MP4、MOV、MKV、WebM 等常见格式，也可以是音频";
    }
    updateOptions();
  }
  form.querySelectorAll("[data-file-input]").forEach((input) => {
    const field = input.closest(".file-field");
    input.addEventListener("change", () => showFile(input));
    ["dragenter", "dragover"].forEach((type) => field.addEventListener(type, () => field.classList.add("dragging")));
    ["dragleave", "drop"].forEach((type) => field.addEventListener(type, () => field.classList.remove("dragging")));
  });

  // ---- 任务类型与选项 ---------------------------------------------------
  function setMode(mode) {
    form.dataset.mode = mode;
    modeSwitch.querySelectorAll("button").forEach((btn) => {
      const on = btn.dataset.value === mode;
      btn.classList.toggle("selected", on);
      btn.setAttribute("aria-checked", String(on));
    });
    form.querySelectorAll("[data-only]").forEach((el) => {
      const on = el.dataset.only === mode;
      el.hidden = !on;
      el.querySelectorAll("input").forEach((input) => (input.disabled = !on));
    });
    target.querySelector("[data-none-label]").textContent =
      mode === "media" ? "不翻译，只识别" : "不翻译，只转换格式";
    updateOptions();
  }
  modeSwitch.addEventListener("click", (event) => {
    const btn = event.target.closest("button");
    if (btn) {
      setMode(btn.dataset.value);
      savePrefs();
    }
  });

  function setToggleEnabled(name, enabled, reason) {
    const wrap = form.querySelector(`[data-toggle-wrap="${name}"]`);
    const toggle = wrap.querySelector("button");
    toggle.disabled = !enabled;
    wrap.classList.toggle("disabled", !enabled);
    wrap.title = enabled ? "" : reason;
  }

  // 不翻译时没有双语；导入字幕且没有附带视频时不能烧录
  function updateOptions() {
    const mode = form.dataset.mode;
    const translating = Boolean(target.value);
    const hasVideo = mode === "media" || Boolean(activeInput("video")?.files[0]);
    setToggleEnabled("bilingual", translating, "不翻译时只有原文");
    setToggleEnabled("burn", hasVideo, "附带视频后才能烧录");
    submitLabel.textContent = translating ? "开始翻译" : mode === "media" ? "开始识别" : "生成字幕";
  }
  target.addEventListener("change", () => {
    updateOptions();
    savePrefs();
  });
  source.addEventListener("change", savePrefs);

  form.querySelectorAll("[data-toggle]").forEach((toggle) =>
    toggle.addEventListener("click", () => {
      const on = !toggle.classList.contains("on");
      toggle.classList.toggle("on", on);
      toggle.setAttribute("aria-checked", String(on));
    }),
  );

  // 折叠的“术语表与补充信息”标题上显示已选数量
  const context = form.querySelector("[data-context]");
  const contextCount = form.querySelector("[data-context-count]");
  function showContext() {
    const n = form.querySelectorAll('input[name="glossaries"]:checked').length;
    const notes = form.querySelector('textarea[name="notes"]').value.trim();
    const parts = [];
    if (n) parts.push(`${n} 个术语表`);
    if (notes) parts.push("有补充信息");
    contextCount.textContent = parts.length ? `· ${parts.join("，")}` : "";
  }
  context.addEventListener("input", showContext);
  context.addEventListener("change", showContext);

  // ---- 提交 -----------------------------------------------------------
  function fail(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
    progressBox.hidden = true;
    submit.disabled = false;
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const mode = form.dataset.mode;
    if (mode === "media" && !activeInput("video")?.files[0]) return fail("请选择视频文件");
    if (mode === "subtitles" && !activeInput("subtitles")?.files[0]) return fail("请选择字幕文件");
    const data = new FormData(form);
    data.set("mode", mode);
    form.querySelectorAll("[data-toggle]").forEach((t) =>
      data.set(t.dataset.toggle, String(t.classList.contains("on") && !t.disabled)),
    );
    if (!target.value) {
      data.delete("glossaries");
      data.delete("notes");
    }
    errorBox.hidden = true;
    progressBox.hidden = false;
    submit.disabled = true;
    bar.style.width = "0%";
    progressText.textContent = "正在上传…";
    savePrefs();

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");
    xhr.responseType = "json";
    xhr.upload.addEventListener("progress", (e) => {
      if (!e.lengthComputable) return;
      const pct = Math.round((e.loaded / e.total) * 100);
      bar.style.width = `${pct}%`;
      progressText.textContent = pct < 100 ? `正在上传 ${pct}%` : "上传完成，正在创建任务…";
    });
    xhr.addEventListener("load", () => {
      if (xhr.status === 201 && xhr.response?.url) {
        window.location.href = xhr.response.url;
      } else {
        fail(xhr.response?.error || `上传失败（HTTP ${xhr.status}）`);
      }
    });
    xhr.addEventListener("error", () => fail("网络错误，上传失败"));
    xhr.send(data);
  });

  // ---- 恢复上次的选择 -----------------------------------------------------
  const prefs = loadPrefs();
  if (prefs.target !== undefined && [...target.options].some((o) => o.value === prefs.target && !o.disabled)) {
    target.value = prefs.target;
  }
  if (prefs.source && [...source.options].some((o) => o.value === prefs.source)) source.value = prefs.source;
  setMode(prefs.mode === "subtitles" ? "subtitles" : "media");

  // ---- 有进行中的任务时定期刷新状态标签 --------------------------------------
  const list = document.querySelector("[data-job-list]");
  async function refresh() {
    const active = list.querySelector('[data-status="running"], [data-status="pending"]');
    if (!active) return;
    try {
      const jobs = await (await fetch("/api/jobs")).json();
      for (const job of jobs) {
        const item = list.querySelector(`[data-job-id="${job.id}"]`);
        if (!item) continue;
        item.dataset.status = job.status;
        const chip = item.querySelector(".status-chip");
        chip.dataset.status = job.status;
        chip.textContent = job.status === "running" ? `${Math.round(job.progress * 100)}%` : job.status_label;
      }
    } catch {
      /* 下次再试 */
    }
  }
  setInterval(refresh, 3000);
  refresh();
})();
