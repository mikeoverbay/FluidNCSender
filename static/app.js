// FluidSender: dockable panels, menus, file handling, machine connection, jobs, the surface scan, the pendant.

import { parseGcode, parseScan, scanFromPoints, scanGrid } from './gcode.js';
import { Viewer } from './viewer.js';
import { Editor } from './editor.js';
import { FileBrowser } from './files.js';
import { MenuBar } from './menu.js';
import { InfoPanel, MachinePanel, ConsolePanel, runWhy, fmtClock, undoWhen } from './panels.js';
import { JogPanel } from './jog.js';
import { ScanPanel, scanPoints, scanTotal, scanDone, scanResultGrid, levelName, levelCoverage, levelMismatch } from './scan.js';
import { MachineClient, firmwareName, jobActive, jogSettingsOf } from './machine.js';
import { ConnectDialog } from './connect.js';
import { PendantDialog, pendantChip } from './pendant.js';
import { readFile, saveFile, baseName, dirName, isScan, store } from './api.js';
import { THEMES, currentTheme, setTheme, onThemeChange } from './theme.js';

const DV = globalThis['dockview-core'];
const $ = id => document.getElementById(id);
const GCODE_EXT = /\.(nc|gcode|ngc|tap)$/i;
const LAYOUT_KEY = 'layout.v5';     // v5: Machine + Jog take the whole left side; under the visualizer and the
                                    // editor column the bottom row is Scan (left) | Console (right); Files /
                                    // Toolpath are tabs below the editor
for (const old of ['layout.v1', 'layout.v2', 'layout.v3', 'layout.v4']) store.remove(old);
const UPLOAD_MS = 1000;             // the edited G-code goes to the server this long after the last change

// ------------------------------------------------------------------ state

// scanLabel: what the Toolpath panel calls the scan shown when it has no file (a scan being made);
// liveScan: the visualizer follows the server's scan (one being made, or the last one made); gcodeName: the
// name of the G-code shown when it has no file here (the server's program, see adoptProgram); opening: files
// being read; edits: each change of the editor's text (a typed one, or a file opened or closed)
const state = {
  gcodePath: '', gcodeName: '', scanPath: '', scanLabel: '', job: null, scan: null, liveScan: false, opening: 0, edits: 0,
};

// ------------------------------------------------------------------ panel contents
// One of each, kept alive when its panel is closed so reopening it is instant.

const viewer = new Viewer();
const editor = new Editor();
const files = new FileBrowser({ onPick: path => openPath(path) });
const info = new InfoPanel();
const machine = new MachineClient();
const machinePanel = new MachinePanel({
  onConnect: () => connectDlg.show(),
  onDisconnect: () => disconnect(),
  onRun: () => runJob(),
  onHold: () => realtime('hold'),
  onResume: () => realtime('resume'),
  onReset: () => softReset(),
  onZero: (axes, value) => zero(axes, value),
  onUndoZero: () => undoZero(),
  onHome: axes => home(axes),
  onUnlock: () => unlock(),
  onOverride: body => override(body),
});
const jogPanel = new JogPanel(machine, { onStop: () => stopJog(), log: (text, kind) => status(text, kind), onGoto: to => gotoZero(to) });
const scanPanel = new ScanPanel(machine, {
  log: (text, kind) => status(text, kind),
  area: () => (state.job && Number.isFinite(state.job.lo[0]) ? { lo: state.job.lo, hi: state.job.hi } : null),
  onStart: settings => startScan(settings),
  onSave: () => saveScanAs(),
  onLevel: (opts, name) => levelProgram(opts, name),
  onLevelOff: () => levelOff(),
  onSaveLeveled: () => saveLeveledAs(),
  onPickScan: () => pickLevelScan(),
});
const konsole = new ConsolePanel({ onSend: line => sendLine(line) });

const PANELS = {
  viewer: { title: 'Visualizer', content: viewer, renderer: 'always' },
  editor: { title: 'G-code Editor', content: editor, renderer: 'always', onShow: () => editor.refresh() },
  files: { title: 'Files', content: files },
  info: { title: 'Toolpath', content: info },
  scan: { title: 'Scan', content: scanPanel, onShow: () => scanPanel.shown() },
  machine: { title: 'Machine', content: machinePanel },
  jog: { title: 'Jog', content: jogPanel },
  console: { title: 'Console', content: konsole, onShow: () => konsole.shown() },
};

// ------------------------------------------------------------------ messages

function status(text, kind = '') {
  $('smsg').textContent = text;
  $('smsg').className = kind;
  konsole.write(text, kind);
}
viewer.onMessage = text => status(text);
viewer.shotName = () => gcodeName() || prog.name;      // (the screenshot button's file name)
onThemeChange(() => viewer.recolor());      // (the rest of the page follows the theme by itself: style.css)

function gcodeName() {
  return state.gcodePath ? baseName(state.gcodePath) : state.gcodeName || (editor.getText() ? 'untitled' : '');
}

/** The window's title: the job's % while it runs, the G-code's name. */
function docTitle() {
  const name = gcodeName(), dirty = !editor.isClean(), job = mach.job;
  const pct = jobActive(job) && job.total ? `${Math.floor((100 * (job.acked || 0)) / job.total)}% ` : '';
  const text = pct + (name ? `${name}${dirty ? ' •' : ''} — ` : '') + 'FluidSender';
  if (document.title !== text) document.title = text;
}

function refreshTitles() {
  const name = gcodeName();
  const dirty = !editor.isClean();
  const running = jobActive(mach.job);
  docTitle();
  // (short: the tab is as wide as the editor column; the banner says the rest)
  const leveled = !lev.active ? '' : lev.showOriginal ? ' (original, read-only)' : ' (LEVELED, read-only)';
  dock.getPanel('editor')?.api.setTitle('G-code Editor' + (name ? ` — ${name}${dirty ? ' •' : ''}` : '') + leveled
    + (running && !lev.active ? ' (read-only: job running)' : ''));
  $('sfile').textContent = name ? `${name}${dirty ? ' (modified)' : ''}` : 'No file';
  info.update(state.job, state.scan, name, state.scanPath ? baseName(state.scanPath) : state.scanLabel);
  files.mark([state.gcodePath, state.scanPath]);
}

// ------------------------------------------------------------------ dock layout

const dock = DV.createDockview($('dock'), {
  theme: DV.themeDark,
  createComponent(options) {
    const p = PANELS[options.name];
    const element = p ? p.content.root : Object.assign(document.createElement('div'), { textContent: 'Unknown panel' });
    return {
      element,
      init() {},
      onShow() { p?.onShow?.(); },
      dispose() {},          // keep the content for when the panel is opened again
    };
  },
});

function addPanel(id, extra = {}) {
  const p = PANELS[id];
  return dock.addPanel({ id, component: id, title: p.title, renderer: p.renderer, ...extra });
}

const TAB_BAR = 35;      // dockview's tab bar above each group
// The bottom row (Scan | Console): this share of the dock's height, at least BOTTOM_MIN px, more if the Scan
// panel's columns need it (BOTTOM_MAX at most); Scan gets SCAN_SHARE of the row's width, more if its columns need it
const BOTTOM_SHARE = 0.3, BOTTOM_MIN = 210, BOTTOM_MAX = 0.4, SCAN_SHARE = 0.55, SCAN_MAX = 0.66;

function defaultLayout() {
  dock.clear();
  addPanel('viewer');
  addPanel('editor', { position: { referencePanel: 'viewer', direction: 'right' }, initialWidth: 460 });
  addPanel('files', { position: { referencePanel: 'editor', direction: 'below' } });
  addPanel('info', { position: { referencePanel: 'files', direction: 'within' }, inactive: true });
  // The bottom row under the visualizer and the editor column: Scan (left) | Console (right); then Machine
  // and Jog on the left, the whole height (the position, Run / Hold / Resume / Soft Reset, the job, the
  // overrides, the jog pad and the toggles all show)
  addPanel('console', { position: { direction: 'below' }, initialHeight: BOTTOM_MIN });
  addPanel('scan', { position: { referencePanel: 'console', direction: 'left' } });
  addPanel('machine', { position: { direction: 'left' }, initialWidth: 290 });
  addPanel('jog', { position: { referencePanel: 'machine', direction: 'below' } });
  dock.getPanel('viewer')?.api.setActive();
  dock.getPanel('files')?.api.setActive();
  defaultSizes();
  defaultAt = performance.now();
  scanPanel.shown();
}

// A window that still changes size just after it opened (placed by the window manager, or a headless
// browser settling) would share the change out unevenly: the default sizes are set again then, unless
// the user has started working in it
let defaultAt = -1e9;
new ResizeObserver(() => { if (performance.now() - defaultAt < 3000) defaultSizes(); }).observe($('dock'));
$('dock').addEventListener('pointerdown', () => { defaultAt = -1e9; }, true);

function defaultSizes() {
  // Lay the dock out at its real size first, otherwise its first automatic layout
  // shares the space out evenly and undoes these sizes
  const r = $('dock').getBoundingClientRect();
  dock.layout(r.width, r.height, true);
  const size = (id, s) => dock.getPanel(id)?.api.group.api.setSize(s);
  const machineWidth = Math.min(340, Math.max(290, Math.round(r.width * 0.24)));
  size('machine', { width: machineWidth });
  const row = r.width - machineWidth;           // the bottom row's width (and the visualizer and editor's)
  size('editor', { width: Math.min(560, Math.max(380, Math.round(r.width * 0.3))) });
  // The bottom row: Scan as wide as its columns need (about 55 % of the row), then as high as they need
  // at that width (about 30 % of the dock), so every Scan control shows without scrolling
  const scanWidth = Math.round(Math.min(row * SCAN_MAX, Math.max(row * SCAN_SHARE, scanPanel.neededWidth())));
  size('scan', { width: scanWidth });
  const bottom = Math.round(Math.min(r.height * BOTTOM_MAX, Math.max(BOTTOM_MIN, r.height * BOTTOM_SHARE,
    scanPanel.neededHeight(scanWidth) + TAB_BAR)));
  size('console', { height: bottom });
  size('scan', { width: scanWidth });           // (again: the row's height can move it)
  const top = r.height - bottom;                // the visualizer and the editor column, above the bottom row
  size('editor', { height: Math.round(top * 0.55) });     // Files / Toolpath below it get the rest
  // Machine on top, the whole Jog panel below it; in a short window the position (DRO) comes first.
  // (Measured once the widths are set: the text wraps by them.)
  size('machine', { height: Math.max(machinePanel.neededHeight() + TAB_BAR, r.height - jogPanel.fullHeight() - TAB_BAR) });
}

