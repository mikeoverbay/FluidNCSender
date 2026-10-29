// 3D toolpath / scan view. Draws only when something changes, so it costs nothing while idle.

import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';

const VIEWS = { top: [0, -0.0001, 1], iso: [-0.6, -1, 0.9], front: [0, -1, 0.15] };
const TOOL_SIZE = 0.045;    // tool marker height as a share of its distance from the camera: a fixed size on screen
const EDGE = 1e-4;          // in: a point this close to the scan's edge is inside it

// The colors come from the theme (style.css: --v-*, --scan-*), read again when it changes (recolor()); the fallbacks
// are the Dark theme's. cutTop / cutDeep: the cuts by depth; a leveled program's scan area is drawn as a rectangle
// (rect), its moves outside it in outside (the leveler gives them the nearest edge's height there); low / mid / high:
// the scan by height; hl: the moves of the line chosen; axisX / Y / Z (when the theme has them, else three.js's own)
const COLORS = {
  bg: ['--v-bg', 0x1b1d21], gridAxis: ['--v-grid-axis', 0x3a3f48], grid: ['--v-grid', 0x2a2e34],
  rapid: ['--v-rapid', 0x5d6b85], cutTop: ['--v-cut-top', 0xf2c14e], cutDeep: ['--v-cut-deep', 0xd9572b],
  todo: ['--v-todo', 0x6a717c], tool: ['--v-tool', 0x7fd4a0], rect: ['--v-rect', 0x8fe0a0],
  outside: ['--v-outside', 0xff4fd8], hl: ['--v-hl', 0x4fd1ff],
  low: ['--scan-low', 0xd23c32], mid: ['--scan-mid', 0xe8c33d], high: ['--scan-high', 0x3cb455],
  axisX: ['--v-axis-x', null], axisY: ['--v-axis-y', null], axisZ: ['--v-axis-z', null],
};
const AXES = [1, 0, 0, 1, 0.6, 0, 0, 1, 0, 0.6, 1, 0, 0, 0, 1, 0, 0.6, 1];     // three.js's AxesHelper colors

function themeColors() {
  const css = getComputedStyle(document.documentElement), col = {};
  for (const [k, [name, fallback]] of Object.entries(COLORS)) {
    const v = css.getPropertyValue(name).trim();
    col[k] = v ? new THREE.Color(v) : fallback === null ? null : new THREE.Color(fallback);
  }
  return col;
}

/** colorFn(z, color, x, y) colors each point. */
function pointColors(positions, colorFn) {
  const col = new Float32Array(positions.length);
  const c = new THREE.Color();
  for (let i = 0; i < positions.length; i += 3) {
    colorFn(positions[i + 2], c, positions[i], positions[i + 1]);
    col[i] = c.r; col[i + 1] = c.g; col[i + 2] = c.b;
  }
  return col;
}

/** Puts the colors col (r, g, b per point) in geo's color attribute. */
function setColors(geo, col) {
  const a = geo.getAttribute('color');
  a.array.set(col);
  a.needsUpdate = true;
}

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

/** Adds the scan height under each segment, splitting long ones so they follow the surface. */
function levelSegments(src, srcLine, height) {
  const pos = [], line = [];
  const step = 0.1;
  for (let s = 0, i = 0; i < src.length; s++, i += 6) {
    const x1 = src[i], y1 = src[i + 1], z1 = src[i + 2], x2 = src[i + 3], y2 = src[i + 4], z2 = src[i + 5];
    const n = Math.max(1, Math.ceil(Math.hypot(x2 - x1, y2 - y1) / step));
    let px = x1, py = y1, pz = z1 + height(x1, y1);
    for (let k = 1; k <= n; k++) {
      const t = k / n, x = x1 + (x2 - x1) * t, y = y1 + (y2 - y1) * t;
      const z = z1 + (z2 - z1) * t + height(x, y);
      pos.push(px, py, pz, x, y, z);
      line.push(srcLine[s]);
      px = x; py = y; pz = z;
    }
  }
  return { pos: new Float32Array(pos), line: new Int32Array(line) };
}

