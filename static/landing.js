/* Landing page behaviour. Two small jobs, no framework. */

// Border appears on the nav only once you have scrolled past the hero edge,
// so a page at rest has one less line on it.
const nav = document.getElementById("nav");
const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 8);
onScroll();
addEventListener("scroll", onScroll, { passive: true });

// Reveal sections as they arrive. IntersectionObserver rather than a scroll
// handler so it costs nothing while idle, and anything already on screen at
// load is revealed immediately instead of waiting for a scroll that may never
// come on a short screen.
const items = document.querySelectorAll(".reveal");
if (!matchMedia("(prefers-reduced-motion: reduce)").matches && "IntersectionObserver" in window) {
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
    }
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.06 });
  items.forEach((el) => io.observe(el));
} else {
  items.forEach((el) => el.classList.add("in"));
}