/** Where a closed panel goes back when it is opened from the View menu. */
function placeFor(id) {
  const has = other => !!dock.getPanel(other);
  const at = (other, direction) => ({ position: { referencePanel: other, direction } });
  const group = other => dock.getPanel(other)?.api.group;
  const r = $('dock').getBoundingClientRect();
  // Scan goes back left of the Console (the bottom row), and the Console right of a Scan that has a place of its
  // own there (not one that is a tab with Files / Toolpath)
  if (id === 'scan' && has('console')) {
    const w = group('console')?.width || r.width * 0.6;
    return { ...at('console', 'left'), initialWidth: Math.round(Math.max(w * SCAN_SHARE, Math.min(w * SCAN_MAX, scanPanel.neededWidth()))) };
  }
  if (id === 'console' && has('scan') && !['files', 'info'].some(t => group(t) && group(t) === group('scan'))) {
    // (the Scan panel has the whole row now: the Console gets its share, the Scan panel keeps what its columns need)
    const w = group('scan')?.width || r.width * 0.6;
    return { ...at('scan', 'right'), initialWidth: Math.round(Math.max(120, Math.min(w * (1 - SCAN_SHARE), w - scanPanel.neededWidth()))) };
  }
  const tabs = ['files', 'info', 'scan'];
  if (tabs.includes(id)) {
    const other = ['files', 'info'].find(t => t !== id && has(t)) || (id !== 'scan' && has('scan') && !has('console') ? 'scan' : '');
    if (other) return at(other, 'within');
    if (has('editor')) return at('editor', 'below');
  }
  if (id === 'jog' && has('machine')) return { ...at('machine', 'below'), initialHeight: jogPanel.fullHeight() + TAB_BAR };
  if (id === 'machine' && has('jog')) return at('jog', 'above');
  if (id === 'console') return { position: { direction: 'below' }, initialHeight: Math.max(BOTTOM_MIN, Math.round(r.height * BOTTOM_SHARE)) };
  if (has('viewer') && id !== 'viewer') {
    return { position: { referencePanel: 'viewer', direction: id === 'editor' || tabs.includes(id) ? 'right' : 'left' }, initialWidth: id === 'machine' || id === 'jog' ? 290 : 460 };
  }
  if (has('editor') && id === 'viewer') return at('editor', 'left');
  return {};
}

function showPanel(id) {
  const p = dock.getPanel(id);
  if (p) p.api.setActive(); else addPanel(id, placeFor(id));
}

function togglePanel(id) {
  const p = dock.getPanel(id);
  if (p) p.api.close(); else addPanel(id, placeFor(id));
}

function restoreLayout() {
  const saved = store.get(LAYOUT_KEY);
  if (!saved) return false;
  try {
    dock.fromJSON(saved);
    // Lay out at the real size now, or the always-rendered panels (visualizer, editor) stay at a stale position
    const r = $('dock').getBoundingClientRect();
    dock.layout(r.width, r.height, true);
    return dock.totalPanels > 0;
  } catch (e) {
    console.warn('saved layout could not be restored', e);
    return false;
  }
}

let saveTimer = 0;
dock.onDidLayoutChange(() => {
  clearTimeout(saveTimer);
  // Not while a panel is maximized: that layout would come back with every other panel at zero size
  saveTimer = setTimeout(() => { if (!dock.hasMaximizedGroup()) store.set(LAYOUT_KEY, dock.toJSON()); }, 500);
});

// The visualizer skips drawing while it is hidden behind another tab: draw it when it shows again; the Scan
// panel too (and it lists the probe's ports the first time it shows)
dock.onDidAddPanel(p => {
  if (p.id === 'viewer') p.api.onDidVisibilityChange(e => { if (e.isVisible) viewer.render(); });
  if (p.id === 'scan') p.api.onDidVisibilityChange(e => { if (e.isVisible) scanPanel.shown(); });
});

// ------------------------------------------------------------------ files

function addRecent(path) {
  const list = store.get('recent', []).filter(p => p !== path);
  list.unshift(path);
  store.set('recent', list.slice(0, 10));
}

function confirmDiscard() {
  return editor.isClean() || confirm(`Discard the unsaved changes to ${gcodeName()}?`);
}

/** The G-code shown can't change while a job runs it: says so and returns true then. (With no G-code shown,
 *  say after the page was reloaded during a job, one can be opened: the job keeps its own copy.) */
function jobBlocks(what) {
  if (!jobActive(mach.job) || !editor.getText()) return false;
  status(`${what}: not while a job is running (Hold, then Soft Reset stops it)`, 'err');
  return true;
}

async function openPath(path) {
  if (isScan(path)) return openScan(path);
  if (jobBlocks(`Open ${baseName(path)}`) || !confirmDiscard()) return;
  status(`Loading ${baseName(path)}…`);
  state.opening++;                   // (meanwhile the server's program is not put in the editor: adoptProgram)
  try {
    const text = await readFile(path);
    if (jobBlocks(`Open ${baseName(path)}`)) return;      // (a job may have started meanwhile: the pendant)
    const t0 = performance.now();
    dropLeveled(false);              // (another G-code: the server turns its leveling off when it gets it)
    editor.setText(text);
    state.edits++;
    state.gcodePath = path;
    state.gcodeName = '';
    state.job = parseGcode(text);
    viewer.setJob(state.job, true);
    addRecent(path);
    store.set('gcode.open', path);
    jobChanged(null);                // (the running job's file: its line is marked from now on)
    refreshTitles();
    status(`Opened ${baseName(path)}: ${state.job.lines.toLocaleString()} lines in ${Math.round(performance.now() - t0)} ms`);
    uploadProgram();
  } catch (e) {
    status(`${baseName(path)}: ${e.message}`, 'err');
  } finally {
    state.opening--;
  }
}

async function openScan(path) {
  status(`Loading ${baseName(path)}…`);
  try {
    const scan = parseScan(await readFile(path));
    state.scan = scan;
    state.scanPath = path;
    state.liveScan = false;          // (the visualizer shows this file now, not the server's last scan)
    viewer.setScan(scan);
    addRecent(path);
    refreshTitles();
    status(`Opened scan ${baseName(path)}: ${scan.xs.length} × ${scan.ys.length} points, height range ${(scan.zmax - scan.zmin).toFixed(3)}`);
  } catch (e) {
    status(`${baseName(path)}: ${e.message}`, 'err');
  }
}

/** Save: this page's G-code (while a leveled program is shown, its original: Save leveled G-code… writes that one). */
async function save() {
  if (noOwnGcode('Save')) return;
  if (!state.gcodePath) return saveAs();
  try {
    const res = await saveFile(state.gcodePath, gcodeText());
    editor.markClean();
    lev.originalDirty = false;
    addRecent(state.gcodePath);
    refreshTitles();
    status(`Saved ${baseName(res.path)} (${Math.round(res.size / 1024)} KB)`);
    if (prog.name !== gcodeName()) {  // (saved under a new name: the server gets it again, which turns its leveling off)
      dropLeveled(true);
      uploadProgram();
    }
  } catch (e) {
    status(`Save failed: ${e.message}`, 'err');
  }
}

function closeGcode() {
  if (jobBlocks('Close G-code') || !confirmDiscard()) return;
  dropLeveled(false);
  editor.setText('');
  state.edits++;
  state.gcodePath = '';
  state.gcodeName = '';
  state.job = null;
  store.remove('gcode.open');
  viewer.setJob(null, false);
  refreshTitles();
  uploadProgram();
}

function closeScan() {
  state.scan = null;
  state.scanPath = '';
  state.scanLabel = '';
  state.liveScan = false;
  viewer.setScan(null);
  refreshTitles();
}

// Re-parse a little after typing stops, so the view follows the edits; the server gets the new text a
// little later still (it is what Run runs)
let editTimer = 0;
editor.onEdit = () => {
  state.edits++;
  refreshTitles();
  clearTimeout(editTimer);
  editTimer = setTimeout(() => {
    state.job = parseGcode(editor.getText());
    viewer.setJob(state.job, false);
    refreshTitles();
  }, 600);
  clearTimeout(prog.timer);
  prog.timer = setTimeout(() => uploadProgram(), UPLOAD_MS);
};
// (the original of a leveled program shown: the visualizer shows the leveled moves of that line)
editor.onLine = line => {
  if (jobActive(mach.job)) return;
  if (lev.active && lev.showOriginal) viewer.highlight(...(levelRange(line) || [0, 0]));
  else viewer.highlight(line);
};
editor.onCursor = (line, col) => { $('spos').textContent = `Ln ${line}, Col ${col}`; };

// ------------------------------------------------------------------ the program (what Run runs)
// The server runs the G-code this page shows: it gets the text when a file is opened, a second after
// edits, and '' when the G-code is closed. Run first makes sure the server has exactly the text shown.
// Nothing else here ever loads, replaces or unloads the server's program: a page opened (or reloaded)
// with no G-code of its own shows the server's (adoptProgram), and a page whose G-code is not the
// server's (another window loaded another one) says so in the job line, until Run here loads it again.
//
// prog.state: 'none' (no G-code), 'uploading' (the first upload of this text), 'sending' (an update, the
// server has an older one), 'ok', 'empty' (no lines to send), 'error' (refused: prog.error says why, with
// the line number), 'old' (the server can't take programs: an older version). prog.warnings: lines FluidNC
// 3.9.4 runs otherwise than they read (an M8 its last number turns off, an M1 that doesn't stop): they run.
// prog.foreign: the server's program changed since, and not by this page (another window: maybe another
// version of the same file); prog.echo: the program event this page's last upload still causes.

const ECHO_MS = 10000;       // this page's own upload's program event, if it comes this late, is taken as another window's
const prog = {
  state: 'none', name: '', lines: null, total: null, error: '', text: null, warnings: [], said: '', timer: 0, busy: null,
  again: false, seq: 0, foreign: false, echo: null,
};
const sameProgram = (a, b) => !!a && !!b && a.name === b.name && a.lines === b.lines && a.total === b.total;

// ------------------------------------------------------------------ auto-level (the editor's side)
// The server levels its loaded program (the Scan panel's Level: POST /api/machine/level) and from then on the
// leveled program is the loaded one: Run and the pendant's Run run it, and GET /api/machine/program and the
// program event say "leveled": { scan, time, zSurface, arcSegmentMm, stats, warnings }. While the server's
// program is the leveling of this page's G-code (or, on a page with no G-code of its own, the server's
// leveled program it shows), the editor shows the leveled text, read-only, under a banner; the visualizer
// draws it with the scan's rectangle; Save writes the original (Save leveled G-code… the leveled text).
// Leveling off, or the server turning it off (another G-code, a new scan, another FluidNC: it says why in the
// Console), shows the original again. Nothing here levels again by itself.
//
// lev.active: the editor shows a leveled program; own: this page's G-code leveled (original: its text,
// originalDirty: it had unsaved changes), else the server's that this page shows (original only if the server
// gives it); info: the server's "leveled"; text, lines, total: the leveled program (as the server has it);
// source: the original line of each leveled line, if the server gives it (range: its inverse, made when needed);
// showOriginal: the editor shows the original (read-only as well); busy: 'level' | 'off' while that call is out
// (program events meanwhile are not news); error: why Level was refused; said: what the last Leveling off did.
const lev = {
  active: false, own: false, info: null, text: '', lines: null, total: null, source: null, original: null,
  originalDirty: false, showOriginal: false, busy: '', error: '', said: '', range: null,
};

