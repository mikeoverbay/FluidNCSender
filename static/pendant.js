// The Bluetooth jog pendant: Machine → Connect Pendant… and the "Pendant:" chip in the status bar.
//
// Blueman makes the pendant's Bluetooth link /dev/rfcomm1. Nothing is opened until Connect is pressed; then
// the server answers the pendant and sends it the position and state a few times a second, and the
// pendant's keys jog (the Jog panel's step and speed), zero, run, hold and stop like the buttons here.
// The port choice is only remembered to fill in the dialog next time (it never connects by itself).

import { store } from './api.js';

const DEFAULT_PORT = '/dev/rfcomm1';
// The pendant is never on FluidNC's kind of port (the server refuses it), and an ACM port is the probe's kind
// (opened only if asked twice: force)
const NOT_PENDANT = { 'usb-serial': 'FluidNC\'s kind of port' };
const RISKY = { 'usb-acm': 'the Z probe (Arduino Leonardo)' };
const STATE_TEXT = { off: 'off', waiting: 'waiting', connected: 'connected' };
const KEY_NAMES = {      // the pendant's keys (NANO_jogbox.ino numbers), for the "last key" note
  0: 'Z− step', 3: 'Z+ step', 4: 'Y− step', 5: 'X+ step', 6: 'Y+ step', 7: 'X− step', 1: 'A−', 2: 'A+',
  8: 'step 0.001', 9: 'step 0.01', 10: 'step 0.1', 11: 'step 1', 12: 'Zero A', 13: 'Zero Z', 14: 'Zero Y', 15: 'Zero X',
  16: 'A', 17: 'machine Z0', 18: 'machine Y0', 19: 'machine X0', 22: 'return to zero', 23: 'Home', 25: 'Run',
  26: 'Pause / Resume', 27: 'Stop',
};

/** A port the pendant may use: free, or held only by Blueman's rfcomm watcher (it only watches the link). */
function usable(p) {
  if (NOT_PENDANT[p.kind]) return false;
  if (!p.inUseBy) return true;
  return String(p.inUseBy).split(/,\s*/).every(h => /^blueman-rfcomm/i.test(h));
}

/** "Pendant: connected" etc. for the status bar, and its tooltip. note: what the pendant can't do now, said
 *  instead (in amber): "Pendant Stop/Pause inactive: started on the page". */
export function pendantChip(pendant, note = '') {
  const st = pendant?.state || 'off';
  let title = st === 'off' ? 'Bluetooth pendant: not connected. Click to connect it.'
    : st === 'waiting' ? `Bluetooth pendant: ${pendant.port} is open, waiting for the pendant to answer (it goes on by itself when it does). Click for more.`
      : `Bluetooth pendant: connected on ${pendant.port}. Click for more.`;
  const k = pendant?.lastKey;
  if (k != null && st !== 'off') {
    const n = typeof k === 'object' ? k.key : k;
    const name = (typeof k === 'object' && k.name) || KEY_NAMES[n];
    const t = typeof k === 'object' && Number.isFinite(k.t) ? ` at ${new Date(k.t * 1000).toLocaleTimeString()}` : '';
    const how = typeof k === 'object' && k.ok === false ? ' (refused)' : '';
    title += ` Last key: ${n}${name ? ` (${name})` : ''}${how}${t}.`;
  }
  if (note && st === 'connected') {
    return { text: note, cls: 'warn', title: `${note}. The original pendant firmware sends its Stop and Pause keys only after its `
      + 'own Run key (the fixed firmware sends them by the status it shows): for this job use the page\'s Hold or Soft Reset. '
      + title };
  }
  if (st === 'connected' && pendant?.stopPause === true) title += ' Its Run, Pause and Stop keys follow the machine.';
  return { text: `Pendant: ${STATE_TEXT[st] || st}`, cls: st, title };
}

export class PendantDialog {
  /** dlg: the <dialog>; machine: a MachineClient; log(text, kind): app messages. */
  constructor(dlg, machine, log) {
    this.dlg = dlg;
    this.machine = machine;
    this.log = log;
    this.ports = [];
    this.pendant = null;
    this.portSel = dlg.querySelector('[data-f=port]');
    this.stateEl = dlg.querySelector('.pstate');
    this.warnEl = dlg.querySelector('.cwarn');
    this.msgEl = dlg.querySelector('.cmsg');
    this.okBtn = dlg.querySelector('[data-act=ok]');
    this.discBtn = dlg.querySelector('[data-act=disc]');
    dlg.querySelector('[data-act=refresh]').onclick = () => this.refresh();
    this.okBtn.onclick = () => this.connect();
    this.discBtn.onclick = () => this.disconnect();
    this.portSel.onchange = () => this.showState();
    dlg.addEventListener('keydown', e => {
      if (e.key === 'Enter' && e.target.tagName !== 'BUTTON') { e.preventDefault(); this.connect(); }
    });
  }

  async show() {
    this.message('');
    this.dlg.showModal();
    this.showState();
    await this.refresh();
  }

