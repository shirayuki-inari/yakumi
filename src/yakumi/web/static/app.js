// 首页：上传视频并创建任务；刷新进行中任务的状态。
(() => {
  const form = document.querySelector("[data-upload-form]");
  const fileInput = form.querySelector('input[type="file"]');
  const fileField = form.querySelector(".file-field");
  const fileName = form.querySelector("[data-file-name]");
  const fileMeta = form.querySelector("[data-file-meta]");
  const submit = form.querySelector("[data-submit]");
  const progressBox = form.querySelector("[data-upload-progress]");
  const bar = form.querySelector("[data-upload-bar]");
  const progressText = form.querySelector("[data-upload-text]");
  const errorBox = form.querySelector("[data-upload-error]");

  const formatSize = (bytes) =>
    bytes >= 1 << 30 ? `${(bytes / (1 << 30)).toFixed(1)} GB`
    : bytes >= 1 << 20 ? `${(bytes / (1 << 20)).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1024))} KB`;

  function showFile() {
    const file = fileInput.files[0];
    fileField.classList.toggle("has-file", Boolean(file));
    fileName.textContent = file ? file.name : "选择或拖入视频";
    fileMeta.textContent = file ? `${file.name} · ${formatSize(file.size)}` : "支持 MP4、MOV、MKV、WebM 等常见格式";
  }
  fileInput.addEventListener("change", showFile);
  ["dragenter", "dragover"].forEach((type) =>
    fileField.addEventListener(type, () => fileField.classList.add("dragging")),
  );
  ["dragleave", "drop"].forEach((type) =>
    fileField.addEventListener(type, () => fileField.classList.remove("dragging")),
  );

  form.querySelectorAll("[data-toggle]").forEach((toggle) =>
    toggle.addEventListener("click", () => {
      const on = !toggle.classList.contains("on");
      toggle.classList.toggle("on", on);
      toggle.setAttribute("aria-checked", String(on));
    }),
  );

  // 折叠的“术语表与补充信息”标题上显示已选数量
  const contextCount = form.querySelector("[data-context-count]");
  function showContext() {
    const n = form.querySelectorAll('input[name="glossaries"]:checked').length;
    const notes = form.querySelector('textarea[name="notes"]').value.trim();
    const parts = [];
    if (n) parts.push(`${n} 个术语表`);
    if (notes) parts.push("有补充信息");
    contextCount.textContent = parts.length ? `· ${parts.join("，")}` : "";
  }
  form.querySelector(".context-options").addEventListener("input", showContext);
  form.querySelector(".context-options").addEventListener("change", showContext);

  function fail(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
    progressBox.hidden = true;
    submit.disabled = false;
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!fileInput.files[0]) return fail("请选择视频文件");
    const data = new FormData(form);
    form.querySelectorAll("[data-toggle]").forEach((t) => data.set(t.dataset.toggle, String(t.classList.contains("on"))));
    errorBox.hidden = true;
    progressBox.hidden = false;
    submit.disabled = true;
    bar.style.width = "0%";
    progressText.textContent = "正在上传…";

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

  // 有进行中的任务时定期刷新状态标签
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
