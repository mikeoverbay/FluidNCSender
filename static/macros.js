// The Motion control panel: two tabs, Jog (jog.js, the whole jog panel: the pad, Go to XY / Z 0, the speed, the
// steps, Vacuum / AUX, the keys) and Macros (below). The chosen tab is remembered (store "motion.tab"), as the Scan
// panel's tabs are. The panel's id stays 'jog' (the saved layout, the View menu and app.js's sizes use it). The tab
// buttons sit in the dock's own tab bar, right of "Motion control", while the panel is the one shown there (app.js
// headerTabs), so they take no height from the jog pad; otherwise they go back on top of the panel.
//
// Macros: named lists of G-code lines the user writes and keeps (the server's settings.json "macros", shared with
// every window). The defaults (settings.py MACROS, until the user changes them): Park X axis (G53 G0 X0), Park Y
// axis (G53 G0 Y0), Park Z axis (G53 G0 Z0): each a rapid to machine zero on one axis, never a homing cycle ($H).
// A macro's button runs it (POST /api/machine/macro): the server sends its lines in order through the line queue,
// as typed ones (the console shows them), and stops at the first error or alarm; Feed Hold, Stop and Soft Reset
// drop the rest. Like Go to XY 0 the buttons take two clicks (twostep.js) and no key presses them (mouseOnly);
// they are off, with the reason in their tooltip, while a macro can't run. ✎ edits one, 🗑 deletes it (asks
// first), + New macro adds one, Restore defaults puts the default ones back (asks first; the user's own stay).
// Editing works while disconnected.

import { store } from './api.js';
import { TwoStep } from './twostep.js';
import { jobActive } from './machine.js';

// The server's defaults (settings.py MACROS): what Restore defaults puts back
export const DEFAULT_MACROS = [
  { name: 'Park X axis', code: 'G53 G0 X0' },
  { name: 'Park Y axis', code: 'G53 G0 Y0' },
  { name: 'Park Z axis', code: 'G53 G0 Z0' },
];
const MAX_NAME = 40;        // characters (settings.py MACRO_NAME)
const MAX_CODE = 4000;      // characters (settings.py MACRO_CODE)
const MAX_MACROS = 50;      // (settings.py MAX_MACROS)

const STATE_WHY = {
  Alarm: 'ALARM: Unlock or Home first',
  Home: 'Homing…',
  Run: 'Running: wait until FluidNC is Idle',
  Jog: 'Jogging',
  Hold: 'Hold: Resume or Soft Reset first',
  Door: 'Door open: close it, then Resume',
  Check: 'Check mode ($C): nothing moves',
  Sleep: 'Sleeping: Soft Reset to wake it',
};

/** Why a macro can't run now ('' when it can): the server's rules (connected, Idle, nothing else going). It
 *  refuses (409) anyway; this only greys the buttons and says why. */
export function macroWhy(m) {
  if (m.link === 'down' || m.link === 'missing') return 'No link to the FluidSender server';
  if (m.link !== 'up' || !m.connected) return 'Not connected';
  const st = m.status;
  if (!st) return 'Waiting for FluidNC';
  if (st.state === 'No response') return 'No response from FluidNC';
  if (m.alarm?.critical) return 'Needs a Soft Reset (FluidNC: Reset to continue)';
  if (m.macroRun) return `Macro ${m.macroRun.name} is running`;
  if (jobActive(m.job)) return 'A job is running';
  if (m.scan?.running) return 'A scan is running';
  if (m.jog) return 'Jogging';
  if (st.state !== 'Idle') return STATE_WHY[st.state] || `Not while FluidNC is in ${st.state}`;
  if (st.stalled) return 'FluidNC has not answered the last line';
  return '';
}

/** The code as the server keeps it: lines trimmed, the empty ones dropped. */
const tidy = code => code.split(/\r?\n/).map(l => l.trim()).filter(Boolean).join('\n');
const sameName = (a, b) => a.trim().toLowerCase() === b.trim().toLowerCase();

