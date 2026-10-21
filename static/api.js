// Calls to the local server. Every call carries the X-FluidSender header the server requires.

export const HEADERS = { 'X-FluidSender': '1' };

/** Throws the server's error message (with .status set) for a failed response. */
async function check(res) {
  if (res.ok) return res;
  let msg = `${res.status} ${res.statusText}`;
  try { msg = (await res.json()).error || msg; } catch { /* not JSON */ }
  throw Object.assign(new Error(msg), { status: res.status });
}

/** A JSON call: GET without a body, POST with one. */
export async function apiJson(path, body) {
  const opts = body === undefined ? { headers: HEADERS } : {
    method: 'POST',
    headers: { ...HEADERS, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  const res = await check(await fetch(path, opts));
  return res.json();
}

export async function listDir(dir) {
  const res = await check(await fetch('/api/list?dir=' + encodeURIComponent(dir || ''), { headers: HEADERS }));
  return res.json();
}

export async function readFile(path) {
  const res = await check(await fetch('/api/file?path=' + encodeURIComponent(path), { headers: HEADERS }));
  return res.text();
}

export async function saveFile(path, text) {
  const res = await check(await fetch('/api/save', {
    method: 'POST',
    headers: { ...HEADERS, 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, text }),
  }));
  return res.json();
}

export const baseName = p => p.split('/').pop();
export const dirName = p => p.slice(0, p.lastIndexOf('/')) || '/';
export const isScan = p => p.toLowerCase().endsWith('.xyz');

/** localStorage that never throws (private windows, blocked storage). */
export const store = {
  get(k, fallback = null) {
    try { const v = localStorage.getItem(k); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
  },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* no storage */ } },
  remove(k) { try { localStorage.removeItem(k); } catch { /* no storage */ } },
};
