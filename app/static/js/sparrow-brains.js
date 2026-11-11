/* Sparrow brains — a 3D map of what Sparrow knows, one subject at a time.
 *
 * window.SparrowBrains.mount(el, opts) builds the map inside `el`:
 *   overview (every brain as a hub with its section nodes) -> brain focused
 *   (fly-to, leaves and section labels, card) -> section selected (its edges
 *   lit, its items listed). Positions come from the server; nothing here runs
 *   physics, so every device shows the same map and a new fact never moves
 *   what was already there.
 *
 * Rendering is Three.js (vendored, /static/js/vendor) through one
 * InstancedMesh of camera-facing discs whose shape (filled / ring / dashed)
 * comes from a per-instance attribute, plus one LineSegments buffer. If
 * WebGL is unavailable the same scene draws on a 2D canvas through the same
 * camera math, so the card and every action still work.
 *
 * opts: { brain, section, threeUrl, onFocus(brain), onSection(key) }
 */
(function () {
  'use strict';

  const SECTION_COLORS = {
    thesis: '#8d85f2', overview: '#8d85f2', performance: '#2ec4b6',
    people: '#5bc0eb', role: '#5bc0eb', relationships: '#7fb3ff',
    status: '#f4a261', commitments: '#2ec4b6', risks: '#e76f51',
    competitors: '#c77dff', decisions: '#90be6d', notes: '#bfae8a',
    open_questions: '#9298a3',
  };
  let HUB_COLOR = '#d9dcf0';
  let HUB_FOCUS = '#ffffff';
  let BG_RGB = [0.086, 0.098, 0.125];     // the surface the lines fade into
  function readTheme() {
    try {
      const cs = getComputedStyle(document.documentElement);
      const text = (cs.getPropertyValue('--text') || '').trim();
      const acc = (cs.getPropertyValue('--acc') || cs.getPropertyValue('--navy') || '').trim();
      const bg = (cs.getPropertyValue('--surface') || '').trim();
      if (/^#[0-9a-f]{6}$/i.test(text)) HUB_COLOR = text;
      if (/^#[0-9a-f]{6}$/i.test(acc)) HUB_FOCUS = acc;
      if (/^#[0-9a-f]{6}$/i.test(bg)) BG_RGB = hexRgb(bg);
    } catch (e) { /* keep defaults */ }
  }
  const STORE_KEY = 'sparrow.brains.v1';
  const THREE_URL = '/static/js/vendor/three.module.min.js';
  const FLY_MS = 600;
  const FADE_MS = 300;
  const LABEL_CAP = 40;
  const DRAG_PX = 4;
  const OVERVIEW_DIST = 24;
  const FOCUS_DIST = 7.5;
  const DIST_MIN = 2.2, DIST_MAX = 70;
  const PITCH_MAX = 70 * Math.PI / 180;      // from the horizon
  const LOCAL_OVERVIEW = 0.45;               // brain-local units -> world, overview
  const LOCAL_FOCUS = 1.6;                   // when focused
  const CACHE_MAX = 20;
  const POLL_MS = 5000;

  const reducedMotion = () =>
    window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const ease = (t) => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  const finite = (v) => typeof v === 'number' && Number.isFinite(v);
  const colorOf = (key) => SECTION_COLORS[key] || '#9fa3b8';
  function hexRgb(hex) {
    const n = parseInt(hex.slice(1), 16);
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  }
  function when(ts) {
    if (!ts) return '';
    try {
      const d = new Date(ts * 1000);
      return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    } catch (e) { return ''; }
  }
  function csrfHeaders() {
    const m = document.cookie.match(/(?:^|;\s*)quill_csrf=([^;]+)/);
    const h = { 'Content-Type': 'application/json' };
    if (m) h['X-CSRF-Token'] = decodeURIComponent(m[1]);
    return h;
  }
  async function getJson(url) {
    const r = await fetch(url, { credentials: 'same-origin' });
    if (!r.ok) throw new Error(url + ' -> ' + r.status);
    return r.json();
  }
  async function postJson(url, body) {
    const r = await fetch(url, { method: 'POST', credentials: 'same-origin',
      headers: csrfHeaders(), body: JSON.stringify(body || {}) });
    let j = null;
    try { j = await r.json(); } catch (e) { j = null; }
    if (!r.ok) {
      const err = new Error((j && (j.detail || j.reason)) || (url + ' -> ' + r.status));
      err.status = r.status; err.body = j;
      throw err;
    }
    return j;
  }

  /* ------------------------------------------------------------ camera -- */
  // One camera for both renderers: orbit (theta around Y, phi from +Y), a
  // distance, and a target. project() is the manual perspective the 2D
  // fallback and the HTML labels use; Three.js gets the same eye/target.
  function makeCamera() {
    return { theta: 0.9, phi: 1.25, dist: OVERVIEW_DIST, target: [0, 0, 0], fov: 50 };
  }
  function eyeOf(cam) {
    const s = Math.sin(cam.phi);
    return [cam.target[0] + cam.dist * s * Math.cos(cam.theta),
      cam.target[1] + cam.dist * Math.cos(cam.phi),
      cam.target[2] + cam.dist * s * Math.sin(cam.theta)];
  }
  function basisOf(cam) {
    const eye = eyeOf(cam);
    const f = norm3(sub3(cam.target, eye));
    let r = cross3(f, [0, 1, 0]);
    if (len3(r) < 1e-6) r = [1, 0, 0];
    r = norm3(r);
    const u = cross3(r, f);
    return { eye, f, r, u };
  }
  function sub3(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
  function cross3(a, b) {
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  }
  function dot3(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
  function len3(a) { return Math.sqrt(dot3(a, a)); }
  function norm3(a) { const l = len3(a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; }
  function projector(cam, w, h) {
    const b = basisOf(cam);
    const focal = (h / 2) / Math.tan((cam.fov * Math.PI / 180) / 2);
    return function (p) {
      const d = sub3(p, b.eye);
      const z = dot3(d, b.f);
      if (z <= 0.05) return null;
      const x = dot3(d, b.r), y = dot3(d, b.u);
      return { x: w / 2 + x * focal / z, y: h / 2 - y * focal / z, z, scale: focal / z };
    };
  }

  /* --------------------------------------------------------- renderers -- */
  // Scene = { nodes: [{id, pos, size, color, opacity, style, kind, label}],
  //           edges: [{a, b, color, opacity}] }  style: 0 filled, 1 ring, 2 dashed
  function makeCanvas2D(host) {
    const canvas = document.createElement('canvas');
    canvas.className = 'sb-canvas';
    canvas.setAttribute('aria-hidden', 'true');
    host.appendChild(canvas);
    const ctx = canvas.getContext('2d');
    let scene = { nodes: [], edges: [] };
    let w = 1, h = 1, dpr = 1;
    function resize() {
      dpr = Math.min(2, window.devicePixelRatio || 1);
      w = host.clientWidth || 1; h = host.clientHeight || 1;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
    }
    resize();
    return {
      kind: '2d', canvas, resize,
      setScene(s) { scene = s; },
      render(cam, depth) {
        const pr = projector(cam, w, h);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);
        ctx.lineWidth = 1;
        for (const e of scene.edges) {
          const a = pr(e.a), b = pr(e.b);
          if (!a || !b) continue;
          const cue = Math.min(depthCue(a.z, depth), depthCue(b.z, depth));
          ctx.strokeStyle = e.color; ctx.globalAlpha = e.opacity * cue;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
        const pts = [];
        for (const n of scene.nodes) {
          const p = pr(n.pos);
          if (!p) continue;
          pts.push([p.z, n, p]);
        }
        pts.sort((a, b) => b[0] - a[0]);
        for (const [, n, p] of pts) {
          const cue = depthCue(p.z, depth);
          const r = Math.max(1.5, 0.5 * n.size * p.scale * (0.6 + 0.4 * cue));
          ctx.globalAlpha = n.opacity * cue;
          ctx.beginPath();
          if (n.style === 2) ctx.setLineDash([Math.max(2, r * 0.6), Math.max(2, r * 0.45)]);
          else ctx.setLineDash([]);
          ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
          if (n.style === 0) { ctx.fillStyle = n.color; ctx.fill(); }
          else { ctx.strokeStyle = n.color; ctx.lineWidth = Math.max(1, r * 0.22); ctx.stroke(); }
        }
        ctx.setLineDash([]); ctx.globalAlpha = 1;
      },
      dispose() { canvas.remove(); },
    };
  }
  function depthCue(z, depth) {
    // scale and opacity fall with distance from the camera; far fades to 25%
    if (!depth) return 1;
    return clamp(1 - (z - depth.near) / (depth.far - depth.near), 0.25, 1);
  }

  function makeThreeRenderer(host, THREE) {
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true,
      powerPreference: 'low-power' });
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    renderer.domElement.className = 'sb-canvas';
    renderer.domElement.setAttribute('aria-hidden', 'true');
    host.appendChild(renderer.domElement);
    const scene3 = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(50, 1, 0.05, 400);
    let w = 1, h = 1;
    function resize() {
      w = host.clientWidth || 1; h = host.clientHeight || 1;
      renderer.setSize(w, h, false);
      camera.aspect = w / h; camera.updateProjectionMatrix();
    }
    resize();

    const vert = `
      attribute vec3 aColor; attribute float aScale; attribute float aOpacity; attribute float aStyle;
      varying vec2 vUv; varying vec3 vColor; varying float vOpacity; varying float vStyle;
      uniform float uNear; uniform float uFar;
      void main() {
        vUv = uv; vColor = aColor; vStyle = aStyle;
        vec4 mv = modelViewMatrix * instanceMatrix * vec4(0.0, 0.0, 0.0, 1.0);
        float dist = -mv.z;
        float cue = clamp(1.0 - (dist - uNear) / (uFar - uNear), 0.25, 1.0);
        vOpacity = aOpacity * cue;
        mv.xy += position.xy * aScale * (0.6 + 0.4 * cue);
        gl_Position = projectionMatrix * mv;
      }`;
    const frag = `
      precision mediump float;
      varying vec2 vUv; varying vec3 vColor; varying float vOpacity; varying float vStyle;
      void main() {
        vec2 c = vUv - 0.5; float d = length(c) * 2.0;
        float edge = 1.0 - smoothstep(0.86, 1.0, d);
        float a = edge;
        if (vStyle > 0.5) {
          float inner = smoothstep(0.58, 0.70, d);
          a = edge * inner;
          if (vStyle > 1.5) {
            float ang = atan(c.y, c.x) / 6.2831853 + 0.5;
            if (fract(ang * 9.0) > 0.62) a = 0.0;
          }
        }
        a *= vOpacity;
        if (a < 0.01) discard;
        gl_FragColor = vec4(vColor, a);
      }`;
    const mat = new THREE.ShaderMaterial({ vertexShader: vert, fragmentShader: frag,
      transparent: true, depthWrite: false, depthTest: true,
      uniforms: { uNear: { value: 4 }, uFar: { value: 40 } } });
    let mesh = null;
    let lines = null;
    const lineMat = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true,
      opacity: 1, depthWrite: false });
    const geo = new THREE.PlaneGeometry(1, 1);
    const tmp = new THREE.Object3D();

    function setScene(s) {
      if (mesh) { scene3.remove(mesh); mesh.geometry.dispose(); mesh = null; }
      if (lines) { scene3.remove(lines); lines.geometry.dispose(); lines = null; }
      const n = s.nodes.length;
      if (n) {
        const ig = geo.clone();
        mesh = new THREE.InstancedMesh(ig, mat, n);
        const col = new Float32Array(n * 3), sc = new Float32Array(n),
          op = new Float32Array(n), st = new Float32Array(n);
        s.nodes.forEach((nd, i) => {
          tmp.position.set(nd.pos[0], nd.pos[1], nd.pos[2]);
          tmp.updateMatrix(); mesh.setMatrixAt(i, tmp.matrix);
          const c = hexRgb(nd.color); col[i * 3] = c[0]; col[i * 3 + 1] = c[1]; col[i * 3 + 2] = c[2];
          sc[i] = nd.size; op[i] = nd.opacity; st[i] = nd.style;
        });
        ig.setAttribute('aColor', new THREE.InstancedBufferAttribute(col, 3));
        ig.setAttribute('aScale', new THREE.InstancedBufferAttribute(sc, 1));
        ig.setAttribute('aOpacity', new THREE.InstancedBufferAttribute(op, 1));
        ig.setAttribute('aStyle', new THREE.InstancedBufferAttribute(st, 1));
        mesh.instanceMatrix.needsUpdate = true;
        mesh.frustumCulled = false;
        scene3.add(mesh);
      }
      if (s.edges.length) {
        const pos = new Float32Array(s.edges.length * 6), col = new Float32Array(s.edges.length * 6);
        s.edges.forEach((e, i) => {
          pos.set(e.a, i * 6); pos.set(e.b, i * 6 + 3);
          const c = hexRgb(e.color).map((v, k) => v * e.opacity + BG_RGB[k] * (1 - e.opacity));
          col.set(c, i * 6); col.set(c, i * 6 + 3);
        });
        const lg = new THREE.BufferGeometry();
        lg.setAttribute('position', new THREE.BufferAttribute(pos, 3));
        lg.setAttribute('color', new THREE.BufferAttribute(col, 3));
        lines = new THREE.LineSegments(lg, lineMat);
        lines.frustumCulled = false;
        scene3.add(lines);
      }
    }
    function render(cam, depth) {
      const eye = eyeOf(cam);
      camera.position.set(eye[0], eye[1], eye[2]);
      camera.up.set(0, 1, 0);
      camera.lookAt(cam.target[0], cam.target[1], cam.target[2]);
      mat.uniforms.uNear.value = depth ? depth.near : 4;
      mat.uniforms.uFar.value = depth ? depth.far : 40;
      renderer.render(scene3, camera);
    }
    return { kind: 'webgl', canvas: renderer.domElement, resize, setScene, render,
      dispose() { renderer.dispose(); renderer.domElement.remove(); } };
  }

  function webglAvailable() {
    try {
      const c = document.createElement('canvas');
      return !!(window.WebGLRenderingContext && (c.getContext('webgl2') || c.getContext('webgl')));
    } catch (e) { return false; }
  }
  async function loadThree(url) {
    if (window.THREE && window.THREE.WebGLRenderer) return window.THREE;
    const mod = await import(url || THREE_URL);
    window.THREE = mod;
    return mod;
  }

  /* ---------------------------------------------------------------- css -- */
  const CSS = `
.sb-root{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:24px;align-items:start;color:var(--text,#e8e9ef)}
@media(max-width:900px){.sb-root{grid-template-columns:1fr}}
.sb-stage{position:relative;height:clamp(360px,62vh,720px);border-radius:var(--radius-lg,14px);overflow:hidden;outline:none;
  background:radial-gradient(ellipse at 50% 40%,color-mix(in srgb,var(--navy,#8d85f2) 10%,var(--surface,#161920)),var(--surface,#161920) 70%);
  border:1px solid var(--line,#2a2e3a);box-shadow:var(--lift-2,0 8px 24px rgba(0,0,0,.35));touch-action:none;user-select:none}
.sb-stage:focus-visible{box-shadow:0 0 0 2px var(--acc,#8d85f2)}
.sb-canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
.sb-labels{position:absolute;inset:0;pointer-events:none;overflow:hidden}
.sb-label{position:absolute;left:0;top:0;white-space:nowrap;font:500 12px/1.3 var(--font,system-ui);color:var(--text,#e8e9ef);
  text-shadow:0 1px 2px rgba(0,0,0,.8);max-width:220px;overflow:hidden;text-overflow:ellipsis}
.sb-label-section{font-size:11px;letter-spacing:.02em;text-transform:uppercase;color:var(--mut,#9298a3)}
.sb-label-leaf{font-weight:400;color:var(--mut,#9298a3)}
.sb-toolbar{position:absolute;left:12px;top:12px;right:12px;display:flex;gap:6px;align-items:center;z-index:var(--z-raised,5)}
.sb-sp{flex:1}
.sb-btn{min-height:44px;min-width:44px;padding:0 12px;border:1px solid var(--line,#2a2e3a);border-radius:var(--r-sm,8px);
  background:color-mix(in srgb,var(--surface,#161920) 82%,transparent);color:var(--text,#e8e9ef);font:inherit;font-size:13px;cursor:pointer}
.sb-btn:hover{border-color:var(--line-strong,#3a3f4e)}
.sb-btn[aria-pressed="true"]{background:var(--acc-12,rgba(141,133,242,.18));border-color:var(--acc,#8d85f2)}
.sb-btn:disabled{opacity:.45;cursor:default}
.sb-btn.sb-primary{background:var(--acc,#8d85f2);border-color:var(--acc,#8d85f2);color:#fff}
.sb-icon{padding:0;font-size:16px}
.sb-hint{position:absolute;left:14px;bottom:10px;font-size:11.5px;color:var(--faint,#6c7180);pointer-events:none}
.sb-msg{position:absolute;left:12px;right:12px;top:66px;padding:10px 12px;border-radius:8px;background:color-mix(in srgb,var(--surface,#161920) 90%,transparent);
  border:1px solid var(--line,#2a2e3a);font-size:13px;z-index:var(--z-raised,5)}
.sb-msg a{color:var(--acc,#8d85f2)}
.sb-card{background:linear-gradient(180deg,color-mix(in srgb,var(--navy,#8d85f2) 2.5%,var(--surface,#161920)),var(--surface,#161920));
  border:1px solid var(--line,#2a2e3a);border-radius:var(--radius-xl,18px);padding:20px 22px;box-shadow:var(--lift-2,0 8px 24px rgba(0,0,0,.35));
  max-height:clamp(360px,62vh,720px);overflow:auto;position:relative}
@media(max-width:720px){.sb-card{position:fixed;left:0;right:0;bottom:0;max-height:55vh;border-radius:18px 18px 0 0;z-index:var(--z-float,40);padding-bottom:calc(16px + env(safe-area-inset-bottom))}}
.sb-card-empty p{margin:0 0 10px;font-size:13.5px;color:var(--mut,#9298a3)}
.sb-card-head h2{margin:2px 0 10px;font:600 20px/1.2 var(--font-display,var(--font,system-ui))}
.sb-kicker{font-size:12px;color:var(--mut,#9298a3);text-transform:capitalize}
.sb-skel{height:12px;border-radius:6px;background:var(--ink-08,rgba(255,255,255,.08));margin:8px 0;animation:sbPulse 1.2s ease-in-out infinite}
.sb-skel.w60{width:60%}
@keyframes sbPulse{50%{opacity:.45}}
.sb-chips{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 12px}
.sb-chip{display:inline-flex;align-items:center;gap:6px;min-height:32px;padding:0 10px;border-radius:999px;border:1px solid var(--line,#2a2e3a);
  background:transparent;color:var(--text,#e8e9ef);font:inherit;font-size:12.5px;cursor:pointer}
.sb-chip i{width:9px;height:9px;border-radius:50%;background:var(--c);display:inline-block}
.sb-chip span{color:var(--mut,#9298a3);font-size:11.5px}
.sb-chip.on{background:var(--c);border-color:var(--c);color:#101216}
.sb-chip.on span{color:#101216}
.sb-chip.on i{background:#101216}
.sb-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 8px}
.sb-toggle{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:var(--mut,#9298a3);min-height:44px}
.sb-withheld{font-size:12px;color:var(--mut,#9298a3);margin:0 0 8px}
.sb-fallback{width:100%;min-height:140px;font:12px/1.4 ui-monospace,monospace;background:var(--ink-08,rgba(255,255,255,.06));color:var(--text,#e8e9ef);
  border:1px solid var(--line,#2a2e3a);border-radius:8px;padding:8px;margin:0 0 10px;box-sizing:border-box}
.sb-sec h3{display:flex;align-items:center;gap:8px;margin:14px 0 6px;font:600 12px/1.2 var(--font,system-ui);letter-spacing:.04em;text-transform:uppercase;color:var(--mut,#9298a3)}
.sb-sec h3 i{width:8px;height:8px;border-radius:50%;background:var(--c)}
.sb-item{position:relative;display:grid;grid-template-columns:14px minmax(0,1fr) 32px;gap:4px 8px;align-items:start;padding:6px 0;border-top:1px solid var(--line,#2a2e3a)}
.sb-item:first-of-type{border-top:0}
.sb-dot{width:10px;height:10px;border-radius:50%;margin-top:5px;border:2px solid var(--c);box-sizing:border-box}
.sb-dot.filled{background:var(--c)}
.sb-dot.dashed{border-style:dashed}
.sb-item-text{text-align:left;background:none;border:0;padding:2px 0;color:var(--text,#e8e9ef);font:inherit;font-size:13.5px;line-height:1.35;cursor:pointer;min-height:24px}
.sb-item-text:hover{color:var(--acc,#8d85f2)}
.sb-item-meta{grid-column:2;font-size:11.5px;color:var(--faint,#6c7180)}
.sb-more{width:32px;height:32px;border:0;background:none;color:var(--mut,#9298a3);font-size:16px;cursor:pointer;border-radius:6px;justify-self:end}
.sb-more:hover{background:var(--ink-08,rgba(255,255,255,.08));color:var(--text,#e8e9ef)}
.sb-menu{grid-column:1/-1;display:flex;flex-direction:column;gap:2px;padding:6px;border:1px solid var(--line,#2a2e3a);border-radius:8px;background:var(--raised,#1d2029);margin-top:4px}
.sb-menu button{display:flex;align-items:center;gap:8px;min-height:36px;padding:0 10px;border:0;background:none;color:var(--text,#e8e9ef);font:inherit;font-size:13px;text-align:left;cursor:pointer;border-radius:6px}
.sb-menu button:hover,.sb-menu button:focus-visible{background:var(--ink-08,rgba(255,255,255,.08))}
.sb-menu button i{width:8px;height:8px;border-radius:50%;background:var(--c,#9fa3b8)}
.sb-empty{font-size:12.5px;color:var(--faint,#6c7180);padding:4px 0}
.sb-evidence{margin-top:14px;padding:10px 12px;border:1px solid var(--line,#2a2e3a);border-radius:10px;font-size:13px}
.sb-ev-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.sb-evidence ul{margin:6px 0;padding-left:18px}
.sb-evidence a{color:var(--acc,#8d85f2)}
.sb-mut{color:var(--mut,#9298a3);font-size:12px}
.sb-list{grid-column:1;overflow:auto;max-height:clamp(360px,62vh,720px);border:1px solid var(--line,#2a2e3a);border-radius:var(--radius-lg,14px)}
.sb-table{width:100%;border-collapse:collapse;font-size:13px}
.sb-table th{position:sticky;top:0;background:var(--surface,#161920);text-align:left;padding:8px 10px;border-bottom:1px solid var(--line,#2a2e3a)}
.sb-table th button{background:none;border:0;color:var(--mut,#9298a3);font:inherit;font-size:12px;text-transform:uppercase;letter-spacing:.04em;cursor:pointer;min-height:32px}
.sb-table td{padding:8px 10px;border-bottom:1px solid var(--line,#2a2e3a)}
.sb-table tr.on td{background:var(--acc-12,rgba(141,133,242,.14))}
.sb-link{background:none;border:0;color:var(--text,#e8e9ef);font:inherit;text-align:left;cursor:pointer;min-height:32px;padding:0}
.sb-link:hover{color:var(--acc,#8d85f2)}
.sb-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
@media(prefers-reduced-motion:reduce){.sb-skel{animation:none}}
@media(pointer:coarse){
  .sb-chip{min-height:44px;padding:0 14px}
  .sb-more{width:44px;height:44px}
  .sb-item-text{min-height:44px;display:flex;align-items:center}
  .sb-table th button,.sb-link,.sb-menu button{min-height:44px}
  .sb-toggle input{width:22px;height:22px}
}
`;
  function injectCss() {
    if (document.getElementById('sb-css')) return;
    const st = document.createElement('style');
    st.id = 'sb-css'; st.textContent = CSS;
    document.head.appendChild(st);
  }

  /* -------------------------------------------------------------- mount -- */
  function mount(el, opts) {
    opts = opts || {};
    injectCss();
    readTheme();
    el.classList.add('sb-root');
    el.innerHTML = `
      <div class="sb-stage" tabindex="0" aria-label="Brain map" role="application" aria-describedby="sb-stage-help">
        <span id="sb-stage-help" class="sb-sr">Arrow keys orbit, plus and minus zoom, Escape steps back. Every brain and action is also in the list view and the card.</span>
        <div class="sb-labels" aria-hidden="true"></div>
        <div class="sb-toolbar">
          <button type="button" class="sb-btn" data-act="all" title="All brains (Esc)">All brains</button>
          <button type="button" class="sb-btn" data-act="list" aria-pressed="false">List</button>
          <span class="sb-sp"></span>
          <button type="button" class="sb-btn sb-icon" data-act="zoom-in" title="Zoom in (+)">+</button>
          <button type="button" class="sb-btn sb-icon" data-act="zoom-out" title="Zoom out (-)">−</button>
          <button type="button" class="sb-btn sb-icon" data-act="reset" title="Reset view">⟲</button>
          <button type="button" class="sb-btn sb-icon" data-act="rotate" title="Auto-rotate" aria-pressed="false">↻</button>
        </div>
        <div class="sb-msg" role="status" aria-live="polite" hidden></div>
        <div class="sb-hint">Drag to orbit · wheel to zoom · click a brain</div>
      </div>
      <div class="sb-list" hidden></div>
      <aside class="sb-card" aria-live="polite" aria-label="Brain detail"></aside>`;
    const stage = el.querySelector('.sb-stage');
    const labelsEl = el.querySelector('.sb-labels');
    const msgEl = el.querySelector('.sb-msg');
    const listEl = el.querySelector('.sb-list');
    const cardEl = el.querySelector('.sb-card');

    const state = {
      brains: [], byId: new Map(), version: null,
      focused: null, section: null, detail: null, cache: new Map(),
      cam: makeCamera(), anim: null, rotate: false, listMode: false,
      includePending: false, hover: null, expand: new Map(), births: new Map(),
      renderer: null, labels: new Map(), loaded: false, destroyed: false,
      scene: { nodes: [], edges: [] }, projected: [], pollTimer: null,
      sort: { key: 'activity', dir: -1 }, lastCopy: null, evidence: null,
    };

    // --- persistence -----------------------------------------------------
    function loadPersisted() {
      try {
        const raw = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
        if (!raw || typeof raw !== 'object') return null;
        const c = raw.cam || {};
        const ok = ['theta', 'phi', 'dist'].every((k) => finite(c[k])) &&
          Array.isArray(c.target) && c.target.length === 3 && c.target.every(finite);
        return { cam: ok ? c : null, focused: typeof raw.focused === 'string' ? raw.focused : null,
          section: typeof raw.section === 'string' ? raw.section : null,
          includePending: !!raw.includePending };
      } catch (e) { return null; }
    }
    function persist() {
      try {
        localStorage.setItem(STORE_KEY, JSON.stringify({
          cam: { theta: state.cam.theta, phi: state.cam.phi, dist: state.cam.dist,
            target: state.cam.target.slice() },
          focused: state.focused, section: state.section,
          includePending: state.includePending }));
      } catch (e) { /* private window */ }
    }
    function syncUrl() {
      try {
        const u = new URL(location.href);
        u.searchParams.set('mode', 'brains');
        if (state.focused) u.searchParams.set('brain', state.focused); else u.searchParams.delete('brain');
        if (state.section) u.searchParams.set('section', state.section); else u.searchParams.delete('section');
        history.replaceState(null, '', u.pathname + u.search + u.hash);
      } catch (e) { /* ignore */ }
    }

    // --- rendering ----------------------------------------------------------
    let frame = null, continuous = false;
    function requestRender() {
      if (frame || state.destroyed) return;
      frame = requestAnimationFrame(tick);
    }
    function tick(now) {
      frame = null;
      let again = false;
      if (state.anim) {
        const a = state.anim;
        const t = clamp((now - a.t0) / a.ms, 0, 1);
        const k = ease(t);
        state.cam.theta = a.from.theta + (a.to.theta - a.from.theta) * k;
        state.cam.phi = a.from.phi + (a.to.phi - a.from.phi) * k;
        state.cam.dist = a.from.dist + (a.to.dist - a.from.dist) * k;
        for (let i = 0; i < 3; i++) state.cam.target[i] = a.from.target[i] + (a.to.target[i] - a.from.target[i]) * k;
        if (t >= 1) { state.anim = null; persist(); } else again = true;
      }
      if (state.rotate && !state.anim) { state.cam.theta += 0.0025; again = true; }
      let expanding = false;
      for (const [id, ex] of state.expand) {
        const goal = id === state.focused ? LOCAL_FOCUS : LOCAL_OVERVIEW;
        const cur = ex.cur;
        const step = reducedMotion() ? goal : cur + (goal - cur) * 0.18;
        ex.cur = Math.abs(goal - step) < 0.003 ? goal : step;
        if (ex.cur !== goal) expanding = true;
        if (ex.cur !== cur) ex.dirty = true;
      }
      if (expanding) again = true;
      const fading = [...state.births.values()].some((b) => now - b < FADE_MS);
      if (fading) again = true;
      if (expanding || fading || state.sceneDirty) buildScene(now);
      draw();
      if (again || continuous) requestRender();
    }
    function depthRange() {
      return { near: state.cam.dist * 0.6, far: state.cam.dist * 2.6 };
    }
    function draw() {
      if (!state.renderer) return;
      state.renderer.render(state.cam, depthRange());
      placeLabels();
    }

    function worldOf(brain, local) {
      const ex = state.expand.get(brain.id);
      const s = (ex ? ex.cur : LOCAL_OVERVIEW) * (ex ? 1 : brain.size || 1);
      return [brain.pos[0] + local[0] * s, brain.pos[1] + local[1] * s, brain.pos[2] + local[2] * s];
    }
    function localScale(brain) {
      const ex = state.expand.get(brain.id);
      return ex ? ex.cur : LOCAL_OVERVIEW;
    }
    function buildScene(now) {
      state.sceneDirty = false;
      const nodes = [], edges = [];
      const focused = state.focused ? state.byId.get(state.focused) : null;
      const placeholder = !state.loaded;
      for (const b of state.brains) {
        const isF = focused && b.id === focused.id;
        const dim = focused && !isF ? 0.15 : 1;
        const hubSize = 0.28 + 0.42 * (b.size || 0.75);
        nodes.push({ id: b.id, kind: 'brain', pos: b.pos, size: hubSize * (isF ? 1.15 : 1),
          color: isF ? HUB_FOCUS : HUB_COLOR, opacity: (placeholder ? 0.35 : 1) * dim, style: 0,
          label: b.label, brain: b });
        const scale = localScale(b) * (isF ? 1 : (b.size || 1));
        const secs = isF && state.detail ? state.detail.sections : (b.sections || []);
        const dirs = isF && state.detail ? null : sectionDirsFor(b);
        secs.forEach((s, i) => {
          const dir = s.dir || (dirs ? dirs[i] : null);
          if (!dir) return;
          const local = [dir[0] * 1.0, dir[1] * 1.0, dir[2] * 1.0];
          const p = [b.pos[0] + local[0] * scale, b.pos[1] + local[1] * scale, b.pos[2] + local[2] * scale];
          const empty = !(s.count > 0);
          const selected = isF && state.section === s.key;
          const col = colorOf(s.key);
          nodes.push({ id: b.id + '|' + s.key, kind: 'section', pos: p,
            size: (isF ? 0.26 : 0.09 + 0.12 * (b.size || 0.75)) * (selected ? 1.15 : 1),
            color: col, opacity: (empty ? 0.55 : 1) * dim * (placeholder ? 0.35 : 1),
            style: empty ? 2 : 0, label: s.label || String(s.key).replace(/_/g, ' '), brain: b, section: s.key });
          if (selected) nodes.push({ id: b.id + '|' + s.key + '|ring', kind: 'ring', pos: p,
            size: 0.46, color: col, opacity: 0.9, style: 1 });
          edges.push({ a: b.pos, b: p, color: col,
            opacity: (selected ? 0.9 : (isF ? 0.4 : 0.22)) * dim });
          if (isF && state.detail) {
            for (const it of s.items) {
              const lp = [b.pos[0] + it.pos[0] * scale, b.pos[1] + it.pos[1] * scale, b.pos[2] + it.pos[2] * scale];
              const birth = state.births.get(it.ref);
              const fade = birth != null ? clamp((now - birth) / FADE_MS, 0, 1) : 1;
              const leafOp = state.section ? (selected ? 1 : 0.3) : 0.85;
              const style = it.review_state === 'approved' ? 0 : it.review_state === 'gap' ? 2 : 1;
              nodes.push({ id: it.ref, kind: 'leaf', pos: lp, size: 0.07 + 0.11 * (it.size || 0.5),
                color: col, opacity: leafOp * fade, style, label: it.text, brain: b, section: s.key, item: it });
              edges.push({ a: p, b: lp, color: col, opacity: (selected ? 0.55 : 0.18) * fade });
            }
          }
        });
      }
      state.scene = { nodes, edges };
      if (state.renderer) state.renderer.setScene(state.scene);
    }
    // Section directions for brains we only have a summary of: the same
    // Fibonacci sphere the server uses, by section index.
    function sectionDirsFor(b) {
      const n = (b.sections || []).length || 1;
      const out = [];
      for (let i = 0; i < n; i++) {
        const y = 1 - 2 * (i + 0.5) / n, r = Math.sqrt(Math.max(0, 1 - y * y)), phi = i * 2.399963229728653;
        out.push([r * Math.cos(phi), y, r * Math.sin(phi)]);
      }
      return out;
    }

    function placeLabels() {
      const w = stage.clientWidth, h = stage.clientHeight;
      const pr = projector(state.cam, w, h);
      const depth = depthRange();
      const cands = [];
      state.projected = [];
      for (const n of state.scene.nodes) {
        const p = pr(n.pos);
        if (!p) continue;
        const r = 0.5 * n.size * p.scale * (0.6 + 0.4 * depthCue(p.z, depth));
        state.projected.push({ n, x: p.x, y: p.y, z: p.z, r });
        if (n.kind === 'ring') continue;
        const inView = p.x > -40 && p.x < w + 40 && p.y > -20 && p.y < h + 20;
        if (!inView) continue;
        const focusedBrain = state.focused && n.brain && n.brain.id === state.focused;
        if (n.kind === 'brain' && (!state.focused || focusedBrain)) cands.push({ n, p, r, pri: r * 10 });
        else if (n.kind === 'section' && focusedBrain) cands.push({ n, p, r, pri: 1000 + r });
        else if (state.hover === n.id) cands.push({ n, p, r, pri: 5000 });
      }
      cands.sort((a, b) => b.pri - a.pri);
      const keep = cands.slice(0, LABEL_CAP);
      const seen = new Set();
      for (const c of keep) {
        seen.add(c.n.id);
        let d = state.labels.get(c.n.id);
        const text = c.n.kind === 'leaf' ? shorten(c.n.label, 80) : c.n.label;
        if (!d) {
          d = document.createElement('div');
          d.className = 'sb-label sb-label-' + c.n.kind;
          d.textContent = text;
          labelsEl.appendChild(d);
          state.labels.set(c.n.id, d);
        } else if (d.textContent !== text) d.textContent = text;
        d.style.transform = `translate(${Math.round(c.p.x + c.r + 6)}px, ${Math.round(c.p.y - 9)}px)`;
        d.style.opacity = String(clamp(depthCue(c.p.z, depth) * (c.n.opacity || 1) + 0.15, 0.25, 1));
      }
      for (const [id, d] of state.labels) if (!seen.has(id)) { d.remove(); state.labels.delete(id); }
    }
    function shorten(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

    function pick(x, y) {
      let best = null, bd = Infinity;
      for (const q of state.projected) {
        if (q.n.kind === 'ring' || q.n.opacity < 0.2) continue;
        const r = Math.max(10, q.r + 6);
        const dx = q.x - x, dy = q.y - y, d = dx * dx + dy * dy;
        if (d <= r * r && d < bd) { bd = d; best = q.n; }
      }
      return best;
    }

    // --- camera moves ---------------------------------------------------------
    function flyTo(to, ms) {
      const from = { theta: state.cam.theta, phi: state.cam.phi, dist: state.cam.dist, target: state.cam.target.slice() };
      const goal = { theta: to.theta != null ? to.theta : from.theta, phi: to.phi != null ? to.phi : from.phi,
        dist: to.dist != null ? to.dist : from.dist, target: to.target || from.target.slice() };
      if (reducedMotion() || ms === 0) {
        state.cam.theta = goal.theta; state.cam.phi = goal.phi; state.cam.dist = goal.dist;
        state.cam.target = goal.target.slice(); state.anim = null; persist(); requestRender(); return;
      }
      state.anim = { from, to: goal, t0: performance.now(), ms: ms || FLY_MS };
      requestRender();
    }
    function zoom(factor) {
      state.cam.dist = clamp(state.cam.dist * factor, DIST_MIN, DIST_MAX);
      persist(); requestRender();
    }
    function orbit(dx, dy) {
      state.cam.theta += dx;
      state.cam.phi = clamp(state.cam.phi + dy, Math.PI / 2 - PITCH_MAX, Math.PI / 2 + PITCH_MAX);
      persist(); requestRender();
    }
    function resetView() {
      if (state.focused) {
        const b = state.byId.get(state.focused);
        flyTo({ target: b ? b.pos.slice() : [0, 0, 0], dist: FOCUS_DIST, theta: 0.9, phi: 1.25 });
      } else flyTo({ target: [0, 0, 0], dist: OVERVIEW_DIST, theta: 0.9, phi: 1.25 });
    }

    // --- state changes ----------------------------------------------------------
    async function focusBrain(id, opt) {
      opt = opt || {};
      if (!id) return showOverview();
      const b = state.byId.get(id);
      if (!b) { message('That brain is not on the map yet.'); return; }
      const changed = state.focused !== id;
      state.focused = id;
      if (changed || opt.section === undefined) state.section = opt.section || null;
      else state.section = opt.section;
      for (const other of state.brains) if (!state.expand.has(other.id)) state.expand.set(other.id, { cur: LOCAL_OVERVIEW });
      if (!opt.keepCamera) flyTo({ target: b.pos.slice(), dist: FOCUS_DIST });
      state.sceneDirty = true; requestRender();
      syncUrl(); persist();
      renderCard(null);   // skeleton while it loads
      try {
        const d = await fetchDetail(id, !!opt.fresh);
        if (state.focused !== id) return;
        state.detail = d;
        if (state.section && !d.sections.some((s) => s.key === state.section)) state.section = null;
        state.sceneDirty = true; requestRender();
        renderCard(d);
        if (opts.onFocus) try { opts.onFocus(d); } catch (e) { /* ignore */ }
      } catch (e) {
        message('Could not load that brain: ' + (e.message || e));
      }
    }
    function showOverview() {
      state.focused = null; state.section = null; state.detail = null; state.evidence = null;
      flyTo({ target: [0, 0, 0], dist: OVERVIEW_DIST });
      state.sceneDirty = true; requestRender();
      syncUrl(); persist(); renderCard(null);
    }
    function selectSection(key) {
      if (!state.focused) return;
      state.section = state.section === key ? null : key;
      state.evidence = null;
      state.sceneDirty = true; requestRender();
      syncUrl(); persist(); renderCard(state.detail);
      if (opts.onSection) try { opts.onSection(state.section); } catch (e) { /* ignore */ }
    }
    function stepBack() {
      if (state.evidence) { state.evidence = null; renderCard(state.detail); return; }
      if (state.section) return selectSection(state.section);
      if (state.focused) return showOverview();
    }

    async function fetchDetail(id, fresh) {
      if (!fresh && state.cache.has(id)) {
        const hit = state.cache.get(id);
        state.cache.delete(id); state.cache.set(id, hit);     // LRU touch
        return hit;
      }
      const d = await getJson('/brains/' + encodeURIComponent(id));
      const old = state.cache.get(id);
      if (old) {
        const had = new Set(old.sections.flatMap((s) => s.items.map((i) => i.ref)));
        const now = performance.now();
        for (const s of d.sections) for (const it of s.items) if (!had.has(it.ref)) state.births.set(it.ref, now);
      }
      state.cache.set(id, d);
      while (state.cache.size > CACHE_MAX) state.cache.delete(state.cache.keys().next().value);
      return d;
    }

    // --- data ----------------------------------------------------------------
    function ingest(list, patch) {
      if (!patch) { state.brains = []; state.byId.clear(); }
      for (const b of list) {
        const prev = state.byId.get(b.id);
        if (prev) Object.assign(prev, b); else { state.brains.push(b); state.byId.set(b.id, b); }
        if (!state.expand.has(b.id)) state.expand.set(b.id, { cur: LOCAL_OVERVIEW });
      }
      state.sceneDirty = true;
    }
    async function loadAll() {
      const j = await getJson('/brains?limit=500');
      state.version = j.version;
      ingest(j.brains || [], false);
      state.loaded = true;
      msgEl.hidden = true;
      state.building = !!j.building;
      if (state.building) {
        message('Compiling brains for the first time. The map fills in as it finishes.');
      } else if (!state.brains.length) {
        message('No brains yet. A subject gets a brain once Sparrow holds 3 facts about it. '
          + '<a href="/inbox">Review what is waiting in the inbox</a>.', true);
      }
      renderList();
      state.sceneDirty = true; requestRender();
    }
    async function pollVersion() {
      if (state.destroyed) return;
      if (document.hidden) return;
      try {
        const v = (await getJson('/graph/version')).version;
        if (state.building && v === state.version) return;   // keep polling until the compile lands
        if (state.building && v !== state.version) { state.building = false; await loadAll(); return; }
        if (state.version && v !== state.version) {
          const j = await getJson('/brains?limit=500&since=' + encodeURIComponent(state.version));
          state.version = j.version;
          if (j.total !== undefined && !j.brains.length && j.total === 0) { /* nothing changed */ }
          ingest(j.brains || [], true);
          if (state.focused && j.brains.some((b) => b.id === state.focused)) {
            const d = await fetchDetail(state.focused, true);
            state.detail = d; renderCard(d);
          }
          renderList();
          state.sceneDirty = true; requestRender();
        }
      } catch (e) { /* retry next tick */ }
    }

    function message(html, isHtml) {
      msgEl.hidden = false;
      if (isHtml) msgEl.innerHTML = html; else msgEl.textContent = html;
    }

    // --- card -----------------------------------------------------------------
    function renderCard(d) {
      if (!state.focused) {
        cardEl.innerHTML = `
          <div class="sb-card-empty">
            <p><b>Brains</b> are what Sparrow knows about one subject, grouped into the sections an analyst expects.</p>
            <p>Click a brain to open it. Copy its context for your agent in one click; only approved facts leave, each with its source.</p>
          </div>`;
        return;
      }
      const b = state.byId.get(state.focused);
      if (!d) {
        cardEl.innerHTML = `
          <div class="sb-card-head"><h2>${esc(b ? b.label : '')}</h2><div class="sb-skel"></div><div class="sb-skel w60"></div></div>`;
        return;
      }
      const chips = d.sections.map((s) => {
        const on = state.section === s.key;
        return `<button type="button" class="sb-chip${on ? ' on' : ''}" data-sec="${esc(s.key)}"
          aria-pressed="${on}" style="--c:${colorOf(s.key)}"><i></i>${esc(s.label)}<span>${s.count}</span></button>`;
      }).join('');
      const secs = state.section ? d.sections.filter((s) => s.key === state.section) : d.sections;
      const items = secs.map((s) => {
        const rows = s.items.map((it) => itemRow(it, s)).join('');
        const empty = !s.items.length ? `<div class="sb-empty">Nothing here yet${s.required ? ' — Sparrow is missing this' : ''}.</div>` : '';
        return `<section class="sb-sec" data-sec="${esc(s.key)}">
          <h3 style="--c:${colorOf(s.key)}"><i></i>${esc(s.label)}</h3>${rows}${empty}</section>`;
      }).join('');
      const copied = state.lastCopy && state.lastCopy.brain === d.id ? state.lastCopy : null;
      const withheld = copied ? `<div class="sb-withheld">Copied ${copied.included} item${copied.included === 1 ? '' : 's'}
          · withheld: ${copied.withheld.pending} unreviewed, ${copied.withheld.privacy} by privacy,
          ${copied.withheld.budget} over budget${copied.withheld.personal ? ', ' + copied.withheld.personal + ' personal' : ''}</div>` : '';
      const fallback = copied && copied.fallbackText ? `<textarea class="sb-fallback" readonly aria-label="Packet text">${esc(copied.fallbackText)}</textarea>` : '';
      cardEl.innerHTML = `
        <div class="sb-card-head">
          <div class="sb-kicker">${esc(d.type)} · ${d.items_total} item${d.items_total === 1 ? '' : 's'} · ${d.sources_total} source${d.sources_total === 1 ? '' : 's'} · updated ${esc(when(d.updated_at))}</div>
          <h2>${esc(d.label)}</h2>
        </div>
        <div class="sb-chips" role="tablist" aria-label="Sections">${chips}</div>
        <div class="sb-actions">
          <button type="button" class="sb-btn sb-primary" data-act="copy-brain">Copy brain for your agent</button>
          <button type="button" class="sb-btn" data-act="copy-section" ${state.section ? '' : 'disabled'}>Copy this section</button>
          <label class="sb-toggle"><input type="checkbox" data-act="pending" ${state.includePending ? 'checked' : ''}> Include pending</label>
        </div>
        ${withheld}${fallback}
        <div class="sb-items">${items}</div>
        ${state.evidence ? evidenceBlock(state.evidence) : ''}`;
    }
    function itemRow(it, s) {
      const dot = it.review_state === 'approved' ? 'filled' : it.review_state === 'gap' ? 'dashed' : 'ring';
      const meta = it.item_type === 'gap'
        ? (it.source_label === 'You' ? 'your question' : 'Sparrow is missing this')
        : [it.review_state, it.source_label, when(it.observed_at)].filter(Boolean).join(' · ');
      const menu = it.item_type === 'gap' ? '' : `<button type="button" class="sb-more" data-ref="${esc(it.ref)}" aria-label="Item menu">⋯</button>`;
      return `<div class="sb-item" data-ref="${esc(it.ref)}" data-sec="${esc(s.key)}" style="--c:${colorOf(s.key)}">
        <i class="sb-dot ${dot}"></i>
        <button type="button" class="sb-item-text" data-act="evidence" data-ref="${esc(it.ref)}">${esc(it.item_type === 'gap' ? 'UNKNOWN: ' + it.text : it.text)}</button>
        ${menu}
        <div class="sb-item-meta">${esc(meta)}</div>
      </div>`;
    }
    function evidenceBlock(ev) {
      const rows = (ev.sources || []).slice(0, 8).map((s) => `<li>${esc(s.text || s.summary || s.kind || 'memory')}
        ${s.event_id ? `<span class="sb-mut">· event ${esc(s.event_id)}</span>` : ''}</li>`).join('');
      return `<div class="sb-evidence"><div class="sb-ev-head"><b>Where this comes from</b>
        <button type="button" class="sb-btn sb-icon" data-act="close-evidence" aria-label="Close">×</button></div>
        <div class="sb-mut">${esc(ev.title || '')}</div>
        ${rows ? `<ul>${rows}</ul>` : `<div class="sb-mut">${esc(ev.note || 'No recorded source behind this item.')}</div>`}
        ${ev.link ? `<a href="${esc(ev.link)}">Open in Memory</a>` : ''}</div>`;
    }
    async function openEvidence(ref) {
      const it = findItem(ref);
      if (!it) return;
      const ev = { title: it.text, sources: [], link: null };
      try {
        if (it.evidence_id) {
          const j = await getJson('/graph/constellation/evidence?id=' + encodeURIComponent(it.evidence_id));
          ev.sources = j.sources || [];
          ev.link = '/memory?q=' + encodeURIComponent(it.text.slice(0, 60));
        } else if (it.source_event_id) {
          const j = await getJson('/console/provenance/' + encodeURIComponent(it.source_event_id));
          const summ = j.summary && typeof j.summary === 'object' ? (j.summary.summary || j.summary.text || '') : (j.summary || '');
          ev.sources = [{ text: summ || ('event ' + it.source_event_id), event_id: it.source_event_id }];
          ev.link = '/memory?q=' + encodeURIComponent(it.text.slice(0, 60));
        } else ev.note = it.item_type === 'relation' ? 'A link in the graph, not a captured sentence.' : 'No recorded source behind this item.';
      } catch (e) { ev.note = 'Could not load the source: ' + (e.message || e); }
      state.evidence = ev;
      renderCard(state.detail);
      const host = cardEl.querySelector('.sb-evidence');
      if (host) host.scrollIntoView({ block: 'nearest' });
    }
    function findItem(ref) {
      if (!state.detail) return null;
      for (const s of state.detail.sections) for (const it of s.items) if (it.ref === ref) return { ...it, section: s.key };
      return null;
    }

    async function copy(scope) {
      if (!state.focused) return;
      const id = state.focused;
      const body = { scope, include_pending: state.includePending, destination: 'clipboard' };
      if (scope === 'section') body.section = state.section;
      let res;
      try { res = await postJson('/brains/' + encodeURIComponent(id) + '/packet', body); }
      catch (e) {
        const deny = e.body && e.body.verdict === 'deny';
        state.lastCopy = { brain: id, included: 0, withheld: { pending: 0, privacy: 0, budget: 0, personal: 0 },
          fallbackText: deny ? ('Nothing left this device: ' + (e.body.reason || 'the rung blocks it.')) : ('Copy failed: ' + (e.message || e)) };
        renderCard(state.detail); return;
      }
      let ok = false;
      try { await navigator.clipboard.writeText(res.text); ok = true; } catch (e) { ok = false; }
      state.lastCopy = { brain: id, included: res.included, withheld: res.withheld,
        fallbackText: ok ? null : res.text };
      renderCard(state.detail);
      if (ok) {
        const btn = cardEl.querySelector('[data-act="copy-' + scope + '"]');
        if (btn) { const t = btn.textContent; btn.textContent = 'Copied'; setTimeout(() => { btn.textContent = t; }, 1400); }
      }
    }

    async function itemMenu(ref, anchor) {
      const it = findItem(ref);
      if (!it || !state.detail) return;
      closeMenu();
      const m = document.createElement('div');
      m.className = 'sb-menu'; m.setAttribute('role', 'menu');
      const moves = state.detail.sections.filter((s) => s.key !== it.section && s.key !== 'open_questions')
        .map((s) => `<button type="button" role="menuitem" data-move="${esc(s.key)}"><i style="--c:${colorOf(s.key)}"></i>Move to ${esc(s.label)}</button>`).join('');
      m.innerHTML = `${moves}<button type="button" role="menuitem" data-hide="1">Hide from this brain</button>
        <button type="button" role="menuitem" data-gap="1">Add a gap here</button>`;
      anchor.closest('.sb-item').appendChild(m);
      m.querySelector('button').focus();
      m.addEventListener('click', async (e) => {
        const btn = e.target.closest('button'); if (!btn) return;
        e.stopPropagation();
        const id = state.focused;
        try {
          if (btn.dataset.move) {
            await postJson('/brains/' + encodeURIComponent(id) + '/items/move',
              { item_type: it.item_type, item_id: it.item_id, section: btn.dataset.move });
          } else if (btn.dataset.hide) {
            await postJson('/brains/' + encodeURIComponent(id) + '/items/hide',
              { item_type: it.item_type, item_id: it.item_id });
          } else if (btn.dataset.gap) {
            const q = window.prompt('What should this section answer?');
            if (!q) { closeMenu(); return; }
            await postJson('/brains/' + encodeURIComponent(id) + '/gaps', { section: it.section, question: q });
          }
          closeMenu();
          await focusBrain(id, { section: state.section, keepCamera: true, fresh: true });
          await refreshList();
        } catch (err) { closeMenu(); message('That did not stick: ' + (err.message || err)); }
      });
    }
    function closeMenu() { el.querySelectorAll('.sb-menu').forEach((n) => n.remove()); }

    // --- list view ----------------------------------------------------------
    function setListMode(on) {
      state.listMode = !!on;
      listEl.hidden = !state.listMode;
      stage.hidden = state.listMode;
      el.querySelector('[data-act="list"]').setAttribute('aria-pressed', String(state.listMode));
      if (state.listMode) renderList(); else requestRender();
    }
    async function refreshList() {
      try { const j = await getJson('/brains?limit=500'); state.version = j.version; ingest(j.brains || [], false); renderList(); }
      catch (e) { /* ignore */ }
    }
    function renderList() {
      if (!state.listMode) return;
      const { key, dir } = state.sort;
      const rows = state.brains.slice().sort((a, b) => {
        const va = sortVal(a, key), vb = sortVal(b, key);
        return (va < vb ? -1 : va > vb ? 1 : 0) * dir;
      });
      const th = (k, label) => `<th scope="col"><button type="button" data-sort="${k}" aria-sort="${key === k ? (dir > 0 ? 'ascending' : 'descending') : 'none'}">${label}${key === k ? (dir > 0 ? ' ▲' : ' ▼') : ''}</button></th>`;
      listEl.innerHTML = `<table class="sb-table"><caption class="sb-sr">All brains</caption>
        <thead><tr>${th('label', 'Name')}${th('type', 'Type')}${th('activity', 'Activity')}${th('filled', 'Sections filled')}${th('updated_at', 'Updated')}</tr></thead>
        <tbody>${rows.map((b) => `<tr data-id="${esc(b.id)}" class="${state.focused === b.id ? 'on' : ''}">
          <td><button type="button" class="sb-link" data-open="${esc(b.id)}" aria-current="${state.focused === b.id ? 'true' : 'false'}">${esc(b.label)}</button></td>
          <td>${esc(b.type)}</td><td>${Math.round((b.activity || 0) * 100)}%</td>
          <td>${filled(b)} of ${(b.sections || []).length}</td><td>${esc(when(b.updated_at))}</td></tr>`).join('')}</tbody></table>
        ${rows.length ? '' : '<div class="sb-empty">No brains yet.</div>'}`;
    }
    function filled(b) { return (b.sections || []).filter((s) => s.count > 0).length; }
    function sortVal(b, k) {
      if (k === 'filled') return filled(b);
      if (k === 'label' || k === 'type') return String(b[k] || '').toLowerCase();
      return b[k] || 0;
    }

    // --- events -----------------------------------------------------------------
    let drag = null, pinch = null;
    stage.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 && e.pointerType === 'mouse') return;
      if (e.target.closest('.sb-toolbar, .sb-msg')) return;   // buttons keep their clicks
      stage.focus({ preventScroll: true });
      if (pinch === null && e.isPrimary) {
        drag = { x: e.clientX, y: e.clientY, moved: false, id: e.pointerId };
        stage.setPointerCapture(e.pointerId);
      } else if (drag && !pinch) {
        pinch = { id2: e.pointerId, x2: e.clientX, y2: e.clientY, d0: null };
      }
    });
    stage.addEventListener('pointermove', (e) => {
      if (pinch && (e.pointerId === pinch.id2 || (drag && e.pointerId === drag.id))) {
        if (e.pointerId === pinch.id2) { pinch.x2 = e.clientX; pinch.y2 = e.clientY; }
        else { drag.x = e.clientX; drag.y = e.clientY; }
        const d = Math.hypot(pinch.x2 - drag.x, pinch.y2 - drag.y);
        if (pinch.d0 == null) pinch.d0 = d; else if (d > 0) { zoom(pinch.d0 / d); pinch.d0 = d; }
        drag.moved = true; return;
      }
      if (drag && e.pointerId === drag.id) {
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        if (!drag.moved && Math.hypot(dx, dy) <= DRAG_PX) return;
        drag.moved = true;
        orbit(dx * 0.006, dy * 0.006);
        drag.x = e.clientX; drag.y = e.clientY;
        return;
      }
      const r = stage.getBoundingClientRect();
      const n = pick(e.clientX - r.left, e.clientY - r.top);
      const id = n ? n.id : null;
      if (id !== state.hover) { state.hover = id; stage.style.cursor = n ? 'pointer' : ''; requestRender(); }
    });
    function endPointer(e) {
      if (pinch && e.pointerId === pinch.id2) { pinch = null; return; }
      if (!drag || e.pointerId !== drag.id) return;
      const was = drag; drag = null; pinch = null;
      if (was.moved) return;                       // a drag never fires a click
      const r = stage.getBoundingClientRect();
      const n = pick(e.clientX - r.left, e.clientY - r.top);
      if (!n) return;
      if (n.kind === 'brain') focusBrain(n.id, { section: null });
      else if (n.kind === 'section') {
        if (state.focused === n.brain.id) selectSection(n.section);
        else focusBrain(n.brain.id, { section: n.section });
      } else if (n.kind === 'leaf') {
        if (state.section !== n.section) selectSection(n.section);
        openEvidence(n.id);
      }
    }
    stage.addEventListener('pointerup', endPointer);
    stage.addEventListener('pointercancel', () => { drag = null; pinch = null; });
    stage.addEventListener('wheel', (e) => {
      e.preventDefault();
      zoom(Math.exp(clamp(e.deltaY, -120, 120) * 0.0018));
    }, { passive: false });
    stage.addEventListener('keydown', (e) => {
      const step = 0.08;
      if (e.key === 'ArrowLeft') orbit(-step, 0);
      else if (e.key === 'ArrowRight') orbit(step, 0);
      else if (e.key === 'ArrowUp') orbit(0, -step);
      else if (e.key === 'ArrowDown') orbit(0, step);
      else if (e.key === '+' || e.key === '=') zoom(0.85);
      else if (e.key === '-' || e.key === '_') zoom(1 / 0.85);
      else if (e.key === 'Escape') stepBack();
      else return;
      e.preventDefault();
    });
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && e.target !== stage) {
        if (el.querySelector('.sb-menu')) { closeMenu(); e.preventDefault(); return; }
        if (state.focused) { stepBack(); e.preventDefault(); }
      }
    });
    el.addEventListener('click', (e) => {
      const sortBtn = e.target.closest('[data-sort]');
      if (sortBtn) {
        const k = sortBtn.dataset.sort;
        state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (k === 'label' || k === 'type' ? 1 : -1) };
        renderList(); return;
      }
      const open = e.target.closest('[data-open]');
      if (open) { focusBrain(open.dataset.open, { section: null }); renderList(); return; }
      const chip = e.target.closest('.sb-chip');
      if (chip) { selectSection(chip.dataset.sec); return; }
      const more = e.target.closest('.sb-more');
      if (more) { itemMenu(more.dataset.ref, more); return; }
      const act = e.target.closest('[data-act]');
      if (!act) { if (!e.target.closest('.sb-menu')) closeMenu(); return; }
      const a = act.dataset.act;
      if (a === 'all') showOverview();
      else if (a === 'list') setListMode(!state.listMode);
      else if (a === 'zoom-in') zoom(0.8);
      else if (a === 'zoom-out') zoom(1.25);
      else if (a === 'reset') resetView();
      else if (a === 'rotate') {
        state.rotate = !state.rotate && !reducedMotion();
        act.setAttribute('aria-pressed', String(state.rotate)); requestRender();
      } else if (a === 'copy-brain') copy('brain');
      else if (a === 'copy-section') copy('section');
      else if (a === 'evidence') openEvidence(act.dataset.ref);
      else if (a === 'close-evidence') { state.evidence = null; renderCard(state.detail); }
    });
    el.addEventListener('change', (e) => {
      if (e.target.matches('[data-act="pending"]')) { state.includePending = e.target.checked; persist(); }
    });
    // focus follows the card when a brain opens from the keyboard (list view)
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.matches('[data-open]')) {
        setTimeout(() => { const h = cardEl.querySelector('h2'); if (h) { h.setAttribute('tabindex', '-1'); h.focus(); } }, 50);
      }
    });
    const onResize = () => { if (state.renderer) state.renderer.resize(); requestRender(); };
    window.addEventListener('resize', onResize);
    const onVis = () => { if (!document.hidden) { pollVersion(); requestRender(); } };
    document.addEventListener('visibilitychange', onVis);
    // the theme toggle flips html[data-theme]; hub and edge colours follow it
    let themeObs = null;
    try {
      themeObs = new MutationObserver(() => { readTheme(); state.sceneDirty = true; requestRender(); });
      themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    } catch (e) { themeObs = null; }

    // --- boot --------------------------------------------------------------------
    const saved = loadPersisted();
    if (saved && saved.cam) Object.assign(state.cam, { theta: saved.cam.theta, phi: saved.cam.phi,
      dist: clamp(saved.cam.dist, DIST_MIN, DIST_MAX), target: saved.cam.target.slice() });
    if (saved) state.includePending = saved.includePending;
    const url = new URL(location.href);
    const wantBrain = opts.brain || url.searchParams.get('brain') || (saved && saved.focused) || null;
    const wantSection = opts.section || url.searchParams.get('section') || (saved && saved.section) || null;

    // a static dot field while the list loads
    for (let i = 0; i < 28; i++) {
      const a = i * 2.399963, r = 6 + (i * 7919 % 11);
      state.brains.push({ id: 'ph' + i, label: '', pos: [Math.cos(a) * r, ((i * 31) % 7) - 3, Math.sin(a) * r],
        size: 0.5, sections: [], activity: 0 });
    }
    renderCard(null);

    (async () => {
      let three = null;
      if (webglAvailable()) {
        try { three = await loadThree(opts.threeUrl); } catch (e) { three = null; }
      }
      if (state.destroyed) return;
      try {
        state.renderer = three ? makeThreeRenderer(stage, three) : makeCanvas2D(stage);
      } catch (e) {
        state.renderer = makeCanvas2D(stage);
      }
      if (state.renderer.kind === '2d') el.classList.add('sb-2d');
      state.sceneDirty = true; requestRender();
      try {
        await loadAll();
      } catch (e) {
        message('Could not load brains: ' + (e.message || e));
        return;
      }
      if (!(saved && saved.cam)) {
        let far = 0;
        for (const b of state.brains) far = Math.max(far, Math.hypot(b.pos[0], b.pos[1], b.pos[2]) + 1);
        state.cam.dist = clamp(far * 2.4, 9, OVERVIEW_DIST);
      }
      if (wantBrain && state.byId.has(wantBrain)) {
        await focusBrain(wantBrain, { section: wantSection, keepCamera: !!(saved && saved.cam && saved.focused === wantBrain) });
      } else { syncUrl(); requestRender(); }
      state.pollTimer = setInterval(pollVersion, POLL_MS);
    })();

    return {
      focus: (id, section) => focusBrain(id, { section: section || null }),
      pick: (x, y) => pick(x, y),
      overview: showOverview,
      selectSection,
      refresh: refreshList,
      state,
      destroy() {
        state.destroyed = true;
        if (state.pollTimer) clearInterval(state.pollTimer);
        window.removeEventListener('resize', onResize);
        document.removeEventListener('visibilitychange', onVis);
        if (themeObs) themeObs.disconnect();
        if (frame) cancelAnimationFrame(frame);
        if (state.renderer) state.renderer.dispose();
        el.innerHTML = ''; el.classList.remove('sb-root');
      },
    };
  }

  window.SparrowBrains = { mount, SECTION_COLORS, _math: { projector, eyeOf, makeCamera } };
})();
