/**
 * Session list (PRD FR-201..205).
 *
 * Shows title, relative update time and last preview, supports creating and
 * switching sessions. Deleting is local-only and always requires confirmation.
 */

import { h, clear, setHidden, setClass } from '../ui/dom.js';
import { t, formatRelativeTime } from '../i18n.js';

/** Preview characters kept in a session row before truncation. */
export const PREVIEW_MAX_CHARS = 120;

export class SessionsView {
  /**
   * @param {{root: HTMLElement, controller: object, toast: object, onOpen: (sessionId: string) => void, onNew: Function}} options
   */
  constructor({ root, controller, toast, onOpen, onNew }) {
    this.root = root;
    this.controller = controller;
    this.toast = toast;
    this.onOpen = onOpen;
    this.onNew = onNew;
    this.unsubscribers = [];
    this._pendingDelete = null;
    this._build();
  }

  _build() {
    clear(this.root);
    this.list = h('ul', { class: 'session-list', role: 'list' });
    this.empty = h(
      'div',
      { class: 'empty-state' },
      h('p', { class: 'empty-state__title' }, t('sessions.empty')),
      h('p', { class: 'empty-state__hint' }, t('sessions.emptyHint')),
    );
    setHidden(this.empty, true);
    this.refreshButton = h(
      'button',
      {
        type: 'button',
        class: 'icon-button',
        'aria-label': t('common.refresh'),
        title: t('common.refresh'),
        onclick: () => this.controller.refreshSessions(),
      },
      '⟳',
    );
    this.newButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--primary',
        'data-action': 'new-session',
        onclick: () => this.onNew?.(),
      },
      t('sessions.new'),
    );
    this.root.append(
      h(
        'section',
        { class: 'screen screen--sessions' },
        h(
          'header',
          { class: 'screen__header' },
          h('h1', { class: 'screen__title' }, t('sessions.title')),
          this.refreshButton,
        ),
        this.list,
        this.empty,
        this.newButton,
      ),
    );
  }

  mount() {
    this.unsubscribers.push(
      this.controller.on('sessions', () => this.render(this.controller.snapshot())),
      this.controller.on('state', (snapshot) => this.render(snapshot)),
    );
  }

  /** Render the list from the controller snapshot. */
  render(snapshot) {
    const now = Math.floor(Date.now() / 1000);
    clear(this.list);
    setHidden(this.empty, snapshot.sessions.length > 0);
    for (const session of snapshot.sessions) {
      this.list.appendChild(this._item(session, now));
    }
    this.refreshButton.disabled = !snapshot.online;
    setClass(this.list, 'session-list--offline', !snapshot.online);
  }

  _item(session, now) {
    const pending = this._pendingDelete === session.sessionId;
    const title = session.title || t('sessions.untitled');
    const rawPreview = session.lastPreview || '';
    // Long previews are truncated so rows keep a stable height (TC-WEB-02).
    const preview = rawPreview.length > PREVIEW_MAX_CHARS ? `${rawPreview.slice(0, PREVIEW_MAX_CHARS)}…` : rawPreview;
    const when = formatRelativeTime(now - session.updatedAt, now);
    const meta = h(
      'div',
      { class: 'session-item__meta' },
      h('span', { class: 'session-item__time' }, t('sessions.updated', { time: when })),
      preview ? h('span', { class: 'session-item__preview' }, preview) : null,
    );
    const deleteButton = h(
      'button',
      {
        type: 'button',
        class: pending ? 'btn btn--danger btn--small' : 'icon-button session-item__delete',
        'aria-label': t('sessions.delete'),
        title: t('sessions.delete'),
        onclick: (event) => {
          event.stopPropagation();
          this._delete(session.sessionId, pending);
        },
      },
      pending ? t('sessions.deleteConfirm') : '🗑',
    );
    if (pending) deleteButton.setAttribute('data-action', 'confirm-delete');

    const item = h(
      'li',
      { class: `session-item${pending ? ' session-item--pending' : ''}` },
      h(
        'button',
        {
          type: 'button',
          class: 'session-item__main',
          'aria-label': t('a11y.sessionItem', { title }),
          onclick: () => {
            if (pending) {
              this._pendingDelete = null;
              this.render(this.controller.snapshot());
              return;
            }
            this.onOpen?.(session.sessionId);
          },
        },
        h('span', { class: 'session-item__title' }, title),
        meta,
      ),
      deleteButton,
    );
    return item;
  }

  _delete(sessionId, pending) {
    if (!pending) {
      this._pendingDelete = sessionId;
      this.render(this.controller.snapshot());
      this.toast?.show({ key: 'sessions.deleteBody' });
      return;
    }
    this._pendingDelete = null;
    this.controller.deleteSessionLocal(sessionId).then(() => {
      this.toast?.show({ key: 'sessions.deleted' });
      this.render(this.controller.snapshot());
    });
  }

  destroy() {
    for (const unsubscribe of this.unsubscribers) unsubscribe?.();
    this.unsubscribers = [];
    clear(this.root);
  }
}

/** Sessions page factory used by app.js. */
export function createSessionsView(options) {
  const view = new SessionsView(options);
  view.mount();
  return view;
}
