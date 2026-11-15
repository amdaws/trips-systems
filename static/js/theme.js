/*
 * Light/dark button in the header. Until it's pressed the site follows the
 * device's setting; a choice is remembered in this browser. The choice is put on
 * <html data-theme> (an inline script in the page head does this before the page
 * draws, so it doesn't flash), and a "themechange" event lets the map recolour.
 */
(function () {
  "use strict";

  var root = document.documentElement;
  var button = document.getElementById("theme-toggle");
  var deviceDark = window.matchMedia("(prefers-color-scheme: dark)");
  var metas = Array.prototype.slice.call(document.querySelectorAll('meta[name="theme-color"]'));
  metas.forEach(function (meta) { meta.dataset.auto = meta.getAttribute("content"); });

  function chosen() {
    var theme = root.getAttribute("data-theme");
    return theme === "light" || theme === "dark" ? theme : "auto";
  }

  function isDark() {
    var theme = chosen();
    return theme === "auto" ? deviceDark.matches : theme === "dark";
  }

  function paintBrowserBar() {
    var theme = chosen();
    metas.forEach(function (meta) {
      meta.setAttribute("content", theme === "auto" ? meta.dataset.auto : theme === "dark" ? "#292929" : "#ffffff");
    });
  }

  function apply(theme) {
    if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
    else root.removeAttribute("data-theme");
    try {
      if (theme === "light" || theme === "dark") localStorage.setItem("theme", theme);
      else localStorage.removeItem("theme");
    } catch (e) { /* private browsing: the choice lasts for this page only */ }
    paintBrowserBar();
    document.dispatchEvent(new CustomEvent("themechange", { detail: { dark: isDark() } }));
  }

  window.siteTheme = { isDark: isDark };
  paintBrowserBar();

  deviceDark.addEventListener("change", function () {
    if (chosen() === "auto") apply("auto");
  });

  if (!button) return;
  function label() { button.setAttribute("aria-label", isDark() ? "Switch to light theme" : "Switch to dark theme"); }
  label();
  button.hidden = false;
  button.addEventListener("click", function () { apply(isDark() ? "light" : "dark"); label(); });
  document.addEventListener("themechange", label);
})();
