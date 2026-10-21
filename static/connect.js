// The Connect dialog: choose the serial port and baud rate. Nothing is opened until Connect is pressed,
// and the choice is only remembered to fill in the dialog next time (never to connect by itself).

import { store } from './api.js';

// Ports the server refuses unless forced: probably the probe or the pendant, not FluidNC
const RISKY = { 'usb-acm': 'the Z probe (Arduino Leonardo)', bluetooth: 'the Bluetooth jog pendant' };

export class ConnectDialog {
  /** dlg: the <dialog> element; machine: a MachineClient; log(text, kind): app messages. */
  constructor(dlg, machine, log) {
    this.dlg = dlg;
    this.machine = machine;
    this.log = log;
    this.ports = [];
    this.portSel = dlg.querySelector('[data-f=port]');
    this.baudSel = dlg.querySelector('[data-f=baud]');
    this.warnEl = dlg.querySelector('.cwarn');
    this.msgEl = dlg.querySelector('.cmsg');
    this.okBtn = dlg.querySelector('[data-act=ok]');
    dlg.querySelector('[data-act=refresh]').onclick = () => this.refresh();
    this.okBtn.onclick = () => this.connect();
    this.portSel.onchange = () => { this.showWarning(); this.okBtn.disabled = this.busy || !this.selected(); };
    dlg.addEventListener('keydown', e => {
      if (e.key === 'Enter' && e.target.tagName !== 'BUTTON') { e.preventDefault(); this.connect(); }
    });
  }

  async show() {
    this.baudSel.value = String(store.get('machine.baud', 115200));
    if (!this.baudSel.value) this.baudSel.value = '115200';
    this.message('');
    this.dlg.showModal();
    await this.refresh();
  }

  async refresh() {
    this.okBtn.disabled = true;
    this.portSel.disabled = true;
    this.portSel.innerHTML = '<option>Looking for serial ports…</option>';
    this.ports = [];
    this.showWarning();
    let data;
    try {
      data = await this.machine.ports();
    } catch (e) {
      this.portSel.innerHTML = '<option>(could not list the ports)</option>';
      this.message(e.message, 'err');
      return;
    }
    this.ports = data.ports || [];
    this.portSel.innerHTML = '<option value="" disabled>Choose a port…</option>';
    for (const p of this.ports) {
      const o = document.createElement('option');
      o.value = p.path;
      o.textContent = `${p.path} — ${p.label || p.product || p.kind}` + (p.inUseBy ? `  (in use by ${p.inUseBy})` : '');
      o.disabled = !!p.inUseBy;
      this.portSel.append(o);
    }
    // Preselect the last port used, else the server's pick (the first free ttyUSB), else another free port.
    // Never the probe or the pendant: those can only be chosen by hand (and then asked about).
    const safe = this.ports.filter(p => !p.inUseBy && !RISKY[p.kind]);
    const pick = safe.find(p => p.path === store.get('machine.port')) || safe.find(p => p.path === data.default) || safe[0];
    const busy = this.ports.find(p => p.inUseBy && !RISKY[p.kind]);    // FluidNC's port, held by UGS?
    if (!this.ports.length) {
      this.portSel.innerHTML = '<option>(no serial ports found)</option>';
      this.message('No serial ports found. Is the FluidNC controller plugged in (USB)?', 'err');
    } else {
      this.portSel.value = pick ? pick.path : '';
      this.portSel.disabled = !this.ports.some(p => !p.inUseBy);
      if (pick) {
        if (this.msgEl.classList.contains('err')) this.message('');
      } else if (busy) {
        this.message(`${busy.path} is in use by ${busy.inUseBy}. Disconnect it there first, then press Refresh.`, 'err');
      } else {
        this.message('No free FluidNC port (USB serial). Is the controller plugged in? Press Refresh to look again.', 'err');
      }
    }
    this.okBtn.disabled = !this.selected();
    this.showWarning();
  }

  selected() { return this.ports.find(p => p.path === this.portSel.value && !p.inUseBy); }

  showWarning() {
    const p = this.selected();
    this.warnEl.textContent = p && RISKY[p.kind] ? `This looks like ${RISKY[p.kind]}, not FluidNC.` : '';
    this.warnEl.hidden = !this.warnEl.textContent;
  }

  message(text, kind = '') {
    this.msgEl.textContent = text;
    this.msgEl.className = 'cmsg ' + kind;
    this.msgEl.hidden = !text;
  }

  async connect() {
    const p = this.selected();
    if (!p || this.busy || this.okBtn.disabled) return;
    const baud = +this.baudSel.value || 115200;
    let force = false;
    if (RISKY[p.kind]) {
      if (!confirm(`This looks like the probe/pendant, connect anyway?\n\n${p.path} is probably ${RISKY[p.kind]}, not FluidNC.`)) return;
      force = true;
    }
    store.set('machine.port', p.path);
    store.set('machine.baud', baud);
    this.busy = true;
    this.okBtn.disabled = true;
    this.message(`Connecting to ${p.path} at ${baud}…`);
    try {
      await this.machine.connect(p.path, baud, force);
      this.dlg.close();
    } catch (e) {
      this.message(e.message, 'err');
      this.log?.(`Connect failed: ${e.message}`, 'err');
    } finally {
      this.busy = false;
      this.okBtn.disabled = !this.selected();
    }
  }
}
