// Themed date picker for the calendar toolbar (replaces the browser's unstyleable native pop-up).
// Markup: <div data-datepicker data-value="YYYY-MM-DD" data-today="YYYY-MM-DD"> with a button and an empty
// .datepick-pop inside a <form> that also holds <input type="date" name="date">. Choosing a day writes it into
// that input and submits the form, so navigation and filters work exactly as without JavaScript.
(function () {
  "use strict";
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December"];
  var DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];

  function parse(iso) { var p = iso.split("-"); return new Date(+p[0], +p[1] - 1, +p[2]); }
  function iso(d) {
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }
  function addDays(d, n) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function init(root) {
    var btn = root.querySelector(".datepick-btn");
    var pop = root.querySelector(".datepick-pop");
    var form = root.closest("form");
    var input = form && form.querySelector('input[type="date"][name="date"]');
    if (!btn || !pop || !input) return;
    var selected = parse(root.getAttribute("data-value"));
    var today = parse(root.getAttribute("data-today"));
    var view = new Date(selected.getFullYear(), selected.getMonth(), 1);
    var focusDay = selected;

    function choose(d) {
      input.value = iso(d);
      root.querySelector(".datepick-label").textContent =
        d.getDate() + " " + MONTHS[d.getMonth()].slice(0, 3) + " " + d.getFullYear();
      close(false);
      form.submit();
    }

    function render() {
      pop.textContent = "";
      var head = el("div", "dp-head");
      var prev = el("button", "dp-nav", "‹"); prev.type = "button"; prev.setAttribute("aria-label", "Previous month");
      var next = el("button", "dp-nav", "›"); next.type = "button"; next.setAttribute("aria-label", "Next month");
      var title = el("div", "dp-title", MONTHS[view.getMonth()] + " " + view.getFullYear());
      title.setAttribute("aria-live", "polite");
      prev.addEventListener("click", function () { shiftMonth(-1); });
      next.addEventListener("click", function () { shiftMonth(1); });
      head.append(prev, title, next);

      var grid = el("div", "dp-grid");
      grid.setAttribute("role", "grid");
      DAYS.forEach(function (n) { grid.appendChild(el("span", "dp-dow", n)); });
      var lead = (view.getDay() + 6) % 7;            // Monday-first offset
      var start = addDays(view, -lead);
      for (var i = 0; i < 42; i++) {
        var d = addDays(start, i);
        var b = el("button", "dp-day", String(d.getDate()));
        b.type = "button";
        b.setAttribute("data-date", iso(d));
        b.setAttribute("aria-label", d.toDateString());
        b.tabIndex = iso(d) === iso(focusDay) ? 0 : -1;
        if (d.getMonth() !== view.getMonth()) b.classList.add("out");
        if (iso(d) === iso(today)) { b.classList.add("today"); b.setAttribute("aria-current", "date"); }
        if (iso(d) === iso(selected)) { b.classList.add("sel"); b.setAttribute("aria-selected", "true"); }
        grid.appendChild(b);
      }
      grid.addEventListener("click", function (ev) {
        var t = ev.target.closest(".dp-day");
        if (t) choose(parse(t.getAttribute("data-date")));
      });

      var foot = el("div", "dp-foot");
      var tbtn = el("button", "dp-link", "Today"); tbtn.type = "button";
      tbtn.addEventListener("click", function () { choose(today); });
      var hint = el("span", "dp-hint", "← → ↑ ↓ to move · Enter to pick");
      foot.append(hint, tbtn);
      pop.append(head, grid, foot);
    }

    function shiftMonth(n) {
      view = new Date(view.getFullYear(), view.getMonth() + n, 1);
      focusDay = new Date(view.getFullYear(), view.getMonth(), Math.min(focusDay.getDate(), 28));
      render();
    }

    function focusCurrent() {
      var b = pop.querySelector('.dp-day[data-date="' + iso(focusDay) + '"]');
      if (b) b.focus();
    }

    function open() {
      view = new Date(selected.getFullYear(), selected.getMonth(), 1);
      focusDay = selected;
      render();
      pop.hidden = false;
      // next frame so the CSS transition runs
      requestAnimationFrame(function () { root.classList.add("open"); focusCurrent(); });
      btn.setAttribute("aria-expanded", "true");
      document.addEventListener("mousedown", outside, true);
    }

    function close(returnFocus) {
      root.classList.remove("open");
      btn.setAttribute("aria-expanded", "false");
      document.removeEventListener("mousedown", outside, true);
      setTimeout(function () { if (!root.classList.contains("open")) pop.hidden = true; }, 160);
      if (returnFocus) btn.focus();
    }

    function outside(ev) { if (!root.contains(ev.target)) close(false); }

    btn.addEventListener("click", function () { root.classList.contains("open") ? close(true) : open(); });

    pop.addEventListener("keydown", function (ev) {
      var moves = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
      if (ev.key === "Escape") { ev.preventDefault(); close(true); return; }
      if (!ev.target.classList.contains("dp-day")) return;
      if (ev.key in moves) {
        ev.preventDefault();
        focusDay = addDays(parse(ev.target.getAttribute("data-date")), moves[ev.key]);
        if (focusDay.getMonth() !== view.getMonth() || focusDay.getFullYear() !== view.getFullYear()) {
          view = new Date(focusDay.getFullYear(), focusDay.getMonth(), 1);
          render();
        } else {
          pop.querySelectorAll(".dp-day").forEach(function (b) { b.tabIndex = -1; });
        }
        var b = pop.querySelector('.dp-day[data-date="' + iso(focusDay) + '"]');
        if (b) { b.tabIndex = 0; b.focus(); }
      } else if (ev.key === "PageUp" || ev.key === "PageDown") {
        ev.preventDefault();
        shiftMonth(ev.key === "PageUp" ? -1 : 1);
        focusCurrent();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-datepicker]").forEach(init);
  });
})();
