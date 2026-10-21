// G-code editor panel (CodeMirror 5, loaded as a plain script in index.html).

const CM = globalThis.CodeMirror;
const NUM = '\\s*[-+]?(?:\\d+\\.?\\d*|\\.\\d+)';

CM.defineSimpleMode('gcode', {
  start: [
    { regex: /\(.*?\)/, token: 'comment' },
    { regex: /;.*/, token: 'comment' },
    { regex: /\s*%.*/, sol: true, token: 'meta' },
    { regex: /\$\S*/, token: 'atom' },
    { regex: new RegExp('[Nn]' + NUM), token: 'linenum' },
    { regex: new RegExp('[Gg]' + NUM), token: 'keyword' },
    { regex: new RegExp('[Mm]' + NUM), token: 'builtin' },
    { regex: new RegExp('[XxYyZzAaBbCc]' + NUM), token: 'variable' },
    { regex: new RegExp('[IiJjKkRr]' + NUM), token: 'variable-2' },
    { regex: new RegExp('[FfSsTtPpLlHhDdQqEe]' + NUM), token: 'number' },
    { regex: /\s+/, token: null },
    { regex: /./, token: null },
  ],
});

export class Editor {
  constructor() {
    this.root = document.createElement('div');
    this.root.className = 'editor';
    // A banner over the text (the leveled program: see app.js), hidden when empty
    this.banner = document.createElement('div');
    this.banner.className = 'ebanner';
    this.banner.hidden = true;
    this.root.append(this.banner);
    this.forcedDirty = false;   // setText(text, { dirty: true }): unsaved changes this text doesn't show (see setText)
    this.cm = CM(this.root, {
      mode: 'gcode',
      theme: 'fluid',
      lineNumbers: true,
      styleActiveLine: true,
      indentUnit: 2,
      viewportMargin: 20,
      value: '',
    });
    this.silent = false;
    this.lastLine = 0;
    this.jobLine = -1;          // the job's line (0-based) marked, -1: none
    this.cm.on('cursorActivity', () => {
      const c = this.cm.getCursor();
      this.onCursor?.(c.line + 1, c.ch + 1);
      if (c.line + 1 !== this.lastLine) {
        this.lastLine = c.line + 1;
        this.onLine?.(this.lastLine);
      }
    });
    this.cm.on('changes', () => { if (!this.silent) this.onEdit?.(); });
    let queued = false;
    new ResizeObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; this.cm.refresh(); });
    }).observe(this.root);
  }

  /** Shows text (not an edit: no undo history, clean). dirty: it counts as not saved all the same (the
   *  original of a leveled program, which had unsaved changes when it was leveled, and the leveled text shown
   *  meanwhile: see app.js), until markClean(). */
  setText(text, { dirty = false } = {}) {
    // Keep the file's own line endings (CarveMaker writes Windows CRLF) so saving doesn't change them
    this.eol = text.includes('\r\n') ? '\r\n' : '\n';
    this.silent = true;
    this.cm.setValue(text);
    this.cm.clearHistory();
    this.cm.markClean();
    this.silent = false;
    this.forcedDirty = dirty;
    this.lastLine = 0;
    this.jobLine = -1;          // (setValue dropped the mark with the old lines)
  }

  getText() { return this.cm.getValue(this.eol || '\n'); }
  isClean() { return !this.forcedDirty && this.cm.isClean(); }
  markClean() { this.forcedDirty = false; this.cm.markClean(); }
  lineCount() { return this.cm.lineCount(); }

  /** The banner over the text: nodes (null or none: no banner). */
  setBanner(...nodes) {
    const list = nodes.filter(Boolean);
    this.banner.replaceChildren(...list);
    if (this.banner.hidden !== !list.length) {
      this.banner.hidden = !list.length;
      requestAnimationFrame(() => this.cm.refresh());      // (the text's room changed)
    }
  }

  goToLine(n) {
    const line = Math.max(0, Math.min(n - 1, this.cm.lineCount() - 1));
    this.cm.setCursor(line, 0);
    this.cm.scrollIntoView({ line, ch: 0 }, 120);
  }

  command(name) {
    this.cm.focus();
    this.cm.execCommand(name);
  }

  refresh() { this.cm.refresh(); }

  // ---------------------------------------------------------------- while a job runs

  /** No editing while a job runs (FluidNC is running these lines) or while a leveled program is shown. */
  setReadOnly(on) {
    if (!!this.cm.getOption('readOnly') === on) return;
    this.cm.setOption('readOnly', on);
    this.root.classList.toggle('readonly', on);
  }

  /** Marks the job's line (1-based; 0 clears it) and keeps it in view, unless the user scrolled the editor
   *  in the last few seconds (to look at something else). */
  setJobLine(n) {
    const line = n > 0 ? Math.min(n, this.cm.lineCount()) - 1 : -1;
    if (line === this.jobLine) return;
    if (this.jobLine >= 0 && this.jobLine < this.cm.lineCount()) {
      this.cm.removeLineClass(this.jobLine, 'background', 'cm-jobline');
      this.cm.removeLineClass(this.jobLine, 'gutter', 'cm-jobgutter');
    }
    this.jobLine = line;
    if (line < 0) return;
    this.cm.addLineClass(line, 'background', 'cm-jobline');
    this.cm.addLineClass(line, 'gutter', 'cm-jobgutter');
    if (!this.watchingScroll) {
      this.watchingScroll = true;
      // The user's own scrolling (wheel, scrollbar, keys) holds the view still for a while
      const touched = () => { this.userScrollAt = performance.now(); };
      this.cm.getWrapperElement().addEventListener('wheel', touched, { passive: true });
      this.cm.getScrollerElement().addEventListener('pointerdown', touched);
      this.cm.on('keydown', (cm, e) => { if (/^(Arrow|Page|Home|End)/.test(e.key)) touched(); });
    }
    if (performance.now() - (this.userScrollAt || -1e9) < FOLLOW_PAUSE_MS) return;
    // Scrolled only when it gets near an edge of the view, then to a third from the top (no jitter)
    const y = this.cm.heightAtLine(line, 'local');
    const view = this.cm.getScrollInfo();
    if (y < view.top + 20 || y > view.top + view.clientHeight - 40) {
      this.cm.scrollTo(null, Math.max(0, y - view.clientHeight / 3));
    }
  }
}

const FOLLOW_PAUSE_MS = 5000;   // after the user scrolls the editor, the job's line is not scrolled to for this long
