// The Scan panel: the surface scan with the Z probe ("the nano", an Arduino Leonardo with its own Z axis).
//
// The probe's port is opened only by its Connect button. Home Probe sends the probe home (H, as UGS's
// "Home Probe"); its first touch after that reads 0.0 and every later one is relative to it. Start Scan
// (it asks first) has the server move the machine over the grid (X and Y only: the CNC's Z never moves)
// and probe each point; the points show in the visualizer as they come. The finished scan is kept by the
// server (scans/last-scan.xyz); Save Scan… writes it where you like, in the UGS .xyz format.
//
// A probe that is connected but not homed (a scan Stop or an abort leaves it so) may have its tip down: the
// panel says so and Home Probe is highlighted. No key presses Start Scan or Home Probe (mouseOnly). While
// the panel is hidden behind another tab it is not redrawn (the status comes 5 times a second): shown()
// brings it up to date.
//
// Its default place is the bottom row, left of the Console: wide and short. So its content is laid out in
// columns that wrap (Probe | Grid | Scan buttons + progress | Auto-level): side by side there, every control
// shows without scrolling; in a tall narrow panel they stack.
//
// Auto-level (the last column): the server levels the loaded G-code with the last complete scan of this
// session or a .xyz file (as the user's UGS does it, see leveler.py), and from then on the leveled program is
// what Run (and the pendant's Run) runs, and what the editor and the visualizer show (app.js). Leveling off
// goes back to the original. Opening or changing the G-code, a new scan, or connecting to another FluidNC
// turns it off (the server says so in the Console); it is never done again by itself.

import { store, baseName } from './api.js';
import { jobActive } from './machine.js';
import { mouseOnly } from './twostep.js';
import { probeMayBeDown } from './jog.js';

// (a first run: no area, Min / Max all 0, which Start Scan refuses, and no offset, which warns. The offset is the
// spindle's distance from the probe: the user's is Y 3, and the probe touches at the bit's position minus it)
const DEFAULTS = { minX: 0, minY: 0, maxX: 0, maxY: 0, xPoints: 15, yPoints: 5, zRetract: 0.2, probeOffX: 0, probeOffY: 0 };
export const areaUnset = s => !!s && ['minX', 'maxX', 'minY', 'maxY'].every(k => num(s[k]) === 0);
const KEYS = Object.keys(DEFAULTS);
const LABEL = { minX: 'Min X', minY: 'Min Y', maxX: 'Max X', maxY: 'Max Y', xPoints: 'Points X', yPoints: 'Points Y', zRetract: 'Z retract',
  probeOffX: 'Probe offset X', probeOffY: 'Probe offset Y' };
const MAX_POINTS = 100, MAX_TOTAL = 2000, MAX_COORD = 100, MAX_OFFSET = 12;     // (the server's limits)
const SAVE_SETTINGS_MS = 600;     // settings typed here are saved on the server this long after the last change
// Ports the probe is never on (the server refuses them): FluidNC's kind (USB serial) and the pendant's (Bluetooth)
const NOT_PROBE = { 'usb-serial': 'FluidNC\'s kind of port', bluetooth: 'the pendant\'s kind of port' };
const PHASE = {      // the scan's phase, as the progress line says it
  starting: 'starting', moving: 'moving to point', probing: 'probing point', homing: 'homing the probe',
  returning: 'back to the first point', stopping: 'stopping',
};

// Auto-level settings (settings.json "level" on the server; the user's UGS: Z surface 0; its 0.01 mm arc segments
// flood the serial line on arcs, so 0.1 here; CarveMaker writes G1 only)
const LEVEL_DEFAULTS = { zSurface: 0, arcSegmentMm: 0.1 };
const LEVEL_KEYS = Object.keys(LEVEL_DEFAULTS);
const ARC_MIN = 0.01, ARC_MAX = 2, Z_SURFACE_MAX = 10;
const PICK = 'pick';          // the source select's "a .xyz file…" (opens the file dialog)

const num = v => (typeof v === 'number' ? v : Number(String(v).trim()));
const int = n => (Number.isFinite(n) ? Math.round(n).toLocaleString() : '?');
// (written only when they change: the panel is redrawn on every status report)
const setText = (node, text) => { if (node.textContent !== text) node.textContent = text; };
const setAttr = (node, key, value) => { if (node[key] !== value) node[key] = value; };
const fmt = v => (Number.isFinite(v) ? String(+v.toFixed(4)) : '');

/** Settings as numbers, or { error } for the first thing wrong with them (the server's rules). */
export function checkSettings(s) {
  const v = Object.fromEntries(KEYS.map(k => [k, num(s[k])]));
  for (const k of KEYS) if (s[k] === '' || !Number.isFinite(v[k])) return { error: `${LABEL[k]} is not a number`, key: k };
  for (const k of ['minX', 'maxX', 'minY', 'maxY']) {
    if (Math.abs(v[k]) > MAX_COORD) return { error: `${LABEL[k]}: from -${MAX_COORD} to ${MAX_COORD} in`, key: k };
  }
  for (const a of ['x', 'y']) {
    const n = v[`${a}Points`], A = a.toUpperCase();
    if (!Number.isInteger(n) || n < 1 || n > MAX_POINTS) return { error: `Points ${A}: a whole number from 1 to ${MAX_POINTS}`, key: `${a}Points` };
    const lo = v[`min${A}`], hi = v[`max${A}`];
    if (areaUnset(v)) continue;          // (all 0: not set yet; Start Scan says so)
    if (lo > hi) return { error: `Min ${A} is more than Max ${A}`, key: `min${A}` };
    if (lo === hi && n > 1) return { error: `Min ${A} and Max ${A} are the same: that takes Points ${A} = 1`, key: `max${A}` };
  }
  if (v.xPoints * v.yPoints > MAX_TOTAL) return { error: `${v.xPoints * v.yPoints} points: at most ${MAX_TOTAL}`, key: 'xPoints' };
  if (v.zRetract < 0.01 || v.zRetract > 2) return { error: 'Z retract: from 0.01 to 2 in', key: 'zRetract' };
  for (const k of ['probeOffX', 'probeOffY']) {
    if (Math.abs(v[k]) > MAX_OFFSET) return { error: `${LABEL[k]}: from -${MAX_OFFSET} to ${MAX_OFFSET} in`, key: k };
  }
  return { settings: v };
}

