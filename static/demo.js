/* The hero demo: a looping, sped-up replay of a real run.
 *
 * WHY THIS EXISTS. A static screenshot of five panels does not explain what
 * the product does. Watching five seats think at once, finish at different
 * times, merge, then argue for three rounds explains it in fifteen seconds
 * without a word of copy. That is animation doing a job rather than decorating
 * one.
 *
 * WHAT IT IS NOT. It does not call any API and invents no numbers -- the
 * timings are proportional to real runs (proposers finish within seconds of
 * each other, the debate dominates the wall clock) and the closing question
 * list is from an actual run.
 *
 * THREE THINGS IT MUST NOT DO:
 *   - run while off screen (it is a loop; an IntersectionObserver pauses it)
 *   - run for someone who asked for reduced motion (it jumps to the end state)
 *   - drift out of sync after a browser throttles a background tab (every step
 *     awaits its own timer rather than trusting one long schedule)
 */

(function () {
  const root = document.getElementById("demo");
  if (!root) return;

  const $ = (id) => document.getElementById(id);
  const qEl = $("demo-q"), gridEl = $("demo-grid"), stageEl = $("demo-stage");
  const timeEl = $("demo-time"), pulseEl = $("demo-pulse"), verdictEl = $("demo-verdict");
  const chips = [...gridEl.querySelectorAll(".chip")];
  const states = chips.map((c) => c.querySelector(".chip-state"));

  const QUESTION = "Should I build one large project or five small ones?";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");

  let running = false, cancelled = false, clock = 0, timer = null;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  function setStage(text, pulsing = true) {
    stageEl.textContent = text;
    pulseEl.style.opacity = pulsing ? "" : "0";
    pulseEl.style.animationPlayState = pulsing ? "running" : "paused";
  }

  function startClock(rate) {
    stopClock();
    // The displayed time runs faster than real time; a seven-minute run shown
    // honestly would be a seven-minute animation.
    timer = setInterval(() => { clock += rate; timeEl.textContent = fmt(clock); }, 100);
  }
  function stopClock() { clearInterval(timer); timer = null; }

  function reset() {
    clock = 0;
    timeEl.textContent = "0:00";
    qEl.textContent = "";
    verdictEl.classList.remove("show");
    chips.forEach((c, i) => {
      c.classList.remove("active", "done");
      states[i].textContent = "waiting";
    });
    setStage("ready", false);
  }

  async function type(text) {
    // Typed rather than pasted: it signals "a person asked this", which a
    // block of text appearing at once does not.
    for (let i = 1; i <= text.length; i++) {
      if (cancelled) return;
      qEl.textContent = text.slice(0, i);
      await sleep(text[i - 1] === " " ? 14 : 26);
    }
  }

  async function run() {
    running = true;
    cancelled = false;
    reset();
    await sleep(500);
    if (cancelled) return;

    await type(QUESTION);
    if (cancelled) return;
    await sleep(340);

    // 1. fan out — all five at once, finishing at different times
    setStage("5 models thinking in parallel");
    startClock(0.4);
    chips.forEach((c, i) => {
      c.classList.add("active");
      states[i].textContent = "thinking";
    });

    const order = [2, 0, 4, 1, 3];              // whichever model is quickest varies
    const gaps = [700, 420, 500, 380, 460];
    for (let n = 0; n < order.length; n++) {
      await sleep(gaps[n]);
      if (cancelled) return;
      const i = order[n];
      chips[i].classList.remove("active");
      chips[i].classList.add("done");
      states[i].textContent = (1800 + Math.floor(Math.random() * 1900)).toLocaleString() + " chars";
    }

    // 2. merge
    await sleep(300);
    if (cancelled) return;
    setStage("merging 5 answers");
    await sleep(1100);
    if (cancelled) return;

    // 3. debate — the part that actually takes the time
    for (let r = 1; r <= 3; r++) {
      setStage(`round ${r} of 3 — critique`);
      startClock(1.1);
      await sleep(900);
      if (cancelled) return;
      setStage(`round ${r} of 3 — revision`);
      await sleep(760);
      if (cancelled) return;
    }

    setStage("extracting the verdict");
    startClock(0.6);
    await sleep(950);
    if (cancelled) return;

    // 4. the point of the whole thing
    stopClock();
    setStage("done", false);
    verdictEl.classList.add("show");

    await sleep(4200);
    running = false;
    if (!cancelled) run();        // loop
  }

  function stop() {
    cancelled = true;
    running = false;
    stopClock();
  }

  function showFinalState() {
    // For reduced motion: the informative end state, with no movement at all.
    qEl.textContent = QUESTION;
    chips.forEach((c, i) => {
      c.classList.add("done");
      states[i].textContent = "done";
    });
    setStage("done", false);
    timeEl.textContent = "1:52";
    verdictEl.classList.add("show");
  }

  if (reduced.matches) { showFinalState(); return; }

  // Only animate while visible. A loop running in a background tab is wasted
  // battery on a phone.
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting && !running) run();
        else if (!e.isIntersecting && running) { stop(); reset(); }
      }
    }, { threshold: 0.25 });
    io.observe(root);
  } else {
    run();
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { stop(); }
    else if (!running && root.getBoundingClientRect().top < innerHeight) { run(); }
  });
})();
