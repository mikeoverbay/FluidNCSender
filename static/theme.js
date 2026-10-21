// The color theme: View → Theme (Dark, Medium, Light). The palettes are in style.css; index.html applies the saved
// theme before the first paint. A change applies at once (CSS, dockview, the editor) and onThemeChange() tells the
// parts that draw their own colors (the 3D view); another window of this page follows it too.

import { store } from './api.js';

const KEY = 'theme';
export const THEMES = [['dark', 'Dark'], ['medium', 'Medium'], ['light', 'Light']];
const DEFAULT = 'medium';
const listeners = [];

const valid = t => THEMES.some(([id]) => id === t);

/** The theme shown now. */
export function currentTheme() {
  const t = document.documentElement.dataset.theme;
  return valid(t) ? t : DEFAULT;
}

function apply(t) {
  if (!valid(t)) t = DEFAULT;
  if (document.documentElement.dataset.theme === t) return;
  document.documentElement.dataset.theme = t;
  for (const fn of listeners) fn(t);
}

/** Shows theme t and saves it for the next start. */
export function setTheme(t) {
  apply(t);
  store.set(KEY, currentTheme());
}

/** fn(theme) runs after each change. */
export function onThemeChange(fn) { listeners.push(fn); }

// Another window of this page chose a theme: follow it
window.addEventListener('storage', e => {
  if (e.key !== KEY) return;
  let t = DEFAULT;
  try { t = JSON.parse(e.newValue); } catch { /* not ours */ }
  apply(t);
});
