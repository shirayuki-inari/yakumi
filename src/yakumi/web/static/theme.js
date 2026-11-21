// 主题（跟随系统 / 浅色 / 深色）与使用说明对话框。
(() => {
  const KEY = "yakumi:theme";
  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function read() {
    try {
      const saved = localStorage.getItem(KEY);
      return saved === "light" || saved === "dark" ? saved : "system";
    } catch {
      return "system";
    }
  }

  function apply(pref) {
    const dark = pref === "dark" || (pref === "system" && media && media.matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = dark ? "#101317" : "#eef0f3";
  }

  const picker = document.querySelector("[data-theme-picker]");
  let preference = read();

  function sync() {
    apply(preference);
    if (!picker) return;
    picker.querySelectorAll("[data-theme-icon]").forEach((svg) => {
      svg.toggleAttribute("hidden", svg.dataset.themeIcon !== preference);
    });
    picker.querySelectorAll("[role=menuitemradio]").forEach((item) => {
      item.setAttribute("aria-checked", String(item.dataset.value === preference));
    });
    const labels = { system: "跟随系统", light: "浅色", dark: "深色" };
    picker.querySelector(".theme-trigger").title = `外观：${labels[preference]}`;
  }

  function choose(value) {
    preference = value;
    try {
      localStorage.setItem(KEY, value);
    } catch {
      /* 存储不可用时只对当前页面生效 */
    }
    sync();
  }

  media?.addEventListener("change", () => preference === "system" && apply(preference));
  window.addEventListener("storage", (event) => {
    if (event.key === KEY || event.key === null) {
      preference = read();
      sync();
    }
  });

  if (picker) {
    const trigger = picker.querySelector(".theme-trigger");
    const menu = picker.querySelector(".theme-menu");
    const items = () => Array.from(menu.querySelectorAll("button"));
    const setOpen = (open) => {
      menu.hidden = !open;
      trigger.setAttribute("aria-expanded", String(open));
      if (open) (menu.querySelector('[aria-checked="true"]') || items()[0]).focus();
    };
    trigger.addEventListener("click", () => setOpen(menu.hidden));
    trigger.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        setOpen(true);
      }
    });
    menu.addEventListener("click", (event) => {
      const item = event.target.closest("button");
      if (!item) return;
      choose(item.dataset.value);
      setOpen(false);
      trigger.focus();
    });
    menu.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        trigger.focus();
        return;
      }
      const list = items();
      const index = list.indexOf(document.activeElement);
      const next =
        event.key === "ArrowDown" ? (index + 1) % list.length
        : event.key === "ArrowUp" ? (index + list.length - 1) % list.length
        : event.key === "Home" ? 0
        : event.key === "End" ? list.length - 1
        : -1;
      if (next >= 0) {
        event.preventDefault();
        list[next].focus();
      }
    });
    document.addEventListener("pointerdown", (event) => {
      if (!menu.hidden && !picker.contains(event.target)) setOpen(false);
    });
    picker.addEventListener("focusout", (event) => {
      if (!picker.contains(event.relatedTarget)) setOpen(false);
    });
  }
  sync();

  // 使用说明
  const help = document.querySelector("[data-help]");
  if (help) {
    let opener = null;
    const close = () => {
      help.hidden = true;
      opener?.focus();
    };
    document.querySelectorAll("[data-open-help]").forEach((btn) =>
      btn.addEventListener("click", () => {
        opener = btn;
        help.hidden = false;
        help.querySelector("[data-close-help]").focus();
      }),
    );
    help.querySelector("[data-close-help]").addEventListener("click", close);
    help.addEventListener("click", (event) => event.target === help && close());
    document.addEventListener("keydown", (event) => event.key === "Escape" && !help.hidden && close());
  }
})();
