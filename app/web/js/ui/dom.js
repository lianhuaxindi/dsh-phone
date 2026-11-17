/**
 * Minimal DOM helpers (no framework, no build step).
 *
 * Every export touches `document` only when called, never at import time, so
 * this module is safe to import from Node tests and from workers.
 */

/**
 * Create an element.
 *
 * Supported attrs: class/className, id, type, value, placeholder, title,
 * hidden, disabled, checked, selected, role, aria-* / data-* strings, style
 * object, dataset object, on<event> listener functions. Children may be
 * strings, numbers, Nodes, arrays or null/false (skipped).
 *
 * @param {string} tag
 * @param {object|null} [attrs]
 * @param {...unknown} children
 * @returns {HTMLElement}
 */
export function h(tag, attrs = null, ...children) {
  const node = document.createElement(tag);
  if (attrs) applyAttrs(node, attrs);
  appendChildren(node, children);
  return node;
}

/** Document fragment from nodes/strings, with null/false skipped. */
export function frag(...children) {
  const fragment = document.createDocumentFragment();
  appendChildren(fragment, children);
  return fragment;
}

function appendChildren(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) continue; // already flattened
    parent.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function applyAttrs(node, attrs) {
  for (const [rawKey, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    const key = rawKey === 'class' ? 'className' : rawKey;
    if (key === 'style' && typeof value === 'object') {
      Object.assign(node.style, value);
      continue;
    }
    if (key === 'dataset' && typeof value === 'object') {
      Object.assign(node.dataset, value);
      continue;
    }
    if (key === 'value' || key === 'checked' || key === 'selected' || key === 'disabled' || key === 'hidden') {
      node[key] = value;
      continue;
    }
    if (key.length > 2 && key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
      continue;
    }
    node.setAttribute(key, value === true ? '' : String(value));
  }
}

/** Remove every child of a node. */
export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** Replace all children. */
export function replaceChildren(node, ...children) {
  clear(node);
  appendChildren(node, children);
  return node;
}

/** `document.querySelector` wrapper. */
export function qs(selector, root = document) {
  return root.querySelector(selector);
}

/** `document.querySelectorAll` as array. */
export function qsa(selector, root = document) {
  return [...root.querySelectorAll(selector)];
}

/** `document.getElementById` wrapper. */
export function byId(id) {
  return document.getElementById(id);
}

/** Add an event listener; returns an unsubscribe function. */
export function on(target, type, handler, options) {
  target.addEventListener(type, handler, options);
  return () => target.removeEventListener(type, handler, options);
}

/** Toggle a class. */
export function setClass(node, name, enabled = true) {
  node.classList.toggle(name, Boolean(enabled));
}

/** Set text content safely. */
export function setText(node, text) {
  node.textContent = text ?? '';
  return node;
}

/** Set hidden state. */
export function setHidden(node, hidden) {
  if (hidden) node.setAttribute('hidden', '');
  else node.removeAttribute('hidden');
}

/** Everything focusable inside a container (Tab order approximation). */
export function focusables(container) {
  return qsa(
    'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    container,
  ).filter((node) => node.getClientRects().length > 0 || node === document.activeElement);
}

/**
 * Trap Tab focus inside `container`.
 * @param {HTMLElement} container
 * @param {{initial?: HTMLElement|null, onEscape?: Function}} [options]
 * @returns {{release: Function, focusDefault: Function}}
 */
export function trapFocus(container, { initial = null, onEscape = null } = {}) {
  const previous = document.activeElement;
  const focusDefault = () => {
    const target = initial ?? focusables(container)[0] ?? container;
    try {
      target.focus({ preventScroll: true });
    } catch {
      /* older WebView */
    }
  };
  const onKeyDown = (event) => {
    if (event.key === 'Escape' && onEscape) {
      event.preventDefault();
      onEscape();
      return;
    }
    if (event.key !== 'Tab') return;
    const list = focusables(container);
    if (list.length === 0) return;
    const first = list[0];
    const last = list[list.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === container)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  container.addEventListener('keydown', onKeyDown);
  return {
    focusDefault,
    release() {
      container.removeEventListener('keydown', onKeyDown);
      if (previous && typeof previous.focus === 'function') {
        try {
          previous.focus({ preventScroll: true });
        } catch {
          /* ignore */
        }
      }
    },
  };
}

/** Human readable elapsed clock: 0:07 / 1:23. */
export function formatClock(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

/** Clamp a number into [min, max]. */
export function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}