/** The points measured so far, [[x, y, z], ...], from the server's scan state, however it lists them. */
export function scanPoints(scan) {
  const list = Array.isArray(scan?.points) ? scan.points : Array.isArray(scan?.result) ? scan.result
    : Array.isArray(scan?.result?.points) ? scan.result.points : [];
  const out = [];
  for (const p of list) {
    const q = Array.isArray(p) ? p : p && typeof p === 'object' ? [p.x, p.y, p.z] : null;
    if (q && Number.isFinite(q[0]) && Number.isFinite(q[1]) && Number.isFinite(q[2])) out.push([q[0], q[1], q[2]]);
  }
  return out;
}

/** Auto-level settings as numbers, or { error, key } (the server's rules: the arc segment 0.01 to 2 mm). */
export function checkLevelSettings(s) {
  const v = Object.fromEntries(LEVEL_KEYS.map(k => [k, num(s[k])]));
  if (s.zSurface === '' || !Number.isFinite(v.zSurface) || Math.abs(v.zSurface) > Z_SURFACE_MAX) {
    return { error: `Z surface: a number from -${Z_SURFACE_MAX} to ${Z_SURFACE_MAX} in`, key: 'zSurface' };
  }
  if (s.arcSegmentMm === '' || !Number.isFinite(v.arcSegmentMm) || v.arcSegmentMm < ARC_MIN || v.arcSegmentMm > ARC_MAX) {
    return { error: `Arc segment: from ${ARC_MIN} to ${ARC_MAX} mm`, key: 'arcSegmentMm' };
  }
  return { settings: v };
}

/** The leveled program's info as the server gives it ({ scan, time, zSurface, arcSegmentMm, stats, warnings }),
 *  in a few words: "last-scan.xyz (15 × 5 points, Z surface 0)". */
export function levelName(info) {
  if (!info) return '';
  const obj = info.scan && typeof info.scan === 'object' ? info.scan : null;
  const sc = info.stats?.scan || obj || {};
  const raw = String(info.scanName || (typeof info.scan === 'string' ? info.scan : obj?.name || obj?.path) || '');
  const name = !raw ? 'the scan' : raw === 'last' ? 'the last scan' : baseName(raw);
  const pts = Number.isFinite(sc.xPoints) && Number.isFinite(sc.yPoints) ? `${sc.xPoints} × ${sc.yPoints} points, ` : '';
  const zs = Number.isFinite(info.zSurface) ? `Z surface ${+info.zSurface.toFixed(4)}` : '';
  return `${name}${pts || zs ? ` (${pts}${zs})` : ''}`.replace(', )', ')');
}

/** The coverage warning of a leveling (some of its points are outside the scan: the edge heights are used
 *  there): "5,038 of 55,265 points outside the scan, up to 0.397 in", or ''. */
export function levelCoverage(info) {
  const s = info?.stats;
  if (!s || !(s.outside > 0)) return '';
  const u = (s.scan?.units || 'INCH') === 'MM' ? 'mm' : 'in';
  return `${int(s.outside)} of ${int(s.leveled)} points outside the scan, up to ${Number.isFinite(s.maxOutside) ? s.maxOutside.toFixed(3) : '?'} ${u} away`
    + ' (the nearest edge\'s height is used there)';
}

/** The scan has every point (the server's result says so, or all points are in). */
export function scanDone(scan) {
  if (!scan || scan.running) return false;
  if (typeof scan.result?.complete === 'boolean') return scan.result.complete;
  const n = scanPoints(scan).length;
  return n > 0 && n >= scanTotal(scan);
}

/** The scan's grid lines { xs, ys } as the server made them (result.xs / ys), else null. */
export function scanResultGrid(scan) {
  const r = scan?.result;
  return r && Array.isArray(r.xs) && Array.isArray(r.ys) && r.xs.length && r.ys.length ? { xs: r.xs, ys: r.ys } : null;
}

/** How many points the scan has in all. */
export function scanTotal(scan) {
  if (Number.isFinite(scan?.points)) return scan.points;
  const s = scan?.settings;
  return s && Number.isFinite(s.xPoints * s.yPoints) ? s.xPoints * s.yPoints : 0;
}

