// Applies the saved light/dark preference before first paint (loaded synchronously in <head>).
// With no saved choice the theme follows the operating system via prefers-color-scheme.
(function () {
  "use strict";
  var KEY = "dcmsts-theme";
  var root = document.documentElement;
  root.classList.add("js");  // lets CSS hide no-JavaScript fallbacks such as the calendar "Go" button
  try {
    var saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  } catch (e) { /* storage unavailable */ }

  function effective() {
    var t = root.getAttribute("data-theme");
    if (t) return t;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest && ev.target.closest("[data-theme-toggle]");
    if (!btn) return;
    var next = effective() === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem(KEY, next); } catch (e) { /* storage unavailable */ }
  });
})();
