// File browser used by the Files panel and the Open / Save As dialogs.

import { listDir, store } from './api.js';

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
        <button data-act="up" title="Up one folder">⬑</button>
        <button data-act="refresh" title="Refresh">⟳</button>
        <span class="fdir"></span>
      </div>
      <div class="flist"></div>`;
    this.dirEl = this.root.querySelector('.fdir');
    this.list = this.root.querySelector('.flist');
    this.root.querySelector('[data-act=up]').onclick = () => this.parent && this.load(this.parent);
    this.root.querySelector('[data-act=refresh]').onclick = () => this.load(this.dir);
  }

  async load(dir = store.get(this.storeKey, '')) {
    let data;
    try {
      data = await listDir(dir);
    } catch (e) {
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
      const row = this.row(f.name, f.name.toLowerCase().endsWith('.xyz') ? 'scan' : '',
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