/** This page's own G-code: the editor's text, or while it shows a leveled program, the text that was leveled. */
function gcodeText() {
  if (!lev.active) return editor.getText();
  return lev.own ? lev.original ?? '' : lev.text;
}

/** The program the server has when it has this page's: its upload, or the leveling of it. */
function expected() {
  return lev.active && lev.own ? { name: prog.name, lines: lev.lines, total: lev.total } : prog;
}

/** The server's program p is the leveling this page shows. */
function sameLeveling(p) {
  const a = p?.leveled, b = lev.info;
  return lev.active && !!a && !!b && p.name === prog.name && p.lines === lev.lines && p.total === lev.total
    && ['time', 'zSurface', 'arcSegmentMm'].every(k => a[k] === b[k]) && levelScanOf(a) === levelScanOf(b);
}

/** The scan a leveling's info names, as the server gives it (its file, or "last"). (Not levelName(): that adds
 *  the points from "stats", which an event may leave out.) */
function levelScanOf(info) {
  const s = info?.scanName ?? info?.scan;
  return typeof s === 'string' ? s : s?.name || s?.path || '';
}

const int = n => (Number.isFinite(n) ? Math.round(n).toLocaleString() : '?');
const node = (tag, cls, text) => Object.assign(document.createElement(tag), { className: cls, textContent: text ?? '' });

/** Resolves once fn() is true (checked every 50 ms), or after ms. */
async function waitFor(fn, ms) {
  for (const end = performance.now() + ms; !fn() && performance.now() < end;) await new Promise(r => setTimeout(r, 50));
}

/** The scan's area { x0, x1, y0, y1 } in inches from the leveling's info (the visualizer draws it), or null. */
function levelRect(info) {
  const s = info?.stats?.scan || (info?.scan && typeof info.scan === 'object' ? info.scan : null);
  if (!s || ![s.minX, s.maxX, s.minY, s.maxY].every(Number.isFinite)) return null;
  const k = s.units === 'MM' ? 1 / 25.4 : 1;
  return { x0: s.minX * k, x1: s.maxX * k, y0: s.minY * k, y1: s.maxY * k };
}

/** The leveled lines [first, last] made from line n of the original, or null (none, or the server didn't say). */
function levelRange(n) {
  if (!lev.source) return null;
  if (!lev.range) {
    lev.range = new Map();
    lev.source.forEach((orig, i) => {
      const r = lev.range.get(orig);
      if (r) r[1] = i + 1; else lev.range.set(orig, [i + 1, i + 1]);
    });
  }
  return lev.range.get(n) || null;
}

/** The editor's line for line n of the program the server runs (the leveled one, while the original is shown:
 *  its source line; 0: not known). */
function editorLine(n) {
  if (!(lev.active && lev.showOriginal)) return n;
  return n > 0 ? lev.source?.[n - 1] || 0 : 0;
}

/** The visualizer highlights the moves of the editor's line (while the original of a leveled program is shown:
 *  the leveled moves made from it). */
function highlightEditorLine(line) {
  if (lev.active && lev.showOriginal) viewer.highlight(...(levelRange(line) || [0, 0]));
  else viewer.highlight(line);
}

/** The server's leveled program got (GET /api/machine/program, with "leveled") into the editor, read-only, under
 *  the banner; the visualizer draws it and the scan's area. own: it is this page's G-code leveled (the original
 *  stays this page's G-code), else the server's, shown by a page that has none of its own. */
function showLeveled(got, own, fit = !own) {
  const source = [got.leveled?.source, got.source].find(Array.isArray) || null;
  Object.assign(lev, {
    active: true, own, info: got.leveled, text: got.text, lines: Number.isFinite(got.lines) ? got.lines : null,
    total: Number.isFinite(got.total) ? got.total : null, source, range: null, showOriginal: false, error: '',
    original: own ? editor.getText() : typeof got.original === 'string' ? got.original : null,
    originalDirty: own ? !editor.isClean() : false,
  });
  if (!own) {                        // (like adoptProgram: the server's program, as this page's)
    const warnings = Array.isArray(got.warnings) ? got.warnings.map(String) : [];
    Object.assign(prog, { state: lev.lines === 0 ? 'empty' : 'ok', name: got.name, lines: lev.lines, total: lev.total,
      error: '', text: got.text, warnings, said: '', foreign: false, echo: null });
    state.gcodePath = '';
    state.gcodeName = got.name;
  }
  editor.setText(lev.text, { dirty: lev.originalDirty });
  state.edits++;
  editor.setReadOnly(true);
  state.job = parseGcode(lev.text);
  viewer.setLeveledProgram(levelRect(lev.info));
  viewer.setJob(state.job, fit);
  showBanner();
  jobChanged(null);
  refreshTitles();
}

/** The editor shows its own G-code again (not leveled): restore: the original back in it (a page that showed the
 *  server's leveled program, and has no original: empty, and it shows the server's program again if readopt). */
function dropLeveled(restore = true, readopt = true) {
  if (!lev.active) return;
  const { own, original, originalDirty: dirty } = lev;
  Object.assign(lev, { active: false, own: false, info: null, text: '', lines: null, total: null, source: null, range: null,
    original: null, originalDirty: false, showOriginal: false });
  viewer.setLeveledProgram(null);
  showBanner();
  if (restore) {
    if (own) {
      editor.setText(original ?? '', { dirty });
      state.job = parseGcode(original ?? '');
    } else {
      editor.setText('');
      Object.assign(prog, { state: 'none', name: '', lines: null, total: null, error: '', text: null, warnings: [], said: '',
        foreign: false, echo: null });
      state.gcodeName = '';
      state.job = null;
    }
    state.edits++;
    viewer.setJob(state.job, false);
  }
  editor.setReadOnly(jobActive(mach.job));
  jobChanged(null);
  refreshTitles();
  if (restore && !own && readopt) adoptProgram();
}

/** ", arcs in 0.1 mm pieces" when the leveling cut arcs (G2/G3) into pieces (its stats.arcs, as the server's
 *  Console line says it), else '' (CarveMaker's G-code has none). */
function arcNote(info) {
  return info?.stats?.arcs > 0 && Number.isFinite(info.arcSegmentMm) ? `, arcs in ${+info.arcSegmentMm} mm pieces` : '';
}

/** The banner over the leveled program: what it is leveled with, its warnings (the points outside the scan in
 *  red), and Show original / Save leveled G-code… / Leveling off. */
let bannerKey = '';
function showBanner() {
  // (rebuilt only when what it shows changed: a job's line changes twice a second, and a rebuild would drop the
  // tooltip the mouse is on)
  // (not lev.info whole: it may carry the leveled program's source lines)
  const key = !lev.active ? '' : JSON.stringify([levelName(lev.info), lev.info?.arcSegmentMm, lev.info?.warnings,
    levelCoverage(lev.info), lev.lines, lev.own, lev.original !== null, lev.showOriginal, !!lev.busy, jobActive(mach.job), prog.lines]);
  if (key === bannerKey) return;
  bannerKey = key;
  if (!lev.active) { editor.setBanner(); return; }
  const info = lev.info || {};
  const arc = arcNote(info);
  const text = node('div', 'ebtext' + (lev.showOriginal ? ' orig' : ''));
  text.append(node('b', '', lev.showOriginal ? 'ORIGINAL' : 'LEVELED'),
    lev.showOriginal ? ` (read-only) of the G-code leveled with ${levelName(info)}: the leveled one runs`
      : ` with ${levelName(info)}${arc} · ${int(lev.lines)} lines to send`
        + (lev.own && Number.isFinite(prog.lines) ? ` (from ${int(prog.lines)})` : ''));
  text.title = text.textContent;
  const btns = node('div', 'ebbtns');
  const button = (label, title, fn, disabled = false) => {
    const b = node('button', '', label);
    b.title = title;
    b.disabled = disabled;
    b.onclick = fn;
    btns.append(b);
  };
  if (lev.original !== null) {
    button(lev.showOriginal ? 'Show leveled' : 'Show original', lev.showOriginal ? 'Show the leveled G-code (what runs)'
      : 'Show the G-code as it was before leveling (read-only; the leveled one still runs)', toggleOriginal);
  }
  button('Save leveled G-code…', 'Save the leveled G-code as a file (default: <name>-autoleveled.gcode in the G-code\'s folder)', saveLeveledAs);
  const running = jobActive(mach.job);
  button('Leveling off', running ? 'Leveling off: not while a job is running' : 'Back to the original G-code (Run runs that again)',
    levelOff, running || !!lev.busy);
  const warns = [];
  const cover = levelCoverage(info);
  if (cover) warns.push(node('div', 'ebwarn bad', `⚠ ${cover}`));
  for (const w of info.warnings || []) if (!/outside the scan/.test(w)) warns.push(node('div', 'ebwarn amber', `⚠ ${w}`));
  for (const w of warns) w.title = w.textContent;
  editor.setBanner(text, btns, ...warns.slice(0, 3));
}

/** Show original / Show leveled (both read-only; the leveled one is what runs). */
function toggleOriginal() {
  if (!lev.active || lev.original === null) return;
  lev.showOriginal = !lev.showOriginal;
  editor.setText(lev.showOriginal ? lev.original : lev.text, { dirty: lev.originalDirty });
  editor.setReadOnly(true);
  showBanner();
  jobChanged(null);
  if (!jobActive(mach.job)) viewer.highlight(0);
  refreshTitles();
}

/** Level (the Scan panel): the server levels this page's G-code (sent again first if it doesn't have exactly
 *  that) with the scan; the leveled program comes back into the editor. Leveled already: the original goes
 *  back first, and that is leveled. */
async function levelProgram(opts, scanName) {
  if (lev.busy) return;
  const busy = jobActive(mach.job) ? 'a job is running' : mach.scan?.running ? 'a surface scan is running' : '';
  if (busy) { status(`Level: not while ${busy}`, 'err'); return; }
  Object.assign(lev, { error: '', said: '', busy: 'level' });
  machineChanged();
  let own = true;
  try {
    if (lev.active) {
      own = lev.own;
      await machine.level({ off: true });
      dropLeveled(true, false);
    } else {
      if (!serverHasIt()) await uploadProgram();
      if (!serverHasIt()) throw new Error(prog.state === 'error' ? prog.error : 'the G-code could not be loaded on the server');
    }
    const name = prog.name;
    konsole.write(`Level ${name || 'the G-code'} with ${scanName} (Z surface ${opts.zSurface}, arcs in ${opts.arcSegmentMm} mm pieces)`, 'info');
    await machine.level(opts);
    const got = await machine.programText();
    if (!got?.leveled || typeof got.text !== 'string' || (own && got.name !== name)) {
      throw new Error('FluidSender did not give the leveled G-code back (another window may have loaded another one)');
    }
    showLeveled(got, own);
    const cover = levelCoverage(lev.info);
    status(`Leveled ${got.name} with ${levelName(lev.info)}: ${int(lev.lines)} lines to send${cover ? `. ${cover}` : ''}`, cover ? 'warn' : '');
    // (the event stream may still bring the program events of the calls above: they are not news)
    await waitFor(() => sameLeveling(mach.program), 3000);
  } catch (e) {
    lev.error = e.status === 404 ? 'This FluidSender server cannot level yet (restart FluidSender after the update)' : e.message;
    status(`Level: ${lev.error}`, 'err');
  } finally {
    lev.busy = '';
    if (!own && !lev.active) adoptProgram();      // (the server's program again, not leveled)
    showBanner();
    machineChanged();
  }
}

