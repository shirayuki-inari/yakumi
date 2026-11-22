// 设置页：按分组显示可修改的配置项，修改后一次保存；环境检查与连接测试。
(() => {
  const $ = (sel) => document.querySelector(sel);
  let data = JSON.parse(document.getElementById("settings-data").textContent);
  let fields = {};
  let changes = {}; // 配置项 ID → 新值；null 表示从配置文件中删除（恢复默认或上一层的值）
  let saving = false;
  let hideTimer = null;

  const groupsBox = $("[data-groups]");
  const nav = $("[data-settings-nav]");
  const saveBar = $("[data-save-bar]");
  const saveText = $("[data-save-text]");
  const saveBtn = $("[data-save-settings]");
  const discardBtn = $("[data-discard]");

  const icon = (name) => document.querySelector(`template[data-icon="${name}"]`).content.firstElementChild.cloneNode(true);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function isOpenRouter(url) {
    try {
      const u = new URL(url);
      return u.protocol === "https:" && u.hostname === "openrouter.ai" && (u.port === "" || u.port === "443");
    } catch {
      return false;
    }
  }

  // 正在编辑的值：有修改时用修改后的，标记删除时用删除后会生效的值
  function valueOf(id) {
    const f = fields[id];
    if (!(id in changes)) return f.value;
    return changes[id] === null ? f.fallback : changes[id];
  }

  function visible(f) {
    const cond = f.show_if;
    if (!cond) return true;
    const value = valueOf(cond.field);
    return cond.openrouter ? isOpenRouter(value) : cond.in.includes(value);
  }

  const display = (value) =>
    value === true ? "开" : value === false ? "关" : value === "" || value == null ? "（空）" : String(value);

  function choiceLabel(f, value) {
    const choice = f.choices.find(([v]) => v === value);
    return choice ? choice[1] : display(value);
  }

  // ---- 记录修改 ---------------------------------------------------------
  function same(f, value) {
    if (f.kind === "int" || f.kind === "float") return value !== "" && Number(value) === Number(f.value);
    return value === f.value;
  }

  function setChange(id, value) {
    const f = fields[id];
    if (f.kind === "secret" ? value === "" : same(f, value)) delete changes[id];
    else changes[id] = value;
    refreshRow(id);
    refreshVisibility();
    refreshSaveBar();
  }

  function refreshRow(id) {
    const row = groupsBox.querySelector(`[data-id="${CSS.escape(id)}"]`);
    if (!row) return;
    const f = fields[id];
    row.classList.toggle("changed", id in changes);
    const note = row.querySelector(".setting-note");
    note.replaceChildren();
    if (f.locked) {
      note.textContent = f.locked;
      return;
    }
    if (id in changes) {
      if (changes[id] === null) {
        note.append(f.kind === "secret" ? "保存后从配置文件中删除这个密钥" : `保存后恢复为 ${f.kind === "choice" ? choiceLabel(f, f.fallback) : display(f.fallback)}`);
      } else {
        note.append(f.kind === "secret" ? "保存后使用新密钥" : `原来是 ${f.kind === "choice" ? choiceLabel(f, f.value) : display(f.value)}`);
      }
      note.append(" · ", linkButton("撤销", () => {
        delete changes[id];
        render();
      }));
    } else if (f.in_target) {
      const label = f.kind === "secret" ? "删除密钥" : "恢复默认";
      if (f.kind !== "secret" || f.set) {
        note.append(linkButton(label, () => {
          changes[id] = null;
          render();
        }));
      }
    }
  }

  function linkButton(text, onClick) {
    const btn = el("button", "text-button inline-link", text);
    btn.type = "button";
    btn.addEventListener("click", onClick);
    return btn;
  }

  function refreshVisibility() {
    groupsBox.querySelectorAll(".setting-row").forEach((row) => (row.hidden = !visible(fields[row.dataset.id])));
  }

  function refreshSaveBar() {
    const n = Object.keys(changes).length;
    if (saving) return;
    clearTimeout(hideTimer);
    saveBar.classList.remove("success", "error");
    saveBar.hidden = n === 0;
    saveBtn.hidden = discardBtn.hidden = false;
    saveText.textContent = `有 ${n} 项未保存的修改`;
  }

  // ---- 控件 -------------------------------------------------------------
  function selectControl(f, id, value) {
    const wrap = el("div", "select-wrap plain");
    const select = el("select");
    select.id = id;
    const choices = [...f.choices];
    if (!choices.some(([v]) => v === value)) choices.push([value, `${display(value)}（配置文件中的值）`]);
    for (const [v, label] of choices) {
      const option = el("option", "", label);
      option.value = v;
      option.selected = v === value;
      select.append(option);
    }
    select.addEventListener("change", () => setChange(f.id, select.value));
    wrap.append(select, icon("chevron"));
    return [wrap, select];
  }

  function toggleControl(f, id, value) {
    const btn = el("button", `toggle${value ? " on" : ""}`);
    btn.type = "button";
    btn.id = id;
    btn.setAttribute("role", "switch");
    btn.setAttribute("aria-checked", String(Boolean(value)));
    btn.append(el("span"));
    btn.addEventListener("click", () => {
      const on = !btn.classList.contains("on");
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-checked", String(on));
      setChange(f.id, on);
    });
    return [btn, btn];
  }

  function inputControl(f, id, value) {
    const input = el("input", "text-input");
    input.id = id;
    input.autocomplete = "off";
    input.spellcheck = false;
    if (f.kind === "int" || f.kind === "float") {
      input.type = "number";
      input.inputMode = f.kind === "int" ? "numeric" : "decimal";
      input.step = f.kind === "int" ? "1" : "any";
      if (f.min !== null) input.min = f.min;
      if (f.max !== null) input.max = f.max;
    } else {
      input.type = f.kind === "url" ? "url" : "text";
    }
    input.value = value ?? "";
    input.placeholder = f.placeholder || "";
    if (f.suggestions.length) {
      const list = el("datalist");
      list.id = `${id}-options`;
      for (const s of f.suggestions) {
        const option = el("option");
        option.value = s;
        list.append(option);
      }
      input.setAttribute("list", list.id);
      input.addEventListener("input", () => setChange(f.id, input.value));
      const box = el("div", "input-box");
      box.append(input, list);
      return [box, input];
    }
    input.addEventListener("input", () => setChange(f.id, input.value));
    return [input, input];
  }

  function textareaControl(f, id, value) {
    const area = el("textarea");
    area.id = id;
    area.rows = 3;
    area.maxLength = 4000;
    area.value = value ?? "";
    area.placeholder = f.placeholder || "";
    const autosize = () => {
      area.style.height = "auto";
      area.style.height = `${area.scrollHeight + 2}px`;
    };
    area.addEventListener("input", () => {
      autosize();
      setChange(f.id, area.value);
    });
    requestAnimationFrame(autosize);
    return [area, area];
  }

  function secretControl(f, id) {
    const box = el("div", "secret-box");
    const status = el("span", `secret-status${f.set ? " set" : ""}`);
    status.append(icon("key"), f.set ? `已设置 · ${f.source}` : "未设置");
    const input = el("input", "text-input");
    input.id = id;
    input.type = "password";
    input.autocomplete = "new-password";
    input.spellcheck = false;
    input.placeholder = f.set ? "输入新密钥以替换" : "粘贴密钥";
    input.value = typeof changes[f.id] === "string" ? changes[f.id] : "";
    input.addEventListener("input", () => setChange(f.id, input.value.trim()));
    box.append(input, status);
    return [box, input];
  }

  function renderField(f) {
    const id = `setting-${f.id.replace(".", "-")}`;
    const row = el("div", "setting-row");
    row.dataset.id = f.id;
    const text = el("div", "setting-text");
    const head = el("div", "setting-head");
    const label = el("label", "setting-label", f.label);
    label.htmlFor = id;
    head.append(label);
    // 只标出来自配置文件或环境变量的值，默认值不打标记
    if (f.kind !== "secret" && f.source && f.source !== "默认") {
      head.append(el("span", `source-badge${f.source === "默认" ? " default" : ""}`, f.source));
    }
    text.append(head);
    if (f.help) text.append(el("p", "field-hint flush", f.help));

    const value = f.kind === "secret" ? null : valueOf(f.id);
    const build = { choice: selectControl, bool: toggleControl, textarea: textareaControl, secret: secretControl }[f.kind] || inputControl;
    const [control, focusable] = build(f, id, value);
    if (f.locked) focusable.disabled = true;
    const controlBox = el("div", `setting-control${f.kind === "textarea" ? " wide" : ""}`);
    controlBox.append(control);
    row.append(text, controlBox, el("p", "setting-note"));
    return row;
  }

  // ---- 整页渲染 -----------------------------------------------------------
  function render() {
    fields = {};
    for (const g of data.groups) for (const f of g.fields) fields[f.id] = f;
    const y = window.scrollY;
    groupsBox.replaceChildren();
    nav.querySelectorAll("[data-generated]").forEach((a) => a.remove());
    for (const g of data.groups) {
      const section = el("section", "control-panel settings-group");
      section.id = `group-${g.id}`;
      const heading = el("div", "panel-heading");
      const h2 = el("h2", "group-title");
      h2.append(icon(g.icon), el("span", "", g.title));
      heading.append(h2);
      section.append(heading, el("p", "field-hint flush group-desc", g.description));
      const rows = el("div", "setting-rows");
      for (const f of g.fields) rows.append(renderField(f));
      section.append(rows);
      groupsBox.append(section);

      const link = el("a");
      link.href = `#group-${g.id}`;
      link.dataset.generated = "";
      link.append(icon(g.icon), el("span", "", g.title));
      nav.append(link);
    }
    const fileLink = el("a");
    fileLink.href = "#file";
    fileLink.dataset.generated = "";
    fileLink.append(icon("file"), el("span", "", "配置文件"));
    nav.append(fileLink);
    Object.keys(fields).forEach(refreshRow);
    refreshVisibility();
    refreshSaveBar();
    renderFileInfo();
    window.scrollTo(0, y);
  }

  function renderFileInfo() {
    const info = $("[data-file-info]");
    info.replaceChildren();
    const add = (term, desc) => info.append(el("dt", "", term), el("dd", "", desc));
    add("保存到", `${data.target}（${data.target_label}${data.target_exists ? "" : "，保存时自动创建，只有你自己可读"}）`);
    add("正在读取", data.files.length ? data.files.map((f) => `${f.path}（${f.label}）`).join("\n") : "没有配置文件，使用默认值和环境变量");
    const list = $("[data-readonly]");
    list.replaceChildren();
    for (const item of data.readonly) list.append(el("dt", "", item.label), el("dd", "", item.value || "（未设置）"));
  }

  // ---- 保存 -------------------------------------------------------------
  function showResult(kind, message) {
    clearTimeout(hideTimer);
    saveBar.hidden = false;
    saveBar.classList.toggle("success", kind === "success");
    saveBar.classList.toggle("error", kind === "error");
    saveText.textContent = message;
    if (kind === "success") {
      saveBtn.hidden = discardBtn.hidden = true;
      hideTimer = setTimeout(() => (saveBar.hidden = true), 5000);
    }
  }

  saveBtn.addEventListener("click", async () => {
    if (saving || !Object.keys(changes).length) return;
    saving = true;
    saveBtn.disabled = discardBtn.disabled = true;
    try {
      const res = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ changes }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      data = body.settings;
      changes = {};
      saving = false;
      render();
      showResult("success", [`已保存到 ${body.path}`, ...body.notices].join("。"));
      runLocal();
    } catch (err) {
      saving = false;
      showResult("error", `保存失败：${err.message}`);
    } finally {
      saveBtn.disabled = discardBtn.disabled = false;
    }
  });

  discardBtn.addEventListener("click", () => {
    changes = {};
    render();
  });

  window.addEventListener("beforeunload", (event) => {
    if (Object.keys(changes).length) event.preventDefault();
  });

  // ---- 环境检查 -----------------------------------------------------------
  const checkList = $("[data-check-list]");
  const onlineList = $("[data-online-list]");
  const localBtn = $("[data-check-local]");
  const onlineBtn = $("[data-check-online]");

  function renderChecks(list, checks, caption) {
    list.replaceChildren();
    if (caption) list.append(el("li", "check-group", caption));
    let group = null;
    for (const c of checks) {
      if (!caption && c.group && c.group !== group) {
        group = c.group;
        list.append(el("li", "check-group", group));
      }
      const li = el("li", `check ${c.status}`);
      const body = el("div", "check-body");
      body.append(el("span", "check-title", c.title));
      for (const line of c.detail || []) body.append(el("span", "check-detail", line));
      if (c.hint) body.append(el("span", "check-hint", c.hint));
      li.append(icon(c.status), body);
      list.append(li);
    }
  }

  function loading(list, text) {
    list.hidden = false;
    list.replaceChildren(el("li", "check loading", text));
  }

  async function runLocal() {
    localBtn.disabled = true;
    loading(checkList, "正在检查…");
    try {
      const res = await fetch("/api/doctor");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      renderChecks(checkList, await res.json());
    } catch (err) {
      renderChecks(checkList, [{ status: "fail", title: "无法完成检查", hint: err.message }]);
    } finally {
      localBtn.disabled = false;
    }
  }

  async function runOnline() {
    if (Object.keys(changes).length && !window.confirm("有未保存的修改，连接测试会使用已保存的设置。继续吗？")) return;
    onlineBtn.disabled = true;
    loading(onlineList, "正在测试连接，最长约 30 秒…");
    try {
      const res = await fetch("/api/doctor/online", { method: "POST" });
      const body = await res.json().catch(() => null);
      if (!res.ok || !Array.isArray(body)) throw new Error(body?.error || `HTTP ${res.status}`);
      renderChecks(onlineList, body.length ? body : [{ status: "info", title: "没有需要联网测试的服务（都使用本地后端）" }], "连接测试");
    } catch (err) {
      renderChecks(onlineList, [{ status: "fail", title: "连接测试失败", hint: err.message }], "连接测试");
    } finally {
      onlineBtn.disabled = false;
    }
  }

  localBtn.addEventListener("click", runLocal);
  onlineBtn.addEventListener("click", runOnline);

  render();
  runLocal();
})();
