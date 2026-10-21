// Small panels: toolpath info, machine (state, run / hold / resume / reset, the job, position, zero, home,
// unlock), console.

import { store } from './api.js';
import { explain } from './codes.js';
import { firmwareName, jobActive } from './machine.js';
import { TwoStep, mouseOnly } from './twostep.js';

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function fmtTime(min) {
  const m = Math.round(min);
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
}

export class InfoPanel {
  constructor() {
    this.root = el('div', 'info pad');
    this.update(null, null, '', '');
  }

  update(job, scan, jobName, scanName) {
    const r = (a, b, cls = '') => `<div class="row ${cls}"><span>${a}</span><b>${b}</b></div>`;
    const f = v => v.toFixed(3);
    let html = `<h3>Files</h3>${r('G-code', esc(jobName || 'none'))}${r('Scan', esc(scanName || 'none'))}`;
    if (job) {
      html += '<h3>Toolpath</h3>'
        + r('Lines', job.lines.toLocaleString())
        + r('Cut moves', job.cutMoves.toLocaleString())
        + r('Rapids', job.rapidMoves.toLocaleString())
        + r('X', `${f(job.lo[0])} … ${f(job.hi[0])}`)
        + r('Y', `${f(job.lo[1])} … ${f(job.hi[1])}`)
        + r('Z', `${f(job.lo[2])} … ${f(job.hi[2])}`)
        + r('Cut length', `${job.cutLen.toFixed(1)} in`)
        + r('Cut time at F', job.cutTime ? fmtTime(job.cutTime) : '—');
      if (job.feeds.length) html += r('Feeds', job.feeds.slice(0, 4).join(', ') + (job.feeds.length > 4 ? ' …' : ''));
      if (job.skipped) html += r('Not drawn (G53/G28…)', job.skipped);
    }
    if (scan) {
      html += '<h3>Scan</h3>'
        + r('Points', `${scan.xs.length} × ${scan.ys.length}${scan.partial ? ` (${scan.count} measured)` : ''}`)
        + r('X', `${f(scan.xs[0])} … ${f(scan.xs.at(-1))}`)
        + r('Y', `${f(scan.ys[0])} … ${f(scan.ys.at(-1))}`)
        + r('Height range', scan.count ? f(scan.zmax - scan.zmin) : '—');
    }
    if (job && scan && !scan.partial) {
      const out = job.lo[0] < scan.xs[0] - 1e-4 || job.hi[0] > scan.xs.at(-1) + 1e-4
        || job.lo[1] < scan.ys[0] - 1e-4 || job.hi[1] > scan.ys.at(-1) + 1e-4;
      if (out) html += '<div class="warn">The job goes outside the scanned area. Outside it the edge heights are used.</div>';
    }
    this.root.innerHTML = html;
  }
}

// ------------------------------------------------------------------ machine

const STATE_CLASS = {
  Idle: 'idle', Run: 'run', Hold: 'hold', Door: 'hold', Alarm: 'alarm', Home: 'home', Jog: 'home', Check: 'check',
};
const SUB_TEXT = {
  'Hold:0': 'Stopped. Resume continues.',
  'Hold:1': 'Slowing to a stop…',
  'Door:0': 'Door closed. Resume continues.',
  'Door:1': 'Stopped. Close the door to resume.',
  'Door:2': 'Door open, stopping…',
  'Door:3': 'Door closed, resuming…',
};
const PIN_NAMES = {
  X: 'X limit', Y: 'Y limit', Z: 'Z limit', A: 'A limit', B: 'B limit', C: 'C limit',
  P: 'probe', D: 'door', H: 'hold', R: 'reset', S: 'cycle start',
};
const AXES = ['X', 'Y', 'Z'];

const fmt4 = v => (Number.isFinite(v) ? (Math.abs(v) < 5e-5 ? 0 : v).toFixed(4) : '—');
const set = (node, text) => { if (node.textContent !== text) node.textContent = text; };
const MAX_ZERO = 100;     // in: the largest work position a zero sets (the server's limit too)

const PROG_STATE = {      // this page's program, before a job: [text, color]
  ok: ['Loaded', 'dim'], sending: ['Loaded (updating…)', 'dim'], uploading: ['Loading…', 'dim'],
  error: ['Not loaded', 'bad'], old: ['Not loaded', 'dim'], empty: ['Nothing to send', 'dim'],
};
const JOB_STATE = {       // the job line: what each state of the server's job reads as, and its color
  running: ['Running', 'run'], paused: ['Paused', 'hold'], stopping: ['Stopping…', 'hold'],
  done: ['Done', 'good'], stopped: ['Stopped', 'dim'], failed: ['Failed', 'bad'],
};
// The overrides block: FluidNC's feed (G1 / G2 / G3), spindle (S) and rapid (G0) overrides. i: its place in the
// status report's Ov:feed,rapid,spindle
const OVR = [
  { which: 'feed', label: 'Feed', i: 0, title: 'Feed override: the speed of the cutting moves (G1, G2, G3), 10 % to 200 % of '
    + 'their F, at once (also during a job or a hold). Jogs and probing keep their own speed.' },
  { which: 'spindle', label: 'Spindle (S)', i: 2, title: 'Spindle override: the spindle speed, 10 % to 200 % of the '
    + 'programmed S, at once (also during a job or a hold).' },
  { which: 'rapid', label: 'Rapid', i: 1, title: 'Rapid override: the speed of the rapid moves (G0): 100 %, 50 % or 25 %.' },
];
const RAPIDS = [25, 50, 100];
const OV_MIN = 10, OV_MAX = 200;
// Spindle types whose speed is only on or off (FluidNC's OnOff and Relay): the override changes only the S reported
const ON_OFF = /^(on.?off|relay)/i;

