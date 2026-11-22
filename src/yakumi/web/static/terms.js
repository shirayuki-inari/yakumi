// 任务页：术语审核。逐个确认扫描到的人名、昵称、作品名等的规范名称和译名，通过、修改或拒绝。
// 与 job.js 通过事件联系：yakumi:job-state（任务状态更新）、yakumi:job-submitted（提交后任务重新开始处理）。
(() => {
  const panel = document.querySelector("[data-terms-panel]");
  if (!panel) return;
  const $ = (sel) => panel.querySelector(sel);
  const CATEGORIES = Object.fromEntries(JSON.parse(document.getElementById("category-data").textContent));
  const STATUS = { pending: "待审核", approved: "已通过", rejected: "已拒绝" };
  const CONFIDENCE = { high: "高", medium: "中", low: "低" };
  const list = $("[data-term-list]");
  const countLabel = $("[data-terms-count]");
  const notice = $("[data-terms-notice]");
  const emptyHint = $("[data-terms-empty]");
  const errorBox = $("[data-terms-error]");
  const dirtyBadge = $("[data-terms-dirty]");
  const saveBtn = $("[data-terms-save]");
  const continueBtn = $("[data-terms-continue]");
  const filterGroup = $("[data-terms-filter]");
  const video = document.querySelector("[data-video]");

  let job = JSON.parse(document.getElementById("job-data").textContent);
  let data = null; // GET /api/jobs/<id>/terms 的结果
  let changes = new Map(); // key → 修改（发给服务器的 update）
  let filter = "pending";
  let loadedAt = null;
  let submitting = false;
  let loadRequest = 0;

  const busy = () => job.busy || job.status === "running" || job.status === "pending";
  const entities = () => data?.review?.entities || [];

  function setDirty() {
    dirtyBadge.hidden = changes.size === 0;
  }

  function fail(message) {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  }

  // 先取整到 0.1 秒，避免 59.96 显示成 00:60.0
  const fmtTime = (t) => {
    const tenths = Math.round(t * 10);
    return `${String(Math.floor(tenths / 600)).padStart(2, "0")}:${((tenths % 600) / 10).toFixed(1).padStart(4, "0")}`;
  };

  function change(entity, patch) {
    Object.assign(entity, patch);
    const pending = changes.get(entity.key) || { key: entity.key };
    changes.set(entity.key, Object.assign(pending, patch));
    setDirty();
  }

  // ---- 渲染 -----------------------------------------------------------
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function sentenceLine(occ) {
    const s = data.sentences?.[occ.sentence_id];
    const span = el("span", "term-sentence");
    if (!s) {
      span.textContent = `（第 ${occ.sentence_id} 句的原文已经改动）`;
      return span;
    }
    const pos = occ.offset == null ? s.text.indexOf(occ.surface) : Array.from(s.text).slice(0, occ.offset).join("").length;
    if (pos < 0 || !s.text.startsWith(occ.surface, pos)) {
      span.textContent = s.text;
      span.title = "原文已经校对改过，这一处不再适用";
      return span;
    }
    span.append(s.text.slice(0, pos), el("mark", "", occ.surface), s.text.slice(pos + occ.surface.length));
    return span;
  }

  function field(label, input) {
    const wrap = el("label", "term-field");
    wrap.append(el("span", "field-label", label), input);
    return wrap;
  }

  function textInput(value, placeholder, onInput) {
    const input = el("input", "text-input");
    input.type = "text";
    input.maxLength = 200;
    input.value = value ?? "";
    input.placeholder = placeholder;
    input.addEventListener("input", () => onInput(input.value.trim()));
    return input;
  }

  function selectWrap(select) {
    const wrap = el("div", "select-wrap plain");
    wrap.append(select);
    wrap.insertAdjacentHTML(
      "beforeend",
      '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>',
    );
    return wrap;
  }

  function card(entity) {
    const node = el("article", `term-card ${entity.status}`);
    node.dataset.key = entity.key;
    const locked = submitting || busy() || loadedAt === null;

    const head = el("div", "term-head");
    const title = el("div", "term-title");
    title.append(el("strong", "", entity.surfaces.join("、")));
    if (entity.canonical && !entity.surfaces.includes(entity.canonical)) title.append(el("span", "term-arrow", ` → ${entity.canonical}`));
    const chip = el("span", "status-chip", STATUS[entity.status]);
    chip.dataset.status = entity.status === "approved" ? "done" : entity.status === "rejected" ? "error" : "pending";
    head.append(title, chip);

    const meta = [CATEGORIES[entity.category] || entity.category, `置信度${CONFIDENCE[entity.confidence] || entity.confidence}`,
      `${entity.occurrences.length} 处`];
    if (entity.library_id) meta.push(entity.auto ? "与背景资料一致，自动通过" : "对应背景资料中的条目");
    if (entity.saved_to?.length) meta.push(`已保存到 ${entity.saved_to.join("、")}`);
    node.append(head, el("p", "term-meta", meta.join(" · ")));
    if (entity.evidence?.length) node.append(el("p", "term-evidence", entity.evidence[0]));

    const fields = el("div", "term-fields");
    fields.append(
      field("规范名称", textInput(entity.canonical, "正确的原文写法；不确定可留空", (v) => change(entity, { canonical: v || null }))),
      field("译名", textInput(entity.translation, "留空由模型决定，全片保持一致", (v) => change(entity, { translation: v || null }))),
    );
    const category = el("select");
    for (const [value, label] of Object.entries(CATEGORIES)) {
      const opt = el("option", "", label);
      opt.value = value;
      opt.selected = value === entity.category;
      category.append(opt);
    }
    category.addEventListener("change", () => change(entity, { category: category.value }));
    fields.append(field("类别", selectWrap(category)));
    node.append(fields);

    // 出现位置：可以单独排除某一处（例如同音的普通词）
    const occ = el("details", "term-occurrences");
    occ.append(el("summary", "", `原文中的 ${entity.occurrences.length} 处`));
    const ul = el("ul");
    entity.occurrences.forEach((o) => {
      const li = el("li", o.status === "rejected" ? "excluded" : "");
      const time = el("button", "cue-time", fmtTime(o.start));
      time.type = "button";
      time.title = video ? "点击跳转" : "";
      time.addEventListener("click", () => {
        if (!video) return;
        video.currentTime = o.start + 0.01;
        video.play().catch(() => {});
      });
      const toggle = el("label", "term-use");
      const box = el("input");
      box.type = "checkbox";
      box.checked = o.status !== "rejected";
      box.disabled = locked;
      box.setAttribute("aria-label", "这一处适用");
      box.addEventListener("change", () => {
        o.status = box.checked ? "pending" : "rejected";
        li.classList.toggle("excluded", !box.checked);
        const occs = entity.occurrences.map(({ sentence_id, surface, status, offset }) => ({ sentence_id, surface, status, offset }));
        change(entity, {});
        changes.get(entity.key).occurrences = occs;
      });
      toggle.append(box, el("span", "", "适用"));
      li.append(time, sentenceLine(o), toggle);
      ul.append(li);
    });
    occ.append(ul);
    node.append(occ);

    const actions = el("div", "term-actions");
    const approve = el("button", "secondary-button", entity.status === "approved" ? "已通过" : "通过");
    approve.type = "button";
    approve.classList.toggle("selected", entity.status === "approved");
    approve.addEventListener("click", () => {
      change(entity, { status: entity.status === "approved" ? "pending" : "approved" });
      render();
    });
    const reject = el("button", "secondary-button danger", entity.status === "rejected" ? "已拒绝" : "拒绝");
    reject.type = "button";
    reject.classList.toggle("selected", entity.status === "rejected");
    reject.addEventListener("click", () => {
      change(entity, { status: entity.status === "rejected" ? "pending" : "rejected" });
      render();
    });
    actions.append(approve, reject);

    // 合并到另一个术语：没能自动关联的同一实体的不同称呼
    const others = entities().filter((e) => e.key !== entity.key && e.status !== "rejected");
    if (others.length) {
      const merge = el("select");
      merge.setAttribute("aria-label", "合并到另一个术语");
      merge.append(Object.assign(el("option", "", "合并到…"), { value: "" }));
      for (const e of others) merge.append(Object.assign(el("option", "", e.canonical || e.surfaces.join("、")), { value: e.key }));
      merge.addEventListener("change", () => {
        if (!merge.value) return;
        submit("save", { merges: [{ into: merge.value, keys: [entity.key] }] });
      });
      actions.append(selectWrap(merge));
    }
    // 保存到背景资料：只保存规范名称和译名，不保存识别错误的写法
    if (data.glossaries.length && entity.status !== "rejected") {
      const save = el("select");
      save.setAttribute("aria-label", "保存到背景资料");
      save.append(Object.assign(el("option", "", "保存到背景资料…"), { value: "" }));
      const sorted = [...data.glossaries].sort((a, b) => data.job_glossaries.includes(b.id) - data.job_glossaries.includes(a.id));
      for (const g of sorted) save.append(Object.assign(el("option", "", g.name), { value: g.id }));
      save.addEventListener("change", () => {
        if (!save.value) return;
        if (!entity.canonical) {
          fail("请先填写规范名称，再保存到背景资料");
          save.value = "";
          return;
        }
        if (entity.status !== "approved") change(entity, { status: "approved" });
        submit("save", { save_to: [{ key: entity.key, glossary: save.value }] });
      });
      actions.append(selectWrap(save));
    }
    node.append(actions);
    if (locked) node.querySelectorAll("input, select, button:not(.cue-time)").forEach((c) => (c.disabled = true));
    return node;
  }

  function render() {
    const all = entities();
    const shown = filter === "all" ? all : all.filter((e) => e.status === filter);
    list.replaceChildren(...shown.map(card));
    const counts = { pending: 0, approved: 0, rejected: 0 };
    all.forEach((e) => (counts[e.status] += 1));
    countLabel.textContent = all.length ? `${all.length} 个 · 待审核 ${counts.pending}` : "";
    filterGroup.querySelectorAll("button").forEach((btn) => {
      const selected = btn.dataset.value === filter;
      btn.classList.toggle("selected", selected);
      btn.setAttribute("aria-selected", String(selected));
      const n = btn.dataset.value === "all" ? all.length : counts[btn.dataset.value];
      btn.textContent = `${{ pending: "待审核", approved: "已通过", rejected: "已拒绝", all: "全部" }[btn.dataset.value]} ${n}`;
    });
    emptyHint.hidden = shown.length > 0;
    emptyHint.textContent = all.length ? "这一类里没有术语。" : "没有扫描到需要统一译名的术语。";
    const awaiting = job.status === "awaiting_review";
    const translated = job.completed?.includes("translate");
    notice.hidden = !awaiting && !(data.invalid > 0) && !data.failed_chunks;
    notice.textContent = awaiting
      ? `扫描到 ${all.length} 个术语，${counts.pending} 个待审核。确认规范名称和译名后点击“完成审核，继续翻译”；仍待审核的术语不会使用。`
      : data.invalid > 0 ? `另有 ${data.invalid} 条扫描结果在原文里找不到对应的片段，已忽略。` : "";
    if (awaiting && data.invalid > 0) notice.textContent += `（另有 ${data.invalid} 条在原文里找不到，已忽略）`;
    if (data.failed_chunks) notice.textContent += ` 有 ${data.failed_chunks} 个文本块扫描失败。`;
    continueBtn.querySelector("span").textContent = translated ? "保存并重新翻译" : "完成审核，继续翻译";
    const locked = submitting || busy() || loadedAt === null;
    saveBtn.disabled = locked || changes.size === 0;
    continueBtn.disabled = locked || !(awaiting || data.review.finished || translated);
  }

  // ---- 读写 -----------------------------------------------------------
  async function load() {
    if (changes.size || submitting || busy() || job.term_mode === "off") return;
    const requestId = ++loadRequest;
    try {
      const res = await fetch(`/api/jobs/${job.id}/terms`);
      if (!res.ok) return;
      const snapshot = await res.json();
      if (requestId !== loadRequest || changes.size || submitting || busy() || snapshot.revision < job.revision) return;
      data = snapshot;
    } catch {
      return; // 稍后任务状态更新时再试
    }
    loadedAt = data.revision;
    setDirty();
    panel.hidden = !data.review;
    if (!data.review) return;
    if (filter === "pending" && !entities().some((e) => e.status === "pending")) filter = "all";
    render();
  }

  async function submit(action, extra = {}) {
    if (submitting || busy() || loadedAt === null) return;
    if (action === "continue" && job.completed?.includes("translate") &&
        !window.confirm("会按审核结果重新翻译，覆盖校对过的译文。确定继续吗？")) return;
    submitting = true;
    fail("");
    render();
    try {
      const res = await fetch(`/api/jobs/${job.id}/terms`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision: loadedAt, action, updates: [...changes.values()], ...extra }),
      });
      const result = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(result.error || `HTTP ${res.status}`);
      job = result;
      changes = new Map();
      loadedAt = null;
      setDirty();
      // 让 job.js 换上新的任务版本；继续处理时它会开始轮询进度
      const name = action === "continue" ? "yakumi:job-submitted" : "yakumi:job-refresh";
      document.dispatchEvent(new CustomEvent(name, { detail: result }));
      submitting = false;
      await load();
    } catch (err) {
      fail(`保存失败：${err.message}`);
    } finally {
      submitting = false;
      if (data?.review) render();
    }
  }

  filterGroup.addEventListener("click", (event) => {
    const btn = event.target.closest("button");
    if (!btn) return;
    filter = btn.dataset.value;
    render();
  });
  saveBtn.addEventListener("click", () => submit("save"));
  continueBtn.addEventListener("click", () => submit("continue"));

  document.addEventListener("yakumi:job-state", (event) => {
    const before = job;
    job = event.detail;
    if (job.term_mode === "off" || busy()) {
      if (data?.review) render();
      return;
    }
    // 扫描刚完成或审核数据在别处改过时重新读取；有未保存的修改时不覆盖
    if (!changes.size && !submitting && (loadedAt === null || before.status !== job.status || job.revision !== loadedAt)) load();
    else if (data?.review) render();
  });

  window.addEventListener("beforeunload", (event) => {
    if (changes.size) event.preventDefault();
  });
})();
