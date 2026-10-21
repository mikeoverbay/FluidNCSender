// Desktop-style menu bar. Click a title to open it; while open, hovering another title
// switches to it. Item state (enabled / checked / submenu) is read each time a menu opens.

const val = (v, ...args) => (typeof v === 'function' ? v(...args) : v);

export class MenuBar {
  /**
   * menus: [{ label, items: [item] }]
   * item: { label, shortcut, action, enabled, checked, items (submenu), separator: true }
   * enabled / checked / items / title may be functions.
   */
  constructor(root, menus) {
    this.root = root;
    this.menus = menus;
    this.open = null;
    root.classList.add('menubar');
    root.setAttribute('role', 'menubar');

    menus.forEach((menu, i) => {
      const title = document.createElement('div');
      title.className = 'mtitle';
      title.textContent = menu.label;
      title.onmousedown = e => {
        e.preventDefault();
        if (this.open?.index === i) this.close(); else this.show(i);
      };
      title.onmouseenter = () => { if (this.open && this.open.index !== i) this.show(i); };
      root.append(title);
      menu.titleEl = title;
    });

    document.addEventListener('mousedown', e => {
      if (this.open && !this.root.contains(e.target) && !e.target.closest('.mdrop')) this.close();
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && this.open) this.close(); });
    window.addEventListener('blur', () => this.close());
  }

  show(index) {
    this.close();
    const menu = this.menus[index];
    const r = menu.titleEl.getBoundingClientRect();
    const drop = this.dropdown(menu.items);
    drop.style.left = r.left + 'px';
    drop.style.top = r.bottom + 'px';
    document.body.append(drop);
    menu.titleEl.classList.add('open');
    this.open = { index, drops: [drop] };
  }

  dropdown(items, level = 0) {
    const drop = document.createElement('div');
    drop.className = 'mdrop';
    drop.setAttribute('role', 'menu');
    for (const item of items) {
      if (item.separator) {
        drop.append(Object.assign(document.createElement('div'), { className: 'msep' }));
        continue;
      }
      const row = document.createElement('div');
      const enabled = item.enabled === undefined ? true : val(item.enabled);
      const checked = item.checked === undefined ? null : val(item.checked);
      const sub = item.items ? val(item.items) : null;
      row.className = 'mitem' + (enabled ? '' : ' off');
      row.setAttribute('role', 'menuitem');
      row.innerHTML = `<span class="mcheck">${checked ? '✓' : ''}</span><span class="mlabel"></span>`
        + `<span class="mkey">${item.shortcut || (sub ? '▸' : '')}</span>`;
      row.querySelector('.mlabel').textContent = item.label;
      const title = val(item.title);
      if (title) row.title = title;

      row.onmouseenter = () => {
        // close deeper submenus, open this one's
        this.open.drops.splice(level + 1).forEach(d => d.remove());
        if (sub && sub.length && enabled) {
          const child = this.dropdown(sub, level + 1);
          const rr = row.getBoundingClientRect();
          child.style.left = rr.right - 2 + 'px';
          child.style.top = rr.top - 4 + 'px';
          document.body.append(child);
          this.open.drops.push(child);
        }
      };
      row.onmousedown = e => e.preventDefault();      // keep focus where it is (e.g. the editor)
      row.onclick = () => {
        if (!enabled || sub) return;
        this.close();
        item.action?.();
      };
      drop.append(row);
    }
    return drop;
  }

  close() {
    if (!this.open) return;
    this.open.drops.forEach(d => d.remove());
    this.menus[this.open.index].titleEl.classList.remove('open');
    this.open = null;
  }
}
