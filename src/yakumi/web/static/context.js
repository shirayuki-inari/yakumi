// 背景资料管理：列表、新建、编辑背景说明和固定译法（以实体为单位）、批量添加、合并建议、删除。
(() => {
  const $ = (sel) => document.querySelector(sel);
  let glossaries = JSON.parse(document.getElementById("glossary-data").textContent);
  let current = null; // 正在编辑的背景资料 ID；新建时为 null
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
      const n = g.entries.length;
      li.querySelector(".job-sub").textContent =
        [g.notes ? g.notes.split("\n")[0].slice(0, 40) : "没有背景说明", n ? `${n} 条固定译法` : ""].filter(Boolean).join(" · ");
      btn.addEventListener("click", () => open(g.id));
      list.append(li);
    }
    emptyList.hidden = glossaries.length > 0;
  }

  // ---- 编辑器 ---------------------------------------------------------
  // 每条固定译法是一个实体：规范名称、类别、已确认的其他称呼、按语言的译名（"*" 表示任何语言）
  const translationTemplate = $("[data-translation-template]");
  const legacyNotice = $("[data-legacy-notice]");
  const mergeHints = $("[data-merge-hints]");
  const splitAliases = (text) => text.split(/[、,，;；]/).map((a) => a.trim()).filter(Boolean);

  function addTranslation(card, lang = "*", text = "", focus = false) {
    const row = translationTemplate.content.firstElementChild.cloneNode(true);
    const select = row.querySelector("[data-lang]");
    if (![...select.options].some((o) => o.value === lang)) select.append(new Option(lang, lang));
    select.value = lang;
    row.querySelector("[data-text]").value = text;
    card.querySelector("[data-translations]").append(row);
    if (focus) row.querySelector("[data-text]").focus();
    return row;
  }

  function addTerm(entry = null, focus = false) {
    const card = termTemplate.content.firstElementChild.cloneNode(true);
    card.dataset.id = entry?.id || "";
    card.querySelector('[data-entry="canonical"]').value = entry?.canonical || "";
    const category = card.querySelector('[data-entry="category"]');
    if (entry?.category && ![...category.options].some((o) => o.value === entry.category)) {
      category.append(new Option(entry.category, entry.category));
    }
    category.value = entry?.category || "";
    card.querySelector('[data-entry="aliases"]').value = (entry?.aliases || []).join("、");
    const translations = Object.entries(entry?.translations || { "*": "" });
    for (const [lang, text] of translations) addTranslation(card, lang, text);
    terms.append(card);
    if (focus) card.querySelector("input").focus();
    countTerms();
    return card;
  }

  function countTerms() {
    const n = collectTerms().length;
    termCount.textContent = n ? `（${n}）` : "";
  }

  function collectTerms() {
    return [...terms.querySelectorAll(".entry-card")]
      .map((card) => {
        const translations = {};
        card.querySelectorAll(".translation-row").forEach((row) => {
          translations[row.querySelector("[data-lang]").value] = row.querySelector("[data-text]").value.trim();
        });
        return {
          id: card.dataset.id || "",
          canonical: card.querySelector('[data-entry="canonical"]').value.trim(),
          category: card.querySelector('[data-entry="category"]').value,
          aliases: splitAliases(card.querySelector('[data-entry="aliases"]').value),
          translations,
        };
      })
      .filter((t) => t.canonical);
  }

  // 译名相同的条目可能是同一实体的不同称呼：提示，由用户决定是否合并
  function showMergeHints() {
    const groups = new Map();
    for (const card of terms.querySelectorAll(".entry-card")) {
      const name = card.querySelector('[data-entry="canonical"]').value.trim();
      if (!name) continue;
      card.querySelectorAll(".translation-row").forEach((row) => {
        const text = row.querySelector("[data-text]").value.trim();
        if (!text) return;
        const key = `${row.querySelector("[data-lang]").value}\u0000${text}`;
        if (!groups.has(key)) groups.set(key, { text, cards: [] });
        if (!groups.get(key).cards.includes(card)) groups.get(key).cards.push(card);
      });
    }
    const hints = [...groups.values()].filter((g) => g.cards.length > 1);
    mergeHints.replaceChildren();
    for (const hint of hints) {
      const names = hint.cards.map((c) => `「${c.querySelector('[data-entry="canonical"]').value.trim()}」`);
      const line = document.createElement("div");
      line.className = "merge-hint";
      const text = document.createElement("span");
      text.textContent = `${names.join("")}的译名都是“${hint.text}”，如果是同一实体的不同称呼，可以合并为一条。`;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "text-button";
      btn.textContent = `合并到${names[0]}`;
      btn.addEventListener("click", () => mergeCards(hint.cards));
      line.append(text, btn);
      mergeHints.append(line);
    }
    mergeHints.hidden = hints.length === 0;
  }

  function mergeCards(cards) {
    const [target, ...rest] = cards;
    const aliasInput = target.querySelector('[data-entry="aliases"]');
    const canonical = target.querySelector('[data-entry="canonical"]').value.trim();
    const category = (card) => card.querySelector('[data-entry="category"]');
    const translations = new Map();
    const conflicts = new Set();
    for (const card of cards) {
      for (const row of card.querySelectorAll(".translation-row")) {
        const lang = row.querySelector("[data-lang]").value;
        const text = row.querySelector("[data-text]").value.trim();
        if (translations.has(lang)) {
          if (translations.get(lang) !== text) conflicts.add(lang);
        } else {
          translations.set(lang, text);
        }
      }
    }
    if (conflicts.size && !window.confirm(`这些语言有不同的译名（${[...conflicts].join("、")}）。保留最靠前条目的译名并合并？`)) return;
    const current = new Set([...target.querySelectorAll("[data-lang]")].map((select) => select.value));
    for (const [lang, text] of translations) {
      if (!current.has(lang)) addTranslation(target, lang, text);
    }
    const aliases = splitAliases(aliasInput.value);
    for (const card of rest) {
      if (!category(target).value) category(target).value = category(card).value;
      aliases.push(card.querySelector('[data-entry="canonical"]').value.trim(), ...splitAliases(card.querySelector('[data-entry="aliases"]').value));
      card.remove();
    }
    aliasInput.value = [...new Set(aliases)].filter((a) => a && a !== canonical).join("、");
    setDirty(true);
    countTerms();
    showMergeHints();
  }

  function open(id) {
    if (dirty && !window.confirm("当前背景资料有未保存的修改，确定放弃吗？")) return;
    const g = glossaries.find((x) => x.id === id) || null;
    current = g ? g.id : null;
    title.textContent = g ? g.name : "新建背景资料";
    nameInput.value = g ? g.name : "";
    notesInput.value = g ? g.notes : "";
    terms.replaceChildren();
    for (const entry of g ? g.entries : []) addTerm(entry);
    if (!g || !g.entries.length) addTerm();
    legacyNotice.hidden = !g?.legacy;
    showMergeHints();
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
  editor.addEventListener("change", (event) => {
    if (event.target.matches("select")) setDirty(true);
    if (event.target.closest(".entry-card")) showMergeHints();
  });

  terms.addEventListener("click", (event) => {
    const removeTerm = event.target.closest("[data-remove-term]");
    const removeTranslation = event.target.closest("[data-remove-translation]");
    const addLang = event.target.closest("[data-add-translation]");
    if (removeTerm) {
      removeTerm.closest(".entry-card").remove();
      if (!terms.children.length) addTerm();
    } else if (removeTranslation) {
      removeTranslation.closest(".translation-row").remove();
    } else if (addLang) {
      const card = addLang.closest(".entry-card");
      const used = new Set([...card.querySelectorAll("[data-lang]")].map((s) => s.value));
      const next = [...translationTemplate.content.querySelector("[data-lang]").options].map((o) => o.value).find((v) => !used.has(v));
      addTranslation(card, next || "*", "", true);
    } else {
      return;
    }
    setDirty(true);
    countTerms();
    showMergeHints();
  });

  $("[data-add-term]").addEventListener("click", () => addTerm(null, true));
  $("[data-new-glossary]").addEventListener("click", () => open(null));

  // 批量添加：每行 “原文 = 译文”，也接受 Tab、逗号分隔（与命令行导入的规则一致），译名不限定语言
  $("[data-bulk-apply]").addEventListener("click", () => {
    const box = $("[data-bulk]");
    const existing = new Map(
      [...terms.querySelectorAll(".entry-card")].map((card) => [card.querySelector('[data-entry="canonical"]').value.trim(), card]),
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
      const card = existing.get(source);
      if (card) {
        const row = [...card.querySelectorAll(".translation-row")].find((r) => r.querySelector("[data-lang]").value === "*");
        if (row) row.querySelector("[data-text]").value = target;
        else addTranslation(card, "*", target);
      } else {
        // 替换掉空白的占位条目
        const blank = [...terms.querySelectorAll(".entry-card")].find(
          (c) => !c.querySelector('[data-entry="canonical"]').value.trim() && ![...c.querySelectorAll("[data-text]")].some((i) => i.value.trim()),
        );
        blank?.remove();
        existing.set(source, addTerm({ canonical: source, translations: { "*": target } }));
      }
      added += 1;
    }
    if (added) {
      box.value = "";
      setDirty(true);
      countTerms();
      showMergeHints();
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
    const dup = items.find((t) => (seen.has(t.canonical) ? true : (seen.add(t.canonical), false)));
    if (dup) return fail(`“${dup.canonical}”重复了`);
    saveBtn.disabled = true;
    errorBox.hidden = true;
    try {
      const res = await fetch(current ? `/api/glossaries/${encodeURIComponent(current)}` : "/api/glossaries", {
        method: current ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, notes: notesInput.value, entries: items,
          revision: glossaries.find((g) => g.id === current)?.revision }),
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
    if (!current || !window.confirm("确定删除这份背景资料吗？已经选用它的任务重新翻译时需要另选。")) return;
    try {
      const revision = glossaries.find((g) => g.id === current)?.revision;
      const res = await fetch(`/api/glossaries/${encodeURIComponent(current)}`, {
        method: "DELETE", headers: revision ? { "If-Match": `"${revision}"` } : {},
      });
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
