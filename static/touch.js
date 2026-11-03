// The Scan panel's Touch probe tab: the surface measured with the bit itself through FluidNC's probe input
// (touchscan.py), for a blank copper-clad PCB or any conductive stock, without the auto probe. One lead clips on the
// copper, the other on the bit; the spindle is OFF. Completely apart from the Auto probe tab: its own settings
// (the server's settings.json "touchScan", and this page's store "touch.settings"), its own Start / Pause / Resume /
// Stop and progress. No offset: the bit touches where it is.
//
// Before every scan the probe check: the user touches the clip to the bit and lets go, and the server must have
// seen FluidNC's probe input open, close and open again (status probeCheck 'verified'); there is no button for it.
// Start is off until then (the server refuses it too), and while the spindle turns or the input is closed.
// Pause takes effect between points (the bit back at the clearance); Resume goes on with the next point. Stop
// stops the machine safely and then raises Z all the way up.
//
// Also here, its own column: Touch off Z, the work Z zero set with a touch plate (a metal "puck" of known thickness
// on the stock under the bit, its lead on the plate, the clip on the bit): the bit comes down until it touches the
// plate, and Z there becomes the plate's thickness, so Z 0 is the stock's top (touchscan.py touch_off). Its settings
// (the server's settings.json "touchOff", this page's store "touchoff.settings"); the button is two-step and mouse
// only (twostep.js), and needs the same probe check as a scan; Undo Zero undoes it like any zero.

import { store } from './api.js';
import { jobActive } from './machine.js';
import { TwoStep, mouseOnly } from './twostep.js';
import { areaUnset, scanPoints, scanTotal, scanDone } from './scan.js';

// (a first run: no area, which Start refuses, as the auto probe's)
const DEFAULTS = { minX: 0, minY: 0, maxX: 0, maxY: 0, xPoints: 5, yPoints: 5, clearance: 0.1, depth: 0.1, feed: 2 };
const KEYS = Object.keys(DEFAULTS);
const LABEL = { minX: 'Min X', minY: 'Min Y', maxX: 'Max X', maxY: 'Max Y', xPoints: 'Points X', yPoints: 'Points Y',
  clearance: 'Clearance', depth: 'Probe depth', feed: 'Probe feed' };
const LIMITS = { clearance: [0.02, 2, 'in'], depth: [0.01, 1, 'in'], feed: [0.1, 50, 'in/min'] };    // (the server's)
const MAX_POINTS = 100, MAX_TOTAL = 2000, MAX_COORD = 100;
const SAVE_SETTINGS_MS = 600;
const PHASE = { starting: 'starting', moving: 'moving to point', probing: 'touching point', retracting: 'back up from point',
  paused: 'paused', stopping: 'stopping (then Z goes up)' };

// Touch off Z (a first run: no plate thickness, which it refuses)
const OFF_DEFAULTS = { plateThickness: 0, searchDistance: 1, feed: 2, liftAfter: 0.25 };
const OFF_KEYS = Object.keys(OFF_DEFAULTS);
const OFF_LABEL = { plateThickness: 'Plate', searchDistance: 'Search', feed: 'Feed', liftAfter: 'Lift' };
const OFF_LIMITS = { plateThickness: [0, 2, 'in'], searchDistance: [0.05, 3, 'in'], feed: [0.5, 20, 'in/min'],
  liftAfter: [0, 2, 'in'] };      // (the server's)
const OFF_PHASE = { probing: 'the bit coming down to the plate', zeroing: 'setting Z', lifting: 'lifting off the plate',
  stopping: 'stopping (then Z goes up)' };

const num = v => (typeof v === 'number' ? v : Number(String(v).trim()));
const setText = (node, text) => { if (node.textContent !== text) node.textContent = text; };
const setAttr = (node, key, value) => { if (node[key] !== value) node[key] = value; };
const fmt = v => (Number.isFinite(v) ? String(+v.toFixed(4)) : '');