export class MotionPanel {
  /** jog: the JogPanel, macros: the MacroTab; each a tab of this panel. */
  constructor(jog, macros) {
    this.jog = jog;
    this.macros = macros;
    this.root = document.createElement('div');
    this.root.className = 'motion';
    this.tabs = document.createElement('div');
    this.tabs.className = 'stabs mtabs';
    this.tabs.setAttribute('role', 'tablist');
    this.tabs.innerHTML = `<button data-tab="jog" role="tab" title="The jog pad, Go to XY / Z 0, the jog speed and steps, Vacuum / AUX">Jog</button>
      <button data-tab="macros" role="tab" title="Your G-code macros: two clicks run one">Macros</button>`;
    this.root.append(this.tabs, jog.root, macros.root);
    this.tabBtns = [...this.tabs.querySelectorAll('[data-tab]')];
    for (const t of this.tabBtns) t.onclick = () => this.setTab(t.dataset.tab);
    this.setTab(store.get('motion.tab', 'jog') === 'macros' ? 'macros' : 'jog');
  }

  /** Shows the Jog ('jog') or Macros ('macros') tab; remembered for next time. A jog held down stops. */
  setTab(tab) {
    this.tab = tab === 'macros' ? 'macros' : 'jog';
    store.set('motion.tab', this.tab);
    if (this.tab !== 'jog') this.jog.release(this.jog.press, 'the Jog tab was hidden');
    this.jog.root.hidden = this.tab !== 'jog';
    this.macros.root.hidden = this.tab !== 'macros';
    for (const t of this.tabBtns) {
      const on = t.dataset.tab === this.tab;
      t.classList.toggle('on', on);
      t.setAttribute('aria-selected', String(on));
    }
  }

  /** Puts the tab buttons in el (the dock's tab bar of the group showing this panel), or back on top of the panel
   *  (el null). */
  tabsInto(el) {
    const to = el || this.root;
    if (this.tabs.parentElement === to) return;
    if (el) el.append(this.tabs); else this.root.prepend(this.tabs);
    this.tabs.classList.toggle('inhead', !!el);
  }

  /** The machine state changed: the Macros tab says when one runs (● as the Scan panel's tabs do). */
  update(m) {
    this.tabBtns.find(t => t.dataset.tab === 'macros').classList.toggle('busy', !!m.macroRun);
  }

  /** The height (px) that shows the whole Jog tab without scrolling, with the tab bar when it is in the panel (for
   *  the default layout). */
  fullHeight() {
    const jog = this.jog.root, hidden = jog.hidden;
    jog.hidden = false;                 // (measured as the Jog tab, whichever tab shows)
    const h = this.jog.fullHeight();
    jog.hidden = hidden;
    const bar = this.tabs;
    if (bar.parentElement !== this.root) return h;
    const css = this.root.isConnected ? getComputedStyle(bar) : null;
    return h + (css ? bar.offsetHeight + parseFloat(css.marginTop) + parseFloat(css.marginBottom) : 26);
  }
}

