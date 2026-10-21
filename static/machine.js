// Link to the server's FluidNC connection: a live event stream (one JSON object per line)
// plus small calls. The event stream reopens by itself when it drops; the serial port is
// only ever opened by connect(), which only runs when the user presses Connect.

import { HEADERS, apiJson } from './api.js';

const RETRY_MS = 1000;          // reopen the event stream this soon after it drops
const MISSING_RETRY_MS = 5000;  // the running server has no machine calls (an older version)
const SILENT_MS = 25000;        // nothing at all (not even the 10 s ping) for this long: the stream is dead

export class MachineClient {
  constructor() {
    // Set by the app. Each gets the event object as the server sent it.
    this.onSnapshot = null;   // first event on every stream: connected, port, firmware, status, modal, axes, jog,
                              // zeroUndo, alarm, program, job, jogSettings, pendant, probe, scan, console[]
    this.onStatus = null;     // state, wpos, mpos, feed, spindle, flood, mist (true | false | null: not known), …
                              // (at most 5 a second). Stage 5: ov [feed, rapid, spindle] %, ovTarget (the values
                              // being set, or null), programmedF / programmedS (in force), spindleType, and for
                              // { vacuum, aux }: coolantBusy / coolantMissing (why a toggle is refused now, or
                              // null), coolantPins, coolantFollows ('modal' | 'report'); coolantDelayMs
    this.onConn = null;       // connected, port, firmware, message
    this.onModal = null;      // units, distance, gc
    this.onConsole = null;    // dir ('tx' | 'rx' | 'info' | 'error'), text, t
    this.onAxes = null;       // axes: { x: { maxRate (in/min), accel (in/s²) }, y, z } (null: not read), plannerBlocks
    this.onJog = null;        // jog: { id, dir: { x, y, z }, feed } while a hold jog runs, null when it ends
    this.onZero = null;       // zeroUndo: { axes, text } after a zero, null once undone or forgotten
    this.onAlarm = null;      // alarm: { code, critical, text } or null
    // Stage 3 + 4 (each gets the event; the same fields are in the snapshot):
    this.onProgram = null;    // program: { name, lines (to send), total (file lines), warnings, leveled: { scan,
                              // time, zSurface, arcSegmentMm, stats, warnings } or null } or null: the loaded program
    this.onJob = null;        // job: { name, state, total, sent, acked, line, started, elapsed, why, error, leveled } or null
    this.onJogSettings = null; // jog step and speed the server keeps (the pendant changes them too): { step, feed }
    this.onPendant = null;    // pendant: { port, state: 'off' | 'waiting' | 'connected', since, lastKey }
    this.onProbe = null;      // probe: { port, connected, ready, busy, last, connecting, mayBeDown }
    this.onScan = null;       // scan: { running, point, points, settings, result, error, savedTo }
    this.onLink = null;       // (link, message): the event stream itself is 'up', 'down' or 'missing'
    this.link = '';
    this.connected = false;
  }

  /** Opens the event stream and keeps it open. */
  start() {
    if (this.started) return;
    this.started = true;
    this.loop();
  }

  async loop() {
    for (;;) {
      let wait = RETRY_MS;
      try {
        await this.stream();
        this.setLink('down', 'The FluidSender server closed the machine event stream');
      } catch (e) {
        if (e.status === 404) {
          wait = MISSING_RETRY_MS;
          this.setLink('missing', 'The running FluidSender server is older than this page and has no machine '
            + 'connection. Close this window and start FluidSender again: the launcher (fluidsender.sh) replaces '
            + `the old server. Or stop it yourself: pkill -f "server.py --no-browser --port ${location.port || 80}"`);
        } else {
          this.setLink('down', `No link to the FluidSender server (${e.message})`);
        }
      }
      await new Promise(resolve => setTimeout(resolve, wait));
    }
  }