export class ScanPanel {
  /**
   * machine: the MachineClient. log(text, kind): app messages. area(): the loaded G-code's X / Y bounds
   * { lo: [x, y], hi: [x, y] } or null. onStart(settings): starts the scan (the app asks first).
   * onSave(): Save Scan…. Auto-level (app.js does them): onLevel({ scan: 'last' | { path }, zSurface,
   * arcSegmentMm }), onLevelOff(), onSaveLeveled() (Save leveled G-code…), onPickScan() (a .xyz file…: the
   * app's file dialog, which calls levelFile(path)).
   */
  constructor(machine, { log, area, onStart, onSave, onLevel, onLevelOff, onSaveLeveled, onPickScan } = {}) {
    this.machine = machine;
    this.log = log;
    this.area = area;
    this.onStart = onStart;
    this.onSave = onSave;
    this.onLevel = onLevel;
    this.onLevelOff = onLevelOff;
    this.onSaveLeveled = onSaveLeveled;
    this.onPickScan = onPickScan;
    this.m = {};
    this.ports = [];
    this.busy = '';            // a probe call waiting for its answer: 'connect' | 'disconnect' | 'home'
    this.msg = null;           // { text, cls }: the last thing a button here did or failed at
    this.scanFile = store.get('level.file', '');    // the .xyz chosen for leveling ('' none yet)
    this.source = store.get('level.source', 'last');  // 'last' | 'file'
    this.root = document.createElement('div');
    this.root.className = 'scan pad';
    const box = (attrs) => `<input ${attrs} type="text" inputmode="decimal" spellcheck="false" autocomplete="off">`;
    this.root.innerHTML = `<div class="scols">
      <section class="scol sc-probe">
        <div class="shead"><span>Probe</span>
          <button data-act="ports" class="sicon" title="Look for serial ports again (opens none)">⟳</button></div>
        <select id="scanPort" data-f="port" aria-label="The probe's serial port"></select>
        <button data-act="pconn">Connect</button>
        <button data-act="home" title="Home the probe (H): it retracts fully. Its first touch after that reads 0.0.">Home Probe</button>
        <div class="sretract soffset" title="How far the spindle centerline is from the probe (yours: X 0, Y 3). Saved. The probe touches at the bit's position minus this, so the bit goes to each point plus it. Min / Max are the job's own area, and the scan is saved there: the probe data is shifted, never the G-code. Leave X/Y zero where the job is zeroed.">
          <label for="scanOffY">Offset from spindle</label><span class="dim">X</span>${box('id="scanOffX" data-k="probeOffX" aria-label="Probe offset X"')}<span class="dim">Y</span>${box('id="scanOffY" data-k="probeOffY" aria-label="Probe offset Y"')}<span class="dim">in</span>
        </div>
        <div class="soffwarn amber" hidden>Offset 0, 0: set it (yours: Y 3)</div>
        <div class="spstate dim"></div>
        <div class="sdown amber" hidden></div>
      </section>
      <section class="scol sc-grid">
        <div class="sgrid">
          <span></span><b>Min</b><b>Max</b><b>Points</b>
          <span>X</span>${['minX', 'maxX', 'xPoints'].map(k => box(`data-k="${k}" aria-label="${LABEL[k]}"`)).join('')}
          <span>Y</span>${['minY', 'maxY', 'yPoints'].map(k => box(`data-k="${k}" aria-label="${LABEL[k]}"`)).join('')}
        </div>
        <div class="sretract">
          <label for="scanRetract">Z retract</label>${box('id="scanRetract" data-k="zRetract"')}<span class="dim">in</span>
        </div>
        <button data-act="area" title="Min and Max X / Y from the G-code that is open">Use G-code area</button>
        <div class="scheck dim"></div>
      </section>
      <section class="scol sc-run">
        <div class="shead"><span>Scan</span></div>
        <button data-act="start" class="primary">Start Scan</button>
        <button data-act="stop">Stop</button>
        <button data-act="save">Save Scan…</button>
        <div class="sprog" hidden></div>
        <div class="sbar" hidden><i></i></div>
        <div class="smsg" hidden></div>
      </section>
      <section class="scol sc-level">
        <div class="shead"><label for="levelSrc">Apply to G-code</label></div>
        <select id="levelSrc" data-f="lsrc" title="The scan the G-code is leveled with"></select>
        <div class="lgrid">
          <label for="levelZ" title="The Z of the G-code's surface: its Z 0 (the user's UGS: 0)">Z surface</label>
          ${box('id="levelZ" data-l="zSurface"')}<span class="dim">in</span>
          <label for="levelArc" title="Arcs (G2 / G3) are cut into straight lines this long, so they follow the surface">Arc segment</label>
          ${box('id="levelArc" data-l="arcSegmentMm"')}<span class="dim">mm</span>
        </div>
        <!-- one box: ticked = the G-code is leveled (it blinks red then); a click turns it on or off -->
        <div class="lbtns"><button data-act="apply" class="lapply" aria-pressed="false"><i></i><span>Apply</span></button></div>
        <div class="lmsg dim" hidden></div>
        <button data-act="lsave">Save leveled G-code…</button>
      </section>
    </div>`;
    const q = s => this.root.querySelector(s);
    this.cols = q('.scols');
    this.portSel = q('[data-f=port]');
    this.pstate = q('.spstate');
    this.downEl = q('.sdown');
    this.inputs = Object.fromEntries([...this.root.querySelectorAll('[data-k]')].map(i => [i.dataset.k, i]));
    this.checkEl = q('.scheck');
    this.progEl = q('.sprog');
    this.bar = q('.sbar i');
    this.msgEl = q('.smsg');
    this.offWarn = q('.soffwarn');
    this.srcSel = q('[data-f=lsrc]');
    this.lInputs = Object.fromEntries([...this.root.querySelectorAll('[data-l]')].map(i => [i.dataset.l, i]));
    this.lmsgEl = q('.lmsg');
    this.btn = Object.fromEntries([...this.root.querySelectorAll('[data-act]')].map(b => [b.dataset.act, b]));
    mouseOnly(this.btn.start);       // (they move the machine, or the probe)
    mouseOnly(this.btn.home);
    this.btn.ports.onclick = () => this.listPorts();
    this.btn.pconn.onclick = () => (this.m.probe?.connected ? this.disconnect() : this.connect());
    this.btn.home.onclick = () => this.home();
    this.btn.area.onclick = () => this.useArea();
    this.btn.start.onclick = () => this.start();
    this.btn.stop.onclick = () => this.stop();
    this.btn.save.onclick = () => this.onSave?.();
    this.btn.apply.onclick = () => (this.m?.lev?.active ? this.onLevelOff?.() : this.level());
    this.btn.lsave.onclick = () => this.onSaveLeveled?.();
    this.portSel.onchange = () => this.show();
    this.srcSel.onchange = () => this.sourceChosen();
    for (const [k, input] of Object.entries(this.inputs)) {
      input.oninput = () => this.edited(k);
      input.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); this.edited(k, true); } };
      input.onblur = () => this.showCheck(checkSettings(this.typed()));     // (a mistyped value stays, marked)
    }
    for (const input of Object.values(this.lInputs)) {
      input.oninput = () => this.levelEdited();
      input.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); this.levelEdited(); } };
    }
    this.settings = { ...DEFAULTS, ...store.get('scan.settings', {}) };
    this.levelSettings = { ...LEVEL_DEFAULTS, ...store.get('level.settings', {}) };
    this.showSettings(true);
    this.showLevelSettings(true);
    this.update({});
  }

  /** The narrowest width (px) that keeps the four columns side by side: their flex bases, the gaps and the
   *  padding (from the style sheet). */
  neededWidth() {
    const cols = [...this.cols.children];
    if (!this.root.isConnected) return 560;
    const css = getComputedStyle(this.cols), root = getComputedStyle(this.root);
    const sum = cols.reduce((n, c) => n + (parseFloat(getComputedStyle(c).flexBasis) || c.offsetWidth), 0);
    return Math.ceil(sum + (parseFloat(css.columnGap) || 0) * (cols.length - 1)
      + parseFloat(root.paddingLeft) + parseFloat(root.paddingRight) + 2);
  }

  /** The height (px) its content takes at this width, nothing scrolled (for the default layout). Measured
   *  out of the flow, with the texts the columns show at their longest: a scan running (its progress) or done
   *  (what it came to), and a leveling's result (two lines). */
  neededHeight(width) {
    if (!this.root.isConnected) return 175;
    const style = this.root.style, old = style.cssText;
    style.cssText += `;position:absolute;height:auto;width:${Math.round(width)}px;overflow:visible`;
    const bar = this.bar.parentElement;
    const keep = [this.progEl, this.msgEl, this.lmsgEl, bar].map(e => [e, e.hidden, [...e.childNodes]]);
    const line = (cls, text) => Object.assign(document.createElement('div'), { className: cls, textContent: text });
    this.lmsgEl.replaceChildren(line('good', 'Leveled: 55,287 → 57,402 lines · 2 warnings'), line('bad', '5,038 points outside the scan (up to 0.397 in)'));
    this.lmsgEl.hidden = false;
    let h = 0;
    for (const running of [true, false]) {
      this.progEl.hidden = bar.hidden = !running;
      this.msgEl.hidden = running;
      this.progEl.textContent = 'Scanning: probing point 75 of 75\nlast Z −0.0123';
      this.msgEl.textContent = 'Last scan: 75 points · height range 0.0123 in · saved to /home/coffee/scans/scan.xyz';
      h = Math.max(h, this.root.scrollHeight);
    }
    for (const [e, hidden, nodes] of keep) { e.hidden = hidden; e.replaceChildren(...nodes); }
    style.cssText = old;
    this.lmsgKey = null;            // (redrawn next time)
    return h || 175;
  }

  /** The panel was shown: up to date again, and the ports listed once (that opens nothing). */
  shown() {
    this.show();
    if (!this.listed) this.listPorts();
  }

  // ---------------------------------------------------------------- probe

  async listPorts() {
    this.listed = true;
    this.portSel.disabled = true;
    this.portSel.innerHTML = '<option>Looking for serial ports…</option>';
    let data;
    try {
      data = await this.machine.probePorts();
    } catch (e) {
      this.portSel.innerHTML = '<option value="">(could not list the ports)</option>';
      this.say(e.status === 404 ? 'This FluidSender server has no probe support yet (restart FluidSender after the update)' : `Ports: ${e.message}`, 'bad');
      this.listed = false;
      this.show();
      return;
    }
    this.ports = data.ports || [];
    const connected = this.m.probe?.connected ? this.m.probe.port : null;
    this.portSel.innerHTML = this.ports.length ? '' : '<option value="">(no serial ports found)</option>';
    for (const p of this.ports) {
      const o = document.createElement('option');
      o.value = p.path;
      const mine = p.path === connected;
      o.textContent = `${p.path} — ${p.label || p.product || p.kind}` + (NOT_PROBE[p.kind] ? `  (${NOT_PROBE[p.kind]}: not the probe)`
        : p.inUseBy && !mine ? `  (in use by ${p.inUseBy})` : '');
      o.disabled = (!!p.inUseBy && !mine) || !!NOT_PROBE[p.kind];
      this.portSel.append(o);
    }
    // The port it is on, else the last one used, else the server's pick, else a free Leonardo / ACM port
    const free = this.ports.filter(p => !p.inUseBy && !NOT_PROBE[p.kind]);
    const pick = this.ports.find(p => p.path === connected)
      || free.find(p => p.path === (store.get('probe.port') || this.m.probe?.port))
      || free.find(p => p.path === data.default) || free.find(p => p.kind === 'usb-acm');
    if (!pick && this.ports.length) this.portSel.prepend(Object.assign(document.createElement('option'), { value: '', textContent: 'Choose a port…', disabled: true }));
    this.portSel.value = pick ? pick.path : '';
    this.portSel.disabled = false;
    this.show();
  }

  selected() {
    return this.ports.find(p => p.path === this.portSel.value && !NOT_PROBE[p.kind] && (!p.inUseBy || p.path === this.m.probe?.port));
  }

  async connect() {
    const p = this.selected();
    if (!p || this.busy) return;
    // A USB ACM port that is not a Leonardo: the server opens it only when told to (force)
    let force = false;
    if (p.kind === 'usb-acm' && !/leonardo/i.test(`${p.label} ${p.product}`)) {
      if (!confirm(`${p.path} (${p.label || p.product || 'USB serial'}) does not look like the probe.\n\nThe probe is an Arduino Leonardo (usually /dev/ttyACM0). Connect the probe to ${p.path} anyway?`)) return;
      force = true;
    }
    store.set('probe.port', p.path);
    await this.call('connect', `Probe: connecting to ${p.path}…`, () => this.machine.probeConnect(p.path, force), `Probe connected on ${p.path}`);
    this.listPorts();
  }

  async disconnect() {
    await this.call('disconnect', 'Probe: disconnecting…', () => this.machine.probeDisconnect(), 'Probe disconnected');
    this.listPorts();
  }

  home() {
    return this.call('home', 'Homing the probe…', () => this.machine.probeHome(), 'Probe homed: its next touch reads 0.0');
  }

  /** One probe call: busy meanwhile, then what it came to (the server's probe event updates the state). */
  async call(what, during, fn, after) {
    if (this.busy) return false;
    this.busy = what;
    this.say(during, 'dim');
    let ok = true;
    try {
      const res = await fn();
      if (res?.probe) this.m.probe = res.probe;
      this.say(after, 'good');
    } catch (e) {
      ok = false;
      this.say(e.status === 404 ? 'This FluidSender server has no probe support yet (restart FluidSender after the update)' : e.message, 'bad');
      this.log?.(`Probe: ${e.message}`, 'err');
    } finally {
      this.busy = '';
      this.show();
    }
    return ok;
  }

  // ---------------------------------------------------------------- settings

  /** The settings in the boxes (strings as typed). */
  typed() { return Object.fromEntries(KEYS.map(k => [k, this.inputs[k].value])); }

  edited(key, now = false) {
    const c = checkSettings(this.typed());
    this.showCheck(c);
    clearTimeout(this.saveTimer);
    if (!c.settings) { this.show(); return; }
    this.settings = c.settings;
    store.set('scan.settings', c.settings);
    this.changedAt = performance.now();
    if (now) this.saveSettings(); else this.saveTimer = setTimeout(() => this.saveSettings(), SAVE_SETTINGS_MS);
    this.show();
  }

  saveSettings() {
    clearTimeout(this.saveTimer);
    if (this.noServer) return;
    Promise.resolve(this.machine.scanSettings(this.settings)).catch(e => {
      if (e.status === 404) this.noServer = true;
      else this.say(`Settings not saved: ${e.message}`, 'bad');
    });
  }

  /** Puts the settings into the boxes (all: also into the one being typed in). */
  showSettings(all = false) {
    for (const k of KEYS) {
      const input = this.inputs[k];
      if (!all && document.activeElement === input) continue;
      const v = this.settings[k];
      if (input.value.trim() === '' || num(input.value) !== v) input.value = k.endsWith('Points') ? String(Math.round(v)) : fmt(v);
    }
    this.showCheck(checkSettings(this.typed()));
  }

  /** Marks a wrong setting, and says what the settings make (or what is wrong with them). */
  showCheck(c) {
    for (const [k, input] of Object.entries(this.inputs)) input.classList.toggle('bad', c.key === k);
    this.valid = !!c.settings;
    let text = '', cls = 'dim';
    if (c.error) { text = c.error; cls = 'bad'; } else if (areaUnset(c.settings)) {
      text = 'Scan area not set: type Min / Max X and Y, or Use G-code area'; cls = 'amber';
    } else {
      const v = c.settings, step = (lo, hi, n) => (n > 1 ? (hi - lo) / (n - 1) : 0);
      text = `${v.xPoints * v.yPoints} points, every ${fmt(step(v.minX, v.maxX, v.xPoints))} in X, `
        + `${fmt(step(v.minY, v.maxY, v.yPoints))} in Y`;
    }
    setText(this.checkEl, text);
    setAttr(this.checkEl, 'className', 'scheck ' + cls);
    setAttr(this.checkEl, 'title', text);
  }

  useArea() {
    const a = this.area?.();
    if (!a) return;
    // Rounded outwards to 0.001 in, so the grid covers the whole toolpath
    const lo = v => Math.floor(v * 1000 + 1e-6) / 1000, hi = v => Math.ceil(v * 1000 - 1e-6) / 1000;
    Object.assign(this.inputs.minX, { value: fmt(lo(a.lo[0])) });
    Object.assign(this.inputs.maxX, { value: fmt(hi(a.hi[0])) });
    Object.assign(this.inputs.minY, { value: fmt(lo(a.lo[1])) });
    Object.assign(this.inputs.maxY, { value: fmt(hi(a.hi[1])) });
    this.edited('minX', true);
    this.say(`Area from the G-code: X ${fmt(lo(a.lo[0]))} … ${fmt(hi(a.hi[0]))}, Y ${fmt(lo(a.lo[1]))} … ${fmt(hi(a.hi[1]))} in`, 'dim');
  }

  // ---------------------------------------------------------------- auto-level

  typedLevel() { return Object.fromEntries(LEVEL_KEYS.map(k => [k, this.lInputs[k].value])); }

  levelEdited() {
    const c = checkLevelSettings(this.typedLevel());
    if (c.settings) {
      this.levelSettings = c.settings;
      store.set('level.settings', c.settings);
      this.levelChangedAt = performance.now();
    }
    this.show();
  }

  /** Puts the auto-level settings into their boxes (all: also into the one being typed in). */
  showLevelSettings(all = false) {
    for (const k of LEVEL_KEYS) {
      const input = this.lInputs[k];
      if (!all && document.activeElement === input) continue;
      const v = this.levelSettings[k];
      if (input.value.trim() === '' || num(input.value) !== v) input.value = fmt(v);
    }
  }

  /** The source select: "the last scan", the .xyz chosen, or "a .xyz file…" (which opens the file dialog). */
  sourceChosen() {
    const v = this.srcSel.value;
    if (v === PICK) {
      this.fillSources();             // (back to what was chosen until a file is picked)
      this.onPickScan?.();
      return;
    }
    this.source = v === 'file' ? 'file' : 'last';
    store.set('level.source', this.source);
    this.show();
  }

  /** A .xyz file was picked for leveling (the app's file dialog). */
  levelFile(path) {
    if (!path) return;
    this.scanFile = path;
    this.source = 'file';
    store.set('level.file', path);
    store.set('level.source', 'file');
    this.show();
  }

  /** The scan a Level click uses: 'last', { path }, or null (none usable now), and its name. */
  levelSource() {
    if (this.source === 'file' && this.scanFile) return { scan: { path: this.scanFile }, name: baseName(this.scanFile) };
    if (this.source !== 'file' && scanDone(this.m.scan)) return { scan: 'last', name: 'the last scan' };
    return null;
  }

  fillSources() {
    const done = scanDone(this.m.scan), sc = this.m.scan;
    const pts = done && sc?.settings ? ` (${sc.settings.xPoints} × ${sc.settings.yPoints})` : '';
    const opts = [['last', `the last scan${done ? pts : ' (none yet)'}`]];
    if (this.scanFile) opts.push(['file', baseName(this.scanFile)]);
    opts.push([PICK, 'a .xyz file…']);
    const key = JSON.stringify(opts);
    if (this.srcKey !== key) {
      this.srcKey = key;
      this.srcSel.replaceChildren(...opts.map(([value, text]) => Object.assign(document.createElement('option'), { value, textContent: text })));
    }
    const want = this.source === 'file' && this.scanFile ? 'file' : 'last';
    if (this.srcSel.value !== want) this.srcSel.value = want;
    this.srcSel.title = want === 'file' ? `Level the G-code with ${this.scanFile}` : done
      ? 'Level the G-code with the last scan made here (FluidSender keeps it as scans/last-scan.xyz)'
      : 'The last scan: none complete yet in this session. Scan first, or choose a .xyz file.';
  }

  /** Why Level can't go now ('' when it can). */
  levelWhy() {
    const m = this.m, lev = m.lev || {};
    if (jobActive(m.job)) return 'A job is running';
    if (m.scan?.running) return 'A surface scan is running';
    if (m.link !== 'up') return 'No link to the FluidSender server';
    if (lev.busy) return 'Leveling…';
    if (!m.prog || m.prog.state === 'none' || !m.prog.state) return 'Open a G-code file first';
    if (m.prog.state === 'error') return 'FluidSender refused the G-code (see the job line)';
    // (a saved leveled G-code: the server refuses it too; Apply would add the scan's heights twice)
    if (!lev.active && m.program?.preLeveled) return `Already leveled: ${m.program.preLeveled}. It runs as it is`;
    if (!this.levelSource()) return this.source === 'file' ? 'Choose a .xyz file' : 'No complete scan yet: scan first, or choose a .xyz file';
    const c = checkLevelSettings(this.typedLevel());
    return c.error || '';
  }

  level() {
    const why = this.levelWhy(), src = this.levelSource();
    if (why || !src) { this.show(); return; }
    const c = checkLevelSettings(this.typedLevel());
    this.onLevel?.({ scan: src.scan, zSurface: c.settings.zSurface, arcSegmentMm: c.settings.arcSegmentMm }, src.name);
  }

  /** The auto-level column: the source, the buttons, and what the leveling came to (lines in -> out, the
   *  warnings, the points outside the scan in red), or why it was refused. */
  showLevel() {
    const m = this.m, lev = m.lev || {}, active = jobActive(m.job), running = !!m.scan?.running;
    this.fillSources();
    const why = this.levelWhy();
    const b = this.btn;
    const on = !!lev.active;
    // Apply: one box. Off: a click levels the G-code with the chosen scan; on (blinking red): a click goes back
    const offWhy = active ? 'not while a job is running' : running ? 'not while scanning' : lev.busy ? 'busy' : '';
    const no = on ? offWhy : why;
    setAttr(b.apply, 'disabled', !!no);
    if (b.apply.getAttribute('aria-pressed') !== String(on)) b.apply.setAttribute('aria-pressed', String(on));
    setAttr(b.apply, 'title', on
      ? (no ? `Apply is ON: the G-code is leveled. Turning it off: ${no}`
        : 'Apply is ON: the G-code is leveled (Run runs the leveled program). Click to turn it off: back to the original G-code.')
      : (no ? `Apply: ${no}` : `Apply: level the G-code with ${this.levelSource()?.name || 'the scan'}: the leveled `
        + 'program replaces the loaded one (Run runs it, the editor shows it). Nothing moves.'));
    b.apply.classList.toggle('on', on);
    const label = lev.busy ? (lev.busy === 'off' ? 'Turning off…' : 'Applying…') : on ? 'Apply: ON' : 'Apply';
    if (b.apply.lastChild.textContent !== label) b.apply.lastChild.textContent = label;
    setAttr(b.lsave, 'disabled', !on || !lev.text);
    setAttr(b.lsave, 'title', on ? 'Save the leveled G-code as a file (default: <name>-autoleveled.gcode in the G-code\'s folder)'
      : 'Save leveled G-code…: once the G-code is leveled');
    for (const input of Object.values(this.lInputs)) setAttr(input, 'disabled', active || running);
    const c = checkLevelSettings(this.typedLevel());
    for (const [k, input] of Object.entries(this.lInputs)) input.classList.toggle('bad', c.key === k);
    // What it came to
    const lines = [];
    if (lev.busy) lines.push([lev.busy === 'off' ? 'Turning leveling off…' : 'Leveling…', 'dim']);
    else if (lev.error) lines.push([lev.error, 'bad']);
    else if (c.error) lines.push([c.error, 'bad']);
    else if (on) {
      // (two lines at most here: every warning is in the editor's banner, in Run's question and in this tooltip)
      // (lines to send: the original's, as loaded, to the leveled program's)
      const s = lev.info?.stats || {}, warnings = (lev.info?.warnings || []).filter(w => !/outside the scan/.test(w));
      const before = lev.own && Number.isFinite(m.prog?.lines) ? m.prog.lines : null;
      const from = before !== null ? `${int(before)} → ` : '';
      const n = warnings.length;     // (besides the points outside the scan: the red line under it)
      lines.push([`Leveled: ${from}${int(lev.lines)} lines${n ? ` · ${n} warning${n > 1 ? 's' : ''}` : ''}`, n ? 'amber' : 'good',
        [levelCoverage(lev.info), ...warnings].filter(Boolean).join('\n')]);
      if (s.outside > 0) {
        lines.push([`${int(s.outside)} points outside the scan (up to ${Number.isFinite(s.maxOutside) ? s.maxOutside.toFixed(3) : '?'} in)`,
          'bad', levelCoverage(lev.info)]);
      }
    } else if (lev.said) lines.push([lev.said, 'dim']);
    const key = JSON.stringify(lines);
    if (this.lmsgKey !== key) {
      this.lmsgKey = key;
      this.lmsgEl.replaceChildren(...lines.map(([text, cls, title]) => Object.assign(document.createElement('div'),
        { className: cls, textContent: text, title: title || text })));
    }
    setAttr(this.lmsgEl, 'hidden', !lines.length);
  }

  // ---------------------------------------------------------------- scan

  start() {
    const c = checkSettings(this.typed());
    this.showCheck(c);
    if (!c.settings || this.startWhy()) return;
    this.saveSettings();
    this.onStart?.(c.settings);
  }

  async stop() {
    try {
      await this.machine.scanStop();
      this.say('Stopping the scan…', 'dim');
    } catch (e) {
      this.say(e.message, 'bad');
      this.log?.(`Stop scan: ${e.message}`, 'err');
    }
  }

  /** Why Start Scan can't go now ('' when it can). */
  startWhy() {
    const m = this.m, st = m.status, pr = m.probe;
    if (m.scan?.running) return 'A scan is running';
    if (!(m.link === 'up' && m.connected)) return 'Connect to FluidNC first';
    if (!st || st.state === 'No response') return 'No response from FluidNC';
    if (jobActive(m.job)) return 'A job is running';
    if (m.jog) return 'Jogging';
    if (st.state !== 'Idle') return `FluidNC must be Idle (it is in ${st.state})`;
    if (!pr?.connected) return 'Connect the probe first';
    if (pr.busy || this.busy) return 'The probe is busy';
    if (!pr.ready) return 'Home the probe first (Home Probe)';
    if (!this.valid) return 'Check the settings';
    if (areaUnset(this.settings)) return 'Set the scan area first: Min / Max X and Y are all 0';
    return '';
  }

  say(text, cls = '') {
    this.msg = text ? { text, cls } : null;
    this.show();
  }

  // ---------------------------------------------------------------- what the server says

  /** m: the app's machine state (link, connected, status, job, jog, probe, scan...). */
  update(m) {
    this.m = m;
    const running = !!m.scan?.running;
    if (running && !this.wasRunning) this.msg = null;      // (a new scan: its own messages from now on)
    this.wasRunning = running;
    const s = m.scan?.settings;
    // The server's settings (saved, or another window's), unless something was typed here just now
    if (s && KEYS.every(k => Number.isFinite(+s[k])) && !(performance.now() - (this.changedAt || -1e9) < 2000)) {
      const next = Object.fromEntries(KEYS.map(k => [k, +s[k]]));
      if (KEYS.some(k => next[k] !== this.settings[k])) {
        this.settings = next;
        store.set('scan.settings', next);
        this.showSettings();
      }
    }
    // The server's auto-level settings (settings.json "level", if it says them), the same way
    const ls = m.levelSettings;
    if (ls && LEVEL_KEYS.every(k => Number.isFinite(+ls[k])) && !(performance.now() - (this.levelChangedAt || -1e9) < 2000)) {
      const next = Object.fromEntries(LEVEL_KEYS.map(k => [k, +ls[k]]));
      if (LEVEL_KEYS.some(k => next[k] !== this.levelSettings[k])) {
        this.levelSettings = next;
        store.set('level.settings', next);
        this.showLevelSettings();
      }
    }
    this.show();
  }

  show() {
    if (!this.root.isConnected) return;     // (hidden behind another tab: shown() redraws it)
    const m = this.m, pr = m.probe, sc = m.scan, running = !!sc?.running;
    // The probe
    let ps = 'Not connected', pcls = 'dim';
    if (this.busy === 'connect' || pr?.connecting) ps = 'Connecting…';
    else if (pr?.connected) {
      ps = pr.busy || this.busy === 'home' ? `${pr.port || ''} · busy…` : pr.ready ? `${pr.port || ''} · ready (homed)` : `${pr.port || ''} · not homed: press Home Probe`;
      pcls = pr.ready && !pr.busy ? 'good' : 'amber';
      if (pr.last != null && pr.last !== '') ps += ` · last ${typeof pr.last === 'number' ? fmt(pr.last) : pr.last}`;
    }
    setText(this.pstate, ps);
    setAttr(this.pstate, 'className', 'spstate ' + pcls);
    // Connected from elsewhere (another window): its port in the select (listed again once if it isn't there)
    if (pr?.connected && pr.port && this.portSel.value !== pr.port && !this.busy && this.listed) {
      if ([...this.portSel.options].some(o => o.value === pr.port)) this.portSel.value = pr.port;
      else if (this.relisted !== pr.port) { this.relisted = pr.port; this.listPorts(); }
    }
    const down = probeMayBeDown(m) && !pr.busy && this.busy !== 'home';
    // (not connected: the server remembers it was disconnected, or let go of with no window open, not homed)
    const gone = down && !pr.connected;
    setText(this.downEl, down ? 'Probe may be down: Home Probe before moving XY' : '');
    setAttr(this.downEl, 'hidden', !down);
    setAttr(this.downEl, 'title', !down ? '' : gone ? 'The probe was disconnected (or let go of once no window was open) '
      + 'before it was homed again: its tip may still be down. Connect it and press Home Probe before moving X or Y.'
      : 'The probe is not homed: its tip may still be down. Press Home Probe before moving X or Y.');
    const b = this.btn;
    setText(b.pconn, pr?.connected ? 'Disconnect' : 'Connect');
    setAttr(b.pconn, 'disabled', !!this.busy || running || (!pr?.connected && !this.selected()));
    setAttr(b.pconn, 'title', pr?.connected ? (running ? 'Not while scanning' : `Close the probe's port (${pr.port || ''})`)
      : 'Open the probe\'s serial port (it opens only when you press this)');
    setAttr(this.portSel, 'disabled', !!pr?.connected || !!this.busy || !this.ports.length);
    // (its column is narrow in the bottom row: the whole name of the port in its tooltip)
    setAttr(this.portSel, 'title', this.portSel.selectedOptions[0]?.textContent || 'The probe\'s serial port');
    setAttr(b.ports, 'disabled', !!this.busy);
    setAttr(b.home, 'disabled', !pr?.connected || !!pr.busy || !!this.busy || running);
    if (b.home.classList.contains('warnb') !== down) b.home.classList.toggle('warnb', down);
    setAttr(b.home, 'title', !pr?.connected ? `Home Probe: connect the probe first${gone ? '. It was disconnected before it '
      + 'was homed again: its tip may be down, so home it before X or Y moves.' : ''}` : running ? 'Home Probe: not while scanning'
      : `Home the probe (H): it retracts fully. Its first touch after that reads 0.0.${down ? ' It is not homed now: its tip '
        + 'may be down, so home it before X or Y moves.' : ''}`);
    const why = this.startWhy();
    const s = this.settings;
    setAttr(this.offWarn, 'hidden', !s || !!(num(s.probeOffX) || num(s.probeOffY)));
    setAttr(b.start, 'disabled', !!why);
    setAttr(b.start, 'title', why ? `Start Scan: ${why}` : 'Start the scan: the machine moves to each point (X and Y only) and the probe measures it. Asks first.');
    setAttr(b.stop, 'disabled', !running);
    if (b.stop.classList.contains('warnb') !== running) b.stop.classList.toggle('warnb', running);
    setAttr(b.stop, 'title', running ? 'Stop the scan: the probe stops at once and the machine stops safely (Hold, then a reset once stopped)' : 'Stop: only while scanning');
    const done = scanDone(sc);
    setAttr(b.save, 'disabled', !done);
    setAttr(b.save, 'title', done ? 'Save the last scan as an .xyz file (the UGS format)' : 'Save Scan…: when a scan has finished');
    const area = !!this.area?.();
    setAttr(b.area, 'disabled', !area || running);
    setAttr(b.area, 'title', area ? 'Min and Max X / Y from the G-code that is open' : 'Use G-code area: open a G-code file first');
    for (const input of Object.values(this.inputs)) setAttr(input, 'disabled', running);

    // While scanning: its progress, under the Scan buttons
    const pts = scanPoints(sc), total = scanTotal(sc);
    if (running) {
      const i = Math.min(total || Infinity, pts.length + 1);
      const ph = sc.phase || 'probing';
      const what = ph === 'moving' || ph === 'probing' ? `${PHASE[ph]} ${i} of ${total}` : `${PHASE[ph] || ph} (${pts.length} of ${total} done)`;
      const z = Number.isFinite(sc.lastZ) ? sc.lastZ : pts.length ? pts.at(-1)[2] : null;
      const text = `Scanning: ${what}` + (z !== null ? `\nlast Z ${z.toFixed(4)}` : '');
      setText(this.progEl, text);
      setAttr(this.progEl, 'title', text.replace('\n', ' · '));
    }
    setAttr(this.progEl, 'hidden', !running);
    setAttr(this.bar.parentElement, 'hidden', !running);
    const width = `${total ? Math.min(100, (100 * pts.length) / total) : 0}%`;
    if (this.bar.style.width !== width) this.bar.style.width = width;

    // Below the buttons: the scan's error, else what a button here did last, else what the last scan came to
    let msg = sc?.error ? { text: sc.error, cls: 'bad' } : this.msg;
    if (!msg && !running && pts.length) {
      const zs = pts.map(p => p[2]);
      const range = pts.length > 1 ? ` · height range ${(Math.max(...zs) - Math.min(...zs)).toFixed(4)} in` : '';
      msg = done ? { text: `${sc.message || `Last scan: ${pts.length} points`}${range}${sc.savedTo ? ` · saved to ${sc.savedTo}` : ''}`, cls: 'dim' }
        : { text: `The last scan stopped after ${pts.length} of ${total} points`, cls: 'dim' };
    }
    setAttr(this.msgEl, 'hidden', !msg);
    if (msg) {
      setText(this.msgEl, msg.text);
      setAttr(this.msgEl, 'className', 'smsg ' + (msg.cls || ''));
      setAttr(this.msgEl, 'title', msg.text);
    }
    this.showLevel();
  }
}
