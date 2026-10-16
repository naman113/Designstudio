/* the side rail shared by every page. Each page includes rail.css and this file (deferred) in its head and nothing else.
   One icon per page, its name on hover and keyboard focus, the current page lit (aria-current="page"); at 700 px and narrower the rail is
   a menu button.
   It reads and writes no page state at all (no localStorage, no cookies): each page keeps its own state. */
(function () {
  "use strict";
  if (document.getElementById("rail")) return;
  var ICON = {
    studio: "M12 3 L20 7.5 V16.5 L12 21 L4 16.5 V7.5 Z M12 12 L20 7.5 M12 12 V21 M12 12 L4 7.5",
    catalogue: "M4 4 H10 V10 H4 Z M14 4 H20 V10 H14 Z M4 14 H10 V20 H4 Z M13.5 16.5 A3 3 0 1 0 19.5 16.5 A3 3 0 1 0 13.5 16.5 Z M18.7 18.7 L21 21",
    activity: "M3 12 H7 L9.5 5 L14.5 19 L17 12 H21",
    services: "M4 3 H20 V8 H4 Z M4 9.5 H20 V14.5 H4 Z M4 16 H20 V21 H4 Z M7 5.5 H8 M7 12 H8 M7 18.5 H8"
  };
  var PAGES = [
    { id: "studio", name: "Studio", href: "/", match: function (p) { return p === "/" || p === "/index.html"; } },
    { id: "catalogue", name: "Catalogue", href: "/catalogue", match: function (p) { return p === "/catalogue"; } },
    { id: "activity", name: "Activity", href: "/activity", match: function (p) { return p === "/activity"; } },
    { id: "services", name: "Services", href: "/services", match: function (p) { return p === "/services"; } }
  ];
  var NS = "http://www.w3.org/2000/svg";

  function svg(d) {
    var s = document.createElementNS(NS, "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false");
    var p = document.createElementNS(NS, "path"); p.setAttribute("d", d); s.appendChild(p);
    return s;
  }
  function here() { var p = location.pathname.replace(/\/+$/, ""); return p === "" ? "/" : p; }

  function item(pg) {
    var a = document.createElement("a");
    a.href = pg.href; a.setAttribute("aria-label", pg.name); a.dataset.page = pg.id;
    if (pg.match(here())) a.setAttribute("aria-current", "page");
    a.appendChild(svg(ICON[pg.id]));
    var nm = document.createElement("span"); nm.className = "nm"; nm.setAttribute("aria-hidden", "true"); nm.textContent = pg.name;
    a.appendChild(nm);
    return a;
  }

  function build() {
    var nav = document.createElement("nav");
    nav.id = "rail"; nav.setAttribute("aria-label", "Pages");
    PAGES.forEach(function (pg) {
      nav.appendChild(item(pg));
    });
    var btn = document.createElement("button");
    btn.id = "railBtn"; btn.type = "button"; btn.setAttribute("aria-label", "Menu"); btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-controls", "rail");
    var open = "M4 7 H20 M4 12 H20 M4 17 H20", shut = "M6 6 L18 18 M18 6 L6 18";
    btn.appendChild(svg(open));
    var scrim = document.createElement("button");
    scrim.id = "railScrim"; scrim.type = "button"; scrim.tabIndex = -1; scrim.setAttribute("aria-label", "Close the menu");

    function set(on) {
      nav.classList.toggle("open", on); scrim.classList.toggle("open", on);
      btn.setAttribute("aria-expanded", on ? "true" : "false");
      btn.setAttribute("aria-label", on ? "Close the menu" : "Menu");
      btn.firstChild.firstChild.setAttribute("d", on ? shut : open);
      if (on) { var cur = nav.querySelector("a[aria-current]") || nav.querySelector("a"); if (cur) cur.focus(); }
    }
    btn.addEventListener("click", function () { set(!nav.classList.contains("open")); });
    scrim.addEventListener("click", function () { set(false); btn.focus(); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && nav.classList.contains("open")) { set(false); btn.focus(); }
    });
    var wide = window.matchMedia("(min-width:701px)");
    var drop = function () { if (wide.matches) set(false); };
    if (wide.addEventListener) wide.addEventListener("change", drop); else if (wide.addListener) wide.addListener(drop);

    document.body.insertBefore(scrim, document.body.firstChild);
    document.body.insertBefore(nav, document.body.firstChild);
    document.body.insertBefore(btn, document.body.firstChild);
  }

  function start() { if (document.body) build(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
