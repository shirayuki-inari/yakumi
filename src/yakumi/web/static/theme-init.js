// 在首次绘制前确定主题，避免深色模式下闪白（同步加载，放在 <head> 里）。
(() => {
  let preference = "system";
  try {
    preference = localStorage.getItem("yakumi:theme") || "system";
  } catch {}
  const dark =
    preference === "dark" ||
    (preference !== "light" && preference !== "dark" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.querySelector('meta[name="theme-color"]').content = dark ? "#101317" : "#eef0f3";
})();

// 登录过期时（接口返回 401）回到登录页
(() => {
  const original = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const res = await original(...args);
    if (res.status === 401) {
      window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
    }
    return res;
  };
})();
