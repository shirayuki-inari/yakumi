// 术语表管理：列表、新建、编辑、批量添加、删除。
(() => {
  const $ = (sel) => document.querySelector(sel);
  let glossaries = JSON.parse(document.getElementById("glossary-data").textContent);
  let current = null; // 正在编辑的术语表 ID；新建时为 null
  let dirty = false;

  const list = $("[data-glossary-list]");
  const emptyList = $("[data-empty-glossaries]");
  const editor = $("[data-editor]");
  const emptyEditor = $("[data-editor-empty]");
  const title = $("[data-editor-title]");
  const nameInput = $('[data-field="name"]');
  const notesInput = $('[data-field="notes"]');
  const terms = $("[data-terms]");
  const termCount = $("[data-term-count]");
  const errorBox = $("[data-glossary-error]");
  const dirtyBadge = $("[data-glossary-dirty]");
  const deleteBtn = $("[data-delete]");
  const saveBtn = $("[data-save]");
  const termTemplate = $("[data-term-template]");
  const itemTemplate = $("[data-item-template]");

  function setDirty(value) {
    dirty = value;
    dirtyBadge.hidden = !value;
  }

  function autosize(el) {
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + 2}px`;
  }

  // ---- 列表 -----------------------------------------------------------
  function renderList() {
    list.replaceChildren();
    for (const g of glossaries) {
      const li = itemTemplate.content.firstElementChild.cloneNode(true);
      const btn = li.querySelector("button");
      btn.dataset.id = g.id;
      btn.classList.toggle("selected", g.id === current);
      li.querySelector(".job-name").textContent = g.name;
      const n = Object.keys(g.terms).length;
      li.querySelector(".job-sub").textContent = `${n} 条术语${g.notes ? " · 有补充信息" : ""}`;
      btn.addEventListener("click", () => open(g.id));
      list.append(li);
    }
    emptyList.hidden = glossaries.length > 0;
  }

  // ---- 编辑器 ---------------------------------------------------------
  function addTerm(source = "", target = "", focus = false) {
    const row = termTemplate.content.firstElementChild.cloneNode(true);
    row.querySelector('[data-term="source"]').value = source;
    row.querySelector('[data-term="target"]').value = target;
    terms.append(row);
    if (focus) row.querySelector("input").focus();
    countTerms();
    return row;
  }

  function countTerms() {
    const n = collectTerms().length;
    termCount.textContent = n ? `（${n}）` : "";
  }

  function collectTerms() {
    return [...terms.querySelectorAll(".term-row")]
      .map((row) => ({
        source: row.querySelector('[data-term="source"]').value.trim(),
        target: row.querySelector('[data-term="target"]').value.trim(),
      }))
      .filter((t) => t.source);
  }

  function open(id) {
    if (dirty && !window.confirm("当前术语表有未保存的修改，确定放弃吗？")) return;
    const g = glossaries.find((x) => x.id === id) || null;
    current = g ? g.id : null;
    title.textContent = g ? g.name : "新建术语表";
    nameInput.value = g ? g.name : "";
    notesInput.value = g ? g.notes : "";
    terms.replaceChildren();
    for (const [src, dst] of Object.entries(g ? g.terms : {})) addTerm(src, dst);
    if (!g || !Object.keys(g.terms).length) addTerm();
    deleteBtn.hidden = !g;
    errorBox.hidden = true;
    editor.hidden = false;
    emptyEditor.hidden = true;
    setDirty(false);
    renderList();
    autosize(notesInput);
    if (!g) nameInput.focus();
    history.replaceState(null, "", g ? `#${encodeURIComponent(g.id)}` : "#new");
  }

  editor.addEventListener("input", (event) => {
    if (event.target.matches("[data-bulk]")) return;
    setDirty(true);
    if (event.target === notesInput) autosize(notesInput);
    countTerms();
  });

  terms.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-remove-term]");
    if (!btn) return;
    btn.closest(".term-row").remove();
    if (!terms.children.length) addTerm();
    setDirty(true);
    countTerms();
  });

  // 在最后一行的译文里按回车，直接新增一行
  terms.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !event.target.matches("input")) return;
    event.preventDefault();
    const row = event.target.closest(".term-row");
    if (event.target.dataset.term === "source") row.querySelector('[data-term="target"]').focus();
    else if (row === terms.lastElementChild) addTerm("", "", true);
    else row.nextElementSibling.querySelector("input").focus();
  });

  $("[data-add-term]").addEventListener("click", () => addTerm("", "", true));
  $("[data-new-glossary]").addEventListener("click", () => open(null));

  // 批量添加：每行 “原文 = 译文”，也接受 Tab、逗号分隔（与命令行导入的规则一致）
  $("[data-bulk-apply]").addEventListener("click", () => {
    const box = $("[data-bulk]");
    const existing = new Map(
      [...terms.querySelectorAll(".term-row")].map((row) => [row.querySelector('[data-term="source"]').value.trim(), row]),
    );
    let added = 0;
    for (let line of box.value.split("\n")) {
      line = line.trim();
      if (!line || line.startsWith("#")) continue;
      const sep = ["\t", "=", ",", "，"].find((s) => line.includes(s));
      const [src, dst = ""] = sep ? [line.slice(0, line.indexOf(sep)), line.slice(line.indexOf(sep) + 1)] : [line];
      const source = src.trim().replace(/^"|"$/g, "");
      const target = dst.trim().replace(/^"|"$/g, "");
      if (!source) continue;
      const row = existing.get(source);
      if (row) row.querySelector('[data-term="target"]').value = target;
      else {
        // 替换掉空白的占位行
        const blank = [...terms.querySelectorAll(".term-row")].find(
          (r) => !r.querySelector('[data-term="source"]').value.trim() && !r.querySelector('[data-term="target"]').value.trim(),
        );
        blank?.remove();
        existing.set(source, addTerm(source, target));
      }
      added += 1;
    }
    if (added) {
      box.value = "";
      setDirty(true);
      countTerms();
    }
  });

  function fail(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  saveBtn.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    if (!name) return fail("请填写名称");
    const seen = new Set();
    const items = collectTerms();
    const dup = items.find((t) => (seen.has(t.source) ? true : (seen.add(t.source), false)));
    if (dup) return fail(`“${dup.source}”重复了`);
    saveBtn.disabled = true;
    errorBox.hidden = true;
    try {
      const res = await fetch(current ? `/api/glossaries/${encodeURIComponent(current)}` : "/api/glossaries", {
        method: current ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, notes: notesInput.value, terms: items }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      glossaries = glossaries.filter((g) => g.id !== current && g.id !== data.id);
      glossaries.push(data);
      glossaries.sort((a, b) => a.id.localeCompare(b.id));
      setDirty(false);
      open(data.id);
    } catch (err) {
      fail(`保存失败：${err.message}`);
    } finally {
      saveBtn.disabled = false;
    }
  });

  deleteBtn.addEventListener("click", async () => {
    if (!current || !window.confirm("确定删除这个术语表吗？已经选用它的任务重新翻译时需要另选。")) return;
    try {
      const res = await fetch(`/api/glossaries/${encodeURIComponent(current)}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      glossaries = glossaries.filter((g) => g.id !== current);
      current = null;
      setDirty(false);
      editor.hidden = true;
      emptyEditor.hidden = false;
      history.replaceState(null, "", "#");
      renderList();
    } catch (err) {
      fail(`删除失败：${err.message}`);
    }
  });

  window.addEventListener("beforeunload", (event) => {
    if (dirty) event.preventDefault();
  });

  renderList();
  const hash = decodeURIComponent(location.hash.slice(1));
  if (hash === "new") open(null);
  else if (glossaries.some((g) => g.id === hash)) open(hash);
})();
