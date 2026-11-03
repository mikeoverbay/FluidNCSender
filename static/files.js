// File browser used by the Files panel and the Open / Save As dialogs.

import { listDir, listPlaces, store } from './api.js';

const fmtSize = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
const fmtDate = t => new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

export class FileBrowser {
  /**
   * onPick(path, name) runs when a file is clicked (or double-clicked if pickOnDouble).
   * filter(name) can hide files.
   */
  constructor({ onPick, onSelect, filter, pickOnDouble = false, storeKey = 'files.dir' } = {}) {
    this.onPick = onPick;
    this.onSelect = onSelect;
    this.filter = filter || (() => true);
    this.pickOnDouble = pickOnDouble;
    this.storeKey = storeKey;
    this.marked = new Set();
    this.dir = '';

    this.root = document.createElement('div');
    this.root.className = 'files';
    this.root.innerHTML = `
      <div class="fbar">
        <button data-act="places" title="Pick a folder: drives, home, USB drives and network shares">📁</button>
        <button data-act="up" title="Up one folder (at the top of a drive: the places to pick from)">⬑</button>
        <button data-act="refresh" title="Refresh">⟳</button>
        <span class="fdir" title="Click to type a folder"></span>
        <input class="fdirin" type="text" spellcheck="false" autocomplete="off" hidden
               title="Type or paste a folder, then Enter (Esc: back)">
      </div>
      <div class="flist"></div>`;
    this.dirEl = this.root.querySelector('.fdir');
    this.dirIn = this.root.querySelector('.fdirin');
    this.list = this.root.querySelector('.flist');
    this.root.querySelector('[data-act=places]').onclick = () => this.showPlaces();
    this.root.querySelector('[data-act=up]').onclick = () => (this.parent ? this.load(this.parent) : this.showPlaces());
    this.root.querySelector('[data-act=refresh]').onclick = () => this.load(this.dir);
    // The folder path: click it to type another (D:\CNC, \\server\share, /media/me/USB ...)
    this.dirEl.onclick = () => {
      this.dirIn.value = this.dir;
      this.dirEl.hidden = true;
      this.dirIn.hidden = false;
      this.dirIn.focus();
      this.dirIn.select();
    };
    const typed = () => { this.dirIn.hidden = true; this.dirEl.hidden = false; };
    this.dirIn.onkeydown = e => {
      if (e.key === 'Enter') { e.preventDefault(); typed(); if (this.dirIn.value.trim()) this.load(this.dirIn.value.trim(), true); }
      else if (e.key === 'Escape') { e.preventDefault(); typed(); }
    };
    this.dirIn.onblur = typed;
  }

  /** The places to pick a working folder from: every drive on Windows; home, /, USB drives and shares on Linux. */
  async showPlaces() {
    let places;
    try {
      places = await listPlaces();
    } catch (e) {
      this.list.textContent = `Could not list the drives: ${e.message}`;
      return;
    }
    this.dirEl.textContent = 'Pick a folder';
    this.dirEl.title = 'Pick a drive or place, or click here to type a folder';
    this.list.innerHTML = '';
    for (const p of places) {
      const row = this.row('▸ ' + p.name, 'dir', p.path);
      row.title = p.path;
      row.onclick = () => this.load(p.path);
    }
    if (!places.length) this.row('(no drives found)', 'empty');
  }

  /** typed: the folder was typed in: if it can't be opened, say so (rather than going back to the default). */
  async load(dir = store.get(this.storeKey, ''), typed = false) {
    let data;
    try {
      data = await listDir(dir);
    } catch (e) {
      if (typed) { this.list.textContent = `Can't open that folder: ${e.message}`; return; }
      if (dir) return this.load('');          // folder gone: start over at the default
      this.list.textContent = e.message;
      return;
    }
    this.dir = data.dir;
    this.parent = data.parent !== data.dir ? data.parent : null;
    store.set(this.storeKey, this.dir);
    this.dirEl.textContent = '‎' + this.dir;   // keeps the leading "/" in place (the box is right-to-left so the end shows)
    this.dirEl.title = this.dir;
    this.list.innerHTML = '';

    for (const d of data.dirs) {
      const row = this.row('▸ ' + d, 'dir');
      row.onclick = () => this.load(this.dir + '/' + d);
    }
    for (const f of data.files.filter(f => this.filter(f.name))) {
      const path = this.dir + '/' + f.name;
      const row = this.row(f.name, f.name.toLowerCase().endsWith('.xyz') ? 'xyz' : '',
        `${fmtSize(f.size)} · ${fmtDate(f.mtime)}`);
      row.dataset.path = path;
      if (this.marked.has(path)) row.classList.add('on');
      const pick = () => this.onPick?.(path, f.name);
      row.onclick = () => {
        this.list.querySelector('.sel')?.classList.remove('sel');
        row.classList.add('sel');
        this.onSelect?.(path, f.name);
        if (!this.pickOnDouble) pick();
      };
      if (this.pickOnDouble) row.ondblclick = pick;
    }
    if (!this.list.children.length) this.row('(no G-code or scan files here)', 'empty');
  }

  row(text, cls, meta) {
    const row = document.createElement('div');
    row.className = 'frow ' + (cls || '');
    row.title = text;
    const name = document.createElement('span');
    name.textContent = text;
    row.append(name);
    if (meta) {
      const m = document.createElement('small');
      m.textContent = meta;
      row.append(m);
    }
    this.list.append(row);
    return row;
  }

  /** Marks the files that are loaded. */
  mark(paths) {
    this.marked = new Set(paths.filter(Boolean));
    for (const row of this.list.querySelectorAll('[data-path]')) {
      row.classList.toggle('on', this.marked.has(row.dataset.path));
    }
  }
}