export class MacroTab {
  /** machine: the MachineClient. onRun(name): run that macro (after the second click). log(text, kind): app
   *  messages. */
  constructor(machine, { onRun, log } = {}) {
    this.machine = machine;
    this.onRun = onRun;
    this.log = log;
    this.m = {};
    this.list = null;          // the macros as the server has them ([{ name, code }]; null: not heard yet)
    this.key = '';             // (the list drawn: redrawn only when it changes)
    this.buttons = [];         // [{ step: TwoStep, macro }]
    this.editing = null;       // the editor open: { name } of the macro edited ('' for a new one)
    this.saving = false;
    this.root = document.createElement('div');
    this.root.className = 'macros pad';
    this.root.innerHTML = `
      <div class="mclist" role="list"></div>
      <div class="mcrun dim" hidden></div>
      <div class="mcmsg" hidden></div>
      <div class="mcfoot">
        <button data-act="new" title="Write a macro: a name and its G-code lines, run in order">+ New macro</button>
        <button data-act="defaults" title="Put back the default macros (Park X / Y / Z axis); your own macros stay">Restore defaults</button>
      </div>
      <form class="mcedit" hidden>
        <label>Name <input data-f="name" type="text" maxlength="${MAX_NAME}" spellcheck="false" autocomplete="off"></label>
        <label class="mccode">G-code, one line each, run in order
          <textarea data-f="code" rows="5" maxlength="${MAX_CODE}" spellcheck="false" placeholder="G53 G0 Z0&#10;G53 G0 X0 Y0"></textarea></label>
        <div class="mcerr bad" hidden></div>
        <div class="mcbtns">
          <button type="submit" class="primary">Save</button>
          <button type="button" data-act="cancel">Cancel</button>
        </div>
      </form>`;
    const q = s => this.root.querySelector(s);
    this.listEl = q('.mclist');
    this.runEl = q('.mcrun');
    this.msgEl = q('.mcmsg');
    this.footEl = q('.mcfoot');
    this.form = q('.mcedit');
    this.nameIn = q('[data-f=name]');
    this.codeIn = q('[data-f=code]');
    this.errEl = q('.mcerr');
    q('[data-act=new]').onclick = () => this.edit(null);
    q('[data-act=defaults]').onclick = () => this.restoreDefaults();
    q('[data-act=cancel]').onclick = () => this.closeEditor();
    this.form.onsubmit = e => { e.preventDefault(); this.save(); };
    this.form.onkeydown = e => { if (e.key === 'Escape') { e.preventDefault(); this.closeEditor(); } };
    this.listEl.addEventListener('click', e => {
      const b = e.target.closest('[data-edit], [data-del]');
      if (!b) return;
      const macro = this.list?.[+(b.dataset.edit ?? b.dataset.del)];
      if (!macro) return;
      if (b.dataset.edit !== undefined) this.edit(macro); else this.remove(macro);
    });
    this.draw();
  }

  /** The server's macro list (the snapshot's, or a "macros" event). */
  setMacros(list) {
    this.list = Array.isArray(list) ? list.filter(m => m && typeof m.name === 'string' && typeof m.code === 'string') : null;
    this.draw();
  }

  /** The machine state changed: the buttons on or off, and the macro running. */
  update(m) {
    this.m = m;
    const why = macroWhy(m);
    const ctx = [m.connected, m.status?.state, jobActive(m.job), !!m.scan?.running, !!m.macroRun, !!m.jog].join();
    for (const { step, macro } of this.buttons) {
      step.context(ctx);
      step.setEnabled(!why, why ? `${macro.name}: ${why}\n\n${macro.code}` : `${macro.name} (two clicks):\n${macro.code}`);
      step.btn.classList.toggle('busy', m.macroRun?.name === macro.name);
    }
    const r = m.macroRun;
    const text = r ? `Running macro ${r.name}: ${r.line ? `line ${r.line} of ${r.total}` : 'starting'}` : '';
    if (this.runEl.textContent !== text) this.runEl.textContent = text;
    this.runEl.hidden = !text;
  }

  /** The list of macro buttons, each with ✎ (edit) and 🗑 (delete). */
  draw() {
    const list = this.list;
    const key = JSON.stringify(list);
    if (key === this.key) return;
    this.key = key;
    this.buttons = [];
    this.listEl.replaceChildren();
    if (!list) {
      this.listEl.innerHTML = '<div class="dim">Waiting for the FluidSender server…</div>';
      return;
    }
    if (!list.length) this.listEl.innerHTML = '<div class="dim">No macros: + New macro writes one, Restore defaults puts back Park X / Y / Z axis.</div>';
    list.forEach((macro, i) => {
      const row = document.createElement('div');
      row.className = 'mcrow';
      row.setAttribute('role', 'listitem');
      row.innerHTML = `<button class="mcbtn"></button>
        <button class="mcicon" data-edit="${i}" title="Edit this macro">✎</button>
        <button class="mcicon" data-del="${i}" title="Delete this macro (asks first)">🗑</button>`;
      const btn = row.querySelector('.mcbtn');
      const label = document.createElement('span');
      label.textContent = macro.name;       // (the user's text: never as HTML)
      const step = new TwoStep(btn, {
        label: '',
        ask: () => ({ lines: [`${macro.name}?`, 'click again'], title: `Run the macro ${macro.name} (${macro.code.split('\n').join(' · ')})?` }),
        run: () => this.onRun?.(macro.name),
      });
      btn.querySelector('.ts-label').append(label);
      this.buttons.push({ step, macro });
      this.listEl.append(row);
    });
    this.update(this.m);
  }