/** Leveling off: the server goes back to the original program (and so does the editor). */
async function levelOff() {
  if (!lev.active || lev.busy) return;
  if (jobActive(mach.job)) { status('Leveling off: not while a job is running', 'err'); return; }
  Object.assign(lev, { busy: 'off', error: '' });
  showBanner();
  machineChanged();
  try {
    await machine.level({ off: true });
    const name = prog.name || 'the G-code';
    if (lev.own) prog.echo = { name: prog.name, lines: prog.lines, total: prog.total, until: performance.now() + ECHO_MS };
    dropLeveled(true);
    lev.said = `Leveling off: ${name} runs as it is again`;
    status(lev.said);
    await waitFor(() => !!mach.program && !mach.program.leveled, 3000);      // (see levelProgram)
  } catch (e) {
    lev.error = e.status === 404 ? 'This FluidSender server cannot level yet (restart FluidSender after the update)' : e.message;
    status(`Leveling off: ${lev.error}`, 'err');
  } finally {
    lev.busy = '';
    adoptProgram();                  // (a page that showed the server's leveled program: the server's program now)
    showBanner();
    machineChanged();
  }
}

function uploadProgram() {
  clearTimeout(prog.timer);
  prog.timer = 0;
  if (prog.busy) {                  // one at a time, so the server can't get them out of order
    prog.again = true;
    return prog.busy;
  }
  prog.busy = (async () => {
    try {
      do {
        prog.again = false;
        await uploadOnce();
      } while (prog.again);
    } finally {
      prog.busy = null;
      machineChanged();
    }
  })();
  return prog.busy;
}

async function uploadOnce() {
  if (lev.active) {            // (the server turns its leveling off when it gets the G-code again)
    const own = lev.own;
    dropLeveled(true);
    if (!own) return;          // (it showed the server's leveled program: nothing of this page's to send)
  }
  const text = editor.getText(), name = gcodeName();
  if (!text.trim()) {
    const had = prog.state !== 'none' || mach.program;
    Object.assign(prog, { state: 'none', name: '', lines: null, total: null, error: '', text: null, warnings: [], said: '',
      foreign: false, echo: null });
    if (had && !jobActive(mach.job)) {
      try { await machine.program('', ''); } catch { /* an older server: nothing was loaded there */ }
      mach.program = null;
      prog.echo = { unload: true, until: performance.now() + ECHO_MS };
    }
    return;
  }
  const seq = ++prog.seq;
  prog.name = name;
  prog.state = prog.state === 'ok' || prog.state === 'sending' ? 'sending' : 'uploading';
  machineChanged();
  try {
    const res = await machine.program(name, text);
    if (seq !== prog.seq) return;
    const lines = Number.isFinite(res.lines) ? res.lines : null, total = Number.isFinite(res.total) ? res.total : null;
    const warnings = Array.isArray(res.warnings) ? res.warnings.map(String) : [];
    Object.assign(prog, { state: lines === 0 ? 'empty' : 'ok', lines, total, error: '', text, warnings, foreign: false,
      echo: { name, lines, total, until: performance.now() + ECHO_MS } });
    mach.program = { name, lines, total, warnings };     // (its program event confirms it)
    sayWarnings(name, warnings);
  } catch (e) {
    if (seq !== prog.seq) return;
    if (e.status === 404) Object.assign(prog, { state: 'old', error: '', text: null, warnings: [] });
    else {
      Object.assign(prog, { state: 'error', error: e.message, text: null, warnings: [], said: '' });
      status(`${name} can't be run: ${e.message}`, 'err');
    }
  }
}

/** The program's warnings, in the status bar and the Console: once, and again only when they change. */
function sayWarnings(name, warnings) {
  const key = `${name}\n${warnings.join('\n')}`;
  if (key === prog.said) return;
  prog.said = key;
  if (!warnings.length) return;
  for (const w of warnings) konsole.write(`${name}: ${w}${w.startsWith('...') ? '' : '. It runs as FluidNC reads it'}.`, 'warn');
  $('smsg').textContent = `${name}: ${warnings.length > 1 ? `${warnings.length} warnings (see the Console): ` : ''}${warnings[0]}`;
  $('smsg').className = 'warn';
}

/** The server has exactly the text shown here as its program. */
function programInSync() { return !lev.busy && serverHasIt(); }

/** programInSync(), whatever leveling call is out. */
function serverHasIt() {
  if (prog.state !== 'ok' || prog.timer || prog.busy || prog.foreign || prog.text !== gcodeText()) return false;
  // (leveled: the server's program is that leveling; else not a leveled one, which another window made)
  return lev.active ? sameLeveling(mach.program) : sameProgram(mach.program, prog) && !mach.program?.leveled;
}

/** A page with no G-code of its own (just opened or reloaded, or its G-code closed) shows the server's program:
 *  the running job's while a job runs, else the loaded one. That is what the job, or the pendant's Run, uses;
 *  it is shown as it is (not uploaded again, not marked changed). Not over a file being opened, the URL's
 *  file, or text typed here. With the file this page had open last, and the same text, it is that file again
 *  (Save writes it); else it has only its name (Save asks where). first: the page's first snapshot. */
let adopting = false;
const adoptable = () => !adopting && !state.opening && !urlFile && mach.link === 'up' && !state.gcodePath
  && !editor.getText().trim() && editor.isClean() && !prog.busy && !prog.timer && !lev.busy && !lev.active;

async function adoptProgram(first = false) {
  if (!adoptable() || !(jobActive(mach.job) || mach.program)) return;
  adopting = true;
  const edits = state.edits;
  try {
    let res;
    try {
      res = await machine.programText();
    } catch (e) {
      if (e.status === 404 && first) {      // an older server: during a job, its file again if this page had it open
        const path = store.get('gcode.open');
        if (jobActive(mach.job) && path && baseName(path) === mach.job.name) {
          status(`A job is running: opening its G-code, ${mach.job.name}, to follow it`);
          openPath(path);
        }
      }
      return;
    }
    if (!res?.name || typeof res.text !== 'string') return;
    if (res.leveled) {               // the server's program is a leveled one: shown leveled, under the banner
      adopting = false;
      if (state.edits !== edits || !adoptable()) return;
      if (res.job && Number.isFinite(res.job.started)) jobSource.set(res.job.started, res.job.source);
      showLeveled(res, false);
      status(res.job ? `A job is running: showing its G-code, ${res.name}, LEVELED with ${levelName(res.leveled)}`
        : `Showing ${res.name}, the G-code FluidSender has loaded, LEVELED with ${levelName(res.leveled)} (the pendant's Run runs it)`);
      sayWarnings(res.name, prog.warnings);
      return;
    }
    let path = '';
    const last = store.get('gcode.open');
    if (last && baseName(last) === res.name) {
      try { if (await readFile(last) === res.text) path = last; } catch { /* gone: the name only */ }
    }
    adopting = false;
    if (state.edits !== edits || !adoptable()) return;      // (something else came into the editor meanwhile)
    editor.setText(res.text);
    state.edits++;
    state.gcodePath = path;
    state.gcodeName = res.name;
    state.job = parseGcode(res.text);
    viewer.setJob(state.job, true);
    const lines = Number.isFinite(res.lines) ? res.lines : null, total = Number.isFinite(res.total) ? res.total : null;
    const warnings = Array.isArray(res.warnings) ? res.warnings.map(String) : [];
    Object.assign(prog, { state: lines === 0 ? 'empty' : 'ok', name: res.name, lines, total, error: '', text: editor.getText(),
      warnings, said: '', foreign: false, echo: null });
    if (res.job && Number.isFinite(res.job.started)) jobSource.set(res.job.started, res.job.source);
    jobChanged(null);                // (a job's: its line is marked)
    refreshTitles();
    status(res.job ? `A job is running: showing its G-code, ${res.name}`
      : `Showing ${res.name}, the G-code FluidSender has loaded (the pendant's Run runs it)`);
    sayWarnings(res.name, warnings);
  } finally {
    adopting = false;
    machineChanged();
  }
}

// Who started each job ('page' or 'pendant'; null: not known): the pendant's own Stop and Pause keys send
// nothing for a job its Run key did not start (its firmware), so the status bar says so (pendantNote)
const jobSource = new Map();

async function learnSource(job) {
  if (!jobActive(job) || !Number.isFinite(job.started) || jobSource.has(job.started)) return;
  jobSource.set(job.started, null);
  try {
    const res = await machine.programText(false);
    if (res?.job?.started === job.started) jobSource.set(job.started, res.job.source || null);
  } catch { /* an older server: not known */ }
  machineChanged();
}

/** The pendant's Stop / Pause keys do nothing now, while a job or a scan runs ('' if not so). The server says
 *  whether its firmware sends them (pendant.stopPause: true, as the fixed firmware always does; false: the
 *  original firmware after its own Stop key, until its Run key; null: not known yet, so a job or scan started
 *  on the page counts as one they don't reach, as with the original firmware). */
function pendantNote() {
  if (mach.link !== 'up' || mach.pendant?.state !== 'connected') return '';
  const job = mach.job, sp = mach.pendant.stopPause, running = !!mach.scan?.running || jobActive(job);
  if (!running || sp === true) return '';
  if (sp === false) return 'Pendant Stop/Pause inactive: press its Run key once to wake them';
  return mach.scan?.running || jobSource.get(job.started) === 'page' ? 'Pendant Stop/Pause inactive: started on the page' : '';
}

// ------------------------------------------------------------------ machine
// The server holds the serial port; this page follows its event stream. Nothing here connects
// by itself: only the Connect dialog's button does.