export class Viewer {
  constructor() {
    // (leveled: the old "Leveled" preview, the scan's heights added here; its checkbox is gone, Apply shows the real
    // leveled toolpath, so it stays off)
    this.options = { toolpath: true, rapids: true, scan: true, leveled: false, zscale: 1 };
    this.job = null;
    this.scan = null;
    this.line = 0;
    this.lastLine = 0;
    this.levRect = null;     // a leveled program's scan area { x0, x1, y0, y1 }, or null (setLeveledProgram)
    this.preLeveled = '';    // why the loaded G-code is a saved leveled one (its heights are in it), or ''
    this.layers = {};
    this.shown = { cut: null, rapid: null };

    this.root = el('div', 'viewer');
    this.bar = el('div', 'vbar', `
      <button data-view="top" title="View from above">Top</button>
      <button data-view="iso" title="3D view">Iso</button>
      <button data-view="front" title="View from the front">Front</button>
      <span class="sep"></span>
      <label title="Show the cutting moves (G1, G2, G3)"><input type="checkbox" data-opt="toolpath"> Toolpath</label>
      <label><input type="checkbox" data-opt="rapids"> Rapids</label>
      <label><input type="checkbox" data-opt="scan"> Scan</label>
      <span class="sep"></span>
      <label title="Stretch heights to make small differences visible">Z <input type="range" data-opt="zscale" min="1" max="40" value="1"> <b class="zs">1×</b></label>
      <button class="vshot" data-act="shot" title="Save a picture of this view (PNG, in Downloads)"><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M9 4 7.2 6H4a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-3.2L15 4H9Zm3 4.5a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9Zm0 2a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5Z"/></svg></button>`);
    this.stage = el('div', 'stage');
    this.canvas = el('canvas');
    this.legend = el('div', 'legend', 'scan low <i></i> high');
    this.legend.hidden = true;
    this.stage.append(this.canvas, this.legend, el('div', 'hint', 'Drag: rotate · Right-drag: pan · Wheel: zoom'));
    this.root.append(this.bar, this.stage);

    this.bar.addEventListener('click', e => {
      const v = e.target.dataset?.view;
      if (v) this.view(v);
      if (e.target.closest?.('[data-act=shot]')) this.snapshot();
    });
    this.bar.addEventListener('input', e => {
      const k = e.target.dataset?.opt;
      if (k) this.setOption(k, e.target.type === 'checkbox' ? e.target.checked : +e.target.value);
    });
    this.syncBar();

    this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, antialias: false, powerPreference: 'low-power' });
    this.renderer.setPixelRatio(1);
    this.col = themeColors();
    this.renderer.setClearColor(this.col.bg);
    this.scene = new THREE.Scene();
    this.world = new THREE.Group();          // the Z scale applies to everything in here
    this.scene.add(this.world);
    this.axes = new THREE.AxesHelper(1);
    this.colorAxes();
    this.world.add(this.axes);
    this.camera = new THREE.PerspectiveCamera(40, 1, 0.01, 2000);
    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = false;     // damping needs a render loop
    this.controls.screenSpacePanning = true;
    this.controls.addEventListener('change', () => this.render());

    new ResizeObserver(() => this.resize()).observe(this.stage);
    this.buildFloor();
    this.view('iso');
  }

  // ---------------------------------------------------------------- drawing

  render() {
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => {
      this.pending = false;
      if (!(this.canvas.width > 0 && this.canvas.height > 0)) return;
      // Not shown (closed, minimized, or behind another tab, which dockview hides with visibility: hidden):
      // skip; the resize observer or the app (when its tab shows again) redraws it
      if (!this.canvas.isConnected || !this.stage.clientWidth) return;
      if (this.stage.checkVisibility && !this.stage.checkVisibility({ visibilityProperty: true })) return;
      if (this.tool?.visible) this.placeTool();
      this.renderer.render(this.scene, this.camera);
    });
  }

  /** Saves what the view shows as a PNG (the browser's download, so ~/Downloads), named after the G-code
   *  (shotName(), set by the app). Drawn here and read at once: the canvas keeps no picture after a frame. */
  snapshot() {
    if (!(this.canvas.width > 0 && this.canvas.height > 0)) return;
    if (this.tool?.visible) this.placeTool();
    this.renderer.render(this.scene, this.camera);
    const url = this.canvas.toDataURL('image/png');
    const d = new Date(), p2 = n => String(n).padStart(2, '0');
    const stamp = `${d.getFullYear()}${p2(d.getMonth() + 1)}${p2(d.getDate())}-${p2(d.getHours())}${p2(d.getMinutes())}${p2(d.getSeconds())}`;
    const base = String(this.shotName?.() || 'toolpath').replace(/\.[^./]*$/, '').replace(/[^\w.-]+/g, '_') || 'toolpath';
    const a = document.createElement('a');
    a.href = url;
    a.download = `${base}-view-${stamp}.png`;
    a.click();
    this.onMessage?.(`Saved a picture of the view: ${a.download} (in Downloads)`);
  }

  resize() {
    const w = this.stage.clientWidth, h = this.stage.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.render();
  }

  dispose(obj) {
    if (!obj) return;
    obj.parent?.remove(obj);
    obj.traverse(o => { o.geometry?.dispose(); o.material?.dispose(); });
  }

  /** colorFn(z, color, x, y) colors each point. */
  lines(positions, colorFn, material) {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    if (colorFn) geo.setAttribute('color', new THREE.BufferAttribute(pointColors(positions, colorFn), 3));
    return new THREE.LineSegments(geo, material);
  }

  // ---------------------------------------------------------------- the theme's colors

  /** The theme changed (View → Theme): everything takes its colors again, in place (nothing is rebuilt). */
  recolor() {
    this.col = themeColors();
    this.renderer.setClearColor(this.col.bg);
    this.colorAxes();
    this.colorFloor();
    if (this.layers.cut) setColors(this.layers.cut.geometry, pointColors(this.shown.cut.pos, this.cutColor()));
    this.layers.rapid?.material.color.copy(this.col.rapid);
    for (const o of this.layers.hl?.children || []) o.material.color.copy(this.col.hl);
    if (this.layers.surface) this.colorScan();
    this.layers.rect?.material.color.copy(this.col.rect);
    if (this.tool) setColors(this.tool.geometry, this.toolColors(this.tool.geometry));
    this.render();
  }

  colorAxes() {
    const { axisX: x, axisY: y, axisZ: z } = this.col;
    if (x && y && z) this.axes.setColors(x, y, z); else setColors(this.axes.geometry, AXES);
  }

  /** The floor grid's colors: its middle lines (the work zero's X and Y) gridAxis, the others grid. */
  colorFloor() {
    const a = this.floor.geometry.getAttribute('color'), n = a.count / 4;    // (2 lines, 4 points, per division)
    for (let i = 0; i < n; i++) {
      const c = i === (n - 1) / 2 ? this.col.gridAxis : this.col.grid;
      for (let k = i * 4; k < i * 4 + 4; k++) a.setXYZ(k, c.r, c.g, c.b);
    }
    a.needsUpdate = true;
  }

  /** The cuts' colors: by depth below the top of the job; a leveled program's moves outside the scan in outside. */
  cutColor() {
    const job = this.job, C = this.col;
    const top = job.hi[2] > 0 ? 0 : job.hi[2];
    const deep = Math.min(job.lo[2], top - 0.001);
    const r = this.levRect;
    const out = r ? (x, y) => x < r.x0 - EDGE || x > r.x1 + EDGE || y < r.y0 - EDGE || y > r.y1 + EDGE : () => false;
    return (z, c, x, y) => (out(x, y) ? c.copy(C.outside)
      : c.copy(C.cutTop).lerp(C.cutDeep, Math.min(1, Math.max(0, (top - z) / (top - deep)))));
  }

  /** The scan's colors by height (low, mid, high): all its points (i, j in order) and the measured ones. */
  scanColors() {
    const { xs, ys, at, zmin, zmax } = this.scan, C = this.col;
    const nx = xs.length, ny = ys.length;
    const all = new Float32Array(nx * ny * 3), done = [];
    const c = new THREE.Color();
    for (let i = 0; i < nx; i++) {
      for (let j = 0; j < ny; j++) {
        const k = (i * ny + j) * 3, has = Number.isFinite(at(i, j)), z = has ? at(i, j) : zmin;
        const t = zmax > zmin ? (z - zmin) / (zmax - zmin) : 0.5;
        if (t < 0.5) c.copy(C.low).lerp(C.mid, t * 2); else c.copy(C.mid).lerp(C.high, (t - 0.5) * 2);
        all[k] = c.r; all[k + 1] = c.g; all[k + 2] = c.b;
        if (has) done.push(c.r, c.g, c.b);
      }
    }
    return { all, done };
  }

  colorScan() {
    const { all, done } = this.scanColors();
    for (const o of this.layers.surface.children) {
      if (o.isMesh) setColors(o.geometry, all);
      else if (o.material.vertexColors) setColors(o.geometry, done);       // the points measured
      else o.material.color.copy(this.col.todo);                           // the points still to come
    }
  }

  buildJob() {
    this.dispose(this.layers.cut); this.dispose(this.layers.rapid);
    this.layers.cut = this.layers.rapid = null;
    this.shown = { cut: null, rapid: null };
    const job = this.job;
    if (!job) return;

    const leveled = this.options.leveled && this.scanUsable() && !this.levRect;
    this.shown.cut = leveled ? levelSegments(job.cut, job.cutLine, this.scan.height) : { pos: job.cut, line: job.cutLine };
    this.shown.rapid = leveled ? levelSegments(job.rapid, job.rapidLine, this.scan.height) : { pos: job.rapid, line: job.rapidLine };

    this.layers.cut = this.lines(this.shown.cut.pos, this.cutColor(), new THREE.LineBasicMaterial({ vertexColors: true }));
    this.layers.rapid = this.lines(this.shown.rapid.pos, null, new THREE.LineBasicMaterial({ color: this.col.rapid }));
    this.layers.cut.visible = this.options.toolpath;
    this.layers.rapid.visible = this.options.rapids;
    this.world.add(this.layers.cut, this.layers.rapid);
  }

  buildScan() {
    this.dispose(this.layers.surface);
    this.layers.surface = null;
    this.legend.hidden = !this.scan;
    if (!this.scan) return;

    // A scan still being made (partial) has points without a height yet: those show as grey dots at the
    // lowest height so far, and only the cells with all four corners measured get a surface
    const { xs, ys, at, zmin } = this.scan;
    const nx = xs.length, ny = ys.length;
    const has = (i, j) => Number.isFinite(at(i, j));
    const pos = new Float32Array(nx * ny * 3);
    const { all: col, done: doneCol } = this.scanColors();
    const done = [], todo = [];
    for (let i = 0; i < nx; i++) {
      for (let j = 0; j < ny; j++) {
        const k = (i * ny + j) * 3, z = has(i, j) ? at(i, j) : zmin;
        pos[k] = xs[i]; pos[k + 1] = ys[j]; pos[k + 2] = z;
        if (has(i, j)) done.push(xs[i], ys[j], z); else todo.push(xs[i], ys[j], z);
      }
    }
    const index = [];
    for (let i = 0; i < nx - 1; i++) {
      for (let j = 0; j < ny - 1; j++) {
        if (!(has(i, j) && has(i + 1, j) && has(i, j + 1) && has(i + 1, j + 1))) continue;
        const a = i * ny + j, b = (i + 1) * ny + j;
        index.push(a, b, a + 1, b, b + 1, a + 1);
      }
    }
    const group = new THREE.Group();
    if (index.length) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
      geo.setAttribute('color', new THREE.BufferAttribute(col, 3));
      geo.setIndex(index);
      group.add(new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
        vertexColors: true, transparent: true, opacity: 0.45, side: THREE.DoubleSide, depthWrite: false,
      })));
    }
    const points = (arr, colors, material) => {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(arr), 3));
      if (colors) geo.setAttribute('color', new THREE.BufferAttribute(new Float32Array(colors), 3));
      group.add(new THREE.Points(geo, material));
    };
    if (done.length) points(done, doneCol, new THREE.PointsMaterial({ vertexColors: true, size: 5, sizeAttenuation: false }));
    if (todo.length) points(todo, null, new THREE.PointsMaterial({ color: this.col.todo, size: 3, sizeAttenuation: false }));
    group.visible = this.options.scan;
    this.layers.surface = group;
    this.world.add(group);
  }

  /** A scan the leveled toolpath can use: complete, at least 2 x 2 points. */
  scanUsable() { return !!this.scan && !this.scan.partial && this.scan.xs.length > 1 && this.scan.ys.length > 1; }

  /** Highlights the moves of one file line (1-based), or of the lines line to last; 0 clears it. */
  highlight(line, last = line) {
    this.line = line;
    this.lastLine = last;
    this.dispose(this.layers.hl);
    this.layers.hl = null;
    if (!line || !this.shown.cut) { this.render(); return; }

    const pts = [];
    let end = null;
    for (const part of [this.shown.cut, this.shown.rapid]) {
      const { pos, line: ln } = part;
      for (let s = 0; s < ln.length; s++) {
        if (ln[s] < line || ln[s] > last) continue;
        const i = s * 6;
        pts.push(pos[i], pos[i + 1], pos[i + 2], pos[i + 3], pos[i + 4], pos[i + 5]);
        end = [pos[i + 3], pos[i + 4], pos[i + 5]];
      }
    }
    if (pts.length) {
      const group = new THREE.Group();
      const segs = this.lines(new Float32Array(pts), null,
        new THREE.LineBasicMaterial({ color: this.col.hl, depthTest: false }));
      const dotGeo = new THREE.BufferGeometry();
      dotGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(end), 3));
      const dot = new THREE.Points(dotGeo, new THREE.PointsMaterial({
        color: this.col.hl, size: 9, sizeAttenuation: false, depthTest: false,
      }));
      segs.renderOrder = dot.renderOrder = 10;
      group.add(segs, dot);
      this.layers.hl = group;
      this.world.add(group);
    }
    this.render();
  }

  // ---------------------------------------------------------------- tool marker

  /** Shows the tool at a work position in inches (the tip of a cone pointing down). setTool(null) hides it. */
  setTool(x, y, z) {
    if (x == null || !Number.isFinite(x + y + z)) {
      if (this.tool?.visible) { this.tool.visible = false; this.render(); }
      return;
    }
    if (!this.tool) this.tool = this.buildTool();
    const at = this.toolAt;
    if (this.tool.visible && at[0] === x && at[1] === y && at[2] === z) return;
    this.toolAt = [x, y, z];
    this.tool.visible = true;
    this.render();
  }

  buildTool() {
    const geo = new THREE.ConeGeometry(0.28, 1, 24);    // 1 tall; scaled each frame
    geo.rotateX(-Math.PI / 2);                          // point down
    geo.translate(0, 0, 0.5);                           // tip at the origin
    geo.setAttribute('color', new THREE.BufferAttribute(this.toolColors(geo), 3));
    const tool = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
      vertexColors: true, transparent: true, opacity: 0.9, depthTest: false,
    }));
    tool.renderOrder = 20;
    tool.visible = false;
    this.scene.add(tool);     // not in this.world: the Z scale moves the tool but must not stretch it
    return tool;
  }

  /** Shaded from a fixed light direction, so it looks round without lights in the scene. */
  toolColors(geo) {
    const normal = geo.getAttribute('normal'), light = new THREE.Vector3(-0.4, -0.6, 0.7).normalize();
    const col = new Float32Array(normal.count * 3), n = new THREE.Vector3(), c = new THREE.Color();
    for (let i = 0; i < normal.count; i++) {
      c.copy(this.col.tool).multiplyScalar(0.45 + 0.55 * Math.max(0, n.fromBufferAttribute(normal, i).dot(light)));
      col[i * 3] = c.r; col[i * 3 + 1] = c.g; col[i * 3 + 2] = c.b;
    }
    return col;
  }

  placeTool() {
    const [x, y, z] = this.toolAt;
    this.tool.position.set(x, y, z * this.world.scale.z);
    this.tool.scale.setScalar(Math.max(1e-6, this.camera.position.distanceTo(this.tool.position) * TOOL_SIZE));
  }

  // ---------------------------------------------------------------- machine travel

  /** The machine's travel in work coordinates (inches): { min: [x, y, z], max: [x, y, z], zero: [x, y] } (zero: the
   *  X and Y of the machine's 0 sides, where the walls go), or null to hide it. Drawn as very transparent black
   *  rectangles: the floor (the bottom of Z's travel) and the two walls on the machine's 0 sides. */
  setTravel(box) {
    const key = box ? JSON.stringify([box.min, box.max, box.zero].map(v => v.map(n => +n.toFixed(4)))) : '';
    if (key === this.travelKey) return;
    this.travelKey = key;
    this.travelBox = box;
    this.buildTravel();
    this.render();
  }

  buildTravel() {
    if (this.travel) {
      this.scene.remove(this.travel);
      this.travel.traverse(o => { o.geometry?.dispose(); o.material?.dispose(); });
      this.travel = null;
    }
    const box = this.travelBox;
    if (!box) return;
    const s = this.world.scale.z;                  // (in the scene, like the tool: the Z stretch moves it, no more)
    const [x0, y0, z0] = box.min, [x1, y1, z1] = box.max, [xw, yw] = box.zero;
    const lo = z0 * s, hi = z1 * s;
    const fill = new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.1, side: THREE.DoubleSide,
      depthWrite: false });
    const edge = new THREE.LineBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.35 });
    const group = new THREE.Group();
    for (const corners of [
      [[x0, y0, lo], [x1, y0, lo], [x1, y1, lo], [x0, y1, lo]],        // the floor
      [[xw, y0, lo], [xw, y1, lo], [xw, y1, hi], [xw, y0, hi]],        // the wall on X's 0 side
      [[x0, yw, lo], [x1, yw, lo], [x1, yw, hi], [x0, yw, hi]],        // the wall on Y's 0 side
    ]) {
      const pts = corners.map(p => new THREE.Vector3(...p));
      const geo = new THREE.BufferGeometry().setFromPoints([pts[0], pts[1], pts[2], pts[0], pts[2], pts[3]]);
      group.add(new THREE.Mesh(geo, fill));
      group.add(new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts), edge));
    }
    group.renderOrder = -1;                        // (under the toolpath and the scan)
    this.travel = group;
    this.scene.add(group);
  }

  // ---------------------------------------------------------------- data and options

  setJob(job, fit = true) {
    this.job = job;
    this.buildJob();
    this.highlight(this.line, this.lastLine);
    this.buildFloor();
    if (fit) this.view('iso'); else this.render();
  }

  /** The program shown is a leveled one (auto-level): rect { x0, x1, y0, y1 } (inches), the scan's area, drawn as
   *  a rectangle, with the moves outside it in another color; null: not leveled. The "Leveled" preview (the scan
   *  heights added to the toolpath) is off meanwhile: the program has them already. */
  setLeveledProgram(rect) {
    const same = (a, b) => (!a && !b) || (a && b && a.x0 === b.x0 && a.x1 === b.x1 && a.y0 === b.y0 && a.y1 === b.y1);
    if (same(rect, this.levRect)) return;
    this.levRect = rect || null;
    if (this.levRect) this.options.leveled = false;
    this.dispose(this.layers.rect);
    this.layers.rect = null;
    if (this.levRect) {
      const { x0, x1, y0, y1 } = this.levRect;
      const geo = new THREE.BufferGeometry().setFromPoints([[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map(([x, y]) => new THREE.Vector3(x, y, 0)));
      const loop = new THREE.LineLoop(geo, new THREE.LineDashedMaterial({ color: this.col.rect, dashSize: 0.15, gapSize: 0.1 }));
      loop.computeLineDistances();
      this.layers.rect = loop;
      this.world.add(loop);
    }
    this.buildJob();
    this.highlight(this.line, this.lastLine);
    this.syncBar();
    this.render();
  }

  /** fit: frame the view on it (a new scan's first points). */
  setScan(scan, fit = false) {
    const leveled = this.options.leveled;
    this.scan = scan;
    if (!this.scanUsable() && this.options.leveled) this.options.leveled = false;
    this.buildScan();
    // The toolpath depends on the scan only when it is shown leveled (a scan being made comes point by point:
    // a big toolpath is not rebuilt for each)
    if (this.job && (leveled || this.options.leveled)) {
      this.buildJob();
      this.highlight(this.line, this.lastLine);
    }
    this.syncBar();
    if (fit) this.view('iso'); else this.render();
  }

  /** why: the loaded G-code is a saved leveled one (the server's preLeveled), or '': the leveled view would add the
   *  scan's heights a second time, so it is off meanwhile. */
  setPreLeveled(why) {
    why = why || '';
    if (why === this.preLeveled) return;
    this.preLeveled = why;
    if (why && this.options.leveled) this.setOption('leveled', false);
    this.syncBar();
  }

  setOption(key, value) {
    if (key === 'leveled' && value && this.preLeveled) {
      value = false;
      this.onMessage?.(`This is ${this.preLeveled.replace(/^it is /, '')}: the toolpath shown has the scan's heights in it already.`);
    } else if (key === 'leveled' && value && this.levRect) {
      value = false;
      this.onMessage?.('The G-code is leveled (Auto-level): the toolpath shown has the scan\'s heights in it already.');
    } else if (key === 'leveled' && value && !this.scanUsable()) {
      value = false;
      this.onMessage?.(this.scan ? 'The leveled toolpath needs a complete scan of at least 2 × 2 points.'
        : 'Load a scan (.xyz) to see the leveled toolpath.');
    }
    this.options[key] = value;
    if (key === 'toolpath' && this.layers.cut) this.layers.cut.visible = value;
    if (key === 'rapids' && this.layers.rapid) this.layers.rapid.visible = value;
    if (key === 'scan' && this.layers.surface) this.layers.surface.visible = value;
    if (key === 'leveled') { this.buildJob(); this.highlight(this.line, this.lastLine); }
    if (key === 'zscale') { this.world.scale.z = value; this.buildTravel(); }
    this.syncBar();
    this.render();
  }

  syncBar() {
    for (const input of this.bar.querySelectorAll('[data-opt]')) {
      const v = this.options[input.dataset.opt];
      if (input.type === 'checkbox') input.checked = v; else input.value = v;
    }
    this.bar.querySelector('.zs').textContent = this.options.zscale + '×';
  }

  // ---------------------------------------------------------------- camera

  bounds() {
    const box = new THREE.Box3();
    if (this.job) box.union(new THREE.Box3(new THREE.Vector3(...this.job.lo), new THREE.Vector3(...this.job.hi)));
    if (this.scan) {
      box.expandByPoint(new THREE.Vector3(this.scan.xs[0], this.scan.ys[0], this.scan.zmin));
      box.expandByPoint(new THREE.Vector3(this.scan.xs.at(-1), this.scan.ys.at(-1), this.scan.zmax));
    }
    if (this.levRect) {
      box.expandByPoint(new THREE.Vector3(this.levRect.x0, this.levRect.y0, 0));
      box.expandByPoint(new THREE.Vector3(this.levRect.x1, this.levRect.y1, 0));
    }
    if (box.isEmpty()) {           // nothing loaded: an area around the work origin, and the tool if it is shown
      box.set(new THREE.Vector3(-1, -1, -1), new THREE.Vector3(9, 9, 1));
      if (this.tool?.visible) box.expandByPoint(new THREE.Vector3(...this.toolAt));
    }
    box.min.z *= this.world.scale.z;
    box.max.z *= this.world.scale.z;
    return box;
  }

  buildFloor() {
    this.dispose(this.floor);
    const b = this.bounds();
    const size = Math.ceil(Math.max(b.max.x, b.max.y, 10)) + 1;
    this.floor = new THREE.GridHelper(size * 2, size * 2, this.col.gridAxis, this.col.grid);
    this.floor.rotation.x = Math.PI / 2;
    this.floor.material.depthWrite = false;
    this.scene.add(this.floor);
  }

  view(name) {
    const b = this.bounds(), center = b.getCenter(new THREE.Vector3()), size = b.getSize(new THREE.Vector3());
    const r = Math.max(size.x, size.y, size.z, 1) * 0.6;
    const dist = r / Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2));
    const d = new THREE.Vector3(...(VIEWS[name] || VIEWS.iso)).normalize();
    this.camera.position.copy(center).addScaledVector(d, dist);
    this.controls.target.copy(center);
    this.camera.near = dist / 200;
    this.camera.far = dist * 20;
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.render();
  }
}
