/* ═══════════════════════════════════════════════════════════════════════════
   VERIS — landing page motion
   Vanilla, no dependencies, no CDN. Everything degrades to a static page if
   JS is off, and every loop stops when prefers-reduced-motion is set.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var $  = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var clamp = function (v, a, b) { return v < a ? a : v > b ? b : v; };

  /* ── 1. split display headlines into masked words ───────────────────────── */
  $$('[data-split]').forEach(function (el) {
    var words = el.textContent.trim().split(/\s+/);
    el.textContent = '';
    words.forEach(function (w, i) {
      var span = document.createElement('span');
      span.className = 'w';
      span.style.setProperty('--wi', i);
      var inner = document.createElement('i');
      inner.textContent = w;
      span.appendChild(inner);
      el.appendChild(span);
      if (i < words.length - 1) el.appendChild(document.createTextNode(' '));
    });
  });

  /* ── 2. reveal on enter ─────────────────────────────────────────────────── */
  var revealTargets = $$('.reveal, [data-split], .rung, .stat, .dv, .chain, .big');

  if (!('IntersectionObserver' in window)) {
    revealTargets.forEach(function (el) { el.classList.add('is-in'); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add('is-in');
        io.unobserve(e.target);
      });
    }, { threshold: 0.16, rootMargin: '0px 0px -8% 0px' });
    revealTargets.forEach(function (el) { io.observe(el); });
  }

  /* ── 3. count-up figures ────────────────────────────────────────────────── */
  function countUp(el) {
    var to = parseFloat(el.getAttribute('data-to'));
    var dp = parseInt(el.getAttribute('data-dp') || '0', 10);
    var dur = 1500;
    var t0 = performance.now();

    if (REDUCED) { el.textContent = to.toFixed(dp); return; }

    (function step(now) {
      var p = clamp((now - t0) / dur, 0, 1);
      var eased = 1 - Math.pow(1 - p, 4);            // quartic out
      var v = to * eased;
      el.textContent = dp ? v.toFixed(dp) : Math.round(v).toLocaleString('en-US');
      if (p < 1) requestAnimationFrame(step);
    })(t0);
  }

  var counters = $$('.count');
  if (!('IntersectionObserver' in window)) {
    counters.forEach(countUp);
  } else {
    var cio = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        countUp(e.target);
        cio.unobserve(e.target);
      });
    }, { threshold: 0.6 });
    counters.forEach(function (el) { cio.observe(el); });
  }

  /* ── 4. scroll progress + sticky nav state ──────────────────────────────── */
  var bar = $('.progress i');
  var nav = $('#nav');
  var ticking = false;

  function onScroll() {
    var doc = document.documentElement;
    var max = doc.scrollHeight - window.innerHeight;
    var p = max > 0 ? window.scrollY / max : 0;
    if (bar) bar.style.transform = 'scaleX(' + p.toFixed(4) + ')';
    if (nav) nav.classList.toggle('is-stuck', window.scrollY > 12);
    ticking = false;
  }
  window.addEventListener('scroll', function () {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(onScroll);
  }, { passive: true });
  onScroll();

  /* ── 5. pointer: cursor glow, magnetic buttons, card glow, scene tilt ───── */
  var fine = window.matchMedia('(hover: hover) and (pointer: fine)').matches;

  if (fine && !REDUCED) {
    document.body.classList.add('has-pointer');

    var glow = $('.cursor-glow');
    var gx = window.innerWidth / 2, gy = window.innerHeight / 2;
    var cx = gx, cy = gy;

    window.addEventListener('pointermove', function (e) {
      gx = e.clientX; gy = e.clientY;
    }, { passive: true });

    (function follow() {
      cx += (gx - cx) * 0.09;
      cy += (gy - cy) * 0.09;
      if (glow) glow.style.transform = 'translate3d(' + cx + 'px,' + cy + 'px,0)';
      requestAnimationFrame(follow);
    })();

    // magnetic buttons
    $$('.magnet').forEach(function (btn) {
      btn.addEventListener('pointermove', function (e) {
        var r = btn.getBoundingClientRect();
        var dx = (e.clientX - (r.left + r.width / 2)) / r.width;
        var dy = (e.clientY - (r.top + r.height / 2)) / r.height;
        btn.style.setProperty('--mx', (dx * 12).toFixed(2) + 'px');
        btn.style.setProperty('--my', (dy * 10).toFixed(2) + 'px');
      });
      btn.addEventListener('pointerleave', function () {
        btn.style.setProperty('--mx', '0px');
        btn.style.setProperty('--my', '0px');
      });
    });

    // radial glow follows cursor inside the big cards
    $$('.big').forEach(function (card) {
      card.addEventListener('pointermove', function (e) {
        var r = card.getBoundingClientRect();
        card.style.setProperty('--mx', (e.clientX - r.left) + 'px');
        card.style.setProperty('--my', (e.clientY - r.top) + 'px');
      });
    });

    // the document stack leans toward the pointer
    var scene = $('#scene');
    var stage = $('#stage');
    if (scene && stage) {
      scene.addEventListener('pointermove', function (e) {
        var r = scene.getBoundingClientRect();
        var dx = (e.clientX - (r.left + r.width / 2)) / r.width;
        var dy = (e.clientY - (r.top + r.height / 2)) / r.height;
        stage.style.setProperty('--ry', (dx * 15).toFixed(2) + 'deg');
        stage.style.setProperty('--rx', (-dy * 12).toFixed(2) + 'deg');
      });
      scene.addEventListener('pointerleave', function () {
        stage.style.setProperty('--ry', '0deg');
        stage.style.setProperty('--rx', '0deg');
      });
    }
  }

  /* ── 6. the scanner: clause lines lit in step with the sweeping beam ────── */
  (function scanner() {
    var host = $('#lines');
    if (!host) return;

    var COUNT = 15;
    var KEY = [3, 7, 8, 12];             // the "clause" lines that resolve to PROVEN
    var WIDTHS = [96, 88, 100, 72, 94, 84, 99, 66, 91, 100, 78, 95, 70, 88, 82];
    var i;

    for (i = 0; i < COUNT; i++) {
      var ln = document.createElement('span');
      ln.className = 'ln' + (KEY.indexOf(i) > -1 ? ' is-key' : '');
      ln.style.width = WIDTHS[i] + '%';
      host.appendChild(ln);
    }

    // faint filler lines on the two sheets behind, so the stack reads as paper
    $$('.sheet--b .sheet__lines, .sheet--c .sheet__lines').forEach(function (h) {
      for (var k = 0; k < 11; k++) {
        var f = document.createElement('span');
        f.className = 'ln';
        f.style.width = WIDTHS[(k * 3) % WIDTHS.length] + '%';
        h.appendChild(f);
      }
    });

    if (REDUCED) {
      $$('.ln.is-key', host).forEach(function (l) { l.classList.add('is-hit'); });
      return;
    }

    var lines = $$('.ln', host);
    var beam = $('#beam');
    var sheet = $('.sheet--a');
    var readout = $('#readoutVal');
    var labels = ['clause 4.2', 'clause 5.1(c)', 'clause 3.20', 'clause 4.13'];
    if (!beam || !sheet) return;

    var BAND = 108;         // beam height, must match .beam in the stylesheet
    var DUR = 5600;         // one full sweep
    var sheetH = 0;
    var tops = [];          // each line's trigger offset inside the sheet box

    // Geometry is measured on resize only — never inside the frame loop.
    function measure() {
      sheetH = sheet.clientHeight;
      tops = lines.map(function (l) { return l.offsetTop + l.offsetHeight * 0.5; });
    }
    measure();

    if ('ResizeObserver' in window) new ResizeObserver(measure).observe(sheet);
    else window.addEventListener('resize', measure);

    var running = true;
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (es) {
        running = es[0].isIntersecting;
      }, { threshold: 0.05 }).observe(sheet);
    }

    var lit = new Array(lines.length);
    var li = 0;
    var t0 = performance.now();

    (function tick(now) {
      requestAnimationFrame(tick);
      if (!running || !sheetH) return;

      var p = ((now - t0) % DUR) / DUR;
      var scan = -BAND + p * (sheetH + BAND);         // leading edge, sheet-relative
      var edge = scan + BAND;

      beam.style.setProperty('--scan', scan.toFixed(1));
      beam.style.setProperty('--fade', (p < 0.08 ? p / 0.08 : p > 0.92 ? (1 - p) / 0.08 : 1).toFixed(3));

      if (p < 0.02) {                                  // wrapped — clear the page
        for (var n = 0; n < lines.length; n++) {
          if (lit[n]) { lines[n].classList.remove('is-hit'); lit[n] = false; }
        }
      }

      for (var m = 0; m < lines.length; m++) {
        if (lit[m] || edge < tops[m]) continue;
        lit[m] = true;
        lines[m].classList.add('is-hit');
        if (lines[m].classList.contains('is-key') && readout) {
          readout.textContent = labels[li++ % labels.length];
        }
      }
    })(t0);
  })();

  /* ── 7. canvas lattice: a proof graph, drifting, reacting to the pointer ── */
  function lattice(canvas, opts) {
    if (!canvas || REDUCED) return;
    var ctx = canvas.getContext('2d');
    if (!ctx) return;

    var cfg = Object.assign({
      count: 46, link: 132, dot: 'rgba(79,133,116,.55)',
      line: 'rgba(111,147,171,.30)', speed: 0.16
    }, opts || {});

    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var W = 0, H = 0, nodes = [], live = true;
    var px = -9999, py = -9999;

    function resize() {
      var r = canvas.getBoundingClientRect();
      W = r.width; H = r.height;
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function seed() {
      var n = Math.round(cfg.count * clamp(W / 900, 0.45, 1.35));
      nodes = [];
      for (var i = 0; i < n; i++) {
        nodes.push({
          x: Math.random() * W,
          y: Math.random() * H,
          vx: (Math.random() - 0.5) * cfg.speed,
          vy: (Math.random() - 0.5) * cfg.speed,
          r: Math.random() * 1.5 + 0.7,
          ph: Math.random() * Math.PI * 2
        });
      }
    }

    resize(); seed();

    var ro = ('ResizeObserver' in window) ? new ResizeObserver(function () {
      resize(); seed();
    }) : null;
    if (ro) ro.observe(canvas); else window.addEventListener('resize', function () { resize(); seed(); });

    canvas.parentElement.addEventListener('pointermove', function (e) {
      var r = canvas.getBoundingClientRect();
      px = e.clientX - r.left; py = e.clientY - r.top;
    }, { passive: true });
    canvas.parentElement.addEventListener('pointerleave', function () {
      px = py = -9999;
    });

    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (es) {
        live = es[0].isIntersecting;
      }, { threshold: 0 }).observe(canvas);
    }

    var t = 0;
    (function frame() {
      requestAnimationFrame(frame);
      if (!live || !W) return;
      t += 0.006;
      ctx.clearRect(0, 0, W, H);

      var i, j, a, b, d, dx, dy;

      for (i = 0; i < nodes.length; i++) {
        a = nodes[i];
        a.x += a.vx; a.y += a.vy;
        if (a.x < 0 || a.x > W) a.vx *= -1;
        if (a.y < 0 || a.y > H) a.vy *= -1;

        // gentle pointer repulsion — the graph opens up around the cursor
        dx = a.x - px; dy = a.y - py;
        d = Math.sqrt(dx * dx + dy * dy);
        if (d < 150 && d > 0.1) {
          a.x += (dx / d) * (150 - d) * 0.018;
          a.y += (dy / d) * (150 - d) * 0.018;
        }
      }

      // links
      ctx.lineWidth = 1;
      for (i = 0; i < nodes.length; i++) {
        a = nodes[i];
        for (j = i + 1; j < nodes.length; j++) {
          b = nodes[j];
          dx = a.x - b.x; dy = a.y - b.y;
          d = Math.sqrt(dx * dx + dy * dy);
          if (d > cfg.link) continue;
          ctx.globalAlpha = (1 - d / cfg.link) * 0.75;
          ctx.strokeStyle = cfg.line;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }

      // nodes, breathing out of phase so it never looks mechanical
      for (i = 0; i < nodes.length; i++) {
        a = nodes[i];
        ctx.globalAlpha = 0.35 + 0.45 * (0.5 + 0.5 * Math.sin(t * 3 + a.ph));
        ctx.fillStyle = cfg.dot;
        ctx.beginPath();
        ctx.arc(a.x, a.y, a.r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    })();
  }

  lattice($('#lattice'), {
    count: 48, link: 134,
    dot: 'rgba(79,133,116,.5)', line: 'rgba(111,147,171,.28)'
  });
  lattice($('#lattice2'), {
    count: 40, link: 150, speed: 0.12,
    dot: 'rgba(185,213,201,.5)', line: 'rgba(185,213,201,.16)'
  });

  /* ── 7b. demo player ─────────────────────────────────────────────────────
     One box, two possible sources. If a recording resolves at landing/media/
     the <video> takes the frame and the tabs seek it. Otherwise the scripted
     DOM replay runs — same chapters, same timings, no layout shift either way. */
  (function player() {
    var frame = $('#frame');
    var replay = $('#replay');
    if (!frame || !replay) return;

    var scenes = $$('.chap', replay);
    var tabs = $$('.tab');
    var segs = $$('.track i');
    var pp = $('#pp');
    var video = $('#shot');
    var ghost = $('#ghost');
    var chLabel = $('#frameCh');

    // Chapter accent, fed into --hue on the frame so the stage light, badges and
    // the cursor's tap ring all take the colour of whatever is being shown.
    var HUES = {
      jade:  '79, 133, 116',
      field: '111, 147, 171',
      amber: '179, 128, 31',
      clay:  '168, 90, 65'
    };

    var HOLD = 1250;                 // pause after the last step of a chapter
    var STEP = 560;                  // gap between steps
    var idx = 0;                     // active chapter
    var paused = false;
    var t0 = 0;
    var seen = -1;                   // steps already revealed in this chapter
    var visible = false;
    var useVideo = false;

    function durationOf(i) {
      return $$('.st', scenes[i]).length * STEP + HOLD;
    }

    function paint(i) {
      scenes.forEach(function (s, n) { s.classList.toggle('is-on', n === i); });
      tabs.forEach(function (t, n) {
        t.classList.remove('is-on');
        t.setAttribute('aria-selected', n === i ? 'true' : 'false');
      });
      // re-adding on the next frame restarts the tab's light sweep
      requestAnimationFrame(function () { tabs[i].classList.add('is-on'); });

      frame.style.setProperty('--hue',
        HUES[scenes[i].getAttribute('data-hue')] || HUES.field);

      if (chLabel) {
        chLabel.textContent = scenes[i].getAttribute('data-title') || '';
        chLabel.classList.remove('is-swap');
        void chLabel.offsetWidth;                 // restart the swap animation
        chLabel.classList.add('is-swap');
      }

      segs.forEach(function (s, n) {
        s.style.setProperty('--p', n < i ? 1 : 0);
      });
    }

    function reset(i) {
      $$('.st', scenes[i]).forEach(function (s) { s.classList.remove('is-in'); });
      $$('[data-tally]', scenes[i]).forEach(function (el) { el.textContent = '0'; });
    }

    // Move the ghost cursor to a step's waypoint and tap it.
    function point(el) {
      if (!ghost) return;
      var cx = el.getAttribute('data-cx');
      var cy = el.getAttribute('data-cy');
      if (cx === null) return;
      ghost.classList.add('is-live');
      ghost.style.left = cx + '%';
      ghost.style.top = cy + '%';
      setTimeout(function () {
        ghost.classList.remove('is-tap');
        void ghost.offsetWidth;
        ghost.classList.add('is-tap');
      }, 380);
    }

    function go(i) {
      reset(idx);
      idx = ((i % scenes.length) + scenes.length) % scenes.length;
      reset(idx);
      seen = -1;
      t0 = performance.now();
      paint(idx);
      if (useVideo && video.duration) {
        video.currentTime = (idx / scenes.length) * video.duration;
      }
    }

    // With reduced motion every step is already revealed, so a tab must only
    // switch chapters — go() would reset them back to hidden.
    tabs.forEach(function (t, n) {
      t.addEventListener('click', function () { REDUCED ? paint(n) : go(n); });
    });

    if (pp) {
      pp.addEventListener('click', function () {
        paused = !paused;
        pp.setAttribute('aria-pressed', paused ? 'true' : 'false');
        pp.setAttribute('aria-label', paused ? 'Play demo' : 'Pause demo');
        var lbl = $('.pp__t', pp);
        if (lbl) lbl.textContent = paused ? 'Play' : 'Pause';
        if (useVideo) { paused ? video.pause() : video.play().catch(function () {}); }
        if (!paused) t0 = performance.now() - seenElapsed();
      });
    }

    var _elapsed = 0;
    function seenElapsed() { return _elapsed; }

    // Does a real recording exist? Only then does the video take over.
    if (video) {
      video.addEventListener('loadeddata', function () {
        useVideo = true;
        frame.classList.add('has-video');
        if (visible && !paused) video.play().catch(function () {});
      }, { once: true });
      video.addEventListener('error', function () { /* replay stays */ });
      // preload="none" until the section is near, then ask for it
    }

    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (es) {
        visible = es[0].isIntersecting;
        if (visible && video && video.preload === 'none') {
          video.preload = 'auto';
          video.load();
        }
        if (useVideo) {
          if (visible && !paused) video.play().catch(function () {});
          else video.pause();
        }
        if (visible && !paused) t0 = performance.now() - _elapsed;
      }, { threshold: 0.25 }).observe(frame);
    } else {
      visible = true;
    }

    if (REDUCED) {
      paint(0);
      scenes.forEach(function (s) {
        $$('.st', s).forEach(function (x) { x.classList.add('is-in'); });
        $$('[data-tally]', s).forEach(function (el) {
          el.textContent = el.getAttribute('data-tally');
        });
      });
      segs.forEach(function (s) { s.style.setProperty('--p', 1); });
      return;
    }

    go(0);

    (function loop(now) {
      requestAnimationFrame(loop);
      if (!visible || paused) { t0 = now - _elapsed; return; }

      var dur = durationOf(idx);
      _elapsed = now - t0;
      var p = clamp(_elapsed / dur, 0, 1);
      if (segs[idx]) segs[idx].style.setProperty('--p', p.toFixed(4));

      var steps = $$('.st', scenes[idx]);
      var want = Math.min(steps.length - 1, Math.floor(_elapsed / STEP));
      while (seen < want) {
        seen++;
        steps[seen].classList.add('is-in');
        // a step may carry several figures (the accounting bar carries three)
        $$('[data-tally]', steps[seen]).forEach(tallyUp);
        point(steps[seen]);
      }

      if (p >= 1) { _elapsed = 0; go(idx + 1); }
    })(performance.now());

    function tallyUp(el) {
      var to = parseFloat(el.getAttribute('data-tally'));
      var s = performance.now();
      (function s2(now) {
        var q = clamp((now - s) / 900, 0, 1);
        el.textContent = Math.round(to * (1 - Math.pow(1 - q, 3)));
        if (q < 1) requestAnimationFrame(s2);
      })(s);
    }
  })();

  /* ── 8. marquee ──────────────────────────────────────────────────────────
     Driven by rAF rather than a keyframe. Two reasons:
       · changing animation-duration on a running CSS animation re-maps its
         elapsed time onto the new duration, so the row visibly jumps. An
         accumulated offset can change speed without moving at all.
       · translateX(-50%) is not a seamless loop when items are separated by a
         gap — half the total width is one cycle minus half a gap, so it snapped
         every lap. The true cycle is measured from the DOM instead.                      */
  (function marquee() {
    var rows = $$('.marquee__row');
    if (!rows.length || REDUCED) return;

    var tracks = rows.map(function (row) {
      var base = Array.prototype.slice.call(row.children);
      return {
        row: row,
        base: base,
        dir: parseFloat(row.getAttribute('data-dir')) || 1,
        speed: parseFloat(row.getAttribute('data-speed')) || 40,  // px per second
        cycle: 0,
        off: 0
      };
    });

    // Repeat the base set until one cycle covers the viewport, then lay down a
    // second cycle behind it. The cycle width is the offsetLeft of the element
    // that begins that second cycle — exact, whatever the gaps are.
    function build(t) {
      var row = t.row;
      row.style.transform = 'translate3d(0,0,0)';

      // reset to a single copy
      while (row.children.length > t.base.length) row.removeChild(row.lastChild);

      var need = t.row.parentElement.getBoundingClientRect().width || window.innerWidth;
      var one = row.getBoundingClientRect().width;
      if (one < 1) { t.cycle = 0; return; }

      var reps = Math.max(1, Math.ceil(need / one));
      var frag = document.createDocumentFragment();
      var i, j;
      for (i = 1; i < reps; i++) {
        for (j = 0; j < t.base.length; j++) frag.appendChild(t.base[j].cloneNode(true));
      }
      row.appendChild(frag);

      var perCycle = row.children.length;          // elements in one cycle
      frag = document.createDocumentFragment();
      for (i = 0; i < perCycle; i++) frag.appendChild(row.children[i].cloneNode(true));
      row.appendChild(frag);

      t.cycle = row.children[perCycle].offsetLeft;
      t.off = t.off % (t.cycle || 1);
    }

    tracks.forEach(build);

    var rebuild;
    window.addEventListener('resize', function () {
      clearTimeout(rebuild);
      rebuild = setTimeout(function () { tracks.forEach(build); }, 180);
    });

    // Scroll velocity feeds a target multiplier; the actual multiplier eases
    // toward it inside the frame loop, so there is no step change in position.
    var boost = 1, boostTarget = 1, lastY = window.scrollY;

    window.addEventListener('scroll', function () {
      var dy = window.scrollY - lastY;
      lastY = window.scrollY;
      boostTarget = clamp(1 + Math.abs(dy) / 26, 1, 3.2);
    }, { passive: true });

    var visible = true;
    var strip = $('.strip');
    if ('IntersectionObserver' in window && strip) {
      new IntersectionObserver(function (es) {
        visible = es[0].isIntersecting;
      }, { threshold: 0 }).observe(strip);
    }

    var prev = performance.now();
    (function frame(now) {
      requestAnimationFrame(frame);

      var dt = (now - prev) / 1000;
      prev = now;
      if (dt > 0.1) dt = 0.1;                    // tab was backgrounded — do not lurch

      boostTarget += (1 - boostTarget) * Math.min(1, dt * 3.2);   // decay to rest
      boost += (boostTarget - boost) * Math.min(1, dt * 8);

      if (!visible) return;

      for (var i = 0; i < tracks.length; i++) {
        var t = tracks[i];
        if (!t.cycle) continue;
        t.off = (t.off + t.speed * boost * dt) % t.cycle;
        var x = t.dir > 0 ? -t.off : t.off - t.cycle;
        t.row.style.transform = 'translate3d(' + x.toFixed(2) + 'px,0,0)';
      }
    })(prev);
  })();

})();
