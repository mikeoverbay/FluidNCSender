// G-code and scan file parsing. Everything is returned in inches.

const MM = 25.4;
const WORD = /([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))/g;

/**
 * Parses G-code into line segments. Returns
 *   { cut: Float32Array, rapid: Float32Array }  6 floats per segment (x1 y1 z1 x2 y2 z2)
 *   { cutLine: Int32Array, rapidLine: Int32Array }  the file line (1-based) of each segment
 * plus stats. Arcs (G2/G3, G17 plane) are broken into short lines. Moves in machine
 * coordinates (G53), homing, probing and relative moves are not drawn. modes: where the program itself
 * first switches to millimeters (mm: G21) or incremental moves (incr: G91), and its first work offset
 * (wcs: 'G55' ..., at line wcsAt); 0 / '' when it doesn't.
 */
export function parseGcode(text) {
  const cut = [], rapid = [], cutLine = [], rapidLine = [];
  let pos = [null, null, null];
  let motion = 0, absolute = true, scale = 1, feed = 0;
  let lines = 0, cutLen = 0, rapidLen = 0, cutTime = 0, skipped = 0;
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  const feeds = new Set();
  const modes = { mm: 0, incr: 0, wcs: '', wcsAt: 0 };

  const add = (a, b, isCut) => {
    const L = Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]);
    if (L === 0) return;
    (isCut ? cut : rapid).push(a[0], a[1], a[2], b[0], b[1], b[2]);
    (isCut ? cutLine : rapidLine).push(lines);
    if (isCut) { cutLen += L; if (feed > 0) cutTime += L / feed; } else rapidLen += L;
    for (let i = 0; i < 3; i++) {
      lo[i] = Math.min(lo[i], a[i], b[i]);
      hi[i] = Math.max(hi[i], a[i], b[i]);
    }
  };

  for (const raw of text.split('\n')) {
    lines++;
    const line = raw.replace(/\(.*?\)/g, '').split(';')[0].toUpperCase();
    if (!line.trim()) continue;
    const g = [], ax = {};
    for (const m of line.matchAll(WORD)) {
      const v = parseFloat(m[2]);
      if (m[1] === 'G') g.push(v); else ax[m[1]] = v;
    }

    let special = false;
    for (const c of g) {
      if (c === 20) scale = 1;
      else if (c === 21) { scale = 1 / MM; modes.mm ||= lines; }
      else if (c === 90) absolute = true;
      else if (c === 91) { absolute = false; modes.incr ||= lines; }
      else if (c >= 54 && c < 60 && !modes.wcs) { modes.wcs = `G${+c.toFixed(1)}`; modes.wcsAt = lines; }
      else if (c === 0 || c === 1 || c === 2 || c === 3) motion = c;
      else if ([10, 28, 30, 53, 92].includes(c) || (c >= 38 && c < 39)) special = true;
    }
    if ('F' in ax) { feed = ax.F * scale; feeds.add(+(ax.F).toFixed(4)); }
    if (!('X' in ax || 'Y' in ax || 'Z' in ax)) continue;
    if (special || !absolute) { skipped++; pos = [null, null, null]; continue; }

    const next = pos.slice();
    if ('X' in ax) next[0] = ax.X * scale;
    if ('Y' in ax) next[1] = ax.Y * scale;
    if ('Z' in ax) next[2] = ax.Z * scale;
    if (pos.includes(null) || next.includes(null)) { pos = next; continue; }

    if ((motion === 2 || motion === 3) && ('I' in ax || 'J' in ax || 'R' in ax)) {
      arc(pos, next, ax, scale, motion === 2, (a, b) => add(a, b, true));
    } else {
      add(pos, next, motion !== 0);
    }
    pos = next;
  }

  return {
    cut: new Float32Array(cut), rapid: new Float32Array(rapid),
    cutLine: new Int32Array(cutLine), rapidLine: new Int32Array(rapidLine),
    lines, cutMoves: cut.length / 6, rapidMoves: rapid.length / 6, skipped,
    cutLen, rapidLen, cutTime, lo, hi, feeds: [...feeds].sort((a, b) => a - b), modes,
  };
}

function arc(a, b, ax, scale, cw, emit) {
  let cx, cy;
  if ('R' in ax && !('I' in ax || 'J' in ax)) {
    const r = ax.R * scale, dx = b[0] - a[0], dy = b[1] - a[1], d = Math.hypot(dx, dy);
    if (d === 0 || Math.abs(r) < d / 2) { emit(a, b); return; }
    const h = Math.sqrt(r * r - d * d / 4) * ((cw ? 1 : -1) * (r > 0 ? 1 : -1));
    cx = (a[0] + b[0]) / 2 - h * dy / d;
    cy = (a[1] + b[1]) / 2 + h * dx / d;
  } else {
    cx = a[0] + (ax.I || 0) * scale;
    cy = a[1] + (ax.J || 0) * scale;
  }
  const r = Math.hypot(a[0] - cx, a[1] - cy);
  const a0 = Math.atan2(a[1] - cy, a[0] - cx);
  let sweep = Math.atan2(b[1] - cy, b[0] - cx) - a0;
  if (cw && sweep >= 0) sweep -= 2 * Math.PI;
  if (!cw && sweep <= 0) sweep += 2 * Math.PI;
  const steps = Math.max(2, Math.min(720, Math.ceil(Math.abs(sweep) * r / 0.01)));
  let prev = a;
  for (let s = 1; s <= steps; s++) {
    const t = s / steps, ang = a0 + sweep * t;
    const p = s === steps ? b : [cx + r * Math.cos(ang), cy + r * Math.sin(ang), a[2] + (b[2] - a[2]) * t];
    emit(prev, p);
    prev = p;
  }
}

