/**
 * Modal bottom sheet primitive shared by the approval and question sheets.
 *
 * One sheet stack keeps the Android back key working: app.js registers
 * `transport.setBackHandler(() => closeTopSheet())`, so the native shell gets
 * `true` while a sheet is open and `false` otherwise.
 */

import { h, trapFocus, byId } from './dom.js';

/** @type {object[]} */
const stack = [];

let sequence = 0;

/** True when any sheet is open. */
export function hasOpenSheet() {
  return stack.length > 0;
}

/** Close the top-most sheet; returns true when a sheet consumed the call. */
export function closeTopSheet(reason = 'back') {
  const top = stack[stack.length - 1];
  if (!top) return false;
  top.dismiss(reason);
  return true;
}

/** Close every open sheet immediately (navigation / disconnect). */
export function closeAllSheets() {
  for (const sheet of [...stack].reverse()) sheet.destroy('reset');
}

/**
 * Open a modal bottom sheet.
 *
 * @param {object} options
 * @param {string} options.title already-localized title
 * @param {string} [options.description] already-localized description
 * @param {HTMLElement|HTMLElement[]} [options.content]
 * @param {Array<{label: string, tone?: string, onClick?: Function, autofocus?: boolean, testId?: string}>} [options.actions]
 * @param {boolean} [options.dismissible] backdrop/escape may close the sheet
 * @param {string} [options.tone] 'default' | 'danger'
 * @param {(reason: string) => void} [options.onDismiss]
 * @param {() => void} [options.onClose]
 * @returns {{root: HTMLElement, sheet: HTMLElement, close: Function, destroy: Function, setTitle: Function}}
 */
export function openSheet({
  title,
  description = '',
  content = [],
  actions = [],
  dismissible = false,
  tone = 'default',
  onDismiss = null,
  onClose = null,
} = {}) {
  if (typeof document === 'undefined') throw new Error('openSheet requires a DOM');
  const id = `sheet-${++sequence}`;
  const titleId = `${id}-title`;
  const nodes = Array.isArray(content) ? content : [content];

  const sheet = h('div', {
    class: `sheet sheet--${tone}`,
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': titleId,
    'data-sheet': id,
  });
  const header = h('div', { class: 'sheet__header' });
  const titleNode = h('h2', { class: 'sheet__title', id: titleId }, title);
  header.appendChild(titleNode);
  if (dismissible) {
    header.appendChild(
      h(
        'button',
        {
          type: 'button',
          class: 'sheet__close icon-button',
          'aria-label': 'close',
          'data-action': 'close',
          onclick: () => dismiss('close'),
        },
        '×',
      ),
    );
  }
  sheet.appendChild(header);

  if (description) sheet.appendChild(h('p', { class: 'sheet__description' }, description));
  const body = h('div', { class: 'sheet__body' });
  for (const node of nodes) if (node) body.appendChild(node);
  sheet.appendChild(body);

  const footer = h('div', { class: 'sheet__actions' });
  /** @type {HTMLElement|null} */
  let defaultFocus = null;
  for (const action of actions) {
    const button = h(
      'button',
      {
        type: 'button',
        class: `btn btn--${action.tone ?? 'secondary'}`,
        'data-action': action.testId ?? action.label,
        onclick: () => {
          action.onClick?.();
        },
      },
      action.label,
    );
    if (action.autofocus) defaultFocus = button;
    footer.appendChild(button);
  }
  if (actions.length > 0) sheet.appendChild(footer);

  const backdrop = h('div', { class: 'sheet-backdrop', 'data-sheet-backdrop': id });
  const root = h('div', { class: 'sheet-host' }, backdrop, sheet);
  backdrop.addEventListener('click', () => {
    if (dismissible) dismiss('backdrop');
  });

  document.body.appendChild(root);
  const trap = trapFocus(sheet, { initial: defaultFocus, onEscape: () => dismiss('escape') });
  // eslint-disable-next-line no-unused-expressions
  requestAnimationFrame(() => root.classList.add('sheet-host--open'));
  trap.focusDefault();

  let closed = false;

  /**
   * Close the sheet. `reason` is passed to onDismiss: 'action' when a button
   * solved the sheet, anything else means the user dismissed/cancelled it.
   */
  function dismiss(reason = 'dismiss') {
    if (closed) return;
    closed = true;
    const index = stack.indexOf(entry);
    if (index >= 0) stack.splice(index, 1);
    trap.release();
    root.classList.remove('sheet-host--open');
    const remove = () => root.remove();
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => setTimeout(remove, 220));
    else remove();
    try {
      onClose?.();
    } finally {
      if (reason !== 'action') onDismiss?.(reason);
    }
  }

  const entry = {
    id,
    root,
    sheet,
    dismiss,
    destroy(reason) {
      dismiss(reason);
    },
    setTitle(nextTitle) {
      titleNode.textContent = nextTitle;
    },
    close: dismiss,
  };
  stack.push(entry);
  return entry;
}

/**
 * Rebuild the sheet stack after a language/theme change (app.js calls it when
 * the active language changes while a sheet is open).
 */
export function topSheet() {
  return stack[stack.length - 1] ?? null;
}

/** Helper: build a label/value row used by the approval sheet. */
export function infoRow(label, value, { mono = false, tone = '' } = {}) {
  return h(
    'div',
    { class: `info-row${tone ? ` info-row--${tone}` : ''}` },
    h('span', { class: 'info-row__label' }, label),
    h('span', { class: `info-row__value${mono ? ' mono' : ''}` }, value),
  );
}

/** Root element for toasts; guarded because document may not exist in tests. */
export function toastRoot() {
  return typeof document !== 'undefined' ? byId('toast-host') : null;
}