// What the server last said (link: the state of the event stream itself). axes: FluidNC's max speeds;
// jog: the hold jog that runs; zeroUndo: what Undo Zero puts back; alarm: { code, critical, text };
// program: the loaded program { name, lines, total }; job: the job (the last one until the next Run);
// pendant, probe, scan: their states; prog: this page's own program upload (see above).
const mach = {
  link: '', linkMsg: '', connected: false, port: null, firmware: null, status: null, modal: null,
  axes: null, jog: null, zeroUndo: null, alarm: null, program: null, job: null, pendant: null, probe: null, scan: null, prog,
  lev, levelSettings: null,
};
const live = () => mach.link === 'up' && mach.connected;
const NOT_LIVE = { status: null, modal: null, axes: null, jog: null, zeroUndo: null, alarm: null };

function machineChanged() {
  viewer.setPreLeveled(mach.program?.preLeveled);
  machinePanel.update(mach);
  jogPanel.update(mach);
  scanPanel.update(mach);
  const running = jobActive(mach.job);
  konsole.setEnabled(live(), running ? 'A job is running: typed lines are refused until it ends (? ! ~ on their own go at once)' : '');
  const w = live() && mach.status?.wpos;
  if (w) viewer.setTool(w[0], w[1], w[2]); else viewer.setTool(null);
  const fw = firmwareName(mach.firmware);
  $('sconn').textContent = live() ? `Connected: ${mach.port}${fw ? ' · ' + fw : ''}` : 'Not connected';
  $('sconn').classList.toggle('on', live());
  const chip = pendantChip(mach.link === 'up' ? mach.pendant : null, pendantNote());
  $('spend').textContent = chip.text;
  $('spend').className = chip.cls;
  $('spend').title = chip.title;
}

/** A message the server may also log as a console line: to the status bar, and once to the console. */
function machineNote(text, kind = 'info') {
  $('smsg').textContent = text;
  $('smsg').className = kind === 'err' ? 'err' : '';
  konsole.note(text, kind);
}

machine.onLink = (link, msg) => {
  const was = mach.link;
  Object.assign(mach, { link, linkMsg: msg || '' });
  if (link !== 'up') Object.assign(mach, { connected: false }, NOT_LIVE);
  if (link === 'up' && (was === 'down' || was === 'missing')) machineNote('The link to the FluidSender server is back');
  else if (link !== 'up' && msg) machineNote(msg, link === 'down' ? 'err' : 'info');
  machineChanged();
};
let snapshots = 0;
machine.onSnapshot = s => {
  const first = !snapshots++;
  Object.assign(mach, {
    connected: !!s.connected, port: s.port ?? null, firmware: s.firmware ?? null,
    status: s.status ?? null, modal: s.modal ?? null,
    axes: s.axes ?? null, jog: s.jog ?? null, zeroUndo: s.zeroUndo ?? null, alarm: s.alarm ?? null,
    program: s.program ?? null, job: s.job ?? null, pendant: s.pendant ?? null, probe: s.probe ?? null,
    // (the server's auto-level settings, settings.json "level" { zSurface, arcSegmentMm }, if its snapshot has them)
    levelSettings: s.levelSettings ?? s.level?.settings ?? (Number.isFinite(s.level?.zSurface) ? s.level : null),
  });
  konsole.replay(s.console || []);
  if (s.jogSettings) jogPanel.serverSettings(jogSettingsOf(s.jogSettings));
  scanEvent(s.scan ?? null, true);
  pendantDlg.setState(mach.pendant);
  jobChanged(null);
  // A leveled program this page shows that the server doesn't have now (it was restarted, or leveling was
  // turned off meanwhile): this page's own G-code again
  if (lev.active && !lev.busy && !sameLeveling(s.program)) {
    const sp = s.program;
    machineNote(`Auto-level off: FluidSender ${!sp ? 'has no program loaded (was it restarted?)'
      : !sp.leveled && sameProgram(sp, prog) ? 'turned it off (the Console says why)' : 'has another program loaded now'}`);
    dropLeveled(true);
  }
  // The server's program is never replaced or unloaded from here (see "the program" above): a page that
  // shows G-code sends it again only to a server that has none (it was restarted); one that differs is
  // said in the job line. A page with no G-code of its own shows the server's.
  if (editor.getText().trim()) {
    if ('program' in s && !s.program && !prog.busy && prog.state !== 'none' && !jobActive(mach.job)) uploadProgram();
  } else adoptProgram(first);
  learnSource(mach.job);
  machineChanged();
};
machine.onConn = c => {
  Object.assign(mach, { connected: !!c.connected, port: c.port ?? null, firmware: c.firmware ?? null });
  if (!c.connected) Object.assign(mach, NOT_LIVE);
  const bad = !c.connected && /error|fail|lost|unplug|respond|denied|gone/i.test(c.message || '');
  if (c.message) machineNote(c.message, bad ? 'err' : 'info');
  machineChanged();
};
machine.onStatus = s => {
  mach.status = s;
  konsole.statusReport(s.raw);
  machineChanged();
};
machine.onModal = m => { mach.modal = m; machineChanged(); };
machine.onConsole = ev => konsole.machine(ev);
machine.onAxes = ev => { mach.axes = ev.axes ?? null; machineChanged(); };
machine.onJog = ev => { mach.jog = ev.jog ?? null; jogPanel.serverJog(mach.jog); machineChanged(); };
machine.onZero = ev => { mach.zeroUndo = ev.zeroUndo ?? null; machineChanged(); };
machine.onAlarm = ev => { mach.alarm = ev.alarm ?? null; machineChanged(); };
machine.onProgram = ev => {
  mach.program = ev.program ?? null;
  const p = mach.program, e = prog.echo;
  if (prog.busy || lev.busy) { /* this page's upload or leveling is on its way: its answer settles what the server has */ }
  else if (lev.active && sameLeveling(p)) { /* the leveling this page shows */ }
  else if (lev.active && lev.own && p && !p.leveled && sameProgram(p, prog)) {
    dropLeveled(true);               // the server went back to this page's G-code (a new scan, another FluidNC: the Console says why)
  } else if (!lev.active && e && performance.now() < e.until && (p ? sameProgram(p, e) && !p.leveled : e.unload)) {
    prog.echo = null;                // (this page's)
  } else {
    prog.foreign = true;             // another window loaded, leveled or closed one
    dropLeveled(true);               // (what this page showed leveled, the server doesn't have now)
  }
  adoptProgram();
  machineChanged();
};
machine.onJob = ev => {
  const was = mach.job;
  mach.job = ev.job ?? null;
  jobChanged(was);
  if (jobActive(mach.job) && !jobActive(was)) {
    learnSource(mach.job);
    adoptProgram();                  // (a page with no G-code shows the job's)
  }
  machineChanged();
};
machine.onJogSettings = js => jogPanel.serverSettings(js);
machine.onPendant = ev => { mach.pendant = ev.pendant ?? null; pendantDlg.setState(mach.pendant); machineChanged(); };
machine.onProbe = ev => { mach.probe = ev.probe ?? null; machineChanged(); };
machine.onScan = ev => { scanEvent(ev.scan ?? null, false); machineChanged(); };

/** The job changed (was: the job before; null: look again, the G-code shown changed). While it runs the
 *  editor is read-only and follows its line. */
function jobChanged(was) {
  const job = mach.job, active = jobActive(job);
  // A running job's events come twice a second: when only its counts changed, only the window title has
  // to follow (the visualizer's highlight walks every segment of the toolpath)
  if (active && jobActive(was) && was.started === job.started && was.state === job.state && was.line === job.line) {
    docTitle();
    return;
  }
  editor.setReadOnly(active || lev.active);
  showBanner();                      // (its Leveling off is off during a job)
  // Its line is marked only if the editor shows the job's G-code (or the original of the leveled program it runs:
  // then the original line it comes from)
  const mine = active && job.name === prog.name && (!Number.isFinite(job.total) || job.total === expected().lines);
  editor.setJobLine(mine ? editorLine(job.line) : 0);
  if (active) {
    if (mine && (job.line || 0) !== viewer.line) viewer.highlight(job.line || 0);
  } else if (jobActive(was)) highlightEditorLine(editor.lastLine);
  if (jobActive(was) && !active && job) {          // it has just ended: say how (the Console has the server's words too)
    const text = { done: 'Job done', stopped: 'Job stopped', failed: 'Job failed' }[job.state] || `Job ${job.state}`;
    const extra = job.state === 'done' ? (Number.isFinite(job.elapsed) ? ` in ${fmtClock(job.elapsed)}` : '')
      : `${Number.isFinite(job.line) && job.line > 0 ? ` at line ${job.line}` : ''}${job.error || job.why ? `: ${job.error || job.why}` : ''}`;
    machineNote(`${text}: ${job.name}${extra}`, job.state === 'failed' ? 'err' : 'info');
  }
  refreshTitles();
}

/** The server's scan state (first: from a snapshot). The visualizer shows it while it is being made, when
 *  it ends, and on a fresh page that has no other scan open (if it is complete). */
function scanEvent(sc, first) {
  const was = mach.scan;
  mach.scan = sc;
  if (!sc) return;
  const pts = scanPoints(sc);
  const started = !!sc.running && (first || !was?.running);
  if (started) state.liveScan = true;
  if (first && !sc.running && scanDone(sc) && !state.scan) state.liveScan = true;     // (not one stopped part-way)
  if (!state.liveScan) return;
  const grid = scanResultGrid(sc) || scanGrid(sc.settings);
  if (!pts.length && !grid) return;
  let scan = null;
  if (scanDone(sc) && sc.result?.text) {          // finished: exactly the file it saved (like an opened .xyz)
    try { scan = parseScan(sc.result.text); } catch { scan = null; }
  }
  scan = scan || scanFromPoints(pts, grid || undefined);
  state.scan = scan;
  state.scanPath = !sc.running && sc.savedTo && isScan(sc.savedTo) ? sc.savedTo : '';
  state.scanLabel = sc.running ? `scanning (${pts.length} of ${scanTotal(sc)})` : scan.partial ? `unfinished (${pts.length} of ${scanTotal(sc)})` : 'last scan';
  viewer.setScan(scan, started || (first && !was));
  refreshTitles();
}

async function machineCall(what, fn) {
  try {
    await fn();
    return true;
  } catch (e) {
    const msg = e.status === 404 ? 'this FluidSender server does not have it yet (restart FluidSender after the update)' : e.message;
    status(`${what} failed: ${msg}`, 'err');
    return false;
  }
}

const REALTIME = {
  hold: 'Hold (!)', resume: 'Resume (~)', reset: 'Soft Reset (Ctrl-X)', status: 'Status (?)', jogcancel: 'Stop Jog (jog cancel)',
};

async function realtime(cmd) {
  if (cmd === 'reset') { machinePanel.noteReset(); machineChanged(); }
  if (cmd !== 'status') konsole.write(REALTIME[cmd], 'info');     // logged first: its effects arrive before the reply
  await machineCall(REALTIME[cmd], () => machine.realtime(cmd));
}

/** Soft Reset: while a job (or a scan) runs, the safe stop (the server holds first and resets once the
 *  machine has stopped); else Ctrl-X at once. Asks first either way. */