/** "1:05", "1:02:05" */
/** "from the pendant, 3 min ago; 2 older kept": where and when the zero that Undo Zero puts back was set. */
export function undoWhen(u) {
  const s = Math.max(0, Date.now() / 1000 - (u.t || 0));
  const ago = !u.t ? '' : s < 60 ? 'just now' : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
  const parts = [u.source === 'pendant' ? 'from the pendant' : 'from the page', ago].filter(Boolean).join(', ');
  return parts + (u.count > 1 ? `; ${u.count - 1} older kept` : '');
}

export function fmtClock(s) {
  if (!Number.isFinite(s) || s < 0) return '';
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

/** Why Run can't start now ('' when it can). m: the app's machine state (with prog: this page's program
 *  upload, see app.js). The server checks the same and refuses with its reason anyway. */
export function runWhy(m) {
  const live = m.link === 'up' && !!m.connected;
  if (!live) return 'Not connected';
  if (jobActive(m.job)) return 'A job is running';
  if (m.scan?.running) return 'A surface scan is running';
  const p = m.prog || {};
  if (p.state === 'none' || !p.state) return 'Open a G-code file first';
  if (p.state === 'old') return 'This FluidSender server cannot run jobs yet (restart FluidSender after the update)';
  if (p.state === 'error') return p.error || 'FluidSender refused the program';
  if (p.state === 'empty') return 'The G-code has no lines to send';
  const st = m.status;
  if (!st || st.state === 'No response') return 'No response from FluidNC';
  if (m.alarm?.critical) return 'FluidNC needs a Soft Reset first';
  if (st.state === 'Alarm') return 'ALARM: Unlock or Home first';
  if (m.jog) return 'Jogging';
  if (st.state !== 'Idle') return `Only when FluidNC is Idle (it is in ${st.state})`;
  if (st.stalled) return 'FluidNC has not answered the last line';
  if (!m.modal?.units) return 'Waiting for FluidNC\'s modes ($G)';
  return '';
}

/** What is wrong with the server's program, for a page that shows its own G-code ('' when it is this page's):
 *  another window loaded another program (or another version of this one), or closed it. Run here loads
 *  this page's G-code first (app.js). m: the app's machine state (program: the server's, prog: this page's). */
export function programMismatch(m) {
  const p = m.prog || {}, s = m.program, lev = m.lev || {};
  if (m.link !== 'up' || p.state !== 'ok' || p.busy || p.timer || jobActive(m.job) || lev.busy) return '';
  if (!s) return 'Not loaded on the server (another window closed it); Run here loads this file first';
  // (this page's leveling: the server has the leveled program)
  const want = lev.active && lev.own ? { lines: lev.lines, total: lev.total } : p;
  if (!p.foreign && s.name === p.name && s.lines === want.lines && s.total === want.total && !!s.leveled === !!lev.active) return '';
  return `FluidSender has ${s.name === p.name ? `another version of ${s.name}` : s.name}${s.leveled ? ' (leveled)' : ''} loaded `
    + '(another window); Run here loads this file first';
}

/** The Alarm note: what the alarm means and the way out. alarm: { code, critical } or null (not known). */
function alarmNote(alarm) {
  const code = alarm?.code ?? null;
  const meaning = code === null ? '' : explain(`ALARM:${code}`);
  const head = code === null ? 'ALARM' : `ALARM:${code}${meaning ? ` (${meaning})` : ''}`;
  if (alarm?.critical) return `${head}. Soft Reset first, then Home or Unlock.`;
  if (code === 14) {
    return `${head}. Home, or Unlock to use it unhomed: Unlock marks every axis homed, so soft limits then count `
      + 'from here (Z may not go up). Home Z first if the bit may be in the work.';
  }
  return `${head}. Home ($H) or Unlock ($X).`;
}

export class MachinePanel {
  /** The buttons call actions.onConnect, onDisconnect, onRun, onHold, onResume, onReset, onZero(axes, value),
   *  onUndoZero, onHome(axes), onUnlock and onOverride({ which, step } | { which, to }). */
  constructor(actions = {}) {
    this.ov = null;
    // The buttons that matter now come first, so they show without scrolling in a short panel:
    // Connect… while not connected, Run / Hold / Resume / Soft Reset and the job while connected, and in
    // Alarm Home / Unlock right under those
    this.root = el('div', 'machine pad off', `
      <div class="state off">NOT CONNECTED</div>
      <div class="msub" hidden></div>
      <div class="outwarn" hidden><b>ALARM: the spindle and vacuum/AUX may still be on.</b> Soft Reset turns them off.</div>
      <p class="note" hidden></p>
      <div class="mbtns top" data-when="off"><button data-act="conn" class="primary">Connect…</button></div>
      <!-- four buttons whose labels never change: what each does is in its tooltip -->
      <div class="mbtns top mjobb" data-when="live" hidden>
        <button data-act="run" class="mrun"><i class="gl">&#x25B6;&#xFE0E;</i> Run</button>
        <button data-act="hold"><i class="gl">&#x23F8;&#xFE0E;</i> Hold</button>
        <button data-act="resume">Resume</button>
        <button data-act="reset">Soft Reset</button>
      </div>
      <div data-slot="alarm"></div>
      <div class="mjob" hidden>
        <div class="mjl"><span class="mjname"></span><b class="mjlev" hidden>leveled</b><span class="mjstate"></span><span class="mjcount"></span></div>
        <div class="mjbar" hidden><i></i></div>
        <div class="mjwhy" hidden></div>
      </div>
      <!-- the feed, spindle and rapid overrides: realtime, so also during a job, a hold or a jog -->
      <div class="movr" data-when="live" hidden>
        ${OVR.map(o => `<span class="olab" title="${o.title}">${o.label}</span>`
          + (o.which === 'rapid' ? `<span class="orap">${RAPIDS.map(v => `<button data-ov="rapid" data-to="${v}">${v}%</button>`).join('')}</span>`
            : `${[-10, -1].map(s => `<button data-ov="${o.which}" data-step="${s}">−${-s}</button>`).join('')}`
              + `<b class="oval" data-ovv="${o.which}">—</b>`
              + `${[1, 10].map(s => `<button data-ov="${o.which}" data-step="${s}">+${s}</button>`).join('')}`
              + `<button data-ov="${o.which}" data-to="100" class="oreset">100%</button>`)).join('')}
        <div class="onote" hidden></div>
      </div>
      <div class="stall" hidden><b>Lines not acknowledged.</b> FluidNC answers status requests but has not
        answered the last line for over 5 s. Nothing is sent by itself. If it stays stuck: Hold, wait for
        Hold, then Soft Reset.</div>
      <h3 class="drohead">Work position <span>(inches)</span><b class="mchip" data-f="chip"></b>
        <button data-act="zeroall" class="zsmall tsfloat"></button></h3>
      <div class="dro">
        ${AXES.map(a => `<div class="axis"><span>${a}</span><div><b>—</b>
          <input class="zin" type="text" inputmode="decimal" spellcheck="false" autocomplete="off" hidden
            title="The new work position of ${a} (inches). Enter sets it, Esc cancels.">
          <small><i>machine </i><span></span></small></div>
          <button class="zsmall" data-zero="${a}"></button></div>`).join('')}
      </div>
      <!-- under the position, not above it: appearing there moves no Zero button under the pointer -->
      <div class="zundo" hidden><span></span><button data-act="undo" class="zsmall"></button></div>
      <div class="abc"></div>
      <div class="row"><span>Feed</span><b data-f="feed">—</b></div>
      <div class="row"><span>Spindle</span><b data-f="spindle">—</b></div>
      <div class="row"><span>Modes</span><b data-f="modal">—</b></div>
      <div class="row" hidden><span>Pins</span><b data-f="pins"></b></div>
      <div class="row"><span>Firmware</span><b data-f="fw">—</b></div>
      <div class="row"><span>Port</span><b data-f="port">—</b></div>
      <div class="mbtns" data-when="live" hidden><button data-act="disc" title="Close the serial port">Disconnect</button></div>
      <div data-slot="normal">
        <div class="mbtns hbtns">
          <button data-act="home">Home ($H)</button>
          <button data-act="homez">Home Z ($HZ)</button>
          <button data-act="unlock">Unlock ($X)</button>
        </div>
      </div>`);
    const q = s => this.root.querySelector(s);
    this.badge = q('.state');
    this.sub = q('.msub');
    this.stall = q('.stall');
    this.outwarn = q('.outwarn');
    this.axes = [...this.root.querySelectorAll('.axis')].map((a, i) => ({
      name: AXES[i], w: a.querySelector('b'), m: a.querySelector('small span'), input: a.querySelector('.zin'),
      zero: a.querySelector('[data-zero]'),
    }));
    this.abc = q('.abc');
    this.f = Object.fromEntries([...this.root.querySelectorAll('[data-f]')].map(b => [b.dataset.f, b]));
    this.btn = Object.fromEntries([...this.root.querySelectorAll('[data-act]')].map(b => [b.dataset.act, b]));
    this.note = q('.note');
    this.when = [...this.root.querySelectorAll('[data-when]')];
    this.hbtns = q('.hbtns');
    this.slot = { alarm: q('[data-slot=alarm]'), normal: q('[data-slot=normal]') };
    this.job = {
      box: q('.mjob'), name: q('.mjname'), state: q('.mjstate'), count: q('.mjcount'), bar: q('.mjbar'),
      fill: q('.mjbar i'), why: q('.mjwhy'), lev: q('.mjlev'),
    };
    // No key presses the buttons that move the machine (Run, Resume, Home; the two-step ones too) or change its
    // speed (the overrides): see mouseOnly
    for (const b of ['run', 'resume', 'home', 'homez']) mouseOnly(this.btn[b]);
    this.ovr = {
      box: q('.movr'), note: q('.onote'), btns: [...this.root.querySelectorAll('[data-ov]')],
      val: Object.fromEntries([...this.root.querySelectorAll('[data-ovv]')].map(b => [b.dataset.ovv, b])),
    };
    for (const b of this.ovr.btns) {
      mouseOnly(b);
      b.onclick = () => {
        if (b.disabled) return;
        const which = b.dataset.ov;
        actions.onOverride?.(b.dataset.step ? { which, step: +b.dataset.step } : { which, to: +b.dataset.to });
      };
    }
    this.btn.conn.onclick = () => actions.onConnect?.();
    this.btn.disc.onclick = () => actions.onDisconnect?.();
    this.btn.run.onclick = () => actions.onRun?.();
    this.btn.hold.onclick = () => actions.onHold?.();
    this.btn.resume.onclick = () => actions.onResume?.();
    this.btn.reset.onclick = () => actions.onReset?.();
    this.btn.home.onclick = () => actions.onHome?.('');
    this.btn.homez.onclick = () => actions.onHome?.('Z');
    this.btn.unlock.onclick = () => actions.onUnlock?.();
    this.undoRow = q('.zundo');
    // Zero, Zero All and Undo Zero are two-step: a stray click (or a double-click) only asks (twostep.js)
    this.zeroAll = new TwoStep(this.btn.zeroall, { label: 'Zero All', ask: ['Zero All?', 'click again'],
      run: () => actions.onZero?.('XYZ', 0) });
    this.undo = new TwoStep(this.btn.undo, { label: 'Undo Zero', ask: ['Undo Zero?', 'click again'],
      run: () => actions.onUndoZero?.() });
    for (const ax of this.axes) {
      ax.ts = new TwoStep(ax.zero, { label: 'Zero', ask: [`Zero ${ax.name}?`, 'click again'], run: () => actions.onZero?.(ax.name, 0) });
      // Click a work position to type a new one: Enter sets it (G10 L20 P0), Esc or leaving the box cancels
      ax.w.onclick = e => { if (e.detail && this.zeroOk) this.edit(ax); };
      ax.input.onkeydown = e => {
        if (e.key === 'Escape') { e.preventDefault(); this.endEdit(ax); }
        if (e.key !== 'Enter') return;
        e.preventDefault();
        const text = ax.input.value.trim();
        const v = Number(text);
        if (!text || !Number.isFinite(v) || Math.abs(v) > MAX_ZERO) {
          ax.input.classList.add('bad');
          ax.input.title = `A number from -${MAX_ZERO} to ${MAX_ZERO} (inches). Esc cancels.`;
          return;
        }
        this.endEdit(ax);
        // The value it showed, not changed: nothing is sent (setting the shown, rounded value would move the zero)
        if (ax.was !== null && Math.abs(v - ax.was) < 5e-5) return;
        actions.onZero?.(ax.name, v);
      };
      ax.input.onblur = () => this.endEdit(ax);
    }
    this.update({});
  }

  edit(ax) {
    ax.input.value = ax.w.textContent === '—' ? '' : ax.w.textContent;
    ax.was = ax.input.value ? Number(ax.input.value) : null;      // (what it showed: Enter on that sends nothing)
    ax.input.classList.remove('bad');
    ax.w.hidden = true;
    ax.input.hidden = false;
    ax.input.focus();
    ax.input.select();
  }

  endEdit(ax) {
    if (ax.input.hidden) return;
    ax.input.hidden = true;
    ax.w.hidden = false;
  }

  /** The height (px) that shows the panel down to the whole position (DRO) once connected, for the default
   *  layout. That is mostly made before connecting, when the Connect… button and its note above the DRO
   *  take more room: sized by those, the Jog panel below it would lose its message and step rows. The job
   *  line counts (it shows once a file is open). */
  neededHeight() {
    const box = this.root.getBoundingClientRect();
    if (!box.height) return 330;
    // Laid out for a moment as when connected and not in an alarm (Run / Hold / Resume / Soft Reset instead
    // of Connect…, no note, a job line, Home / Unlock in their usual place under the DRO), measured, then put back
    const rows = [this.note, this.sub, this.stall, ...this.when, this.job.box, this.job.bar];
    const was = rows.map(r => r.hidden), slot = this.hbtns.parentElement;
    const text = this.job.name.textContent;
    for (const r of rows) r.hidden = r !== this.job.box && r !== this.job.bar && r.dataset.when !== 'live';
    if (!text) this.job.name.textContent = 'job.nc';
    this.slot.normal.append(this.hbtns);
    const z = this.axes.at(-1).w.closest('.axis').getBoundingClientRect();
    const h = Math.ceil(z.bottom - this.root.getBoundingClientRect().top + this.root.scrollTop + 6);
    rows.forEach((r, i) => { r.hidden = was[i]; });
    this.job.name.textContent = text;
    slot.append(this.hbtns);
    return h;
  }

  /** m: { link, linkMsg, connected, port, firmware, status, modal, jog, zeroUndo, alarm, program, job, scan,
   *  prog }, as the app keeps them. */
  update(m) {
    const live = m.link === 'up' && !!m.connected;
    if (live !== this.live) this.root.scrollTop = 0;    // connected or not: show the badge and the buttons that matter now
    this.live = live;
    const st = live ? m.status : null;
    const state = !live ? 'Not connected' : st?.state || 'Connected';
    if (!live) this.ov = null;
    else if (st?.ov) this.ov = st.ov;          // FluidNC reports the overrides only now and then

    set(this.badge, state.toUpperCase());
    this.badge.className = 'state ' + (STATE_CLASS[state] || 'off');
    const sub = st?.sub != null && st.sub !== '' ? `${state}:${st.sub}` : '';
    set(this.sub, SUB_TEXT[sub] || sub);
    this.sub.hidden = !sub;
    this.stall.hidden = !st?.stalled;
    this.root.classList.toggle('off', !st);
    this.root.classList.toggle('stale', state === 'No response');

    AXES.forEach((a, i) => {
      set(this.axes[i].w, st ? fmt4(st.wpos?.[i]) : '—');
      set(this.axes[i].m, st ? fmt4(st.mpos?.[i]) : '—');
    });
    const abc = st?.abc || [];           // extra axes, in their own units
    if (this.abc.childElementCount !== abc.length) {
      this.abc.innerHTML = abc.map((v, i) => `<div class="row"><span>${'ABC'[i] || '?'}</span><b></b></div>`).join('');
    }
    abc.forEach((v, i) => set(this.abc.children[i].lastChild, Number.isFinite(v) ? v.toFixed(3) : '—'));

    // The live feed and spindle speed (FluidNC's FS report: the spindle's is the programmed S times its override),
    // with the F and S in force when the server says them
    const pf = Number.isFinite(st?.programmedF) ? ` · F${+st.programmedF.toFixed(1)}` : '';
    const ps = Number.isFinite(st?.programmedS) ? ` · S${Math.round(st.programmedS)}` : '';
    set(this.f.feed, Number.isFinite(st?.feed) ? `${st.feed.toFixed(1)} in/min${pf}` : '—');
    set(this.f.spindle, Number.isFinite(st?.spindle) ? `${Math.round(st.spindle)} rpm${ps}` : '—');
    this.f.feed.title = pf ? 'The feed now (FluidNC\'s report, with the feed override) · the F in force' : '';
    this.f.spindle.title = (ps ? 'The spindle speed now (the S in force times the spindle override) · the S in force' : '')
      + (st?.spindleType ? `${ps ? '. ' : ''}FluidNC's spindle: ${st.spindleType}` : '');
    const md = live ? m.modal : null;
    const odd = !!md?.units && (md.units !== 'G20' || md.distance === 'G91');     // not inch + absolute
    set(this.f.modal, md?.units ? `${md.units} ${md.units === 'G20' ? 'inch' : 'mm'} · ${md.distance} ${md.distance === 'G91' ? 'incr' : 'abs'}` : '—');
    this.f.modal.classList.toggle('amber', odd);
    this.f.modal.title = md?.gc || '';
    // The same, short, next to the heading: it must show without scrolling
    set(this.f.chip, md?.units ? `${md.units} · ${md.distance}` : '');
    this.f.chip.classList.toggle('amber', odd);
    this.f.chip.title = md?.units ? `FluidNC reads the lines you send in ${md.units === 'G20' ? 'inches' : 'millimeters'}, `
      + `${md.distance === 'G91' ? 'incremental (G91)' : 'absolute (G90)'}. The position is always shown in inches.` : '';
    const pins = st?.pins || '';
    this.f.pins.parentElement.hidden = !pins;
    set(this.f.pins, [...pins].map(c => PIN_NAMES[c] || c).join(', '));
    set(this.f.fw, live && m.firmware ? firmwareName(m.firmware) : '—');
    this.f.fw.title = live ? m.firmware || '' : '';
    set(this.f.port, live && m.port ? m.port : '—');

    for (const row of this.when) row.hidden = (row.dataset.when === 'live') !== live;
    this.btn.conn.disabled = m.link !== 'up';
    this.btn.conn.title = m.link === 'up' ? '' : m.linkMsg || '';
    const critical = live && !!m.alarm?.critical;
    // FluidNC 3.9.4 leaves the spindle and the coolant on in an alarm: only a reset turns them off
    const outputs = live && !!m.alarm?.outputs;
    this.outwarn.hidden = !outputs;
    this.jobButtons(m, live, state, critical, outputs);
    this.overrides(m, live, st);
    this.buttons(m, live, state, critical);
    this.showJob(m);

    let note = '';
    if (m.link === 'down' || m.link === 'missing') note = m.linkMsg || '';
    else if (!live) note = 'Press Connect… to choose the port. FluidSender never connects by itself.';
    else if (state === 'Alarm') note = alarmNote(m.alarm);
    else if (state === 'No response') {
      note = 'FluidNC has not answered a status request for 2 s. FluidSender keeps asking and sends nothing else. Check the USB cable and the controller power.';
    }
    set(this.note, note);
    this.note.hidden = !note;
    this.note.classList.toggle('bad', state === 'Alarm' || state === 'No response' || m.link === 'down');
  }

  /** ▶ Run, ⏸ Hold, Resume, Soft Reset: four buttons whose labels never change (their tooltips say what each
   *  does now, or why not). Run and Soft Reset know about the job (app.js asks before either). */
  jobButtons(m, live, state, critical, outputs) {
    const active = jobActive(m.job), scanning = !!m.scan?.running;
    this.runWhy = runWhy(m);
    const b = this.btn;
    b.run.disabled = !!this.runWhy;
    const prog = m.prog?.state === 'ok' || m.prog?.state === 'sending' ? m.prog : null;
    const other = programMismatch(m);
    const lev = m.lev?.active ? m.lev : null;
    const lines = lev ? lev.lines : prog?.lines;
    const what = `${prog?.name || 'the G-code'}${lev ? ', LEVELED' : ''}${Number.isFinite(lines) ? `, ${lines.toLocaleString()} lines` : ''}`;
    b.run.title = this.runWhy ? `Run: ${this.runWhy}. (Run starts the loaded G-code from the top.)`
      : `Run: starts the loaded G-code from the top (${what}). `
        + (other ? `${other.replace('; Run here loads this file first', '')}, so Run loads this one first, then asks` : 'Asks first.');
    b.run.classList.toggle('go', !this.runWhy);
    // In an ALARM a feed hold does nothing useful, and after a hard or soft limit it would let FluidNC out of
    // the alarm without homing (FluidNC 3.9.4 and 4.1.1): the server refuses Hold and Resume there
    const alarm = live && state === 'Alarm';
    b.hold.disabled = !live || alarm;
    b.hold.title = alarm ? 'Hold: not in an ALARM (there it would let FluidNC out of a limit alarm without homing). '
      + `${critical ? 'Soft Reset first, then' : ''} Home or Unlock.`.trim()
      : 'Hold (feed hold, !): slows down to a stop and keeps the position; a job waits there. Resume goes on.';
    b.hold.classList.toggle('warnb', !alarm && (state === 'Run' || state === 'Jog' || m.job?.state === 'running'));
    const held = !alarm && (state === 'Hold' || state === 'Door' || m.job?.state === 'paused');
    b.resume.disabled = !live || !held;
    b.resume.title = (alarm ? 'Resume: not in an ALARM. '
      : live && !held ? 'Resume (~): nothing is paused now. ' : 'Resume (~): ')
      + 'It continues after a Hold, an M0 pause or an error pause (a job goes on with the next line).';
    b.resume.classList.toggle('primary', live && held);
    b.reset.disabled = !live;
    b.reset.title = active ? 'Soft Reset: stops the job safely (Hold first, then a reset once the machine has stopped). Asks first.'
      : scanning ? 'Soft Reset: stops the surface scan safely (Hold first, then a reset once stopped). Asks first.'
        : outputs ? 'Soft Reset (Ctrl-X): turns the spindle, the vacuum and AUX off, which the alarm left on. Asks first.'
          : 'Soft Reset (Ctrl-X): stops everything at once and drops the lines waiting to be sent; during a job it stops '
            + 'the job safely instead. Asks first.';
    b.reset.classList.toggle('primary', critical && !outputs);
    b.reset.classList.toggle('danger', outputs);
  }

  /** The overrides block: the values FluidNC reports (or is being set to), amber when not 100 %, the buttons
   *  enabled whenever FluidNC answers (also during a job and a hold; FluidNC takes them in every state). */
  overrides(m, live, st) {
    const O = this.ovr;
    const answering = live && !!st && st.state !== 'No response';
    const why = !live ? 'not connected' : !answering ? 'no response from FluidNC' : '';
    const ov = Array.isArray(st?.ov) ? st.ov : this.ov, target = Array.isArray(st?.ovTarget) ? st.ovTarget : null;
    const known = !!ov;
    const values = {};
    for (const o of OVR) {
      const got = known ? ov[o.i] : null, want = target?.[o.i] ?? null;
      const v = want ?? got;
      values[o.which] = v;
      const el = O.val[o.which];
      if (!el) continue;
      const pending = want !== null && got !== null && want !== got;
      set(el, v == null ? '—' : `${v}%${pending ? '…' : ''}`);
      el.classList.toggle('amber', v != null && v !== 100);
      el.classList.toggle('pend', pending);
      el.title = v == null ? `${o.label} override: not known yet (FluidNC reports it in its status)`
        : `${o.label} override ${v}%${pending ? ` (FluidNC reports ${got}% so far)` : ''}`;
    }
    for (const b of O.btns) {
      const which = b.dataset.ov, v = values[which], step = b.dataset.step ? +b.dataset.step : 0, to = +b.dataset.to;
      const o = OVR.find(x => x.which === which);
      let no = why;
      // (feed and spindle need the values FluidNC has now, as the server does; the rapid values are absolute)
      if (!no && which !== 'rapid' && !known) no = 'not known yet: FluidNC reports the overrides in its status';
      if (!no && step > 0 && v >= OV_MAX) no = `already at ${OV_MAX}%`;
      if (!no && step < 0 && v <= OV_MIN) no = `already at ${OV_MIN}%`;
      b.disabled = !!no;
      const does = step ? `${o.label} override ${step > 0 ? '+' : '−'}${Math.abs(step)}%` : `${o.label} override ${to}%`;
      b.title = no ? `${does}: ${no}` : `${does} (at once; also during a job)`;
      b.classList.toggle('on', !step && v === to);
    }
    // Under it: the spindle speed now (FluidNC's FS: the S in force times the override); an on/off spindle
    const notes = [];
    if (answering && Number.isFinite(st?.spindle)) {
      notes.push([`S now ${Math.round(st.spindle)}${Number.isFinite(st?.programmedS) ? ` (programmed S${Math.round(st.programmedS)})` : ' rpm'}`, 'dim']);
    }
    if (answering && ON_OFF.test(st?.spindleType || '')) {
      // (two lines at most in a 290 px panel: the whole sentence is in its tooltip and in Help)
      notes.push([`On/off spindle: the override changes only the S reported; below 100% a small S can switch it off`, 'amber']);
    }
    const key = JSON.stringify(notes);
    if (O.noteKey !== key) {
      O.noteKey = key;
      O.note.replaceChildren(...notes.map(([text, cls]) => Object.assign(document.createElement('div'), { className: cls, textContent: text, title: text })));
    }
    O.note.hidden = !notes.length;
  }

  /** The job line: the job while it runs (name, state, lines answered of all, %, time), else the program
   *  this page has loaded (or that the server has another one), and what the last job came to. Its second
   *  line is one line at most (the whole text in its tooltip; the status bar and the Console have it too). */
  showJob(m) {
    const J = this.job, job = m.job, p = m.prog || {}, active = jobActive(job);
    let name = '', stateText = '', cls = '', count = '', why = '', whyCls = '', pct = null;
    // In an alarm, the alarm that ended the job is said by the alarm note right above: not twice (another
    // one, like the ALARM:1 before a reset's ALARM:14, is still said)
    const code = m.alarm?.code, ended = job?.error || job?.why || '';
    const alarmSaid = !active && m.link === 'up' && !!m.connected && m.status?.state === 'Alarm'
      && (code == null ? /ALARM/.test(ended) : new RegExp(`ALARM:${code}(?!\\d)`).test(ended));
    if (active || (job && !p.name)) {
      const [text, c] = JOB_STATE[job.state] || [job.state, ''];
      name = job.name || 'job';
      stateText = text;
      cls = c;
      const total = Number.isFinite(job.total) ? job.total : 0, done = Number.isFinite(job.acked) ? job.acked : 0;
      pct = total ? Math.min(100, (100 * done) / total) : 0;
      count = `${done.toLocaleString()} / ${total.toLocaleString()} · ${Math.floor(pct)}%`
        + (Number.isFinite(job.elapsed) ? ` · ${fmtClock(job.elapsed)}` : '');
      if (job.error) { why = job.error; whyCls = 'bad'; } else if (job.why) why = job.why;
      if (alarmSaid) why = '';
      if (!active) pct = null;
    } else if (p.name) {
      name = p.name;
      [stateText, cls] = PROG_STATE[p.state] || ['', ''];
      const lines = m.lev?.active && Number.isFinite(m.lev.lines) ? m.lev.lines : p.lines;     // (leveled: what Run sends)
      if ((p.state === 'ok' || p.state === 'sending') && Number.isFinite(lines)) count = `${lines.toLocaleString()} lines`;
      if (p.state === 'error') { why = p.error; whyCls = 'bad'; }
      const warn = p.state === 'ok' || p.state === 'sending' ? p.warnings || [] : [];
      if (warn.length) {     // it runs, but FluidNC reads these lines otherwise (the Console has them all)
        why = `⚠ ${warn[0]}${warn.length > 1 ? ` (and ${warn.length - 1} more: see the Console)` : ''}`;
        whyCls = 'amber';
      }
      if (p.state === 'old') why = 'This FluidSender server cannot run jobs yet (restart FluidSender after the update)';
      const other = programMismatch(m);
      if (other) {           // the server has another program (another window's): the pendant's Run would run that
        [stateText, cls] = ['Not loaded', 'hold'];
        why = other;
        whyCls = 'amber';
      }
      if (job && !why && !alarmSaid) {     // what the last job came to
        const [text] = JOB_STATE[job.state] || [job.state];
        why = `Last job${job.name && job.name !== p.name ? ` (${job.name})` : ''}: ${text}`
          + (job.state === 'done' && Number.isFinite(job.elapsed) ? ` in ${fmtClock(job.elapsed)}` : '')
          + (job.state !== 'done' && Number.isFinite(job.line) && job.line > 0 ? ` at line ${job.line.toLocaleString()}` : '')
          + (job.error ? `: ${job.error}` : job.why ? `: ${job.why}` : '');
        whyCls = job.state === 'failed' || job.error ? 'bad' : 'dim';
      }
    }
    J.box.hidden = !name;
    set(J.name, name);
    J.name.title = name;
    // "leveled": the job runs a leveled program (auto-level), or the program loaded here is one
    const leveled = active ? job.leveled === true : !!name && !!m.lev?.active && !programMismatch(m);
    J.lev.hidden = !leveled;
    J.lev.title = leveled ? `${active ? 'This job runs' : 'Run runs'} the G-code leveled with the scan (auto-level)` : '';
    set(J.state, stateText);
    J.state.className = 'mjstate ' + cls;
    set(J.count, count);
    J.bar.hidden = pct === null;
    if (pct !== null) J.fill.style.width = `${pct.toFixed(2)}%`;
    set(J.why, why);
    J.why.title = why;
    J.why.hidden = !why;
    J.why.className = 'mjwhy ' + whyCls;
  }

  /** Zero, Undo Zero, Home and Unlock: enabled as the server allows them (it refuses the rest anyway). */
  buttons(m, live, state, critical) {
    const jogging = !!m.jog;          // a hold jog runs: the server takes nothing else meanwhile
    const busy = jobActive(m.job) ? 'a job is running' : m.scan?.running ? 'a surface scan is running' : '';
    const why = !live ? 'not connected' : critical ? 'FluidNC needs a Soft Reset first'
      : busy || (jogging ? 'jogging' : m.status?.stalled ? 'FluidNC has not answered the last line'
        : state === 'No response' ? 'no response from FluidNC' : `not while ${state}`);
    const idle = state === 'Idle';
    this.zeroOk = live && !critical && !busy && !jogging && !m.status?.stalled && (idle || state === 'Jog');
    this.homeOk = live && !critical && !busy && !jogging && (idle || state === 'Alarm');
    this.unlockOk = live && !critical && !busy && state === 'Alarm';
    const tip = (b, ok, text) => {
      b.disabled = !ok;
      b.title = ok ? text : `${text} (${why})`;
    };
    // An armed Zero button bails out when any of this changes (a click, then the machine starts moving...)
    const key = `${live}|${state}|${jogging}|${busy}|${critical}|${!!m.status?.stalled}`;
    const two = (ts, ok, text) => {
      ts.context(key);
      ts.setEnabled(ok, ok ? `${text}. Two clicks: the first one asks.` : `${text} (${why})`);
    };
    for (const ax of this.axes) {
      two(ax.ts, this.zeroOk, `Zero ${ax.name}: G10 L20 P0 ${ax.name}0 (its work position becomes 0)`);
      ax.w.classList.toggle('zedit', this.zeroOk);
      ax.w.title = this.zeroOk ? `Click to type a new work position for ${ax.name}` : '';
      if (!this.zeroOk) this.endEdit(ax);
    }
    two(this.zeroAll, this.zeroOk, 'Zero All: G10 L20 P0 X0 Y0 Z0 (the work position of X, Y and Z becomes 0)');
    const undo = live ? m.zeroUndo : null;
    this.undoOk = !!undo && this.zeroOk && idle;
    this.undoRow.hidden = !undo;
    if (undo) {
      set(this.undoRow.firstChild, `Before the last zero${undo.source === 'pendant' ? ' (pendant)' : ''}: ${undo.text}`);
      this.undo.context(`${key}|${undo.t}|${undo.text}`);
      two(this.undo, this.undoOk, `Undo Zero: put back the work position from before the last zero (${undo.text}; ${undoWhen(undo)})`);
    } else this.undo.setEnabled(false, '');
    tip(this.btn.home, this.homeOk, 'Home all axes ($H). Asks first.');
    tip(this.btn.homez, this.homeOk, 'Home Z only ($HZ). Asks first.');
    tip(this.btn.unlock, this.unlockOk, 'Unlock ($X): leave the alarm without homing');

    // In Alarm, Home and Unlock go right under Run / Hold / Resume / Soft Reset (no scrolling to find them)
    const slot = state === 'Alarm' ? this.slot.alarm : this.slot.normal;
    if (this.hbtns.parentElement !== slot) slot.append(this.hbtns);
    this.hbtns.classList.toggle('top', slot === this.slot.alarm);
  }
}

// ------------------------------------------------------------------ console

const MAX_LINES = 500;        // each new line costs layout time in proportion (about 8 ms at 500 on this PC)
const HISTORY = 50;
// One formatter for every line: toLocaleTimeString with options builds a new one each time (slow on this PC)
const CLOCK = new Intl.DateTimeFormat([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });

export class ConsolePanel {
  /** onSend(line) runs when a line is entered. write(text, kind) shows the app's own messages. */
  constructor({ onSend } = {}) {
    this.onSend = onSend;
    this.root = el('div', 'console');
    this.log = el('div', 'clog');
    this.bar = el('div', 'cbar', `
      <input class="cin" type="text" spellcheck="false" autocomplete="off" maxlength="254">
      <label title="Also show FluidNC's status reports (at most 2 a second)"><input type="checkbox"> Verbose</label>
      <button title="Clear the console">Clear</button>`);
    this.root.append(this.log, this.bar);
    this.input = this.bar.querySelector('.cin');
    const verbose = this.bar.querySelector('[type=checkbox]');
    this.verbose = verbose.checked = !!store.get('console.verbose', false);
    verbose.onchange = () => { this.verbose = verbose.checked; store.set('console.verbose', this.verbose); };
    this.bar.querySelector('button').onclick = () => { this.log.textContent = ''; this.follow(true); this.autoTop = 0; };

    this.history = store.get('console.history', []);
    this.hpos = this.history.length;
    this.draft = '';
    this.input.onkeydown = e => this.key(e);
    this.autoTop = 0;                  // where our own scrolling left the log (it only ever scrolls down)
    this.readTop = 0;                  // where the user scrolled to read
    this.follow(true);                 // follow new lines, unless scrolled up to read
    this.log.onscroll = () => {
      if (!this.log.isConnected) return;
      // The event of our own scroll can come after more lines were added, so only a move up means reading
      if (this.log.scrollHeight - this.log.scrollTop - this.log.clientHeight < 8) this.follow(true);
      else if (this.log.scrollTop < this.autoTop - 1) this.follow(false);
      if (!this.stick) this.readTop = this.log.scrollTop;
    };
    this.recent = new Map();           // text -> { source, at }: see seen()
    this.lastRaw = 0;
    this.input.disabled = false;
    this.setEnabled(false);
  }

  /** Adds a line. kind: '' | 'err' | 'info' | 'warn' | 'tx' | 'rx' | 'raw'; t: unix seconds; note: dim text after it. */
  write(text, kind = '', { t, note } = {}) {
    const line = el('div', kind ? 'cline k-' + kind : 'cline');     // k-: page classes like .info don't apply
    line.textContent = `${CLOCK.format(new Date(t ? t * 1000 : Date.now()))}  ${text}`;
    if (note) line.append(el('span', 'cnote', esc(`  (${note})`)));
    this.log.append(line);
    while (this.log.childElementCount > MAX_LINES) this.log.firstChild.remove();
    if (this.stick) this.scrollDown();
  }

  /** Follow new lines (on) or keep still for reading (off). While reading, the browser's scroll anchoring
   *  keeps the text in place when old lines are dropped; while following it would fight the scrolling. */
  follow(on) {
    if (this.stick === on) return;
    this.stick = on;
    this.log.style.overflowAnchor = on ? 'none' : 'auto';
  }

  scrollDown() {
    if (this.scrollQueued) return;   // one scroll per frame, however many lines arrive
    this.scrollQueued = true;
    requestAnimationFrame(() => {
      this.scrollQueued = false;
      if (!this.stick || !this.log.isConnected) return;
      this.log.scrollTop = this.log.scrollHeight;
      this.autoTop = this.log.scrollTop;
    });
  }

  /** The panel is shown again (dockview took it out of the page while hidden, which loses the scroll position). */
  shown() {
    if (this.stick) this.scrollDown();
    else this.log.scrollTop = this.readTop;
  }

  /** An app message that may also arrive as a machine console line: shows it once. */
  note(text, kind = 'info') {
    if (!this.seen(text, 'app')) this.write(text, kind);
  }

  /** True if the other source showed the same text in the last 3 s. A match completes the pair and is
   *  forgotten, so the same message coming again (say, a second disconnect) is shown again. */
  seen(text, source) {
    const now = Date.now(), prev = this.recent.get(text);
    this.recent.delete(text);
    if (prev && prev.source !== source && now - prev.at < 3000) return true;
    this.recent.set(text, { source, at: now });
    if (this.recent.size > 50) this.recent.delete(this.recent.keys().next().value);
    return false;
  }

  /** A console event from the FluidNC connection: { dir: 'tx' | 'rx' | 'info' | 'error', text, t }. */
  machine(ev) {
    const text = String(ev.text ?? ''), t = ev.t;
    this.lastKey = `${t}|${ev.dir}|${text}`;
    if (ev.dir === 'tx') this.write('> ' + text, 'tx', { t });
    else if (ev.dir === 'info' || ev.dir === 'error') {
      if (!this.seen(text, 'machine')) this.write(text, ev.dir === 'info' ? 'info' : 'err', { t });
    } else {
      const kind = /^(error:|alarm:|\[MSG:ERR)/i.test(text) ? 'err' : /^\[MSG:WARN/i.test(text) ? 'warn' : 'rx';
      this.write(text, kind, { t, note: explain(text) });
    }
  }

  /** The console history from a snapshot. After the event stream reopens, shows only the new part. */
  replay(events) {
    const keys = events.map(e => `${e.t}|${e.dir}|${e.text}`);
    const from = this.lastKey ? keys.lastIndexOf(this.lastKey) + 1 : 0;
    events.slice(from).forEach(e => this.machine(e));
  }

  /** Verbose mode: shows a raw status report, at most 2 a second. */
  statusReport(raw) {
    if (!this.verbose || !raw) return;
    const now = performance.now();
    if (now - this.lastRaw < 500) return;
    this.lastRaw = now;
    this.write(raw, 'raw');
  }

  /** busy: why typed lines are refused now though connected (a job runs), shown in the box. */
  setEnabled(on, busy = '') {
    const text = !on ? 'Connect to FluidNC to send commands' : busy || 'Command to FluidNC, e.g. $$ or G0 X1   (Enter sends, ↑ ↓ history)';
    if (this.input.placeholder !== text) this.input.placeholder = text;
    if (this.input.disabled !== !on) this.input.disabled = !on;
  }

  key(e) {
    if (e.isComposing) return;
    if (e.key === 'Enter') {
      e.preventDefault();
      const line = this.input.value.trim();
      if (!line) return;
      this.history = this.history.filter(h => h !== line).concat(line).slice(-HISTORY);
      store.set('console.history', this.history);
      this.hpos = this.history.length;
      this.draft = '';
      this.input.value = '';
      this.onSend?.(line);
    } else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      e.preventDefault();
      const up = e.key === 'ArrowUp';
      if (up ? this.hpos === 0 : this.hpos >= this.history.length) return;
      if (up && this.hpos === this.history.length) this.draft = this.input.value;
      this.hpos += up ? -1 : 1;
      this.input.value = this.hpos < this.history.length ? this.history[this.hpos] : this.draft;
      this.input.setSelectionRange(this.input.value.length, this.input.value.length);
    }
  }
}