/** Touch scan settings as numbers, or { error, key } for the first thing wrong with them (the server's rules). */
export function checkTouchSettings(s) {
  const v = Object.fromEntries(KEYS.map(k => [k, num(s[k])]));
  for (const k of KEYS) if (s[k] === '' || !Number.isFinite(v[k])) return { error: `${LABEL[k]} is not a number`, key: k };
  for (const k of ['minX', 'maxX', 'minY', 'maxY']) {
    if (Math.abs(v[k]) > MAX_COORD) return { error: `${LABEL[k]}: from -${MAX_COORD} to ${MAX_COORD} in`, key: k };
  }
  for (const a of ['x', 'y']) {
    const n = v[`${a}Points`], A = a.toUpperCase();
    if (!Number.isInteger(n) || n < 1 || n > MAX_POINTS) return { error: `Points ${A}: a whole number from 1 to ${MAX_POINTS}`, key: `${a}Points` };
    if (areaUnset(v)) continue;
    const lo = v[`min${A}`], hi = v[`max${A}`];
    if (lo > hi) return { error: `Min ${A} is more than Max ${A}`, key: `min${A}` };
    if (lo === hi && n > 1) return { error: `Min ${A} and Max ${A} are the same: that takes Points ${A} = 1`, key: `max${A}` };
  }
  if (v.xPoints * v.yPoints > MAX_TOTAL) return { error: `${v.xPoints * v.yPoints} points: at most ${MAX_TOTAL}`, key: 'xPoints' };
  for (const [k, [lo, hi, unit]] of Object.entries(LIMITS)) {
    if (v[k] < lo || v[k] > hi) return { error: `${LABEL[k]}: from ${lo} to ${hi} ${unit}`, key: k };
  }
  return { settings: v };
}

/** Touch off Z's settings as numbers, or { error, key } (the server's rules; a plate thickness of 0 passes: not set). */
export function checkOffSettings(s) {
  const v = Object.fromEntries(OFF_KEYS.map(k => [k, num(s[k])]));
  for (const k of OFF_KEYS) {
    const [lo, hi, unit] = OFF_LIMITS[k];
    if (s[k] === '' || !Number.isFinite(v[k])) return { error: `${OFF_LABEL[k]} is not a number`, key: k };
    if (v[k] < lo || v[k] > hi) return { error: `${OFF_LABEL[k]}: from ${lo} to ${hi} ${unit}`, key: k };
  }
  return { settings: v };
}

/** FluidNC's probe input is closed now (its report's Pn: has P). */
export const probeClosed = st => /P/.test(st?.pins || '');

