/* Who's Here: refreshing on phones.
 *
 * - Pull down from the top of the page to reload. Browsers already do this themselves
 *   (Safari, Chrome), so ours only switches on when the board runs from a home-screen
 *   icon, where there is no browser bar or reload button.
 * - Pages marked data-live reload when you come back to them after 30+ seconds away,
 *   because phones freeze pages in the background and would otherwise show old data.
 * - Any element with data-refresh-now reloads the page when tapped.
 */
(function () {
  "use strict";
  var THRESHOLD = 70;      // px to pull before it reloads
  var MAX_PULL = 110;
  var STALE_MS = 30000;
  var loadedAt = Date.now();
  var body = document.body;

  function reload() {
    body.classList.add("ptr-refreshing");
    // a beat so the "Refreshing…" state is visible
    setTimeout(function () { window.location.reload(); }, 150);
  }

  // Tap-to-refresh buttons/links
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-refresh-now]");
    if (el) { e.preventDefault(); reload(); }
  });

  // Coming back to a live page after a while: show fresh data
  if (body.hasAttribute("data-live")) {
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible" && Date.now() - loadedAt > STALE_MS) reload();
    });
    window.addEventListener("pageshow", function (e) {
      if (e.persisted && Date.now() - loadedAt > STALE_MS) reload();  // restored from back/forward cache
    });
  }

  // Pull to refresh
  function standalone() {
    return (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
           window.navigator.standalone === true;
  }

  function enablePull() {
    if (enablePull.done) return;
    enablePull.done = true;
    document.documentElement.classList.add("ptr-on");
    var ind = document.createElement("div");
    ind.className = "ptr";
    ind.setAttribute("aria-hidden", "true");
    ind.innerHTML = '<span class="ptr-arrow">↓</span><span class="ptr-text">Pull to refresh</span>';
    body.appendChild(ind);
    var text = ind.querySelector(".ptr-text");

    var startY = null, pull = 0, active = false;

    function atTop() { return (window.scrollY || document.documentElement.scrollTop || 0) <= 0; }
    function typing() {
      var a = document.activeElement;
      return a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName);
    }
    function show(px) {
      ind.style.transform = "translateY(" + (px - 60) + "px)";
      ind.style.opacity = Math.min(1, px / THRESHOLD);
      var ready = px >= THRESHOLD;
      ind.classList.toggle("ready", ready);
      text.textContent = ready ? "Release to refresh" : "Pull to refresh";
    }
    function reset() {
      ind.style.transition = "transform .2s, opacity .2s";
      ind.style.transform = "translateY(-60px)";
      ind.style.opacity = 0;
      setTimeout(function () { ind.style.transition = ""; }, 220);
      startY = null; pull = 0; active = false;
    }

    window.addEventListener("touchstart", function (e) {
      if (e.touches.length !== 1 || !atTop() || typing()) { startY = null; return; }
      startY = e.touches[0].clientY;
      pull = 0; active = false;
    }, { passive: true });

    window.addEventListener("touchmove", function (e) {
      if (startY === null) return;
      var dy = e.touches[0].clientY - startY;
      if (dy <= 0 || !atTop()) { if (active) reset(); startY = dy <= 0 ? null : startY; return; }
      active = true;
      pull = Math.min(MAX_PULL, dy * 0.55);  // resistance, like native pull-to-refresh
      show(pull);
      if (e.cancelable) e.preventDefault();  // keep the page still while pulling
    }, { passive: false });

    window.addEventListener("touchend", function () {
      if (!active) { startY = null; return; }
      if (pull >= THRESHOLD) {
        text.textContent = "Refreshing…";
        ind.querySelector(".ptr-arrow").textContent = "↻";
        ind.classList.remove("ready");
        ind.classList.add("spinning");
        reload();
      } else {
        reset();
      }
    }, { passive: true });
  }

  if (standalone()) enablePull();
  window.WhosHere = { enablePullToRefresh: enablePull, refresh: reload };  // used by tests
})();