  // ---------------------------------------------------------------- editing (works while disconnected)

  /** Opens the editor on a macro (null: a new one). */
  edit(macro) {
    this.editing = { name: macro ? macro.name : '' };
    this.nameIn.value = macro ? macro.name : '';
    this.codeIn.value = macro ? macro.code : '';
    this.errEl.hidden = true;
    this.form.hidden = false;
    this.listEl.hidden = this.footEl.hidden = true;
    this.say('');
    this.nameIn.focus();
  }

  closeEditor() {
    this.editing = null;
    this.form.hidden = true;
    this.listEl.hidden = this.footEl.hidden = false;
  }

  async save() {
    if (this.saving || !this.editing) return;
    const name = this.nameIn.value.trim().replace(/\s+/g, ' ');
    const code = tidy(this.codeIn.value);
    const fail = text => { this.errEl.textContent = text; this.errEl.hidden = false; };
    if (!name) return fail('Give it a name');
    if (!code) return fail('Write its G-code: one line each');
    const list = [...(this.list || [])];
    const at = this.editing.name ? list.findIndex(m => sameName(m.name, this.editing.name)) : -1;
    if (list.some((m, i) => i !== at && sameName(m.name, name))) return fail(`There is a macro named ${name} already`);
    if (at >= 0) list[at] = { name, code }; else list.push({ name, code });
    if (await this.store(list, fail)) {
      this.closeEditor();
      this.say(`Saved the macro ${name}`);
    }
  }

  remove(macro) {
    if (!confirm(`Delete the macro ${macro.name}?\n\n${macro.code}`)) return;
    this.store(this.list.filter(m => m !== macro), text => this.say(text, 'bad'))
      .then(ok => ok && this.say(`Deleted the macro ${macro.name}`));
  }

  /** Puts back the default macros: the ones missing are added at the end, the ones there get the default code
   *  again; the user's own macros stay. */
  restoreDefaults() {
    if (!confirm(`Restore the default macros?\n\n${DEFAULT_MACROS.map(d => `${d.name}: ${d.code}`).join('\n')}\n\n`
      + 'Each is added again if it is missing, or gets its default G-code back. Your own macros stay.')) return;
    const list = [...(this.list || [])];
    for (const d of DEFAULT_MACROS) {
      const at = list.findIndex(m => sameName(m.name, d.name));
      if (at >= 0) list[at] = { ...d }; else list.push({ ...d });
    }
    if (list.length > MAX_MACROS) { this.say(`That would make ${list.length} macros; at most ${MAX_MACROS}`, 'bad'); return; }
    this.store(list, text => this.say(text, 'bad')).then(ok => ok && this.say('Restored the default macros'));
  }

  /** Sends the whole list to the server (it checks every line). fail(text): its refusal. */
  async store(list, fail) {
    this.saving = true;
    try {
      const out = await this.machine.setMacros(list);
      this.setMacros(out.macros);
      return true;
    } catch (e) {
      fail(e.status === 404 ? 'This FluidSender server has no macros yet: restart FluidSender after the update' : e.message);
      return false;
    } finally {
      this.saving = false;
    }
  }

  /** A line under the list: what the last edit did ('' hides it). */
  say(text, cls = 'dim') {
    this.msgEl.textContent = text;
    this.msgEl.className = `mcmsg ${cls}`;
    this.msgEl.hidden = !text;
  }
}