function softReset() {
  const job = mach.job;
  if (jobActive(job) && job.state === 'stopping') {
    if (!confirm('FluidSender is already stopping the job: it holds first and resets once the machine has stopped.\n\n'
      + 'Reset now (Ctrl-X), without waiting? A reset while the machine is moving can lose the position: home again afterwards.')) return;
    return realtime('reset');
  }
  if (jobActive(job)) {
    if (!confirm(`Stop the job ${job.name}?\n\nFluidSender stops it safely: first a Hold (the machine slows down to a stop), then, once `
      + 'it has stopped, a Soft Reset (Ctrl-X). The reset drops the lines FluidNC has not run yet and turns the spindle, the vacuum '
      + 'and AUX off. The position is kept.\n\nIf the machine has not stopped within 10 s it resets anyway, and the position may then be lost.')) return;
    konsole.write('Stop the job (Hold, then Soft Reset once stopped)', 'info');
    machinePanel.noteReset(); machineChanged();
    return machineCall('Stop the job', () => machine.stop());
  }
  if (mach.scan?.running) {
    if (!confirm('Stop the surface scan?\n\nThe probe stops at once, and the machine stops safely: a Hold first, then a Soft Reset once '
      + 'it has stopped (the position is kept). The points measured so far stay.')) return;
    machinePanel.noteReset(); machineChanged();
    return machineCall('Stop the scan', () => machine.stop());
  }
  if (!confirm('Soft reset FluidNC (Ctrl-X)?\n\nIt stops everything at once and drops the lines waiting to be sent. '
    + 'A reset while the machine is moving can lose the position: home again afterwards. '
    + 'When the machine is stopped, the position is kept.')) return;
  realtime('reset');
}

/** Run: the G-code shown (sent again first if the server's copy is not exactly it), after asking. */
async function runJob() {
  let why = runWhy(mach);
  if (why) { status(`Run: ${why}`, 'err'); return; }
  if (lev.active && !programInSync()) {
    // (the editor showed a leveled program that is not the server's any more: never run something else instead)
    dropLeveled(true);
    status('Run: FluidSender does not have the leveled G-code loaded any more (see the Console), so the editor shows '
      + 'the original again. Level it again, or Run to run it not leveled.', 'err');
    return;
  }
  if (!programInSync()) await uploadProgram();
  why = runWhy(mach) || (!programInSync() ? 'the G-code could not be loaded on the server' : '');
  if (why) { status(`Run: ${why}`, 'err'); return; }
  const p = mach.program, skipped = p.total - p.lines, warn = prog.warnings || [];
  if (!confirm(`Run ${p.name}?\n\n${p.lines.toLocaleString()} lines to send`
    + (skipped > 0 && !lev.active ? ` (${p.total.toLocaleString()} in the file; comments and empty lines are not sent)` : '')
    + `.${levelNote()}\n\n${runModes()}\n\nThe spindle and the moves start: check the bit, the work zero and the clamps.\n\n`
    + 'Hold pauses it, Resume goes on, Soft Reset stops the job safely.'
    + (warn.length ? `\n\nWarning: ${warn[0]}.${warn.length > 1 ? ` (${warn.length - 1} more in the Console.)` : ''}` : ''))) return;
  why = runWhy(mach) || (!programInSync() ? 'the G-code changed' : '');
  if (why) { status(`Run: ${why}`, 'err'); return; }
  await machineCall('Run', async () => {
    const res = await machine.run();
    if (Number.isFinite(res?.job?.started)) jobSource.set(res.job.started, 'page');
  });
}

/** What Run's question says about auto-level: "LEVELED with ..." (and its warnings), or "NOT leveled" when a
 *  complete scan exists in this session and leveling is off; else nothing. */
function levelNote() {
  if (lev.active) {
    const info = lev.info || {}, cover = levelCoverage(info);
    const arc = arcNote(info);
    const more = (info.warnings || []).filter(w => !/outside the scan/.test(w));
    const wrong = levelMismatch(info);
    return (wrong ? `\n\n${wrong}. Cancel unless you are sure.` : '')
      + `\n\nLEVELED with ${levelName(info)}${arc}: the moves' Z follows the scanned surface.`
      + (cover ? `\nWarning: ${cover}.` : '') + more.map(w => `\nWarning: ${w}.`).join('');
  }
  if (mach.program?.preLeveled) {
    return `\n\nALREADY LEVELED: ${mach.program.preLeveled}. It runs as it is: the scan is not added again.`;
  }
  if (scanDone(mach.scan)) {
    const s = mach.scan.settings;
    return `\n\nNOT leveled: the last scan${s ? ` (${s.xPoints} × ${s.yPoints} points)` : ''} is not applied to it `
      + '(Level, in the Scan panel, would).';
  }
  return '';
}

/** The units and modes a program starts in (FluidSender sends G20 G90 ahead of it), the work offset FluidNC
 *  has, and where the program itself changes them. */
function runModes() {
  const wcs = /\bG5[4-9](?:\.[1-3])?\b/.exec(mach.modal?.gc || '')?.[0];
  let text = `It runs in inches (G20), absolute (G90)${wcs ? `, ${wcs}` : ''}: FluidSender sends G20 G90 first, `
    + 'so a program without units runs in inches.';
  const own = state.job?.modes || {};
  if (own.mm) text += ` The program switches to millimeters (G21) at line ${own.mm}.`;
  if (own.incr) text += ` It switches to incremental moves (G91) at line ${own.incr}.`;
  if (own.wcs && own.wcs !== wcs) text += ` It uses the work offset ${own.wcs} from line ${own.wcsAt}.`;
  return text;
}

/** Stops every jog at once (0x85: FluidNC slows down and drops the rest of the jog). */
function stopJog() { return realtime('jogcancel'); }

// The overrides (FluidNC's realtime bytes, so also during a job, a hold or a jog): the server sends them and
// says each new value in the Console once FluidNC's report confirms it
const OV_WHAT = { feed: 'Feed override', spindle: 'Spindle override', rapid: 'Rapid override', all: 'Overrides' };

/** body: { which: 'feed' | 'spindle', step: ±1 | ±10 } or { which, to: 10..200 } or { which: 'rapid', to: 100 | 50 | 25 }
 *  or { which: 'all', reset: true }. */
function override(body) {
  const how = body.reset ? 'to 100%' : 'step' in body ? `${body.step > 0 ? '+' : '−'}${Math.abs(body.step)}%` : `${body.to}%`;
  return machineCall(`${OV_WHAT[body.which] || 'Override'} ${how}`, () => machine.override(body));
}

// Home, Unlock, Zero and Go to send their lines like typed ones (the Console shows them); the server
// refuses them in the wrong state, with the reason
const HOME_WHAT = {
  '': ['Home all axes ($H)?', 'The machine moves to its limit switches.'],
  X: ['Home X ($HX)?', 'X moves to its limit switch.'],
  Y: ['Home Y ($HY)?', 'Y moves to its limit switch.'],
  Z: ['Home Z ($HZ)?', 'Z moves to its limit switch.'],
};

function home(axes = '') {
  const [ask, moves] = HOME_WHAT[axes];
  if (!confirm(`${ask}\n\n${moves} Your work zero (G54) is kept.\n\n`
    + '(A work zero set while the machine was not homed will not match after homing.)')) return;
  return machineCall(ask.replace('?', ''), () => machine.home(axes));
}

function unlock() { return machineCall('Unlock ($X)', () => machine.unlock()); }

/** axes: 'X', 'XYZ'...: their work position becomes value (inches). */
function zero(axes, value = 0) {
  return machineCall(`Zero ${[...axes].join(' ')}`, () => machine.zero(axes, value));
}

function undoZero() { return machineCall('Undo Zero', () => machine.zeroUndo()); }

const GOTO_WHAT = { xy: 'Go to XY 0', z: 'Go to Z 0', zero: 'Go to X0 Y0 Z0' };

/** to: 'xy' or 'z' (the work zero). */
function gotoZero(to) { return machineCall(GOTO_WHAT[to], () => machine.goto(to)); }

// The Machine menu's Zero and Go to items ask with a dialog (the panels' buttons ask with a second click)
function askZero(axes) {
  const names = [...axes].join(' ');
  if (!confirm(`Zero ${names}?\n\nThe work position of ${names} becomes 0 (G10 L20 P0 ${[...axes].map(a => a + '0').join(' ')}). `
    + 'Undo Zero puts the old one back.')) return;
  zero(axes);
}

function askUndoZero() {
  const u = mach.zeroUndo;
  if (!confirm(`Undo Zero?\n\nPuts back the work position from before the last zero${u ? ` (${undoWhen(u)})` : ''}: ${u?.text || ''}.`)) return;
  undoZero();
}

function askGoto(to) {
  const z = mach.status?.wpos?.[2];
  const text = to === 'xy' ? `A rapid move (G0) to X0 Y0 (work)${Number.isFinite(z) && z < 0.2 ? '. Z is below 0.2 in, so it first rises to Z 0.2' : ''}.`
    : 'A rapid move (G0) to Z0 (work). X and Y stay where they are.';
  if (!confirm(`${GOTO_WHAT[to]}?\n\n${text}`)) return;
  gotoZero(to);
}

function startScan(settings) {
  const s = settings, n = s.xPoints * s.yPoints;
  const r = v => +v.toFixed(4);
  const offset = s.probeOffX || s.probeOffY
    ? `Offset from spindle X ${s.probeOffX} Y ${s.probeOffY} in: the bit goes to X ${r(s.minX + s.probeOffX)} … ${r(s.maxX + s.probeOffX)} `
      + `and Y ${r(s.minY + s.probeOffY)} … ${r(s.maxY + s.probeOffY)} so the probe lands on the area above.\n\n`
    : '⚠ WARNING: Offset from spindle is 0, 0. That is right only if the probe is on the spindle centerline. If it '
      + 'is not (yours: Y 3), Cancel and set it in the Probe section first: the scan would measure the wrong place.\n\n';
  if (!confirm(`Start the surface scan?\n\n${offset}${n} points (${s.xPoints} × ${s.yPoints}) over X ${s.minX} … ${s.maxX} and Y ${s.minY} … ${s.maxY} in. `
    + 'The machine moves (G0, rapid) to each point, in X and Y only: its Z does not move, so make sure the bit clears the work '
    + `and the clamps. At each point the probe goes down to the surface and back up ${s.zRetract} in.\n\n`
    + 'Stop (or Soft Reset) stops it.')) return;
  return machineCall('Start Scan', () => machine.scanStart(settings));
}