export class TouchTab {
  /**
   * machine: the MachineClient. log(text, kind). area(): the loaded G-code's X / Y bounds or null.
   * onStart(settings): starts the scan (the app asks first). onSave(): Save Scan… (the app's dialog).
   * changed(): something here wants the panel redrawn.
   */
  constructor(machine, { log, area, onStart, onSave, changed } = {}) {
    Object.assign(this, { machine, log, area, onStart, onSave, changed });
    this.offMsg = null;
    this.m = {};
    this.msg = null;
    const box = attrs => `<input ${attrs} type="text" inputmode="decimal" spellcheck="false" autocomplete="off">`;
    const wrap = document.createElement('div');
    wrap.innerHTML = `
      <section class="scol sc-tgrid" data-tab="touch">
        <div class="shead"><span>Touch grid</span></div>
        <div class="sgrid">
          <span></span><b>Min</b><b>Max</b><b>Points</b>
          <span>X</span>${['minX', 'maxX', 'xPoints'].map(k => box(`data-t="${k}" aria-label="Touch ${LABEL[k]}"`)).join('')}
          <span>Y</span>${['minY', 'maxY', 'yPoints'].map(k => box(`data-t="${k}" aria-label="Touch ${LABEL[k]}"`)).join('')}
        </div>
        <button data-act="tarea" title="Min and Max X / Y from the G-code that is open">Use G-code area</button>
        <div class="scheck tcheck dim"></div>
      </section>
      <section class="scol sc-tset" data-tab="touch">
        <div class="shead"><span>Bit as probe</span></div>
        <div class="lgrid">
          <label for="touchClear" title="The work Z the bit travels at between points (above the copper)">Clearance</label>
          ${box('id="touchClear" data-t="clearance"')}<span class="dim">in</span>
          <label for="touchDepth" title="How far below work Z 0 a touch goes looking for the copper before it gives up (no contact)">Probe depth</label>
          ${box('id="touchDepth" data-t="depth"')}<span class="dim">in</span>
          <label for="touchFeed" title="How fast the bit comes down to touch (G38.2's F)">Probe feed</label>
          ${box('id="touchFeed" data-t="feed"')}<span class="dim">in/min</span>
        </div>
        <div class="tpin"></div>
        <div class="tverify"></div>
      </section>
      <section class="scol sc-trun" data-tab="touch">
        <div class="shead"><span>Touch scan</span></div>
        <button data-act="tstart" class="primary">Start Scan</button>
        <button data-act="tpause">Pause</button>
        <button data-act="tresume">Resume</button>
        <button data-act="tstop">Stop</button>
        <button data-act="tsave">Save Scan…</button>
        <div class="sprog" hidden></div>
        <div class="sbar" hidden><i></i></div>
        <div class="smsg" hidden></div>
      </section>
      <section class="scol sc-toff" data-tab="touch">
        <div class="shead"><span title="Set work Z 0 on the stock's top with a touch plate: plate on the stock under the bit, its lead on the plate, the clip on the bit, spindle OFF">Touch off Z</span></div>
        <div class="lgrid">
          <label for="toffPlate" title="The touch plate's thickness (measure it once): Z 0 ends up this far below where the bit touches, on the stock's top">Plate</label>
          ${box('id="toffPlate" data-o="plateThickness"')}<span class="dim">in</span>
          <label for="toffSearch" title="How far down the bit goes looking for the plate before it gives up (no contact)">Search</label>
          ${box('id="toffSearch" data-o="searchDistance"')}<span class="dim">in</span>
          <label for="toffFeed" title="How fast the bit comes down to the plate (G38.2's F)">Feed</label>
          ${box('id="toffFeed" data-o="feed"')}<span class="dim">in/min</span>
          <label for="toffLift" title="How far the bit goes back up off the plate afterwards (0: it stays on it)">Lift</label>
          ${box('id="toffLift" data-o="liftAfter"')}<span class="dim">in</span>
        </div>
        <div class="lbtns"><button data-act="toff" class="primary"></button><button data-act="toffstop">Stop</button></div>
        <div class="smsg toffmsg" hidden></div>
      </section>`;
    this.sections = [...wrap.children];
    const q = s => wrap.querySelector(s);
    this.inputs = Object.fromEntries([...wrap.querySelectorAll('[data-t]')].map(i => [i.dataset.t, i]));
    this.offInputs = Object.fromEntries([...wrap.querySelectorAll('[data-o]')].map(i => [i.dataset.o, i]));
    this.offMsgEl = q('.toffmsg');
    this.checkEl = q('.tcheck');
    this.pinEl = q('.tpin');
    this.verifyEl = q('.tverify');
    this.progEl = q('.sc-trun .sprog');
    this.bar = q('.sc-trun .sbar i');
    this.msgEl = q('.sc-trun .smsg');
    this.btn = Object.fromEntries([...wrap.querySelectorAll('[data-act]')].map(b => [b.dataset.act, b]));
    mouseOnly(this.btn.tstart);          // (it moves the machine)
    this.btn.tarea.onclick = () => this.useArea();
    this.btn.tstart.onclick = () => this.start();
    this.btn.tpause.onclick = () => this.call('Pause', () => this.machine.touchPause(), '');
    this.btn.tresume.onclick = () => this.call('Resume', () => this.machine.touchResume(), '');
    this.btn.tstop.onclick = () => this.call('Stop', () => this.machine.touchStop(), 'Stopping the touch scan (then Z goes up)…');
    this.btn.tsave.onclick = () => this.onSave?.();
    // Touch off Z: two-step, mouse only (it moves the machine and sets the Z zero)
    this.offBtn = new TwoStep(this.btn.toff, { label: 'Touch off Z', ask: () => this.offAsk(), run: () => this.touchOff() });
    this.btn.toffstop.onclick = () => this.offCall(() => this.machine.touchStop(), 'Stopping Touch off Z (then Z goes up)…');
    for (const [k, input] of Object.entries(this.offInputs)) {
      input.oninput = () => this.offEdited(k);
      input.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); this.offEdited(k, true); } };
      input.onblur = () => this.showOffCheck(checkOffSettings(this.offTyped()));
    }
    this.offSettings = { ...OFF_DEFAULTS, ...store.get('touchoff.settings', {}) };
    this.showOffSettings(true);
    for (const [k, input] of Object.entries(this.inputs)) {
      input.oninput = () => this.edited(k);
      input.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); this.edited(k, true); } };
      input.onblur = () => this.showCheck(checkTouchSettings(this.typed()));
    }
    this.settings = { ...DEFAULTS, ...store.get('touch.settings', {}) };
    this.showSettings(true);
  }

  // ---------------------------------------------------------------- settings

  typed() { return Object.fromEntries(KEYS.map(k => [k, this.inputs[k].value])); }

  edited(key, now = false) {
    const c = checkTouchSettings(this.typed());
    this.showCheck(c);
    clearTimeout(this.saveTimer);
    if (c.settings) {
      this.settings = c.settings;
      store.set('touch.settings', c.settings);
      this.changedAt = performance.now();
      if (now) this.saveSettings(); else this.saveTimer = setTimeout(() => this.saveSettings(), SAVE_SETTINGS_MS);
    }
    this.changed?.();
  }

  saveSettings() {
    clearTimeout(this.saveTimer);
    if (this.noServer) return;
    Promise.resolve(this.machine.touchSettings(this.settings)).catch(e => {
      if (e.status === 404) this.noServer = true;
      else this.say(`Settings not saved: ${e.message}`, 'bad');
    });
  }

  showSettings(all = false) {
    for (const k of KEYS) {
      const input = this.inputs[k];
      if (!all && document.activeElement === input) continue;
      const v = this.settings[k];
      if (input.value.trim() === '' || num(input.value) !== v) input.value = k.endsWith('Points') ? String(Math.round(v)) : fmt(v);
    }
    this.showCheck(checkTouchSettings(this.typed()));
  }

  showCheck(c) {
    for (const [k, input] of Object.entries(this.inputs)) input.classList.toggle('bad', c.key === k);
    this.valid = !!c.settings;
    let text = '', cls = 'dim';
    if (c.error) { text = c.error; cls = 'bad'; } else if (areaUnset(c.settings)) {
      text = 'Scan area not set: type Min / Max X and Y, or Use G-code area'; cls = 'amber';
    } else {
      const v = c.settings, step = (lo, hi, n) => (n > 1 ? (hi - lo) / (n - 1) : 0);
      text = `${v.xPoints * v.yPoints} points, every ${fmt(step(v.minX, v.maxX, v.xPoints))} in X, ${fmt(step(v.minY, v.maxY, v.yPoints))} in Y`;
    }
    setText(this.checkEl, text);
    setAttr(this.checkEl, 'className', 'scheck tcheck ' + cls);
    setAttr(this.checkEl, 'title', text);
  }

  useArea() {
    const a = this.area?.();
    if (!a) return;
    const lo = v => Math.floor(v * 1000 + 1e-6) / 1000, hi = v => Math.ceil(v * 1000 - 1e-6) / 1000;
    this.inputs.minX.value = fmt(lo(a.lo[0]));
    this.inputs.maxX.value = fmt(hi(a.hi[0]));
    this.inputs.minY.value = fmt(lo(a.lo[1]));
    this.inputs.maxY.value = fmt(hi(a.hi[1]));
    this.edited('minX', true);
  }

  // ---------------------------------------------------------------- Touch off Z settings

  offTyped() { return Object.fromEntries(OFF_KEYS.map(k => [k, this.offInputs[k].value])); }

  offEdited(key, now = false) {
    const c = checkOffSettings(this.offTyped());
    this.showOffCheck(c);
    clearTimeout(this.offSaveTimer);
    if (c.settings) {
      this.offSettings = c.settings;
      store.set('touchoff.settings', c.settings);
      this.offChangedAt = performance.now();
      if (now) this.saveOffSettings(); else this.offSaveTimer = setTimeout(() => this.saveOffSettings(), SAVE_SETTINGS_MS);
    }
    this.changed?.();
  }

  saveOffSettings() {
    clearTimeout(this.offSaveTimer);
    if (this.noServer) return Promise.resolve();
    return Promise.resolve(this.machine.touchOffSettings(this.offSettings)).catch(e => {
      if (e.status === 404) this.noServer = true;
      else this.offSay(`Settings not saved: ${e.message}`, 'bad');
    });
  }

  showOffSettings(all = false) {
    for (const k of OFF_KEYS) {
      const input = this.offInputs[k];
      if (!all && document.activeElement === input) continue;
      const v = this.offSettings[k];
      if (input.value.trim() === '' || num(input.value) !== v) input.value = fmt(v);
    }
    this.showOffCheck(checkOffSettings(this.offTyped()));
  }

  showOffCheck(c) {
    for (const [k, input] of Object.entries(this.offInputs)) input.classList.toggle('bad', c.key === k);
    this.offValid = !!c.settings;
    this.offError = c.error || '';
  }

  // ---------------------------------------------------------------- Touch off Z

  /** Why Touch off Z can't go now ('' when it can). The server checks all of it again. */
  offWhy() {
    const m = this.m, st = m.status, t = m.touch;
    if (t?.off?.running) return 'Touch off Z is running';
    if (t?.running || m.scan?.running) return 'A scan is running';
    if (!(m.link === 'up' && m.connected)) return 'Connect to FluidNC first';
    if (!st || st.state === 'No response') return 'No response from FluidNC';
    if (jobActive(m.job)) return 'A job is running';
    if (m.jog) return 'Jogging';
    if (st.state !== 'Idle') return `FluidNC must be Idle (it is in ${st.state})`;
    if ((st.spindle || 0) > 0) return 'The spindle is on (or FluidNC still shows its speed): M5, then jog a little';
    if (!this.offValid) return this.offError || 'Check the settings';
    if (!(this.offSettings.plateThickness > 0)) return 'Type the plate thickness first (measure the plate once)';
    if (probeClosed(st)) return 'The probe input is closed: check the clip (it must not touch the plate yet)';
    if (st.probeCheck !== 'verified') return 'Probe check first: touch the plate to the bit, then let go';
    return '';
  }

  offAsk() {
    const s = this.offSettings;
    return { lines: ['Touch off Z?', 'click again'],
      title: `Touch off Z: the bit comes down at ${s.feed} in/min until it touches the plate (${s.searchDistance} in at most), `
        + `then Z there = ${s.plateThickness} in (the plate), so Z 0 is the stock's top${s.liftAfter > 0 ? `; then up ${s.liftAfter} in` : ''}. `
        + 'Spindle OFF, the plate under the bit, the clip on the bit.' };
  }

  async touchOff() {
    const c = checkOffSettings(this.offTyped());
    this.showOffCheck(c);
    if (!c.settings || this.offWhy()) return;
    this.offSettings = c.settings;
    this.offSay('Touch off Z: starting…', 'dim');
    this.log?.(`Touch off Z (plate ${c.settings.plateThickness} in)`, 'info');
    await this.offCall(() => this.machine.touchOff(c.settings), '');
  }

  async offCall(fn, during) {
    try {
      await fn();
      this.offSay(during, 'dim');
    } catch (e) {
      this.offSay(e.message, 'bad');
      this.log?.(`Touch off Z: ${e.message}`, 'err');
    }
  }

  offSay(text, cls = '') {
    this.offMsg = text ? { text, cls } : null;
    this.changed?.();
  }

  showOff() {
    const t = this.m.touch, off = t?.off, running = !!off?.running, b = this.btn;
    const why = this.offWhy();
    this.offBtn.context(`${this.m.status?.state}|${running}`);
    this.offBtn.setEnabled(!why, why ? `Touch off Z: ${why}` : 'Touch off Z: the bit comes down until it touches the plate, '
      + 'then Z = the plate\'s thickness, so Z 0 is the stock\'s top. Two clicks (the second 0.5 to 3 s after the first).');
    setAttr(b.toffstop, 'disabled', !running);
    if (b.toffstop.classList.contains('warnb') !== running) b.toffstop.classList.toggle('warnb', running);
    setAttr(b.toffstop, 'title', running ? 'Stop Touch off Z: the machine stops safely, then Z goes all the way up'
      : 'Stop: only while touching off');
    for (const input of Object.values(this.offInputs)) setAttr(input, 'disabled', running);
    let msg;
    if (running) msg = { text: `Touch off Z: ${OFF_PHASE[off.phase] || off.phase || 'starting'}…`, cls: '' };
    else if (this.offMsg) msg = this.offMsg;
    else if (off?.error) msg = { text: off.error, cls: 'bad' };
    else if (off?.message) msg = { text: off.message, cls: 'good' };
    else if (!this.offValid) msg = { text: this.offError, cls: 'bad' };
    else if (!(this.offSettings.plateThickness > 0)) msg = { text: 'Plate thickness not set: measure the plate and type it', cls: 'amber' };
    setAttr(this.offMsgEl, 'hidden', !msg);
    if (msg) {
      setText(this.offMsgEl, msg.text);
      setAttr(this.offMsgEl, 'className', 'smsg toffmsg ' + (msg.cls || ''));
      setAttr(this.offMsgEl, 'title', msg.text);
    }
  }

  // ---------------------------------------------------------------- scan

  start() {
    const c = checkTouchSettings(this.typed());
    this.showCheck(c);
    if (!c.settings || this.startWhy()) return;
    this.saveSettings();
    this.onStart?.(c.settings);
  }

  async call(what, fn, during) {
    try {
      await fn();
      this.say(during, 'dim');          // ('' for Pause / Resume: the progress line says it)
    } catch (e) {
      this.say(e.message, 'bad');
      this.log?.(`Touch scan ${what}: ${e.message}`, 'err');
    }
  }

  say(text, cls = '') {
    this.msg = text ? { text, cls } : null;
    this.changed?.();
  }

  /** Why Start Touch Scan can't go now ('' when it can). The server checks all of it again. */
  startWhy() {
    const m = this.m, st = m.status;
    if (m.scan?.running) return 'A scan is running';
    if (m.touch?.off?.running) return 'Touch off Z is running';
    if (!(m.link === 'up' && m.connected)) return 'Connect to FluidNC first';
    if (!st || st.state === 'No response') return 'No response from FluidNC';
    if (jobActive(m.job)) return 'A job is running';
    if (m.jog) return 'Jogging';
    if (st.state !== 'Idle') return `FluidNC must be Idle (it is in ${st.state})`;
    if ((st.spindle || 0) > 0) return 'The spindle is on (or FluidNC still shows its speed): M5, then jog a little';
    if (!this.valid) return 'Check the settings';
    if (areaUnset(this.settings)) return 'Set the scan area first: Min / Max X and Y are all 0';
    if (probeClosed(st)) return 'The probe input is closed: check the clip';
    if (st.probeCheck !== 'verified') return 'Probe check first: touch the clip to the bit, then let go';
    return '';
  }

  update(m) {
    this.m = m;
    const t = m.touch, running = !!t?.running;
    if (running && !this.wasRunning) this.msg = null;
    this.wasRunning = running;
    const offRunning = !!t?.off?.running;
    if (offRunning && !this.offWasRunning) this.offMsg = null;     // (its own messages from now on)
    this.offWasRunning = offRunning;
    const os = t?.off?.settings;
    if (os && OFF_KEYS.every(k => Number.isFinite(+os[k])) && !(performance.now() - (this.offChangedAt || -1e9) < 2000)) {
      const next = Object.fromEntries(OFF_KEYS.map(k => [k, +os[k]]));
      if (OFF_KEYS.some(k => next[k] !== this.offSettings[k])) {
        this.offSettings = next;
        store.set('touchoff.settings', next);
        this.showOffSettings();
      }
    }
    const s = t?.settings;
    if (s && KEYS.every(k => Number.isFinite(+s[k])) && !(performance.now() - (this.changedAt || -1e9) < 2000)) {
      const next = Object.fromEntries(KEYS.map(k => [k, +s[k]]));
      if (KEYS.some(k => next[k] !== this.settings[k])) {
        this.settings = next;
        store.set('touch.settings', next);
        this.showSettings();
      }
    }
  }

  show() {
    const m = this.m, st = m.status, t = m.touch, running = !!t?.running, b = this.btn;
    this.showOff();
    const live = m.link === 'up' && m.connected && st && st.state !== 'No response';
    // The probe input, live, and the check
    const closed = probeClosed(st), check = st?.probeCheck;
    setText(this.pinEl, !live ? 'Probe input: not known (not connected)' : closed ? 'Probe input: CLOSED' : 'Probe input: open');
    setAttr(this.pinEl, 'className', 'tpin spstate ' + (!live ? 'dim' : closed ? 'amber' : 'good'));
    let verify = '', vcls = 'amber';
    if (running || t?.off?.running) { verify = `Probe check used by this ${running ? 'scan' : 'touch-off'}`; vcls = 'dim'; } else if (!live) { verify = ''; } else if (check === 'verified') {
      verify = 'Probe check passed ✓'; vcls = 'good';
    } else if (check === 'closed') verify = 'Probe check: now let go of the clip';
    else verify = 'Probe check: touch the clip to the bit, then let go';
    setText(this.verifyEl, verify);
    setAttr(this.verifyEl, 'className', 'tverify spstate ' + vcls);
    setAttr(this.verifyEl, 'title', 'Before every touch scan: FluidSender must see the probe input close and open again '
      + '(clip on the copper, touch it to the bit, let go). That proves the wiring works. Each scan uses it up; a reset, '
      + 'an alarm, a disconnect or 10 minutes clear it.');
    // The buttons
    const why = this.startWhy();
    setAttr(b.tstart, 'disabled', !!why);
    setAttr(b.tstart, 'title', why ? `Start Scan (touch probe): ${why}` : 'Start the touch scan: the bit comes down on each point '
      + 'until it touches the copper (G38.2), spindle OFF. Asks first.');
    const paused = !!t?.paused, pausing = !!t?.pausing;
    setAttr(b.tpause, 'disabled', !running || paused || pausing || t?.phase === 'stopping');
    setAttr(b.tpause, 'title', running ? 'Pause after this point (the bit back up at the clearance)' : 'Pause: only while scanning');
    setAttr(b.tresume, 'disabled', !running || !(paused || pausing) || t?.phase === 'stopping');
    setAttr(b.tresume, 'title', paused ? 'Go on with the next point' : 'Resume: only while paused');
    setAttr(b.tstop, 'disabled', !running);
    if (b.tstop.classList.contains('warnb') !== running) b.tstop.classList.toggle('warnb', running);
    setAttr(b.tstop, 'title', running ? 'Stop the touch scan: the machine stops safely, then Z goes all the way up' : 'Stop: only while scanning');
    const done = scanDone(t);
    setAttr(b.tsave, 'disabled', !done);
    setAttr(b.tsave, 'title', done ? 'Save the touch scan as an .xyz file (the UGS format)' : 'Save Scan…: when a touch scan has finished');
    const area = !!this.area?.();
    setAttr(b.tarea, 'disabled', !area || running || !!t?.off?.running);
    for (const input of Object.values(this.inputs)) setAttr(input, 'disabled', running);
    // Progress
    const pts = scanPoints(t), total = scanTotal(t);
    if (running) {
      const i = Math.min(total || Infinity, pts.length + 1);
      const ph = t.phase || 'probing';
      const what = paused ? `Paused at point ${pts.length} of ${total}` : ph === 'moving' || ph === 'probing' || ph === 'retracting'
        ? `${PHASE[ph]} ${ph === 'retracting' ? pts.length : i} of ${total}` : `${PHASE[ph] || ph} (${pts.length} of ${total} done)`;
      const z = Number.isFinite(t.lastZ) ? t.lastZ : null;
      const text = `${paused ? '' : 'Touch scan: '}${what}${pausing ? ' · pausing after it' : ''}` + (z !== null ? `\nlast Z ${z.toFixed(4)}` : '');
      setText(this.progEl, text);
      setAttr(this.progEl, 'title', text.replace('\n', ' · '));
    }
    setAttr(this.progEl, 'hidden', !running);
    setAttr(this.bar.parentElement, 'hidden', !running);
    const width = `${total ? Math.min(100, (100 * pts.length) / total) : 0}%`;
    if (this.bar.style.width !== width) this.bar.style.width = width;
    let msg = t?.error ? { text: t.error, cls: 'bad' } : this.msg;
    if (!msg && !running && pts.length) {
      const zs = pts.map(p => p[2]);
      const range = pts.length > 1 ? ` · height range ${(Math.max(...zs) - Math.min(...zs)).toFixed(4)} in` : '';
      msg = done ? { text: `${t.message || `Last touch scan: ${pts.length} points`}${range}${t.savedTo ? ` · saved to ${t.savedTo}` : ''}`, cls: 'dim' }
        : { text: `The last touch scan stopped after ${pts.length} of ${total} points`, cls: 'dim' };
    }
    setAttr(this.msgEl, 'hidden', !msg);
    if (msg) {
      setText(this.msgEl, msg.text);
      setAttr(this.msgEl, 'className', 'smsg ' + (msg.cls || ''));
      setAttr(this.msgEl, 'title', msg.text);
    }
  }
}