  async stream() {
    const abort = new AbortController();
    let timer = 0;
    const alive = () => { clearTimeout(timer); timer = setTimeout(() => abort.abort(), SILENT_MS); };
    alive();
    try {
      const res = await fetch('/api/machine/events', { headers: HEADERS, signal: abort.signal, cache: 'no-store' });
      if (!res.ok) throw Object.assign(new Error(`${res.status} ${res.statusText}`), { status: res.status });
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) return;
        alive();
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (line) this.dispatch(line);
        }
      }
    } catch (e) {
      throw abort.signal.aborted ? new Error(`nothing heard for ${SILENT_MS / 1000} s`) : e;
    } finally {
      clearTimeout(timer);
      abort.abort();
    }
  }

  dispatch(line) {
    let ev;
    try { ev = JSON.parse(line); } catch { console.warn('machine: bad event line', line); return; }
    try {
      if (ev.type === 'snapshot') {
        this.connected = !!ev.connected;
        this.setLink('up', '');
        this.onSnapshot?.(ev);
      } else if (ev.type === 'conn') {
        this.connected = !!ev.connected;
        this.onConn?.(ev);
      } else if (ev.type === 'status') this.onStatus?.(ev);
      else if (ev.type === 'modal') this.onModal?.(ev);
      else if (ev.type === 'console') this.onConsole?.(ev);
      else if (ev.type === 'axes') this.onAxes?.(ev);
      else if (ev.type === 'jog') this.onJog?.(ev);
      else if (ev.type === 'zero') this.onZero?.(ev);
      else if (ev.type === 'alarm') this.onAlarm?.(ev);
      else if (ev.type === 'program') this.onProgram?.(ev);
      else if (ev.type === 'job') this.onJob?.(ev);
      else if (ev.type === 'jogsettings') this.onJogSettings?.(jogSettingsOf(ev));
      else if (ev.type === 'pendant') this.onPendant?.(ev);
      else if (ev.type === 'probe') this.onProbe?.(ev);
      else if (ev.type === 'scan') this.onScan?.(ev);
      // 'ping' only keeps the stream alive
    } catch (e) {
      console.error('machine: handling a', ev.type, 'event failed', e);   // a page bug must not drop the stream
    }
  }

  setLink(link, message) {
    if (link !== 'up') this.connected = false;
    if (link === this.link) return;
    this.link = link;
    this.onLink?.(link, message);
  }

  // ---------------------------------------------------------------- calls (each throws the server's error)

  ports() { return apiJson('/api/machine/ports'); }
  connect(port, baud = 115200, force = false) { return apiJson('/api/machine/connect', { port, baud, force }); }
  disconnect() { return apiJson('/api/machine/disconnect', {}); }
  send(line) { return apiJson('/api/machine/send', { line }); }
  /** cmd: 'hold' (!), 'resume' (~), 'reset' (Ctrl-X), 'status' (?) or 'jogcancel' (0x85: stops any jog) */
  realtime(cmd) { return apiJson('/api/machine/realtime', { cmd }); }
  state() { return apiJson('/api/machine/state'); }

  // Jogs, in inches and inches per minute. The server refuses (409) in the wrong state.
  /** One step: step { x, y, z } (signed inches). Resolves to { line } (the $J= line sent). */
  jogStep(step, feed) { return apiJson('/api/machine/jog', { step, feed }); }
  /** A hold jog that way (dir { x, y, z }: each -1, 0 or 1). Resolves to its id; it runs while jogKeep(id)
   *  comes every 100 ms (the server stops it 0.4 s after the last one) and until jogStop(id). */
  async jogStart(dir, feed) { return (await apiJson('/api/machine/jog', { start: dir, feed })).id; }
  jogKeep(id) { return apiJson('/api/machine/jog', { keep: id }); }
  /** id: the hold jog to stop, or 'all'. An ended or unknown id is fine. keepalive: still sent while the
   *  page is being closed. */
  jogStop(id, keepalive = false) {
    if (!keepalive) return apiJson('/api/machine/jog', { stop: id });
    return fetch('/api/machine/jog', {
      method: 'POST', keepalive: true, headers: { ...HEADERS, 'Content-Type': 'application/json' }, body: JSON.stringify({ stop: id }),
    });
  }

  /** axes: 'X', 'XY', 'XYZ'...: their work position becomes value (inches; G10 L20 P0). */
  zero(axes, value = 0) { return apiJson('/api/machine/zero', { axes, value }); }
  /** Puts back the work offsets the last zero changed. */
  zeroUndo() { return apiJson('/api/machine/zero', { undo: true }); }
  /** axes: '' for all ($H), or 'X', 'Y' or 'Z' ($HX...). */
  home(axes = '') { return apiJson('/api/machine/home', { axes }); }
  unlock() { return apiJson('/api/machine/unlock', {}); }

  // ---------------------------------------------------------------- stage 3 + 4

  /** which: 'vacuum' (M8, flood) or 'aux' (M7, mist); on: the state wanted. The server sends FluidNC's toggle
   *  (0xA0 / 0xA1) only if it is not so already. */
  coolant(which, on) { return apiJson('/api/machine/coolant', { which, on }); }
  /** to: 'xy' (Z up to 0.2 in first if lower), 'z', 'zero' (both) - work zero; 'mx', 'my', 'mz' - machine zero. */
  goto(to) { return apiJson('/api/machine/goto', { to }); }

  // ---------------------------------------------------------------- stage 5

  /** An override (FluidNC's realtime bytes, at once): { which: 'feed' | 'spindle', step: ±1 | ±10 }, { which,
   *  to: 10..200 }, { which: 'rapid', to: 100 | 50 | 25 } or { which: 'all', reset: true }. Resolves to { sent,
   *  expect: [feed, rapid, spindle] }. */
  override(body) { return apiJson('/api/machine/override', body); }
  /** Levels the loaded program: { scan: 'last' | { path }, zSurface, arcSegmentMm } -> { lines, total, warnings,
   *  stats, scan }; { off: true } goes back to the original. The leveled text: programText(). */
  level(body) { return apiJson('/api/machine/level', body); }
  /** The G-code the page shows becomes the loaded program. text '' unloads it. Resolves to { lines, total }. */
  program(name, text) { return apiJson('/api/machine/program', text ? { name, text } : { text: '' }); }
  /** What a page with no G-code of its own shows: the running job's program while a job runs, else the loaded
   *  one, as a page uploaded it: { name, lines, total, warnings, text, job: { started, source: 'page' |
   *  'pendant' } or null }; { name: null } when there is none. withText: false leaves the text out. */
  programText(withText = true) { return apiJson('/api/machine/program' + (withText ? '' : '?text=0')); }
  run() { return apiJson('/api/machine/run', {}); }
  /** The safe stop: a job (feed hold, then a reset once stopped), a scan, or a jog (jog cancel). */
  stop() { return apiJson('/api/machine/stop', {}); }
  /** settings: { step?, feed? }: the jog step (inches) and speed (in/min) every page and the pendant use. */
  setJogSettings(settings) { return apiJson('/api/machine/jogsettings', settings); }

  pendantPorts() { return apiJson('/api/pendant/ports'); }
  pendantConnect(port, force = false) { return apiJson('/api/pendant/connect', { port, force }); }
  pendantDisconnect() { return apiJson('/api/pendant/disconnect', {}); }

  probePorts() { return apiJson('/api/probe/ports'); }
  probeConnect(port, force = false) { return apiJson('/api/probe/connect', { port, force }); }
  probeDisconnect() { return apiJson('/api/probe/disconnect', {}); }
  probeHome() { return apiJson('/api/probe/home', {}); }

  scanStart(settings) { return apiJson('/api/scan/start', { settings }); }
  scanStop() { return apiJson('/api/scan/stop', {}); }
  /** Saves the scan settings (the server keeps them for next time). */
  scanSettings(settings) { return apiJson('/api/scan/settings', settings); }
  /** Writes the last scan to path (.xyz, the UGS format). */
  scanSave(path) { return apiJson('/api/scan/save', { path }); }
}

/** A job that is still going (running, paused or stopping), as the server's job event says. */
export const jobActive = job => !!job && ['running', 'paused', 'stopping'].includes(job.state);

/** The jogsettings event as { step, feed }, whether the fields come at the top or in "jogSettings". */
export function jogSettingsOf(ev) {
  const s = ev?.jogSettings && typeof ev.jogSettings === 'object' ? ev.jogSettings : ev;
  return s ? { step: s.step, feed: s.feed } : null;
}

/** "[VER:3.7 FluidNC v3.9.4:]" or "3.7 FluidNC v3.9.4:" -> "FluidNC v3.9.4" */
export function firmwareName(fw) {
  if (!fw) return '';
  const m = /FluidNC\s+v?[\w.-]+/i.exec(fw);
  return m ? m[0].replace(/:$/, '') : fw.replace(/^\[VER:/, '').replace(/:?\]?$/, '').trim();
}
