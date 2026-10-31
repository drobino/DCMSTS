// Offline capture for field technicians: when the device is offline, eligible form posts are
// queued in localStorage and replayed (with their CSRF token) once connectivity returns.
(function () {
  "use strict";
  var KEY = "dcmsts-offline-queue";

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { return []; }
  }
  function save(q) {
    try { localStorage.setItem(KEY, JSON.stringify(q)); } catch (e) { /* storage unavailable */ }
  }
  function banner() {
    var el = document.getElementById("offline-banner");
    if (!el) return;
    var pending = load().length;
    el.classList.toggle("hidden", navigator.onLine && pending === 0);
    el.textContent = navigator.onLine
      ? "Syncing " + pending + " offline update(s)…"
      : "Offline: " + pending + " update(s) saved on this device; they will sync when the connection returns.";
  }

  async function flush() {
    var q = load();
    if (!q.length || !navigator.onLine) return banner();
    var remaining = [];
    for (var i = 0; i < q.length; i++) {
      try {
        var resp = await fetch(q[i].action, {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: q[i].body
        });
        if (resp.status >= 500) remaining.push(q[i]);
      } catch (e) {
        remaining.push(q[i]);
      }
    }
    save(remaining);
    banner();
    if (remaining.length < q.length) window.location.reload();
  }

  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (navigator.onLine || form.method.toLowerCase() !== "post" || form.hasAttribute("data-no-offline")
        || form.enctype === "multipart/form-data") return;
    ev.preventDefault();
    var q = load();
    q.push({ action: form.action, body: new URLSearchParams(new FormData(form)).toString(), at: Date.now() });
    save(q);
    banner();
  });

  // Inputs marked data-autosubmit (e.g. the calendar date picker) submit their form as soon as a complete,
  // plausible value is chosen. The year check stops typing a year digit by digit from jumping to year 0002.
  document.addEventListener("change", function (ev) {
    var el = ev.target;
    if (!el.hasAttribute || !el.hasAttribute("data-autosubmit") || !el.form || !el.value) return;
    if (el.type === "date" && !/^(19|2\d)\d\d-\d\d-\d\d$/.test(el.value)) return;
    if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
  });

  window.addEventListener("online", flush);
  window.addEventListener("offline", banner);
  document.addEventListener("DOMContentLoaded", function () { banner(); flush(); });
})();