  async refresh() {
    this.portSel.disabled = true;
    this.portSel.innerHTML = '<option>Looking for serial ports…</option>';
    this.ports = [];
    this.showState();
    let data;
    try {
      data = await this.machine.pendantPorts();
    } catch (e) {
      this.portSel.innerHTML = '<option value="">(could not list the ports)</option>';
      this.message(e.status === 404 ? 'This FluidSender server has no pendant support yet (restart FluidSender after the update).' : e.message, 'err');
      this.showState();
      return;
    }
    this.ports = data.ports || [];
    const mine = this.pendant?.state !== 'off' ? this.pendant?.port : null;
    this.portSel.innerHTML = '<option value="" disabled>Choose a port…</option>';
    for (const p of this.ports) {
      const o = document.createElement('option');
      o.value = p.path;
      const ok = usable(p) || p.path === mine;
      o.textContent = `${p.path} — ${p.label || p.product || p.kind}` + (NOT_PENDANT[p.kind] ? `  (${NOT_PENDANT[p.kind]}: not the pendant)`
        : p.inUseBy && !ok ? `  (in use by ${p.inUseBy})` : '');
      o.disabled = !ok;
      this.portSel.append(o);
    }
    // The port it is on, else the last one used, else /dev/rfcomm1, else the server's pick, else a Bluetooth port
    const free = this.ports.filter(usable);
    const pick = this.ports.find(p => p.path === mine) || free.find(p => p.path === store.get('pendant.port'))
      || free.find(p => p.path === DEFAULT_PORT) || free.find(p => p.path === data.default) || free.find(p => p.kind === 'bluetooth');
    if (!this.ports.length) {
      this.portSel.innerHTML = '<option value="">(no serial ports found)</option>';
      this.message(`No serial ports found. Is the pendant paired and connected in Blueman (${DEFAULT_PORT})?`, 'err');
    } else {
      this.portSel.value = pick ? pick.path : '';
      this.portSel.disabled = false;
      if (!pick && !mine) this.message(`${DEFAULT_PORT} is not there: connect the pendant in Blueman first (Serial Port), then press Refresh.`, 'err');
      else if (this.msgEl.classList.contains('err')) this.message('');
    }
    this.showState();
  }

  selected() { return this.ports.find(p => p.path === this.portSel.value && usable(p)); }

  /** The server's pendant state { port, state, since, lastKey }. */
  setState(pendant) {
    const was = this.pendant?.state;
    this.pendant = pendant;
    // (the note "is open... once it answers" is done with once it has answered)
    if (pendant?.state === 'connected' && was !== 'connected' && !this.msgEl.classList.contains('err')) this.message('');
    if (this.dlg.open) this.showState();
  }

  showState() {
    const pd = this.pendant, on = !!pd && pd.state && pd.state !== 'off';
    const chip = pendantChip(pd);
    let text = on ? `${chip.text.replace('Pendant: ', 'The pendant is ')} (${pd.port})` : 'The pendant is not connected.';
    if (on && Number.isFinite(pd.since)) text += ` since ${new Date(pd.since * 1000).toLocaleTimeString()}`;
    this.stateEl.textContent = text;
    this.stateEl.className = 'pstate ' + (pd?.state === 'connected' ? 'good' : pd?.state === 'waiting' ? 'amber' : 'dim');
    this.discBtn.hidden = !on;
    this.okBtn.hidden = on;
    this.okBtn.disabled = this.busy || !this.selected();
    this.portSel.disabled = on || !this.ports.length;
    const p = this.selected();
    this.warnEl.textContent = !on && p && RISKY[p.kind] ? `This looks like ${RISKY[p.kind]}, not the pendant.` : '';
    this.warnEl.hidden = !this.warnEl.textContent;
  }

  message(text, kind = '') {
    this.msgEl.textContent = text;
    this.msgEl.className = 'cmsg ' + kind;
    this.msgEl.hidden = !text;
  }

  async connect() {
    const p = this.selected();
    if (!p || this.busy || this.okBtn.hidden || this.okBtn.disabled) return;
    let force = false;
    if (RISKY[p.kind]) {
      if (!confirm(`${p.path} looks like ${RISKY[p.kind]}, not the pendant.\n\nConnect the pendant to it anyway?`)) return;
      force = true;
    }
    store.set('pendant.port', p.path);
    this.busy = true;
    this.showState();
    this.message(`Opening ${p.path}…`);
    try {
      const res = await this.machine.pendantConnect(p.path, force);
      if (res?.pendant) this.setState(res.pendant);
      if (this.pendant?.state === 'connected') this.message('');
      else this.message(`${p.path} is open. The pendant shows "connected" once it answers (switch it on if it is off).`);
    } catch (e) {
      this.message(e.status === 404 ? 'This FluidSender server has no pendant support yet (restart FluidSender after the update).' : e.message, 'err');
      this.log?.(`Pendant: ${e.message}`, 'err');
    } finally {
      this.busy = false;
      this.showState();
    }
  }

  async disconnect() {
    try {
      const res = await this.machine.pendantDisconnect();
      if (res?.pendant) this.setState(res.pendant);
      this.message('Pendant disconnected (the port is closed).');
      this.refresh();
    } catch (e) {
      this.message(e.message, 'err');
      this.log?.(`Pendant: ${e.message}`, 'err');
    }
  }
}
