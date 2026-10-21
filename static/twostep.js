// Two-step ("second chance") buttons, for the ones a stray click must not set off: Zero X / Y / Z, Zero All,
// Undo Zero, Go to XY 0, Go to Z 0.
//
// The first click arms the button: it turns amber and says what a second click does ("Zero X?  click
// again"). A second click 0.5 s to 3 s after the click before it does it. Anything else bails out, and the
// button is as before: 3 s without that click, any key but Tab or Shift (Esc too), a press anywhere else on
// the page, the window losing focus, the page hidden, or the state changing (the panels call context() /
// setEnabled()). The clicks of a double-click (or a triple-click) never count: the browser numbers them
// (detail 2, 3 ...), and a click sooner than 0.5 s after the one before it is ignored too. An ignored click
// starts the wait again, so the second click has to come 0.5 s to 3 s after the last click.
//
// No key ever presses them, nor the other buttons that move the machine, change an offset or switch an
// output (see mouseOnly): Tab skips them, a click doesn't give them the focus, and a click that comes from
// a key or a script (detail 0) is ignored.
//
// An armed button never moves and never grows over another button: its armed text either fits its own
// place (the panels lay that out) or floats over plain text only (see .ts-float in style.css), so the
// second click lands where the first did.

const MIN_MS = 500;       // a click this soon after the one before it is ignored (a double-click)
const MAX_MS = 3000;      // the second click must come within this long of the one before it

let armed = null;         // the one armed button at a time

function bailArmed() { armed?.bail(); }

// Installed once: the ways out that don't depend on which button is armed
document.addEventListener('pointerdown', e => {
  if (armed && !armed.btn.contains(e.target)) armed.bail();
}, true);
// Any key (typing a value after a mis-click, Enter, Space, a jog key...); Tab and Shift only move the focus
document.addEventListener('keydown', e => { if (e.key !== 'Tab' && e.key !== 'Shift') bailArmed(); }, true);
window.addEventListener('blur', bailArmed);
document.addEventListener('visibilitychange', () => { if (document.hidden) bailArmed(); });

/**
 * btn becomes a button no key can press, for the ones that move the machine, change an offset or switch an
 * output: Tab skips it, a click doesn't give it the focus (so a later Enter or Space can't press it), and a
 * click that comes from a key or a script (detail 0) is ignored. focusTo: where a click puts the focus (the
 * Jog panel, so its keys work after a click there); else the focus stays where it was, but a text box being
 * typed in is left, as a click on a button always did.
 */
export function mouseOnly(btn, focusTo = null) {
  btn.tabIndex = -1;
  btn.addEventListener('mousedown', e => {
    e.preventDefault();
    const f = document.activeElement;
    if (focusTo) focusTo.focus({ preventScroll: true });
    else if (f instanceof HTMLElement && f.matches('input, textarea, select')) f.blur();
  });
  // (the capture phase on the button runs before its own click handlers)
  btn.addEventListener('click', e => {
    if (e.detail === 0) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
}

export class TwoStep {
  /**
   * btn: the button. label: its HTML when not armed. ask: what it says when armed, [first line, ..., last
   * line] (e.g. ['Zero X?', 'click again']), or a function that returns { lines, title, ... } when it is armed
   * (title: the whole sentence, for the tooltip). run(): what the second click does. focusTo: see mouseOnly.
   * onArm(on, ask): told when it is armed (with what ask returned) and when that ends.
   */
  constructor(btn, { label, ask, run, focusTo = null, onArm = null }) {
    this.btn = btn;
    this.ask = ask;
    this.run = run;
    this.onArm = onArm;
    this.key = undefined;
    this.title = '';
    btn.classList.add('ts');
    // The label stays in the button while it is armed (hidden, or keeping the button's width when the
    // armed text floats: see style.css)
    btn.innerHTML = `<span class="ts-label">${label}</span>`;
    this.askEl = document.createElement('span');
    this.askEl.className = 'ts-ask';
    mouseOnly(btn, focusTo);
    btn.addEventListener('click', e => this.click(e));
  }

  get isArmed() { return armed === this; }

  click(e) {
    if (this.btn.disabled || !e.detail) return;     // (a key or a script: never; mouseOnly stops those first)
    const now = e.timeStamp || performance.now();
    if (armed !== this) {
      if (e.detail === 1) this.arm(now);           // (the rest of a double-click that began elsewhere: nothing)
      return;
    }
    if (e.detail > 1 || now - this.last < MIN_MS) {  // part of a double-click, or too soon: ignored, and the
      this.wait(now);                                // wait for the second click starts again
      return;
    }
    if (now - this.last > MAX_MS || !this.btn.isConnected) { this.bail(); return; }
    this.bail();
    this.run();
  }

  arm(now) {
    armed?.bail();
    armed = this;
    const ask = typeof this.ask === 'function' ? this.ask() : { lines: this.ask };
    this.askEl.replaceChildren(...ask.lines.map((text, i) => {
      const s = document.createElement('span');
      s.className = i === 0 ? 'ts-q' : 'ts-a';
      s.textContent = text;
      return s;
    }));
    this.btn.classList.add('armed');
    this.btn.append(this.askEl);
    this.btn.title = `${ask.title || ask.lines[0]} Click again to do it (0.5 to 3 s later). Any key, or a click anywhere `
      + 'else, cancels.';
    this.wait(now);
    this.onArm?.(true, ask);
  }

  /** The second click may come from 0.5 s to 3 s after now. */
  wait(now) {
    this.last = now;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.bail(), MAX_MS);
  }

  bail() {
    if (armed !== this) return;
    armed = null;
    clearTimeout(this.timer);
    this.btn.classList.remove('armed');
    this.askEl.remove();
    this.btn.title = this.title;
    this.onArm?.(false);
  }

  /** Enables the button (title: what it does, or why not). Disabling it bails. */
  setEnabled(ok, title) {
    this.btn.disabled = !ok;
    this.title = title;
    if (!ok) this.bail();
    if (!this.isArmed) this.btn.title = title;
  }

  /** key: the state the arming was made in (machine state, job, ...). A different key bails. */
  context(key) {
    if (this.key !== key && this.isArmed) this.bail();
    this.key = key;
  }
}

/** Bails whichever button is armed. */
export function bailAll() { bailArmed(); }
