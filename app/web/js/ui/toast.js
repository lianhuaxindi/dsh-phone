/**
 * Toast host — transient status messages with polite ARIA announcements.
 */

import { h, byId, clear } from './dom.js';
import { t } from '../i18n.js';

const DEFAULT_DURATION_MS = 4200;
const MAX_VISIBLE = 3;

/** Keys that render with a "danger" tone. */
const DANGER_KEYS = new Set([
  'toast.error',
  'approval.expiredToast',
  'question.expiredToast',
  'toast.repair',
  'chat.notOnline',
]);

/**
 * Small stack of toasts attached to a host element.
 */
export class ToastHost {
  /**
   * @param {{root?: HTMLElement|null, scheduler?: object, durationMs?: number}} [options]
   */
  constructor({ root = null, scheduler = null, durationMs = DEFAULT_DURATION_MS } = {}) {
    this.root = root ?? (typeof document !== 'undefined' ? byId('toast-host') : null);
    if (this.root) {
      this.root.setAttribute('role', 'status');
      this.root.setAttribute('aria-live', 'polite');
      this.root.setAttribute('aria-atomic', 'false');
    }
    this.scheduler = scheduler;
    this.durationMs = durationMs;
    this._items = [];
  }

  /**
   * Show a translated toast.
   * @param {{key: string, vars?: object, tone?: string, durationMs?: number}} message
   */
  show({ key, vars, tone, durationMs = this.durationMs }) {
    if (!this.root) return null;
    const text = t(key, vars);
    return this.showText(text, { tone: tone ?? (DANGER_KEYS.has(key) ? 'danger' : 'info'), durationMs });
  }

  /** Show plain text (already localized). */
  showText(text, { tone = 'info', durationMs = this.durationMs } = {}) {
    if (!this.root || !text) return null;
    while (this._items.length >= MAX_VISIBLE) this._dismiss(this._items[0]);
    const node = h('div', { class: `toast toast--${tone}`, role: 'status' }, text);
    this.root.appendChild(node);
    const item = { node, timer: null };
    this._items.push(item);
    if (durationMs > 0) {
      item.timer = this._setTimer(() => this._dismiss(item), durationMs);
    }
    return item;
  }

  _setTimer(fn, ms) {
    if (this.scheduler?.setTimeout) return this.scheduler.setTimeout(fn, ms);
    return setTimeout(fn, ms);
  }

  _clearTimer(handle) {
    if (handle === null || handle === undefined) return;
    if (this.scheduler?.clearTimeout) this.scheduler.clearTimeout(handle);
    else clearTimeout(handle);
  }

  _dismiss(item) {
    const index = this._items.indexOf(item);
    if (index >= 0) this._items.splice(index, 1);
    this._clearTimer(item.timer);
    item.node.classList.add('toast--leaving');
    const remove = () => item.node.remove();
    if (this.scheduler?.setTimeout) this.scheduler.setTimeout(remove, 180);
    else remove();
  }

  /** Remove every visible toast. */
  clearAll() {
    for (const item of [...this._items]) this._dismiss(item);
    if (this.root) clear(this.root);
  }

  get size() {
    return this._items.length;
  }
}

/** Convenience singleton attached by app.js. */
export const toastHost = new ToastHost();
