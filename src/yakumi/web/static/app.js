// 首页：选择任务类型（视频文件、视频链接、已有字幕），上传或选择服务器上的文件并创建任务；刷新进行中任务的状态。
(() => {
  const form = document.querySelector("[data-upload-form]");
  const submit = form.querySelector("[data-submit]");
  const submitLabel = form.querySelector("[data-submit-label]");
  const target = form.querySelector("[data-target]");
  const source = form.querySelector("[data-source]");
  const urlInput = form.querySelector("[data-url-input]");
  const fileMeta = form.querySelector("[data-file-meta]");
  const progressBox = form.querySelector("[data-upload-progress]");
  const bar = form.querySelector("[data-upload-bar]");
  const progressText = form.querySelector("[data-upload-text]");
  const errorBox = form.querySelector("[data-upload-error]");
  const modeSwitch = form.querySelector("[data-mode-switch]");
  const PREFS = "yakumi:upload";
  const MODES = ["media", "url", "subtitles"];
  const DEFAULT_META = "支持 MP4、MOV、MKV、WebM 等常见格式，也可以是音频";

  const formatSize = (bytes) =>
    bytes >= 1 << 30 ? `${(bytes / (1 << 30)).toFixed(1)} GB`
    : bytes >= 1 << 20 ? `${(bytes / (1 << 20)).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1024))} KB`;
  const icon = (name) => document.querySelector(`template[data-icon="${name}"]`).content.firstElementChild.cloneNode(true);

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

  // ---- 文件位置：上传，或服务器上的文件 ------------------------------------
  // 每个位置有一个文件上传框和一个隐藏的“服务器文件”引用（根目录编号:相对路径），两者只用其一。
  const slots = [...form.querySelectorAll("[data-slot]")];
  const slotParts = (slot) => ({
    field: slot.querySelector(".file-field"),
    input: slot.querySelector("[data-file-input]"),
    ref: slot.querySelector("[data-ref-input]"),
    label: slot.querySelector(".file-name"),
    clear: slot.querySelector("[data-clear-slot]"),
  });
  const slotSelected = (slot) => {
    const { input, ref } = slotParts(slot);
    return !input.disabled && Boolean(input.files[0] || ref.value);
  };

  function showSlot(slot, serverPath = null) {
    const { field, input, ref, label, clear } = slotParts(slot);
    const file = input.files[0];
    field.classList.toggle("has-file", Boolean(file || ref.value));
    field.classList.toggle("from-server", Boolean(ref.value));
    if (ref.value) label.textContent = `服务器：${serverPath ?? ref.dataset.display ?? ref.value}`;
    else label.textContent = file ? file.name : label.dataset.placeholder;
    if (serverPath) ref.dataset.display = serverPath;
    clear.hidden = !(file || ref.value);
    if (slot.closest('[data-only="media"]')) {
      fileMeta.textContent = file ? `${file.name} · ${formatSize(file.size)}` : ref.value ? `服务器上的文件：${ref.dataset.display}` : DEFAULT_META;
    }
    updateOptions();
  }

  for (const slot of slots) {
    const { field, input, ref, clear } = slotParts(slot);
    input.addEventListener("change", () => {
      ref.value = "";
      showSlot(slot);
    });
    ["dragenter", "dragover"].forEach((type) => field.addEventListener(type, () => field.classList.add("dragging")));
    ["dragleave", "drop"].forEach((type) => field.addEventListener(type, () => field.classList.remove("dragging")));
    clear.addEventListener("click", () => {
      input.value = "";
      ref.value = "";
      showSlot(slot);
    });
    slot.querySelector("[data-pick-server]").addEventListener("click", () =>
      openBrowser(slot.dataset.kind, (refValue, display) => {
        input.value = "";
        ref.value = refValue;
        showSlot(slot, display);
      }),
    );
  }

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
      el.querySelectorAll("input, button").forEach((input) => {
        input.disabled = !on || input.hasAttribute("data-unavailable");
      });
    });
    target.querySelector("[data-none-label]").textContent =
      mode === "subtitles" ? "不翻译，只转换格式" : "不翻译，只识别";
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

  // 不翻译时没有双语；导入字幕且没有附带视频时不能烧录；没有 yt-dlp 时不能用链接
  function updateOptions() {
    const mode = form.dataset.mode;
    const translating = Boolean(target.value);
    const optionalVideo = form.querySelector(".optional-video [data-slot]");
    const hasVideo = mode !== "subtitles" || slotSelected(optionalVideo);
    setToggleEnabled("bilingual", translating, "不翻译时只有原文");
    setToggleEnabled("burn", hasVideo, "附带视频后才能烧录");
    submitLabel.textContent = translating ? "开始翻译" : mode === "subtitles" ? "生成字幕" : "开始识别";
    submit.disabled = mode === "url" && urlInput.hasAttribute("data-unavailable");
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

  // ---- 服务器文件浏览 -----------------------------------------------------
  const browser = document.querySelector("[data-browser]");
  const rootSelect = browser.querySelector("[data-browser-root]");
  const upBtn = browser.querySelector("[data-browser-up]");
  const pathLabel = browser.querySelector("[data-browser-path]");
  const list = browser.querySelector("[data-browser-list]");
  const hint = browser.querySelector("[data-browser-hint]");
  const AUDIO = /\.(mp3|m4a|aac|wav|flac|ogg|opus|wma)$/i;
  const browsing = { kind: "video", root: 0, path: "", parent: null, onPick: null, roots: [] };

  function openBrowser(kind, onPick) {
    Object.assign(browsing, { kind, onPick });
    browser.hidden = false;
    load(browsing.root, browsing.path);
  }
  function closeBrowser() {
    browser.hidden = true;
  }
  browser.querySelector("[data-close-browser]").addEventListener("click", closeBrowser);
  browser.addEventListener("click", (event) => {
    if (event.target === browser) closeBrowser();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !browser.hidden) closeBrowser();
  });

  function message(text) {
    list.replaceChildren(Object.assign(document.createElement("li"), { className: "browser-empty", textContent: text }));
  }

  async function load(root, path) {
    message("正在读取…");
    try {
      const params = new URLSearchParams({ root, path, kind: browsing.kind });
      let res = await fetch(`/api/files?${params}`);
      let data = await res.json();
      if (!res.ok && path) {
        // 上次浏览的目录不在了：回到根目录
        res = await fetch(`/api/files?${new URLSearchParams({ root, path: "", kind: browsing.kind })}`);
        data = await res.json();
      }
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      render(data);
    } catch (err) {
      message(`无法读取：${err.message}`);
    }
  }

  function render(data) {
    Object.assign(browsing, { root: data.root, path: data.path, parent: data.parent, roots: data.roots });
    rootSelect.replaceChildren(
      ...data.roots.map((r) => Object.assign(document.createElement("option"), { value: r.id, textContent: r.label })),
    );
    rootSelect.value = String(data.root);
    browser.querySelector("[data-root-wrap]").hidden = data.roots.length < 2;
    upBtn.disabled = data.parent === null;
    const root = data.roots.find((r) => r.id === data.root);
    pathLabel.textContent = data.path ? `${root.label}/${data.path}` : root.label;
    pathLabel.title = `${root.path}${data.path ? `/${data.path}` : ""}`;
    const what = browsing.kind === "subtitles" ? "SRT / VTT 字幕" : "视频和音频";
    hint.textContent = `只显示文件夹和${what}文件。“任务目录”是 ${data.roots[0].path}，把文件放进去就能在这里选择；` +
      "其他目录需要在配置文件的 [web] file_roots 中添加。";
    if (!data.entries.length) return message(`这里没有${what}文件`);
    list.replaceChildren(
      ...data.entries.map((entry) => {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "browser-item";
        const kindIcon = entry.dir ? "folder" : browsing.kind === "subtitles" ? "subtitles" : AUDIO.test(entry.name) ? "audio" : "video";
        const name = Object.assign(document.createElement("span"), { className: "browser-name", textContent: entry.name });
        const meta = Object.assign(document.createElement("span"), {
          className: "browser-meta",
          textContent: entry.dir ? "" : formatSize(entry.size),
        });
        btn.append(icon(kindIcon), name, meta);
        if (entry.dir) btn.append(icon("chevron"));
        const rel = data.path ? `${data.path}/${entry.name}` : entry.name;
        btn.addEventListener("click", () => {
          if (entry.dir) return load(data.root, rel);
          browsing.onPick?.(`${data.root}:${rel}`, data.roots.length > 1 ? `${root.label}/${rel}` : rel);
          closeBrowser();
        });
        li.append(btn);
        return li;
      }),
    );
    if (data.truncated) list.append(Object.assign(document.createElement("li"), { className: "browser-empty", textContent: "文件太多，只显示了前 2000 个" }));
  }
  rootSelect.addEventListener("change", () => load(rootSelect.value, ""));
  upBtn.addEventListener("click", () => browsing.parent !== null && load(browsing.root, browsing.parent));

  // ---- 提交 -----------------------------------------------------------
  function fail(text) {
    errorBox.textContent = text;
    errorBox.hidden = false;
    progressBox.hidden = true;
    submit.disabled = false;
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const mode = form.dataset.mode;
    const mainSlot = form.querySelector(`[data-only="${mode}"] [data-slot]`);
    if (mode === "media" && !slotSelected(mainSlot)) return fail("请选择视频文件");
    if (mode === "subtitles" && !slotSelected(mainSlot)) return fail("请选择字幕文件");
    if (mode === "url" && !urlInput.value.trim()) return fail("请粘贴视频链接");
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
    const uploading = [...form.querySelectorAll("[data-file-input]:not(:disabled)")].some((i) => i.files[0]);
    progressText.textContent = uploading ? "正在上传…" : "正在创建任务…";
    savePrefs();

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");
    xhr.responseType = "json";
    xhr.upload.addEventListener("progress", (e) => {
      if (!e.lengthComputable || !uploading) return;
      const pct = Math.round((e.loaded / e.total) * 100);
      bar.style.width = `${pct}%`;
      progressText.textContent = pct < 100 ? `正在上传 ${pct}%` : "上传完成，正在创建任务…";
    });
    xhr.addEventListener("load", () => {
      if (xhr.status === 201 && xhr.response?.url) {
        window.location.href = xhr.response.url;
      } else if (xhr.status === 401) {
        window.location.href = "/login";
      } else {
        fail(xhr.response?.error || `创建任务失败（HTTP ${xhr.status}）`);
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
  setMode(MODES.includes(prefs.mode) ? prefs.mode : "media");

  // ---- 有进行中的任务时定期刷新状态标签 --------------------------------------
  const jobList = document.querySelector("[data-job-list]");
  async function refresh() {
    const active = jobList.querySelector('[data-status="running"], [data-status="pending"]');
    if (!active) return;
    try {
      const jobs = await (await fetch("/api/jobs")).json();
      for (const job of jobs) {
        const item = jobList.querySelector(`[data-job-id="${job.id}"]`);
        if (!item) continue;
        item.dataset.status = job.status;
        item.querySelector(".job-name").textContent = job.name;
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
