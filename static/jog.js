// The Jog panel: an XY pad with a Z column, the jog speed and five step distances (inches).
//
// A click (let go within HOLD_MS) moves one step, sent when the button is let go. Held longer, the button
// starts a hold jog: the server streams short pieces only while this page keeps saying "still held"
// (every KEEP_MS; 0.4 s without that and the server stops the jog by itself), and stops it when the
// button is let go. A hold must always end, so every way a press can end stops it here: the button or
// key let go, pointer cancel or capture lost, the panel losing focus (keys), the window losing focus,
// the page hidden or closed, the machine state leaving Idle/Jog, the event link dropping, and the
// server saying the jog has ended. A jog that ended never starts again by itself: that takes a new press.
//
// The jog buttons never get the disabled attribute (a disabled button gets no pointerup, so a held one
// would never be let go): an 'off' class greys them, and the handlers check this.enabled.
//
// Also here: Go to XY 0 / Go to Z 0 (two-step: see twostep.js; armed, they say how far and which way), and
// the Vacuum (M8) and AUX (M7) toggles, lit from FluidNC's M7 / M8 / M9 state, so a program's M8 / M9 shows too;
// not connected, they show the last state seen with a "?". No key presses any of these (mouseOnly). The
// step and the speed are the server's (POST /api/machine/jogsettings), shared with other windows and the
// Bluetooth pendant, whose step keys change them here too.

import { store } from './api.js';
import { TwoStep, mouseOnly } from './twostep.js';
import { jobActive } from './machine.js';

const HOLD_MS = 500;        // held this long: a hold jog instead of one step (UGS's long-press time)
const KEEP_MS = 100;        // "still held" this often while a hold jog runs
const STEPS = [10, 1, 0.1, 0.01, 0.001];
const UGS_FEED = 150;       // in/min: the user's jog speed in UGS, the default here
const NO_MAX = 400;         // in/min: the speed's top until FluidNC's max rates are known
const MIN_FEED = 1;
const MSG_MS = 6000;        // a refused jog's message shows this long
const FAKE_REPEAT_MS = 40;  // a keydown this soon after the same key's keyup is a key repeat, not a new press
const AX = ['x', 'y', 'z'];
const HINT = 'Click: one step · Hold: moves until you let go';
const SEND_FEED_MS = 250;   // while the speed slider is dragged, the server hears the speed at most this often
const ECHO_MS = 1200;       // the server's echo of an older step or speed is ignored this long after a change here
const COOL_WAIT_MS = 1500;  // a toggle waits this long for FluidNC's report to show the new state
const SAFE_Z = 0.2;         // in: Go to XY 0 first lifts Z to here if it is lower (the server does it)
const ARROWS = ['→', '↗', '↑', '↖', '←', '↙', '↓', '↘'];     // which way a move goes in X and Y, by eighths
const PROBE_DOWN = 'Probe may be down: Home Probe before moving XY';

// The toggles: FluidNC's flood coolant (M8) runs the vacuum, mist (M7) the AUX relay. pin: its FluidNC setting
// ($/coolant/flood_pin, read at connect); noPin: what the button says when FluidNC has none
const COOL = {
  vacuum: { key: 'vacuum', name: 'Vacuum', field: 'flood', m: 'M8', pin: 'flood_pin', noPin: 'FluidNC has no flood_pin configured (the vacuum output)' },
  aux: { key: 'aux', name: 'AUX', field: 'mist', m: 'M7', pin: 'mist_pin', noPin: 'FluidNC has no mist_pin configured (the AUX output)' },
};
// FluidNC acts on the coolant toggles (0xA0 / 0xA1) only in these states
const COOL_STATES = ['Idle', 'Run', 'Hold'];

/** FluidNC's pin for the output ('gpio.25', 'NO_PIN', ...) as the server read it at connect, or ''. (The server
 *  says them in the status as coolantPins: { vacuum, aux }, or by the setting's or the output's name.) */
function coolPin(st, c) {
  const p = st?.coolantPins;
  if (!p || typeof p !== 'object') return '';
  const v = p[c.key] ?? p[c.field] ?? p[c.pin] ?? p[`/coolant/${c.pin}`] ?? p[`$/coolant/${c.pin}`];
  return typeof v === 'string' ? v.trim() : '';
}
const noPin = pin => /^no[_ ]?pin$/i.test(pin);


// The XY pad (Y+ at the top) and the Z column at its right: [row, column, direction (null: Stop), label]
const PAD = [
  [1, 1, { x: -1, y: 1 }, '↖'], [1, 2, { y: 1 }, '↑ Y+'], [1, 3, { x: 1, y: 1 }, '↗'], [1, 5, { z: 1 }, '↑ Z+'],
  [2, 1, { x: -1 }, '← X−'], [2, 2, null, 'Stop'], [2, 3, { x: 1 }, 'X+ →'], [2, 5, null, 'Stop'],
  [3, 1, { x: -1, y: -1 }, '↙'], [3, 2, { y: -1 }, '↓ Y−'], [3, 3, { x: 1, y: -1 }, '↘'], [3, 5, { z: -1 }, '↓ Z−'],
];

