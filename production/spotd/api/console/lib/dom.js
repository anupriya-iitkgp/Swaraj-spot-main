/**
 * A hyperscript + patch layer, in about 120 lines.
 *
 * Why not innerHTML: this console re-renders on a poll. Replacing a subtree's
 * HTML every second destroys focus, selection and scroll position, which makes
 * a form impossible to fill in and a long table impossible to read. So we
 * reconcile: same tag in the same slot is updated in place, and only genuinely
 * new nodes are created.
 *
 * Why not a framework: there is no build step here (see api/static.py). A
 * runtime-sized diff is the price of that, and the price is this file.
 *
 * Keys matter for lists whose rows move — pass `key` and rows are matched by
 * identity rather than by position, so a lease that shifts row does not inherit
 * the previous occupant's countdown.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';
const SVG_TAGS = new Set([
  'svg', 'g', 'path', 'rect', 'circle', 'line', 'polyline', 'polygon',
  'text', 'tspan', 'defs', 'linearGradient', 'stop', 'clipPath', 'title',
]);

/** Create a virtual node. `h('div.card', {...}, child, child)` */
export function h(selector, props, ...children) {
  // The second argument is props only if it is a plain object that is not
  // itself a vnode. Conditional children (`cond && h(...)`) land here as
  // false/null/undefined all the time, and `null` is typeof "object" — so the
  // null check comes first or every optional child crashes the render.
  const isProps = props != null
    && typeof props === 'object'
    && !Array.isArray(props)
    && !props.tag
    && props.text === undefined;
  if (!isProps) {
    if (props !== undefined) children.unshift(props);
    props = {};
  }
  const { tag, id, classes } = parseSelector(selector);
  const attrs = { ...(props || {}) };
  if (id && !attrs.id) attrs.id = id;
  if (classes.length) {
    attrs.class = [classes.join(' '), attrs.class].filter(Boolean).join(' ');
  }
  return { tag, attrs, children: flatten(children), key: attrs.key };
}

export const text = (value) => ({ text: value == null ? '' : String(value) });

function flatten(nodes) {
  const out = [];
  for (const node of nodes) {
    if (node == null || node === false || node === true) continue;
    if (Array.isArray(node)) out.push(...flatten(node));
    else if (typeof node === 'object' && (node.tag || node.text !== undefined)) out.push(node);
    else out.push(text(node));
  }
  return out;
}

const selectorCache = new Map();
function parseSelector(selector) {
  let parsed = selectorCache.get(selector);
  if (parsed) return parsed;
  const classes = [];
  let tag = 'div';
  let id = '';
  const match = selector.match(/^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)$/);
  if (match) {
    if (match[1]) tag = match[1];
    for (const token of match[2].match(/[.#][\w-]+/g) || []) {
      if (token[0] === '.') classes.push(token.slice(1));
      else id = token.slice(1);
    }
  } else {
    tag = selector;
  }
  parsed = { tag, id, classes };
  selectorCache.set(selector, parsed);
  return parsed;
}

/** Reconcile `vnodes` into `parent`. */
export function render(parent, vnodes) {
  patchChildren(parent, Array.isArray(vnodes) ? flatten(vnodes) : flatten([vnodes]));
}

function patchChildren(parent, vnodes) {
  const existing = Array.from(parent.childNodes);
  const keyed = new Map();
  for (const node of existing) {
    const key = node.__key;
    if (key != null) keyed.set(key, node);
  }

  const next = [];
  for (let i = 0; i < vnodes.length; i++) {
    const vnode = vnodes[i];
    let target = null;
    if (vnode.key != null && keyed.has(vnode.key)) {
      target = keyed.get(vnode.key);
      keyed.delete(vnode.key);
    } else {
      const candidate = existing[i];
      // Only reuse an unkeyed node; reusing a keyed one would steal its identity.
      if (candidate && candidate.__key == null && matches(candidate, vnode)) target = candidate;
    }
    next.push(patch(parent, target, vnode));
  }

  // Place in order, then drop whatever is left over.
  for (let i = 0; i < next.length; i++) {
    const node = next[i];
    const current = parent.childNodes[i];
    if (current !== node) parent.insertBefore(node, current || null);
  }
  while (parent.childNodes.length > next.length) {
    parent.removeChild(parent.lastChild);
  }
}

function matches(node, vnode) {
  if (vnode.text !== undefined) return node.nodeType === 3;
  return node.nodeType === 1 && node.nodeName.toLowerCase() === vnode.tag.toLowerCase();
}

function patch(parent, node, vnode) {
  if (vnode.text !== undefined) {
    if (node && node.nodeType === 3) {
      if (node.nodeValue !== vnode.text) node.nodeValue = vnode.text;
      return node;
    }
    return document.createTextNode(vnode.text);
  }

  if (!node || !matches(node, vnode)) {
    node = SVG_TAGS.has(vnode.tag)
      ? document.createElementNS(SVG_NS, vnode.tag)
      : document.createElement(vnode.tag);
    node.__props = {};
  }

  applyProps(node, vnode.attrs || {});
  if (vnode.key != null) node.__key = vnode.key;
  patchChildren(node, vnode.children);
  return node;
}

function applyProps(node, attrs) {
  const previous = node.__props || {};

  for (const name of Object.keys(previous)) {
    if (name in attrs || name === 'key') continue;
    if (name.startsWith('on')) node[name.toLowerCase()] = null;
    else if (name === 'value' || name === 'checked') node[name] = '';
    else node.removeAttribute(name);
  }

  for (const [name, value] of Object.entries(attrs)) {
    if (name === 'key') continue;
    if (name.startsWith('on') && typeof value === 'function') {
      node[name.toLowerCase()] = value;
    } else if (name === 'value') {
      // Never clobber what the operator is typing.
      if (document.activeElement !== node && node.value !== String(value ?? '')) {
        node.value = value ?? '';
      }
    } else if (name === 'checked' || name === 'disabled' || name === 'selected') {
      node[name] = Boolean(value);
      if (!value) node.removeAttribute(name); else node.setAttribute(name, '');
    } else if (name === 'style' && typeof value === 'object') {
      for (const [prop, val] of Object.entries(value)) node.style.setProperty(prop, val);
    } else if (value == null || value === false) {
      node.removeAttribute(name);
    } else if (previous[name] !== value) {
      node.setAttribute(name, value === true ? '' : String(value));
    }
  }

  node.__props = attrs;
}

/** Escape-hatch for the rare node whose content is a raw SVG string. */
export function raw(html) {
  const holder = document.createElement('div');
  holder.innerHTML = html;
  return holder;
}
