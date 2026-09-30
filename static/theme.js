/* Theme switching, shared by the landing page and the app.
 *
 * WHY IT FELT BROKEN BEFORE. Dozens of elements each carry their own
 * `transition: background .2s` so that hovering a control feels responsive.
 * Flip a theme and all of them ease at once, slightly out of step, and the
 * page appears to hang for a beat and then snap. The fix is to turn every one
 * of those off for the duration and let a single animation carry the change.
 *
 * THE ANIMATION is a circle opening from the button you pressed, via the View
 * Transitions API: the browser snapshots the old page, we swap the theme, and
 * the new page is revealed through an expanding clip-path. One movement, one
 * timing, starting where your finger was.
 *
 * WHERE IT IS NOT SUPPORTED the theme simply changes, instantly and with the
 * transitions still suppressed. That is the same end state, just without the
 * wipe -- never a broken page.
 */
(function () {
  var root = document.documentElement;
  var reduced = matchMedia("(prefers-reduced-motion: reduce)");

  function apply(next) {
    root.setAttribute("data-theme", next);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", next === "light" ? "#EFEEE8" : "#0C0C0D");
    try { localStorage.setItem("unstuck-theme", next); } catch (e) { /* private mode */ }
  }

  function toggle(event) {
    var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";

    // Suppress every per-element colour transition while the theme changes.
    root.setAttribute("data-switching", "");
    function release() { root.removeAttribute("data-switching"); }

    if (!document.startViewTransition || reduced.matches) {
      apply(next);
      requestAnimationFrame(release);
      return;
    }

    // Start the circle where the press landed, and make it big enough to reach
    // the furthest corner -- otherwise it stops short and leaves a ring of the
    // old theme in the far corner of a wide screen.
    var x = (event && event.clientX) || window.innerWidth - 60;
    var y = (event && event.clientY) || 40;
    var radius = Math.hypot(
      Math.max(x, window.innerWidth - x),
      Math.max(y, window.innerHeight - y)
    );

    var transition = document.startViewTransition(function () { apply(next); });

    transition.ready.then(function () {
      root.animate(
        {
          clipPath: [
            "circle(0px at " + x + "px " + y + "px)",
            "circle(" + radius + "px at " + x + "px " + y + "px)"
          ]
        },
        {
          duration: 620,
          easing: "cubic-bezier(.22, 1, .36, 1)",
          pseudoElement: "::view-transition-new(root)"
        }
      );
    }).catch(release);

    transition.finished.then(release, release);
  }

  function wire() {
    var btn = document.getElementById("theme-toggle");
    if (btn && !btn.dataset.wired) {
      btn.dataset.wired = "1";
      btn.addEventListener("click", toggle);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