// While the panel has focus: the arrows jog X and Y, PageUp/PageDown Z (Escape stops)
const KEYS = {
  ArrowLeft: { x: -1 }, ArrowRight: { x: 1 }, ArrowUp: { y: 1 }, ArrowDown: { y: -1 }, PageUp: { z: 1 }, PageDown: { z: -1 },
};

// Why jogging is off now ('' when it is allowed). The server checks the same and refuses (409) anyway;
// this only greys the pad and says why.
const STATE_WHY = {
  Alarm: 'ALARM: Unlock or Home first',
  Home: 'Homing…',
  Run: 'Running',
  Hold: 'Hold: Resume or Soft Reset',
  Door: 'Door open: close it, then Resume',
  Check: 'Check mode ($C): nothing moves',
  Sleep: 'Sleeping: Soft Reset to wake it',
};

function blockedWhy(m) {
  if (m.link === 'down' || m.link === 'missing') return 'No link to the FluidSender server';
  if (m.link !== 'up' || !m.connected) return 'Not connected';
  const st = m.status;
  if (!st) return 'Waiting for FluidNC';
  if (st.state === 'No response') return 'No response from FluidNC';
  if (m.alarm?.critical) return 'Needs a Soft Reset (FluidNC: Reset to continue)';
  if (jobActive(m.job)) return 'A job is running';
  if (m.scan?.running) return 'A surface scan is running';
  if (st.state === 'Idle' || st.state === 'Jog') return st.stalled ? 'FluidNC has not answered the last line' : '';
  return STATE_WHY[st.state] || `Not while FluidNC is in ${st.state}`;
}

/** Why Go to XY 0 / Go to Z 0 can't go now ('' when they can): the server's rules (fresh Idle, nothing else going). */
function gotoWhy(m) {
  const why = blockedWhy(m);
  if (why) return why;
  if (m.jog) return 'Jogging';
  if (m.status.state !== 'Idle') return 'Only when FluidNC is Idle';
  if (!m.status.wpos) return 'Waiting for the work position';
  return '';
}

/** Why a coolant toggle can't be used now ('' when it can). The server refuses the same (409) anyway. */
function coolWhy(m, c) {
  if (m.link === 'down' || m.link === 'missing') return 'No link to the FluidSender server';
  if (m.link !== 'up' || !m.connected) return 'Not connected';
  const st = m.status;
  if (!st || st.state === 'No response') return 'No response from FluidNC';
  // FluidNC has no pin for this output (the server says why: coolantMissing; or its pin reads NO_PIN)
  if (st.coolantMissing?.[c.key]) return String(st.coolantMissing[c.key]);
  if (noPin(coolPin(st, c))) return c.noPin;
  if (!(c.field in st)) return 'This FluidSender server does not report it (restart FluidSender after the update)';
  if (st[c.field] == null) return 'Not known yet: FluidSender reads it from FluidNC ($G) once connected';
  // The program's M8 / M7 / M9 that hasn't run yet (it waits for the moves before it): a toggle now would cancel it
  const busy = st.coolantBusy?.[c.key];
  if (busy) return String(busy);
  if (m.jog || st.state === 'Jog') return `FluidNC ignores the ${c.name} toggle while jogging`;
  if (!COOL_STATES.includes(st.state)) return `FluidNC ignores the ${c.name} toggle while it is in ${st.state}`;
  return '';
}

/** The probe's tip may be down, under the bit's level, and an X / Y move would drag it. Connected: it is not
 *  homed (just connected, or a scan Stop, an abort or a failed Home left it so). Not connected: the server's
 *  mayBeDown, which outlives the port (disconnected after a scan Stop, an abort or a failed Home; or let go of with
 *  no window open while not homed, just connected too) until the probe is connected and homed again. (The server
 *  refuses no move for it: this is a warning.) */
export const probeMayBeDown = m => !m.scan?.running && (m.probe?.connected ? !m.probe.ready : !!m.probe?.mayBeDown);

const full = d => ({ x: 0, y: 0, z: 0, ...d });
const f3 = v => (Math.abs(v) < 5e-4 ? 0 : v).toFixed(3);
const signed = v => (v < 0 ? '−' : '+') + f3(Math.abs(v));
const dirName = d => AX.filter(a => d[a]).map(a => a.toUpperCase() + (d[a] > 0 ? '+' : '−')).join(' ');
const isField = t => t instanceof Element && t.matches('input, select, textarea, [contenteditable]');
const rate = (axes, a) => (Number.isFinite(axes?.[a]?.maxRate) && axes[a].maxRate > 0 ? axes[a].maxRate : null);
const inside = (el, x, y) => {
  const r = el.getBoundingClientRect();
  return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
};