function disconnect() {
  // Closing the port neither stops nor resets this board, and the buttons that could are gone once disconnected
  if (jobActive(mach.job) || mach.scan?.running) {
    status(`Disconnect: not while a ${mach.scan?.running ? 'scan' : 'job'} is running (stop it first)`, 'err');
    return;
  }
  const st = mach.status, ask = [];
  const busy = { Run: 'moving', Jog: 'jogging', Home: 'homing', Hold: 'in a feed hold', Door: 'stopped by the door' }[st?.state];
  if (busy) {
    ask.push(`FluidNC is ${busy}. Disconnecting does not stop or reset it, and FluidSender can only Hold, Resume or `
      + 'Soft Reset while connected.');
  }
  // ... nor does it switch anything off: the relays stay as they are, and the toggles can't reach them
  const on = [st?.flood && 'the vacuum (M8)', st?.mist && 'AUX (M7)', st?.spindle > 0 && 'the spindle'].filter(Boolean);
  if (on.length) {
    const list = on.length > 1 ? `${on.slice(0, -1).join(', ')} and ${on.at(-1)}` : on[0], it = on.length > 1 ? 'them' : 'it';
    ask.push(`${list[0].toUpperCase()}${list.slice(1)} ${on.length > 1 ? 'are' : 'is'} on. Disconnecting leaves ${it} on, `
      + `and FluidSender can't switch ${it} off until you connect again.`);
  }
  if (ask.length && !confirm(`${ask.join('\n\n')}\n\nDisconnect anyway?`)) return;
  return machineCall('Disconnect', () => machine.disconnect());
}

async function sendLine(line) {
  // A realtime character typed on its own goes at once, not behind the lines waiting to be sent
  const rt = { '?': 'status', '!': 'hold', '~': 'resume' }[line];
  if (rt) {
    await realtime(rt);
    if (rt === 'status' && mach.status?.raw) konsole.write(mach.status.raw, 'raw');
    return;
  }
  try {
    await machine.send(line);
  } catch (e) {
    konsole.write(`Not sent: ${line}  (${e.message})`, 'err');
  }
}

// ------------------------------------------------------------------ dialogs

const openDlg = $('openDlg');
const openBrowser = new FileBrowser({
  pickOnDouble: true,
  storeKey: 'files.dir',
  onSelect: path => { openDlg.dataset.path = path; },
  onPick: path => { openDlg.close(); openPath(path); },
});
openDlg.querySelector('.dbody').append(openBrowser.root);
openDlg.querySelector('[data-act=ok]').onclick = () => {
  const p = openDlg.dataset.path;
  if (p) { openDlg.close(); openPath(p); }
};
function showOpen() {
  openDlg.dataset.path = '';
  openBrowser.load(state.gcodePath ? dirName(state.gcodePath) : undefined);
  openDlg.showModal();
}

const saveDlg = $('saveDlg');
const saveName = saveDlg.querySelector('input');
const saveBrowser = new FileBrowser({
  filter: name => GCODE_EXT.test(name),
  storeKey: 'files.dir',
  onSelect: (path, name) => { saveName.value = name; },
  onPick: () => {},
});
saveDlg.querySelector('.dbody').append(saveBrowser.root);
let saveLeveledMode = false;       // the dialog is Save leveled G-code… (else Save As)
async function doSaveAs() {
  let name = saveName.value.trim();
  if (!name) return;
  if (!GCODE_EXT.test(name)) name += saveLeveledMode ? '.gcode' : '.nc';
  const path = saveBrowser.dir + '/' + name;
  const exists = saveBrowser.list.querySelector(`[data-path="${CSS.escape(path)}"]`);
  if (exists && !confirm(`${name} already exists. Replace it?`)) return;
  saveDlg.close();
  if (saveLeveledMode) {
    await saveLeveled(path);
  } else {
    state.gcodePath = path;
    await save();
  }
  files.load(files.dir);
}
saveDlg.querySelector('[data-act=ok]').onclick = doSaveAs;
saveName.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); doSaveAs(); } };
/** A window that shows the server's leveled program has no G-code of its own to save (Save leveled G-code… then). */
function noOwnGcode(what) {
  if (!(lev.active && !lev.own)) return false;
  status(`${what}: this window shows the leveled G-code FluidSender has loaded, not a file of its own. Use Save leveled G-code….`, 'err');
  return true;
}
function saveAs() {
  if (!editor.getText() || noOwnGcode('Save As')) return;
  saveLeveledMode = false;
  saveDlg.querySelector('.dhead').textContent = 'Save G-code as';
  saveName.value = state.gcodePath ? baseName(state.gcodePath) : state.gcodeName || 'untitled.nc';
  saveBrowser.load(state.gcodePath ? dirName(state.gcodePath) : undefined);
  saveDlg.showModal();
  saveName.select();
}

/** Save leveled G-code…: the leveled text exactly as FluidSender runs it (what UGS's Export writes for the same
 *  file and scan), by default as <name>-autoleveled.gcode in the G-code's folder. */
function saveLeveledAs() {
  if (!lev.active || !lev.text) return;
  saveLeveledMode = true;
  saveDlg.querySelector('.dhead').textContent = 'Save leveled G-code as';
  const base = (state.gcodePath ? baseName(state.gcodePath) : prog.name || state.gcodeName || 'untitled').replace(/\.[^./]*$/, '');
  saveName.value = `${base}-autoleveled.gcode`;
  saveBrowser.load(state.gcodePath ? dirName(state.gcodePath) : undefined);
  saveDlg.showModal();
  saveName.setSelectionRange(0, saveName.value.length - '.gcode'.length);
}

/** The leveled text with a header line that marks it as leveled already (after a first "%" line, if it has one):
 *  FluidSender then never levels it again (leveling.py pre_leveled). ASCII and short: FluidNC 4.1.1 refuses lines
 *  over 127 characters. */