/** Parses a UGS .xyz scan into a grid with the same bilinear lookup UGS uses. */
export function parseScan(text) {
  let scale = 1;
  const pts = [];
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith('#')) {
      const m = line.match(/units=(\w+)/i);
      if (m && m[1].toUpperCase().startsWith('M')) scale = 1 / MM;
      continue;
    }
    const v = line.split(/\s+/).map(Number);
    if (v.length >= 3 && v.every(Number.isFinite)) pts.push([v[0] * scale, v[1] * scale, v[2] * scale]);
  }
  const scan = scanFromPoints(pts);
  if (scan.partial || scan.xs.length < 2 || scan.ys.length < 2) throw new Error('scan is not a full grid');
  return scan;
}

/**
 * A scan grid from points [[x, y, z], ...] in inches: { xs, ys, at(i, j), height(x, y), zmin, zmax, count,
 * partial }. grid { xs, ys }: the grid a scan still being made will fill (its points are placed on the
 * nearest grid line, and at() is undefined where no height has been measured yet: partial is then true).
 * height() (UGS's bilinear lookup) needs a full grid of at least 2 x 2 points.
 */
export function scanFromPoints(pts, grid) {
  const key = (x, y) => x.toFixed(5) + ',' + y.toFixed(5);
  const xs = grid ? grid.xs.slice() : [...new Set(pts.map(p => +p[0].toFixed(5)))].sort((a, b) => a - b);
  const ys = grid ? grid.ys.slice() : [...new Set(pts.map(p => +p[1].toFixed(5)))].sort((a, b) => a - b);
  const nearest = (arr, v) => {
    let best = 0;
    for (let i = 1; i < arr.length; i++) if (Math.abs(arr[i] - v) < Math.abs(arr[best] - v)) best = i;
    return best;
  };
  const z = new Map();
  for (const p of pts) {
    if (!Number.isFinite(p[2])) continue;
    const i = grid ? nearest(xs, p[0]) : xs.indexOf(+p[0].toFixed(5));
    const j = grid ? nearest(ys, p[1]) : ys.indexOf(+p[1].toFixed(5));
    z.set(key(xs[i], ys[j]), p[2]);
  }
  const at = (i, j) => z.get(key(xs[i], ys[j]));
  const zs = [...z.values()];
  const partial = z.size !== xs.length * ys.length;

  const idx = (arr, v) => {
    let i = 0;
    while (i < arr.length - 2 && v >= arr[i + 1]) i++;
    return i;
  };
  const height = (x, y) => {
    x = Math.min(Math.max(x, xs[0]), xs[xs.length - 1]);
    y = Math.min(Math.max(y, ys[0]), ys[ys.length - 1]);
    const i = idx(xs, x), j = idx(ys, y);
    const x1 = xs[i], x2 = xs[i + 1], y1 = ys[j], y2 = ys[j + 1];
    const r1 = ((x2 - x) * at(i, j) + (x - x1) * at(i + 1, j)) / (x2 - x1);
    const r2 = ((x2 - x) * at(i, j + 1) + (x - x1) * at(i + 1, j + 1)) / (x2 - x1);
    return ((y2 - y) * r1 + (y - y1) * r2) / (y2 - y1);
  };

  return {
    xs, ys, at, height, partial, count: z.size,
    zmin: zs.length ? Math.min(...zs) : 0, zmax: zs.length ? Math.max(...zs) : 0,
  };
}

/** The grid lines of a scan from its settings { minX, maxX, xPoints, minY, maxY, yPoints } (as the scanner
 *  makes them: evenly from min to max), or null if they are not usable. */
export function scanGrid(s) {
  const line = (lo, hi, n) => {
    n = Math.round(n);
    if (!Number.isFinite(lo) || !Number.isFinite(hi) || !(n >= 1) || n > 1000) return null;
    return n === 1 ? [lo] : Array.from({ length: n }, (_, i) => lo + ((hi - lo) * i) / (n - 1));
  };
  const xs = s && line(+s.minX, +s.maxX, +s.xPoints), ys = s && line(+s.minY, +s.maxY, +s.yPoints);
  return xs && ys ? { xs, ys } : null;
}