/** ['X'] -> "X", ['X', 'Z'] -> "X and Z", ['X', 'Y', 'Z'] -> "X, Y and Z" */
function listAxes(names) {
  return names.length < 2 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names.at(-1)}`;
}

/** One line, so it fits the panel at 1280x800 (its title says more) */
const softNote = names => `Soft limits are off on ${listAxes(names)}: jogs can hit the end`;

export class JogPanel {
  /** machine: the MachineClient. onStop(): cancel every jog now (0x85). log(text, kind): app messages.
   *  onGoto(to): Go to XY 0 ('xy') / Go to Z 0 ('z'), after the second click. */
  constructor(machine, { onStop, log, onGoto } = {}) {
    this.machine = machine;
    this.onStop = onStop;
    this.log = log;
    this.onGoto = onGoto;
    this.m = {};
    this.press = null;       // the one press at a time (see begin)
    this.enabled = false;    // jogging allowed now (the server decides in the end)
    this.live = false;       // connected: the Stop buttons work
    this.why = '';           // why jogging is off
    this.msg = null;         // { text, until, cls }: a refused jog, or why a jog was cut short
    this.max = NO_MAX;
    this.zmax = null;
    this.known = false;      // FluidNC's max rates are known
    this.cool = {};          // which -> { on, until }: a toggle sent, until FluidNC's report shows it
    this.seen = {};          // which -> true | false: its state when FluidSender last knew it (shown when not live)
    this.armedNote = '';     // what an armed Go to button does, said under the pad
    this.changedAt = { step: -1e9, feed: -1e9 };    // the last change made here (see ECHO_MS)

    this.root = document.createElement('div');
    this.root.className = 'jog pad off';
    this.root.tabIndex = 0;    // the keys work while this panel has focus
    this.root.innerHTML = `
      <div class="jtop">
        <div class="jpad">${PAD.map(([r, c, d, label]) => (d
          ? `<button class="jbtn" tabindex="-1" style="grid-area:${r}/${c}" data-dir='${JSON.stringify(full(d))}'
              title="Jog ${dirName(d)}: click for one step, hold to move until you let go">${label}</button>`
          : `<button class="jbtn jstop" tabindex="-1" style="grid-area:${r}/${c}"
              title="Stop jogging now (jog cancel). Esc does the same while this panel has focus.">${label}</button>`)).join('')}
        </div>
        <div class="jgo">
          <button data-go="xy"></button>
          <button data-go="z"></button>
        </div>
      </div>
      <div class="jcap"><span>Speed</span><span class="jkeys" title="While this panel has focus: ← → jog X, ↑ ↓ jog Y, PageUp / PageDown jog Z, Esc stops. A tap moves one step, holding moves until you let go.">keys on</span><b></b></div>
      <div class="jspeed">
        <input type="range" min="${MIN_FEED}" max="${NO_MAX}" step="1" aria-label="Jog speed, inches per minute">
        <input type="number" min="${MIN_FEED}" max="${NO_MAX}" step="1" aria-label="Jog speed, inches per minute">
        <span>in/min</span>
      </div>
      <div class="jcool">${Object.entries(COOL).map(([k, c]) => `<button class="tog" data-cool="${k}" aria-pressed="false">
        <i class="lamp"></i><span>${c.name}</span><small></small></button>`).join('')}</div>
      <div class="jnotes">
        <div class="jmsg"></div>
        <div class="jprobe amber" hidden>${PROBE_DOWN}</div>
        <div class="jsoft amber" hidden></div>
      </div>
      <div class="jsteps">${STEPS.map(s => `<button data-step="${s}" title="Step ${s} in: a click moves ${s} inch">${s}</button>`).join('')}</div>`;
    const q = s => this.root.querySelector(s);
    this.pad = q('.jpad');
    this.slider = q('[type=range]');
    this.num = q('[type=number]');
    this.maxEl = q('.jcap b');
    this.msgEl = q('.jmsg');
    this.softEl = q('.jsoft');
    this.probeEl = q('.jprobe');
    this.stepBtns = [...this.root.querySelectorAll('[data-step]')];
    this.stops = [...this.root.querySelectorAll('.jstop')];
    this.coolBtns = Object.fromEntries([...this.root.querySelectorAll('[data-cool]')].map(b => [b.dataset.cool, b]));
    // (a click on these, the toggles or the pad leaves the focus on the panel: its keys go on working)
    const go = to => new TwoStep(q(`[data-go=${to}]`), {
      label: `Go to<br><b>${to.toUpperCase()} 0</b>`, ask: () => this.gotoAsk(to), run: () => this.onGoto?.(to),
      focusTo: this.root, onArm: (on, ask) => { this.armedNote = on ? ask.note || '' : ''; this.show(); },
    });
    this.go = { xy: go('xy'), z: go('z') };

    // Until the server says (its jog settings), the step and the speed this page used last
    const step = store.get('jog.step', 0.1);
    this.step = STEPS.includes(step) ? step : 0.1;
    const feed = store.get('jog.feed', UGS_FEED);
    this.wanted = Number.isFinite(feed) && feed >= MIN_FEED ? feed : UGS_FEED;
    for (const b of this.stepBtns) b.onclick = () => this.setStep(+b.dataset.step);
    this.slider.oninput = () => this.setFeed(+this.slider.value, false);
    this.slider.onchange = () => this.setFeed(+this.slider.value);
    this.num.onchange = () => this.setFeed(parseFloat(this.num.value));
    this.num.onkeydown = e => {
      if (e.key !== 'Enter') return;
      this.root.focus({ preventScroll: true });     // (the keys jog again)
      this.setFeed(parseFloat(this.num.value));
    };
    for (const [k, b] of Object.entries(this.coolBtns)) {
      mouseOnly(b, this.root);
      b.onclick = () => this.toggleCool(k);
    }

    this.listen();
    this.showStep();
    this.update({});
  }

  // ---------------------------------------------------------------- input

  listen() {
    const pad = this.pad;
    pad.addEventListener('pointerdown', e => this.pointerDown(e));
    // No focus on the button and no text selection from a press (the panel takes the focus, so the keys
    // work after a click); a long touch must not open a menu
    pad.addEventListener('mousedown', e => e.preventDefault());
    pad.addEventListener('contextmenu', e => e.preventDefault());
    // Let go: watched on the window, in the capture phase, so it is seen even if the pointer capture failed
    window.addEventListener('pointerup', e => {
      const p = this.press;
      if (p && p.pointerId === e.pointerId) this.release(p, 'up', e);
    }, true);
    window.addEventListener('pointercancel', e => this.pointerLost(e, 'the pointer was cancelled'), true);
    window.addEventListener('pointermove', e => {
      const p = this.press;
      if (!p || p.pointerId !== e.pointerId) return;
      p.x = e.clientX;
      p.y = e.clientY;
      // With another mouse button still down there is no pointerup when the main one is let go, only this
      if (!(e.buttons & 1)) this.release(p, 'up', e);
    }, true);
    // A capture is lost at the button, or at the document when the button left the page
    document.addEventListener('lostpointercapture', e => this.pointerLost(e, 'the pointer capture was lost'), true);

    this.root.addEventListener('keydown', e => this.keyDown(e));
    this.root.addEventListener('keyup', e => {
      this.keyUp = { key: e.key, at: e.timeStamp };
      const p = this.press;
      if (p && p.key === e.key) { e.preventDefault(); this.release(p, 'up', e); }
    });
    this.root.addEventListener('focusin', e => this.setKeys(!isField(e.target)));
    this.root.addEventListener('focusout', e => {
      const to = e.relatedTarget;
      this.setKeys(!!to && this.root.contains(to) && !isField(to));
    });
    window.addEventListener('blur', () => this.release(this.press, 'the window lost focus'));
    document.addEventListener('visibilitychange', () => { if (document.hidden) this.release(this.press, 'the page was hidden'); });
    window.addEventListener('pagehide', () => this.release(this.press, 'the page was closed', null, true));
    document.addEventListener('freeze', () => this.release(this.press, 'the page was frozen', null, true));
  }

  pointerDown(e) {
    const btn = e.target.closest('.jbtn');
    if (!btn || e.button !== 0) return;
    const stop = btn.classList.contains('jstop');
    // A jog button: the main button, the first finger, the pen tip. Stop: any (a second finger must stop
    // the jog the first one holds)
    if (!stop && !e.isPrimary) return;
    e.preventDefault();
    this.root.focus({ preventScroll: true });
    if (stop) { this.stopAll(); return; }                     // Stop acts at once, on the press
    if (this.press || !this.enabled) return;                  // one press at a time; none while jogging is off
    let captured = false;
    try { btn.setPointerCapture(e.pointerId); captured = true; } catch { /* the window listeners still see the release */ }
    this.begin({ dir: JSON.parse(btn.dataset.dir), btn, pointerId: e.pointerId, captured, x: e.clientX, y: e.clientY,
      t0: e.timeStamp });
  }

  pointerLost(e, why) {
    const p = this.press;
    if (p && p.pointerId === e.pointerId) this.release(p, why);
  }

  keyDown(e) {
    if (isField(e.target) || e.isComposing) return;     // (the arrows of the speed boxes are their own)
    if (e.key === 'Escape') {
      e.preventDefault();
      if (!e.repeat) this.stopAll();
      return;
    }
    const d = KEYS[e.key];
    if (!d || e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
    e.preventDefault();                                   // (no scrolling)
    if (e.repeat || this.press || !this.enabled) return;  // a held key repeats: only its first keydown counts
    // Some systems repeat a held key as keyup + keydown pairs: those must not become a string of steps
    if (this.keyUp?.key === e.key && e.timeStamp - this.keyUp.at < FAKE_REPEAT_MS) return;
    const dir = full(d);
    this.begin({ dir, btn: this.pad.querySelector(`[data-dir='${JSON.stringify(dir)}']`), key: e.key, t0: e.timeStamp });
  }

  /** The keys work while the panel or one of its buttons has focus (not the speed boxes). */
  setKeys(on) {
    this.keysOn = on;
    this.root.classList.toggle('keys', on);
    if (!on && this.press?.key) this.release(this.press, 'the Jog panel lost focus');
  }

  // ---------------------------------------------------------------- one press

  /** p: { dir, btn, pointerId, captured, x, y, t0 } or { dir, btn, key, t0 } (t0: the press event's timeStamp,
   *  when the user pressed, however late the page handled it). Its phase: 'wait' (pressed, not yet
   *  HOLD_MS), 'start' (asking the server for a hold jog), 'hold' (running: keep it), 'over' (the server
   *  ended or refused it), 'void' (slid off the button: does nothing). Nothing happens after 'over' or
   *  'void' until it is let go. */
  begin(p) {
    p.phase = 'wait';
    p.timer = setTimeout(() => this.startHold(p), HOLD_MS);
    this.press = p;
    this.msg = null;
    this.show();
  }

  async startHold(p) {
    if (this.press !== p || p.phase !== 'wait') return;
    const lost = this.lostWhy(p);
    if (lost) { this.release(p, lost); return; }
    if (p.pointerId !== undefined && !inside(p.btn, p.x, p.y)) {    // slid off the button: no jog
      p.phase = 'void';
      this.show();
      return;
    }
    p.phase = 'start';
    this.show();
    let id;
    try {
      id = await this.machine.jogStart(p.dir, this.feed());
    } catch (e) {
      if (this.press === p) p.phase = 'over';
      this.refused(e);
      if (e.status !== 409) this.stopJog('all');    // no answer: it may have started all the same
      return;
    }
    p.id = id;
    if (this.press !== p) {           // let go while the server was starting it: stop it now
      this.stopJog(id, p.keepalive);
      return;
    }
    p.phase = 'hold';
    p.keeper = setInterval(() => this.keep(p), KEEP_MS);
    this.show();
  }

  async keep(p) {
    if (this.press !== p || p.phase !== 'hold') { clearInterval(p.keeper); return; }
    const lost = this.lostWhy(p);
    if (lost) { this.release(p, lost); return; }
    // One at a time: a slow server must not get a pile of these in front of the stop
    // (if they stay away for 0.4 s, the server stops the jog by itself)
    if (p.keeping) return;
    p.keeping = true;
    try {
      await this.machine.jogKeep(p.id);
    } catch (e) {
      if (this.press !== p || p.phase !== 'hold') return;
      if (e.status === 409) {
        this.over(p);                 // the server ended it (the Console says why): no more calls
      } else {
        this.release(p, e.message);   // the server can't be told it is still held: stop it
        this.log?.(`Jog stopped: ${e.message}`, 'err');
      }
    } finally {
      p.keeping = false;
    }
  }

  /** Why a press can't go on though no release came (checked before a hold starts and before each keep). */
  lostWhy(p) {
    if (!this.enabled) return this.why || 'jogging is off';
    if (document.hidden) return 'the page was hidden';
    if (!p.btn?.isConnected || !this.root.isConnected) return 'the Jog panel was closed';
    if (p.key && !(this.keysOn && this.root.contains(document.activeElement))) return 'the Jog panel lost focus';
    if (p.captured && !p.btn.hasPointerCapture(p.pointerId)) return 'the pointer capture was lost';
    return '';
  }

  /** The jog ended on the server (a 409 on keep, or the jog event): stop keeping it; wait for the let go. */
  over(p) {
    clearInterval(p.keeper);
    p.phase = 'over';
    this.show();
  }

  /** Ends the press p. how: 'up' (let go: a short press moves one step, if let go on its button), else
   *  why it was cut short (no step). A hold jog is stopped either way. keepalive: the page is closing. */
  release(p, how, e = null, keepalive = false) {
    if (!p || this.press !== p) return;
    this.press = null;
    clearTimeout(p.timer);
    clearInterval(p.keeper);
    p.keepalive = keepalive;
    if (p.phase === 'wait') {
      const onButton = p.pointerId === undefined || !e || inside(p.btn, e.clientX, e.clientY);
      // A click by the events' own times, not by the timer: a page busy across HOLD_MS (a big file loading)
      // runs the queued let-go before the overdue timer, and a hold must not become a step after it
      const click = !!e && e.timeStamp - p.t0 < HOLD_MS;
      if (how === 'up' && onButton && this.enabled) {
        if (click) this.sendStep(p.dir);
        else this.msg = { text: 'Not moved: the page was busy when the hold should have started. Press again.', until: Date.now() + MSG_MS, cls: 'amber' };
      }
    } else if (p.phase === 'start' || p.phase === 'hold') {
      // While starting, its id is not known yet: stop whatever runs now, and startHold() stops it by its id
      // once the server answers (this stop can reach the server before the start)
      this.stopJog(p.phase === 'hold' ? p.id : 'all', keepalive);
      if (how !== 'up' && how !== 'Stop') this.msg = { text: `Jog stopped: ${how}`, until: Date.now() + MSG_MS, cls: 'amber' };
    }
    this.show();
  }

  async sendStep(dir) {
    const s = this.step;
    try {
      await this.machine.jogStep({ x: dir.x * s, y: dir.y * s, z: dir.z * s }, this.feed());
    } catch (e) {
      this.refused(e);
    }
  }

  stopJog(id, keepalive = false) {
    // A stop that fails: the server stops the jog by itself 0.4 s after the last keep
    Promise.resolve(this.machine.jogStop(id, keepalive)).catch(e => this.log?.(`Jog stop not sent: ${e.message}`, 'err'));
  }

  /** The Stop buttons and Escape: end the press here, then cancel every jog (0x85). */
  stopAll() {
    this.release(this.press, 'Stop');
    if (this.live) this.onStop?.();
  }

  refused(e) {
    this.msg = { text: e.message, until: Date.now() + MSG_MS, cls: 'bad' };
    this.log?.(`Jog: ${e.message}`, 'err');
    this.show();
  }

  // ---------------------------------------------------------------- what the server says

  /** The server's jog event: { id, dir, feed } while a hold jog runs, null once it has ended. */
  serverJog(jog) {
    const p = this.press;
    if (p && p.phase === 'hold' && jog?.id !== p.id) this.over(p);
  }

  /** m: the app's machine state { link, connected, status, axes, alarm, ... }. */
  update(m) {
    this.m = m;
    this.live = m.link === 'up' && !!m.connected;
    this.why = blockedWhy(m);
    this.enabled = !this.why;
    if (!this.enabled) this.release(this.press, this.why);
    this.root.classList.toggle('off', !this.enabled);
    for (const b of this.stops) b.classList.toggle('off', !this.live);

    const xy = [rate(m.axes, 'x'), rate(m.axes, 'y')].filter(v => v);
    const z = rate(m.axes, 'z');
    this.known = xy.length > 0;
    this.max = this.known ? Math.max(MIN_FEED, Math.floor(Math.max(...xy))) : NO_MAX;
    this.zmax = z ? Math.floor(z) : null;
    if (this.slider.max !== String(this.max)) this.slider.max = this.num.max = String(this.max);
    this.showFeed();

    // The server may add softLimits: false for the axes whose soft limits are off (nothing when it doesn't)
    const soft = AX.filter(a => m.axes?.[a]?.softLimits === false).map(a => a.toUpperCase());
    const text = soft.length ? softNote(soft) : '';
    if (this.softEl.textContent !== text) this.softEl.textContent = text;
    this.softEl.title = soft.length ? `Soft limits are off on ${listAxes(soft)}: FluidNC does not stop a jog at the end `
      + 'of travel, so a long jog can run into it' : '';
    this.softEl.hidden = !text;
    const down = probeMayBeDown(m);
    if (this.probeEl.hidden === down) this.probeEl.hidden = !down;
    this.probeEl.title = !down ? '' : m.probe?.connected
      ? 'The probe is not homed: after a scan Stop or an abort (or just connected) its tip may still be '
        + 'down. Press Home Probe in the Scan panel before moving X or Y.'
      : 'The probe was disconnected (or let go of once no window was open) before it was homed again: after a scan '
        + 'Stop, an abort or a failed Home (or, let go of, not homed since it was connected) its tip may still be down. '
        + 'Connect it and press Home Probe in the Scan panel before moving X or Y.';
    this.showCool();
    this.showGoto();
    this.show();
  }

  // ---------------------------------------------------------------- display

  /** The height (px) that shows the whole panel without scrolling, for the default layout. It leaves room
   *  for the soft-limit note too, which only shows once connected. */
  fullHeight() {
    if (!this.root.isConnected) return 230;       // (closed: about what it needs at the usual width)
    const soft = this.softEl, text = soft.textContent, hidden = soft.hidden;
    if (!text) soft.textContent = softNote(['X', 'Y', 'Z']);
    soft.hidden = false;
    // (measured out of the flow: dockview's flex container would stretch it to the panel's height)
    const style = this.root.style, old = style.cssText;
    style.cssText += `;position:absolute;height:auto;width:${this.root.offsetWidth}px`;
    const h = this.root.offsetHeight;
    style.cssText = old;
    soft.textContent = text;
    soft.hidden = hidden;
    return h || 250;
  }

  /** The speed of the next jog: the one chosen, at most FluidNC's max. */
  feed() { return Math.min(Math.max(this.wanted, MIN_FEED), this.max); }

  /** A speed chosen here. done: false while the slider is being dragged (the server hears it at most every
   *  SEND_FEED_MS then), true once it is let go or typed. */
  setFeed(v, done = true) {
    if (Number.isFinite(v)) {
      this.wanted = Math.min(Math.max(Math.round(v), MIN_FEED), this.max);
      store.set('jog.feed', this.wanted);
      this.changedAt.feed = performance.now();
      clearTimeout(this.feedTimer);
      if (done) this.sendSettings({ feed: this.wanted });
      else this.feedTimer = setTimeout(() => this.sendSettings({ feed: this.wanted }), SEND_FEED_MS);
    }
    this.showFeed(true);
  }

  setStep(step) {
    this.step = step;
    store.set('jog.step', step);
    this.changedAt.step = performance.now();
    this.showStep();
    this.sendSettings({ step });
  }

  /** The server keeps the step and speed for every window and the pendant. An older server doesn't: then
   *  they stay this page's own. */
  sendSettings(s) {
    if (this.noServerSettings) return;
    Promise.resolve(this.machine.setJogSettings(s)).catch(e => {
      if (e.status === 404) this.noServerSettings = true;
      else this.log?.(`Jog ${'step' in s ? 'step' : 'speed'} not saved on the server: ${e.message}`, 'err');
    });
  }

  /** The server's jog settings { step, feed } (snapshot, or its event when they change: another window,
   *  the pendant's step keys). An echo of an older change made here is ignored for a moment. */
  serverSettings(js) {
    if (!js) return;
    this.noServerSettings = false;
    const now = performance.now();
    if (STEPS.includes(js.step) && js.step !== this.step && now - this.changedAt.step > ECHO_MS) {
      this.step = js.step;
      store.set('jog.step', js.step);
      this.showStep();
    }
    const dragging = document.activeElement === this.slider && this.slider.matches(':active');
    if (Number.isFinite(js.feed) && js.feed >= MIN_FEED && js.feed !== this.wanted && !dragging
        && now - this.changedAt.feed > ECHO_MS) {
      this.wanted = js.feed;
      store.set('jog.feed', js.feed);
      this.showFeed();
    }
  }

  // ---------------------------------------------------------------- vacuum / AUX

  /** A toggle clicked: ask for the other state (the server sends FluidNC's toggle only if it is not so already). */
  async toggleCool(which) {
    const c = COOL[which], st = this.m.status;
    if (coolWhy(this.m, c) || this.cool[which]) return;
    const on = !st[c.field];
    this.cool[which] = { on, until: performance.now() + COOL_WAIT_MS };
    this.showCool();
    setTimeout(() => this.showCool(), COOL_WAIT_MS + 20);
    try {
      await this.machine.coolant(which, on);
    } catch (e) {
      delete this.cool[which];
      this.refused(e.status === 404 ? new Error('This FluidSender server has no vacuum / AUX toggles yet (restart FluidSender after the update)') : e);
      this.showCool();
    }
  }

  showCool() {
    const m = this.m, st = m.status, now = performance.now();
    for (const [k, c] of Object.entries(COOL)) {
      const b = this.coolBtns[k], why = coolWhy(m, c);
      // Known now: FluidNC's last report said it (live). Else the state last seen, with a "?": not connected
      // (or no link to the server) leaves the relay as it was, so it must not read as off
      const known = this.live && st?.[c.field] != null;
      const isOn = known && !!st[c.field];
      if (known) this.seen[k] = isOn;
      const last = this.seen[k];
      let wait = this.cool[k];
      if (wait && (now > wait.until || (st && st[c.field] === wait.on))) wait = this.cool[k] = undefined;
      b.disabled = !!why || !!wait;
      b.classList.toggle('lit', isOn);
      b.classList.toggle('wait', !!wait);
      b.classList.toggle('maybe', !known && last === true);
      b.setAttribute('aria-pressed', String(isOn));
      const none = this.live && (!!st?.coolantMissing?.[c.key] || noPin(coolPin(st, c)));
      const word = none ? 'no pin' : wait ? (wait.on ? 'on…' : 'off…') : known ? (isOn ? 'on' : 'off')
        : last === true ? 'on?' : last === false ? 'off?' : '?';
      const small = b.querySelector('small');
      if (small.textContent !== word) small.textContent = word;
      const what = `${c.name} (${c.m}, FluidNC's ${c.field === 'flood' ? 'flood' : 'mist'} coolant output)`;
      const seen = known || last == null ? '' : ` It was ${last ? 'ON' : 'off'} when FluidSender last knew${last ? ', and it stays on until it is switched '
        + 'off (connect again to do it from here)' : ''}.`;
      // FluidNC's coolant delay_ms: switching on dwells that long inside FluidNC, which holds up the planning of a job's moves
      const delay = Number.isFinite(st?.coolantDelayMs) && st.coolantDelayMs > 0 && jobActive(m.job) && !isOn
        ? ` FluidNC's coolant delay is ${Math.round(st.coolantDelayMs)} ms: switching it on during the job holds up FluidNC's `
          + 'planning that long (short moves can stall and leave a mark).' : '';
      // (an output on a plain ESP32 pin reads back 0, so FluidNC's reports can't show it: the server follows its
      // M7 / M8 / M9 state then, coolantFollows "modal")
      const pin = coolPin(st, c), modal = st?.coolantFollows?.[c.key] === 'modal';
      const follows = modal ? `FluidNC's M7 / M8 / M9 state (its reports can't show this output: ${pin ? `pin ${pin}` : 'its pin'} `
        + 'reads back off)' : 'FluidNC\'s M7 / M8 / M9 state';
      b.title = why ? `${what}: ${why.replace(/\.$/, '')}.${seen}` : `${what} is ${isOn ? 'on' : 'off'}. Click to turn it ${isOn ? 'off' : 'on'}`
        + ` (works during a job too). It shows ${follows}, so a program's M8 / M7 / M9 shows here once FluidNC has run it.${delay}`;
    }
  }

  // ---------------------------------------------------------------- go to zero

  /** What an armed Go to button says (lines), under the pad (note) and in its tooltip (title): how far and
   *  which way, from the work position now (a change of it bails the button, see showGoto). */
  gotoAsk(to) {
    const [x, y, z] = this.m.status?.wpos || [];
    if (to === 'z') {
      if (!Number.isFinite(z)) return { lines: ['Go to Z 0?', 'click again'] };
      const down = z > 0, d = f3(Math.abs(z));
      return {
        lines: [+d ? `Z ${down ? '↓' : '↑'} ${d} in` : 'Z is at 0', 'click again'],
        note: +d ? `Go to Z 0: Z ${down ? 'down' : 'up'} ${d} in at rapid (X and Y stay). Click again to go.` : 'Go to Z 0: Z is at 0 already.',
        title: +d ? `Go to Z 0: Z moves ${down ? 'down' : 'up'} ${d} in at rapid (G0), from Z ${f3(z)} to the work zero; X and Y stay.`
          : 'Go to Z 0: Z is at the work zero already.',
      };
    }
    if (!Number.isFinite(x) || !Number.isFinite(y)) return { lines: ['Go to XY 0?', 'click again'] };
    const dx = -x, dy = -y, d = f3(Math.hypot(dx, dy));
    const arrow = ARROWS[Math.round(Math.atan2(dy, dx) / (Math.PI / 4)) & 7];
    const lift = Number.isFinite(z) && z < SAFE_Z - 1e-4;
    const move = `X ${signed(dx)} Y ${signed(dy)}`;
    return {
      lines: [+d ? `${arrow} ${d} in` : 'at XY 0', ...(lift ? [`Z ↑ ${SAFE_Z} first`] : []), 'click again'],
      note: !+d ? `Go to XY 0: X and Y are at 0 already${lift ? `; Z goes up to ${SAFE_Z}` : ''}.`
        : `Go to XY 0: ${lift ? `Z up to ${SAFE_Z} first, then ${move}` : `${move} (Z stays at ${f3(z)})`} at rapid. Click again to go.`,
      title: `Go to XY 0: ${lift ? `Z first rises ${f3(SAFE_Z - z)} in to Z ${SAFE_Z}, then ` : ''}`
        + (+d ? `X ${signed(dx)} and Y ${signed(dy)} in (${d} in ${arrow}) at rapid (G0), to the work zero.` : 'X and Y are at the work zero already.'),
    };
  }

  showGoto() {
    const m = this.m, why = gotoWhy(m);
    const wpos = (m.status?.wpos || []).map(v => (Number.isFinite(v) ? v.toFixed(4) : '')).join(',');
    const key = `${this.live}|${m.status?.state}|${!!m.jog}|${jobActive(m.job)}|${!!m.scan?.running}|${why}|${wpos}`;
    const z = m.status?.wpos?.[2];
    const lift = Number.isFinite(z) && z < SAFE_Z - 1e-4;
    this.go.xy.context(key);
    this.go.z.context(key);
    this.go.xy.setEnabled(!why, why ? `Go to XY 0: ${why}`
      : `Go to XY 0: a rapid move (G0) to the work zero of X and Y${lift ? `. Z is below ${SAFE_Z} in, so it first rises to Z ${SAFE_Z}` : ` (Z stays where it is, above ${SAFE_Z} in)`}. Two clicks: the first one asks.`);
    this.go.z.setEnabled(!why, why ? `Go to Z 0: ${why}` : 'Go to Z 0: a rapid move (G0) down or up to the work zero of Z (X and Y stay). Two clicks: the first one asks.');
  }

  /** force: also while the number box has focus (else what is being typed there stays). */
  showFeed(force = false) {
    const f = this.feed();
    if (this.slider.value !== String(f)) this.slider.value = String(f);
    if (force || document.activeElement !== this.num) this.num.value = String(f);
    const over = this.zmax !== null && f > this.zmax;
    const text = this.known ? `FluidNC max ${this.max}${over ? ` · Z max ${this.zmax}` : ''}` : 'max from FluidNC once connected';
    if (this.maxEl.textContent !== text) this.maxEl.textContent = text;
    this.maxEl.classList.toggle('amber', over);
    const r = a => (rate(this.m.axes, a) ? Math.floor(rate(this.m.axes, a)) : '?');
    this.maxEl.title = this.known ? `FluidNC's max speeds: X ${r('x')} · Y ${r('y')} · Z ${r('z')} in/min. `
      + 'A jog goes at most as fast as its slowest axis allows.'
      : 'Until FluidNC tells its max speeds (when connected), up to 400 in/min. FluidNC caps every jog at its max anyway.';
  }

  showStep() {
    for (const b of this.stepBtns) b.classList.toggle('on', +b.dataset.step === this.step);
  }

  show() {
    const p = this.press;
    for (const b of this.pad.children) {
      b.classList.toggle('held', !!p && b === p.btn && p.phase !== 'void');
      b.classList.toggle('moving', !!p && b === p.btn && p.phase === 'hold');
    }
    let text = HINT, cls = '';
    if (p?.phase === 'start' || p?.phase === 'hold') {
      const sj = this.m.jog;      // the server's own account: its speed is capped by each axis's max rate
      const f = sj && sj.id === p.id && Number.isFinite(sj.feed) ? sj.feed : this.feed();
      text = `Jogging ${dirName(p.dir)} at ${+f.toFixed(1)} in/min: let go to stop`;
      cls = 'amber';
    } else if (p?.phase === 'over' && !this.msg) {
      text = 'The jog has stopped (the Console says why). Let go, then press again.';
      cls = 'bad';
    } else if (this.armedNote) {
      text = this.armedNote;
      cls = 'amber';
    } else if (this.msg && Date.now() < this.msg.until) {
      ({ text, cls } = this.msg);
    } else if (this.why) {
      text = this.why;
      cls = this.live ? 'bad' : '';
    }
    if (this.msgEl.textContent !== text) this.msgEl.textContent = text;
    this.msgEl.className = 'jmsg ' + cls;
    clearTimeout(this.msgTimer);
    if (this.msg && Date.now() < this.msg.until) this.msgTimer = setTimeout(() => this.show(), this.msg.until - Date.now() + 20);
  }
}