function markLeveled(text) {
  const info = lev.info || {}, sc = info.stats?.scan || {};
  const scan = levelName(info).replace(/ \(.*$/, '').replace(/[()]/g, '');
  const pts = Number.isFinite(sc.xPoints) ? ` ${sc.xPoints}x${sc.yPoints} pts` : '';
  const zs = Number.isFinite(info.zSurface) ? ` Zsurf ${+info.zSurface.toFixed(4)}` : '';
  const d = new Date(), p2 = n => String(n).padStart(2, '0');
  const when = `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
  const mark = `(FluidSender: auto-leveled, do not level again: ${scan}${pts}${zs} ${when})`.replace(/[^\x20-\x7e]/g, '?');
  const line = mark.length > 120 ? `${mark.slice(0, 119)})` : mark;
  const nl = text.includes('\r\n') ? '\r\n' : '\n';
  const first = /^%[^\n]*\n/.exec(text);
  return first ? text.slice(0, first[0].length) + line + nl + text.slice(first[0].length) : line + nl + text;
}

async function saveLeveled(path) {
  if (!lev.active || !lev.text) return;
  try {
    const res = await saveFile(path, markLeveled(lev.text));
    status(`Saved the leveled G-code to ${res.path || path} (${Math.max(1, Math.round((res.size || lev.text.length) / 1024))} KB)`);
    addRecent(res.path || path);
  } catch (e) {
    status(`Save leveled G-code failed: ${e.message}`, 'err');
  }
}

// A .xyz file to level with (the Scan panel's "a .xyz file…")
const xyzDlg = $('xyzDlg');
const xyzBrowser = new FileBrowser({
  filter: name => /\.xyz$/i.test(name),
  pickOnDouble: true,
  storeKey: 'files.dir',
  onSelect: path => { xyzDlg.dataset.path = path; },
  onPick: path => { xyzDlg.close(); scanPanel.levelFile(path); },
});
xyzDlg.querySelector('.dbody').append(xyzBrowser.root);
xyzDlg.querySelector('[data-act=ok]').onclick = () => {
  const p = xyzDlg.dataset.path;
  if (p) { xyzDlg.close(); scanPanel.levelFile(p); }
};
function pickLevelScan() {
  xyzDlg.dataset.path = '';
  xyzBrowser.load(state.scanPath ? dirName(state.scanPath) : state.gcodePath ? dirName(state.gcodePath) : undefined);
  xyzDlg.showModal();
}

// Save Scan…: like Save As, for the last scan (the server writes it, in the UGS .xyz format)
const scanSaveDlg = $('scanSaveDlg');
const scanSaveName = scanSaveDlg.querySelector('input');
const scanSaveBrowser = new FileBrowser({
  filter: name => /\.xyz$/i.test(name),
  storeKey: 'files.dir',
  onSelect: (path, name) => { scanSaveName.value = name; },
  onPick: () => {},
});
scanSaveDlg.querySelector('.dbody').append(scanSaveBrowser.root);
const scanSaveable = () => scanDone(mach.scan);
async function doSaveScan() {
  let name = scanSaveName.value.trim();
  if (!name) return;
  if (!/\.xyz$/i.test(name)) name += '.xyz';
  const path = scanSaveBrowser.dir + '/' + name;
  const exists = scanSaveBrowser.list.querySelector(`[data-path="${CSS.escape(path)}"]`);
  if (exists && !confirm(`${name} already exists. Replace it?`)) return;
  scanSaveDlg.close();
  try {
    const res = await machine.scanSave(path);
    const saved = res?.path || path;
    status(`Saved the scan to ${saved}`);
    scanPanel.say(`Saved to ${saved}`, 'good');
    if (state.liveScan) { state.scanPath = saved; refreshTitles(); }
    addRecent(saved);
    files.load(files.dir);
  } catch (e) {
    const msg = e.status === 404 ? 'this FluidSender server does not have it yet (restart FluidSender after the update)' : e.message;
    status(`Save Scan failed: ${msg}`, 'err');
    scanPanel.say(`Not saved: ${msg}`, 'bad');
  }
}
scanSaveDlg.querySelector('[data-act=ok]').onclick = doSaveScan;
scanSaveName.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); doSaveScan(); } };
function saveScanAs() {
  if (!scanSaveable()) return;
  scanSaveName.value = 'scan.xyz';
  scanSaveBrowser.load(state.gcodePath ? dirName(state.gcodePath) : undefined);
  scanSaveDlg.showModal();
  scanSaveName.select();
}

const connectDlg = new ConnectDialog($('connDlg'), machine, (text, kind) => status(text, kind));
const pendantDlg = new PendantDialog($('pendDlg'), machine, (text, kind) => status(text, kind));
$('spend').onclick = () => { if (mach.link === 'up') pendantDlg.show(); };

async function disconnectPendant() {
  if (!confirm(`Disconnect the pendant (${mach.pendant?.port || ''})?\n\nIts keys do nothing until it is connected again.`)) return;
  await machineCall('Disconnect Pendant', () => machine.pendantDisconnect());
}

for (const dlg of document.querySelectorAll('dialog')) {
  dlg.querySelector('[data-act=cancel]')?.addEventListener('click', () => dlg.close());
}

// ------------------------------------------------------------------ menus

const panelItem = (id, label) => ({ label, checked: () => !!dock.getPanel(id), action: () => togglePanel(id) });
const viewOpt = (key, label, extra = {}) => ({ label, checked: () => viewer.options[key], action: () => { showPanel('viewer'); viewer.setOption(key, !viewer.options[key]); }, ...extra });
const editCmd = (label, cmd, shortcut) => ({ label, shortcut, action: () => { showPanel('editor'); editor.command(cmd); } });
const later = (label, stage) => ({ label, enabled: false, title: `Coming in ${stage}` });
const coolItem = (which, label) => ({
  label, checked: () => live() && !!mach.status?.[which === 'vacuum' ? 'flood' : 'mist'],
  enabled: () => !jogPanel.coolBtns[which].disabled, title: () => jogPanel.coolBtns[which].title,
  action: () => jogPanel.toggleCool(which),
});
const gotoOk = () => !jogPanel.go.xy.btn.disabled;
const pendantOn = () => mach.link === 'up' && !!mach.pendant && mach.pendant.state !== 'off';

new MenuBar($('menubar'), [
  {
    label: 'File', items: [
      { label: 'Open…', shortcut: 'Ctrl+O', action: showOpen },
      {
        label: 'Open Recent', items: () => {
          const list = store.get('recent', []);
          return list.length ? list.map(p => ({ label: baseName(p), title: p, action: () => openPath(p) }))
            : [{ label: '(none yet)', enabled: false }];
        },
      },
      { separator: true },
      { label: 'Save', shortcut: 'Ctrl+S', enabled: () => !!editor.getText() && (!editor.isClean() || !state.gcodePath), action: save },
      { label: 'Save As…', shortcut: 'Ctrl+Shift+S', enabled: () => !!editor.getText(), action: saveAs },
      { label: 'Reload from Disk', enabled: () => !!state.gcodePath && !jobActive(mach.job), action: () => { if (confirmDiscard()) { editor.markClean(); openPath(state.gcodePath); } } },
      { label: 'Save Scan…', enabled: scanSaveable, title: () => (scanSaveable() ? '' : 'When a surface scan has finished'), action: saveScanAs },
      {
        label: 'Save Leveled G-code…', enabled: () => lev.active && !!lev.text, action: saveLeveledAs,
        title: () => (lev.active ? '' : 'When the G-code is leveled (Scan panel: Level)'),
      },
      { separator: true },
      { label: 'Close G-code', enabled: () => !!(state.gcodePath || editor.getText()) && !jobActive(mach.job), action: closeGcode },
      { label: 'Close Scan', enabled: () => !!state.scan, action: closeScan },
    ],
  },
  {
    label: 'Edit', items: [
      editCmd('Undo', 'undo', 'Ctrl+Z'),
      editCmd('Redo', 'redo', 'Ctrl+Y'),
      { separator: true },
      editCmd('Find…', 'find', 'Ctrl+F'),
      editCmd('Find Next', 'findNext', 'Ctrl+G'),
      editCmd('Replace…', 'replace', 'Shift+Ctrl+F'),
      editCmd('Go to Line…', 'jumpToLine', 'Alt+G'),
    ],
  },
  {
    label: 'View', items: [
      panelItem('viewer', 'Visualizer'),
      panelItem('editor', 'G-code Editor'),
      panelItem('machine', 'Machine'),
      panelItem('jog', 'Jog'),
      panelItem('files', 'Files'),
      panelItem('info', 'Toolpath'),
      panelItem('scan', 'Scan'),
      panelItem('console', 'Console'),
      { separator: true },
      viewOpt('toolpath', 'Show Toolpath'),
      viewOpt('rapids', 'Show Rapids'),
      viewOpt('scan', 'Show Scan Surface'),
      viewOpt('leveled', 'Leveled Toolpath', { enabled: () => viewer.scanUsable() && !viewer.levRect && !viewer.preLeveled }),
      { separator: true },
      { label: 'Top View', action: () => { showPanel('viewer'); viewer.view('top'); } },
      { label: 'Iso View', action: () => { showPanel('viewer'); viewer.view('iso'); } },
      { label: 'Front View', action: () => { showPanel('viewer'); viewer.view('front'); } },
      { separator: true },
      {
        label: 'Maximize Panel', shortcut: 'F11', enabled: () => !!dock.activePanel,
        checked: () => dock.hasMaximizedGroup(), action: toggleMaximize,
      },
      { label: 'Float Panel', enabled: () => !!dock.activePanel, action: () => dock.addFloatingGroup(dock.activePanel) },
      { label: 'Reset Layout', action: () => { store.remove(LAYOUT_KEY); defaultLayout(); refreshTitles(); } },
      { separator: true },
      {
        label: 'Theme', title: 'The colors: Dark (the original look), Medium (mid-gray) or Light',
        items: () => THEMES.map(([id, label]) => ({ label, checked: () => currentTheme() === id, action: () => setTheme(id) })),
      },
    ],
  },
  {
    label: 'Machine', items: [
      {
        label: 'Connect…', enabled: () => mach.link === 'up' && !mach.connected,
        title: () => (mach.link === 'up' ? '' : mach.linkMsg), action: () => connectDlg.show(),
      },
      { label: 'Disconnect', enabled: () => live() && !jobActive(mach.job) && !mach.scan?.running, action: disconnect },
      { separator: true },
      { label: 'Run…', enabled: () => !runWhy(mach), title: () => (runWhy(mach) ? `Run: ${runWhy(mach)}` : ''), action: runJob },
      { label: 'Hold (!)', enabled: () => !machinePanel.btn.hold.disabled, title: () => machinePanel.btn.hold.title, action: () => realtime('hold') },
      { label: 'Resume (~)', enabled: () => !machinePanel.btn.resume.disabled, title: () => machinePanel.btn.resume.title, action: () => realtime('resume') },
      { label: 'Soft Reset (Ctrl-X)…', enabled: live, title: () => machinePanel.btn.reset.title, action: softReset },
      {
        label: 'Overrides to 100%', enabled: () => live() && mach.status?.state !== 'No response',
        title: () => 'Feed, rapid and spindle overrides back to 100 % (at once)', action: () => override({ which: 'all', reset: true }),
      },
      { separator: true },
      { label: 'Home All ($H)…', enabled: () => machinePanel.homeOk, action: () => home('') },
      { label: 'Home X ($HX)…', enabled: () => machinePanel.homeOk, action: () => home('X') },
      { label: 'Home Y ($HY)…', enabled: () => machinePanel.homeOk, action: () => home('Y') },
      { label: 'Home Z ($HZ)…', enabled: () => machinePanel.homeOk, action: () => home('Z') },
      { label: 'Unlock ($X)', enabled: () => machinePanel.unlockOk, action: unlock },
      { separator: true },
      { label: 'Zero X…', enabled: () => machinePanel.zeroOk, action: () => askZero('X') },
      { label: 'Zero Y…', enabled: () => machinePanel.zeroOk, action: () => askZero('Y') },
      { label: 'Zero Z…', enabled: () => machinePanel.zeroOk, action: () => askZero('Z') },
      { label: 'Zero All…', enabled: () => machinePanel.zeroOk, action: () => askZero('XYZ') },
      {
        label: 'Undo Zero…', enabled: () => machinePanel.undoOk, action: askUndoZero,
        title: () => (live() && mach.zeroUndo ? `Puts back the work position from before the last zero: ${mach.zeroUndo.text}` : ''),
      },
      { label: 'Go to XY 0…', enabled: gotoOk, action: () => askGoto('xy') },
      { label: 'Go to Z 0…', enabled: gotoOk, action: () => askGoto('z') },
      { separator: true },
      coolItem('vacuum', 'Vacuum (M8)'),
      coolItem('aux', 'AUX (M7)'),
      { label: 'Stop Jog', enabled: live, action: stopJog },
      { separator: true },
      { label: 'Connect Pendant…', enabled: () => mach.link === 'up' && !pendantOn(), action: () => pendantDlg.show() },
      { label: 'Disconnect Pendant', enabled: pendantOn, action: disconnectPendant },
    ],
  },
  {
    label: 'Tools', items: [
      { label: 'Surface Scan…', action: () => showPanel('scan') },
      { label: 'Auto-level…', title: () => 'Level the G-code with a scan: the Scan panel\'s last column', action: () => showPanel('scan') },
      later('Probe Accuracy Test…', 'a later stage'),
      later('Check Leveled File…', 'a later stage'),
    ],
  },
  {
    label: 'Help', items: [
      { label: 'Keyboard Shortcuts', action: () => $('helpDlg').showModal() },
      { label: 'About FluidSender', action: () => $('aboutDlg').showModal() },
    ],
  },
]);

function toggleMaximize() {
  if (dock.hasMaximizedGroup()) {
    dock.exitMaximizedGroup();
    store.set(LAYOUT_KEY, dock.toJSON());      // (the save above skips maximized layouts)
  } else if (dock.activePanel) dock.maximizeGroup(dock.activePanel);
}

// ------------------------------------------------------------------ keyboard

globalThis.CodeMirror.commands.save = () => save();

document.addEventListener('keydown', e => {
  if (e.defaultPrevented || document.querySelector('dialog[open]')) return;
  const ctrl = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
  if (ctrl && k === 'o') { e.preventDefault(); showOpen(); }
  else if (ctrl && e.shiftKey && k === 's') { e.preventDefault(); saveAs(); }
  else if (ctrl && k === 's') { e.preventDefault(); save(); }
  else if (ctrl && !e.shiftKey && k === 'f') { e.preventDefault(); showPanel('editor'); editor.command('find'); }
  else if (e.key === 'F11') { e.preventDefault(); toggleMaximize(); }
});

window.addEventListener('beforeunload', e => {
  if (!editor.isClean()) { e.preventDefault(); e.returnValue = ''; }
});

// ------------------------------------------------------------------ start

const params = new URLSearchParams(location.search);
let urlFile = !!params.get('g');      // the URL's G-code is opened first (the server's program is not shown over it)
if (params.has('reset') || !restoreLayout()) defaultLayout();
if (dock.getPanel('scan')?.api.isVisible) scanPanel.shown();      // (in its own place: shown from the start)
refreshTitles();
files.load();
status('Ready. Open a G-code file or a scan from the Files panel or File → Open.');
machineChanged();
machine.start();      // follows the server's machine state; it never opens a port by itself

// Files can also be given in the URL: ?g=/path/job.nc&s=/path/scan.xyz&line=123&leveled
(async () => {
  if (params.get('s')) await openScan(params.get('s'));
  if (params.get('g')) {
    await openPath(params.get('g'));
    urlFile = false;
    adoptProgram();                 // (it could not be opened: the server's then)
  }
  if (params.has('leveled') && state.scan) viewer.setOption('leveled', true);
  if (params.get('line')) { showPanel('editor'); editor.goToLine(+params.get('line')); }
  if (PANELS[params.get('panel')]) showPanel(params.get('panel'));      // for screenshots: show a panel
  if (params.get('menu')) {         // for screenshots: open a menu
    [...document.querySelectorAll('.mtitle')].find(t => t.textContent === params.get('menu'))
      ?.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  }
  console.log('fluidsender ready: ' + $('smsg').textContent);
})();
