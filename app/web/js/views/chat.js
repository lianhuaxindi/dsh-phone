/**
 * Chat screen (PRD FR-301..306, §12).
 *
 * Rendering is incremental: `message-added` appends one node, `message-delta`
 * updates the streaming text node directly, which keeps scrolling smooth even
 * with a high delta rate. Auto-scroll follows the stream until the user scrolls
 * up; then a "back to bottom" button appears and following stops.
 */

import { h, clear, setHidden, setClass, formatClock } from '../ui/dom.js';
import { t, errorCopy } from '../i18n.js';

const FOLLOW_THRESHOLD_PX = 96;
const MAX_INPUT_ROWS = 5;

/**
 * Split assistant text into plain-text and fenced-code segments.
 * Pure helper (no DOM) so the code-block rendering rules are unit-testable.
 * @param {string} text
 * @returns {Array<{type:'text',text:string}|{type:'code',language:string,code:string}>}
 */
export function parseMessageSegments(text) {
  const source = typeof text === 'string' ? text : '';
  const pattern = /```([^\n`]*)\n?([\s\S]*?)```/g;
  const segments = [];
  let lastIndex = 0;
  let match;
  while ((match = pattern.exec(source)) !== null) {
    if (match.index > lastIndex) segments.push({ type: 'text', text: source.slice(lastIndex, match.index) });
    segments.push({ type: 'code', language: (match[1] ?? '').trim(), code: (match[2] ?? '').replace(/\n$/, '') });
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < source.length) segments.push({ type: 'text', text: source.slice(lastIndex) });
  return segments;
}

export class ChatView {
  /**
   * @param {{root: HTMLElement, controller: object, toast: object, overlay: object,
   *          onBack: Function, onOpenSessions: Function}} options
   */
  constructor({ root, controller, toast, overlay, onBack = null, onOpenSessions = null }) {
    this.root = root;
    this.controller = controller;
    this.toast = toast;
    this.overlay = overlay;
    this.onBack = onBack;
    this.onOpenSessions = onOpenSessions;
    this.unsubscribers = [];
    /** @type {Map<string, {node: HTMLElement, body: HTMLElement, text: string, status: string}>} */
    this.rendered = new Map();
    this.follow = true;
    this.sessionId = null;
    this.toolNode = null;
    this.toolTimer = null;
    this._pressing = false;
    this._cancelArmed = false;
    this._timerHandle = null;
    this._recordingStartedAt = null;
    this._partialFrame = null;
    this._pendingPartialText = '';
    this.pendingImages = [];
    this._pendingSignature = '';
    this.chatMeta = null;
    this._build();
  }

  _build() {
    clear(this.root);
    this.sessionTitle = h('h1', { class: 'chat__title' });
    this.statusChip = h('span', { class: 'chip chip--status', role: 'status' });
    this.menuButton = h(
      'button',
      {
        type: 'button',
        class: 'icon-button',
        'aria-label': t('a11y.openMenu'),
        title: t('nav.menu'),
        onclick: () => (this.onOpenSessions ?? this.onBack)?.(),
      },
      '☰',
    );

    // v1.1 top bar (D-032): model chip, thinking chip and context usage.
    this.modelChip = h('span', { class: 'chip chip--model' });
    this.reasoningChip = h('span', { class: 'chip chip--reasoning' });
    this.contextBar = h('span', { class: 'chat__context-bar' }, h('span', { class: 'chat__context-fill' }));
    this.contextMeter = h(
      'div',
      { class: 'chat__context', role: 'status' },
      this.contextBar,
      h('span', { class: 'chat__context-label' }),
    );
    this.metaRow = h('div', { class: 'chat__meta' }, this.modelChip, this.reasoningChip, this.contextMeter);
    setHidden(this.metaRow, true);

    this.header = h(
      'header',
      { class: 'chat__header' },
      h('div', { class: 'chat__header-main' }, this.menuButton, h('div', { class: 'chat__heading' }, this.sessionTitle), this.statusChip),
      this.metaRow,
    );

    this.list = h('div', { class: 'chat__messages', role: 'log', 'aria-live': 'polite', tabindex: '0' });
    this.emptyState = h(
      'div',
      { class: 'empty-state' },
      h('p', { class: 'empty-state__title' }, t('chat.empty')),
      h('p', { class: 'empty-state__hint' }, t('chat.emptyHint')),
    );
    setHidden(this.emptyState, true);

    this.errorBanner = h('div', { class: 'banner banner--danger chat__error', role: 'alert' });
    setHidden(this.errorBanner, true);
    this.errorTitle = h('strong', { class: 'banner__title' });
    this.errorNext = h('span', { class: 'banner__next' });
    this.errorRetry = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--secondary btn--small',
        onclick: () => {
          this.controller.retryLastTurn();
          setHidden(this.errorBanner, true);
        },
      },
      t('chat.retry'),
    );
    this.errorBanner.append(this.errorTitle, this.errorNext, h('div', { class: 'banner__actions' }, this.errorRetry));

    this.toolRow = h('div', { class: 'tool-status', 'aria-live': 'polite' });
    setHidden(this.toolRow, true);

    this.scrollButton = h(
      'button',
      {
        type: 'button',
        class: 'scroll-bottom',
        'aria-label': t('a11y.scrollBottom'),
        title: t('chat.scrollBottom'),
        onclick: () => this._scrollToBottom(true),
      },
      '↓',
    );
    setHidden(this.scrollButton, true);

    this.partialPreview = h('div', { class: 'partial-preview', 'aria-live': 'polite' });
    this.partialLabel = h('span', { class: 'partial-preview__label' }, t('chat.partialLabel'));
    this.partialText = h('span', { class: 'partial-preview__text' });
    this.partialPreview.append(this.partialLabel, this.partialText);
    setHidden(this.partialPreview, true);

    this.textarea = h('textarea', {
      class: 'chat__input',
      rows: 1,
      placeholder: t('chat.placeholder'),
      'aria-label': t('chat.placeholder'),
      oninput: () => {
        this._autoResize();
        this._syncSendState();
        // Drafts are local-only (TC-WEB-03): never sent over the protocol.
        if (this.sessionId) this.controller.saveDraft?.(this.sessionId, this.textarea.value);
      },
      onkeydown: (event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          this._send();
        }
      },
    });
    this.autoSendChip = h('button', {
      type: 'button',
      class: 'chip chip--toggle',
      onclick: () => this._toggleAutoSend(),
    });
    this.sendButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--primary chat__send',
        'data-action': 'chat-send',
        'aria-label': t('a11y.send'),
        onclick: () => this._send(),
      },
      t('chat.send'),
    );
    this.sendButton.disabled = true;
    this.stopButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--danger chat__stop',
        'data-action': 'chat-stop',
        'aria-label': t('a11y.stop'),
        onclick: () => this.controller.cancelTurn(),
      },
      t('chat.stop'),
    );
    setHidden(this.stopButton, true);

    this.micButton = h(
      'button',
      {
        type: 'button',
        class: 'chat__mic',
        'data-action': 'chat-mic',
        'aria-label': t('a11y.record'),
        title: t('recording.hint'),
      },
      '🎙',
    );
    this._bindPushToTalk();

    this.attachButton = h(
      'button',
      {
        type: 'button',
        class: 'chat__attach',
        'data-action': 'chat-attach',
        'aria-label': t('a11y.attachImage'),
        title: t('chat.attachImage'),
        onclick: () => this._pickImage('gallery'),
      },
      '📎',
    );
    this.cameraButton = h(
      'button',
      {
        type: 'button',
        class: 'chat__camera',
        'data-action': 'chat-camera',
        'aria-label': t('a11y.takePhoto'),
        title: t('chat.takePhoto'),
        onclick: () => this._pickImage('camera'),
      },
      '📷',
    );

    this.attachments = h('div', { class: 'chat__attachments' });
    setHidden(this.attachments, true);

    this.composer = h(
      'div',
      { class: 'chat__composer' },
      this.partialPreview,
      this.attachments,
      h(
        'div',
        { class: 'chat__composer-row' },
        this.attachButton,
        this.cameraButton,
        this.textarea,
        this.micButton,
        this.sendButton,
        this.stopButton,
      ),
      h('div', { class: 'chat__composer-meta' }, this.autoSendChip),
    );

    this.root.append(
      h(
        'section',
        { class: 'screen screen--chat' },
        this.header,
        h('div', { class: 'chat__list-wrap' }, this.list, this.emptyState, this.toolRow, this.scrollButton),
        this.errorBanner,
        this.composer,
      ),
    );

    this.list.addEventListener('scroll', () => {
      const distance = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight;
      this.follow = distance < FOLLOW_THRESHOLD_PX;
      setHidden(this.scrollButton, this.follow);
    });
  }

  /* ------------------------------------------------------------- lifecycle */

  mount() {
    const controller = this.controller;
    this.unsubscribers.push(
      controller.on('state', (snapshot) => this.render(snapshot)),
      controller.on('sessions', () => this._renderHeader(controller.snapshot())),
      controller.on('session-opened', ({ sessionId, messages, draft }) => {
        this.sessionId = sessionId;
        this._rebuildMessages(messages);
        this._restoreDraft(draft);
      }),
      controller.on('message-added', ({ sessionId, message }) => {
        if (!this._isActive(sessionId)) return;
        this._appendMessage(message);
        this._afterContentChange();
      }),
      controller.on('message-delta', ({ sessionId, messageId, text }) => {
        if (!this._isActive(sessionId)) return;
        this._updateStreaming(messageId, text);
        this._afterContentChange();
      }),
      controller.on('message-updated', ({ sessionId, message }) => {
        if (!this._isActive(sessionId)) return;
        this._updateMessage(message);
        this._afterContentChange();
      }),
      controller.on('tool-status', (payload) => {
        if (payload.sessionId && payload.sessionId !== this.sessionId) return;
        this._showToolStatus(payload);
      }),
      controller.on('partial', (payload) => this._renderPartial(payload)),
      controller.on('asr-final', (payload) => this._onAsrFinal(payload)),
      controller.on('audio-state', (payload) => this._onAudioState(payload)),
      controller.on('audio-level', ({ level }) => this.overlay?.update({ level })),
      controller.on('audio-route', (route) => this.overlay?.update(this._routePatch(route))),
      controller.on('error', (payload) => this._showError(payload)),
      controller.on('toast', (payload) => this.toast?.show(payload)),
      controller.on('chat-meta', ({ meta }) => this._renderChatMeta(meta)),
      controller.on('pending-images', ({ images }) => this._renderPendingImages(images)),
      controller.on('media', ({ mediaId, media }) => this._onMedia(mediaId, media)),
    );
  }

  destroy() {
    for (const unsubscribe of this.unsubscribers) unsubscribe?.();
    this.unsubscribers = [];
    this._stopTimer();
    if (this._partialFrame !== null && this._partialFrame !== undefined) {
      if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this._partialFrame);
      this._partialFrame = null;
    }
    clear(this.root);
  }

  _isActive(sessionId) {
    return sessionId === this.sessionId;
  }

  /* --------------------------------------------------------------- render */

  /** Full render for controller snapshots. */
  render(snapshot) {
    const previous = this.sessionId;
    this.sessionId = snapshot.activeSessionId;
    if (this.sessionId !== previous) this._rebuildMessages(snapshot.messages ?? []);
    this._renderHeader(snapshot);
    const busy = snapshot.phase === 'waiting-for-reply';
    setHidden(this.stopButton, !busy);
    setHidden(this.sendButton, busy);
    this.textarea.disabled = busy;
    this.micButton.disabled = !snapshot.canRecord;
    setClass(this.micButton, 'chat__mic--disabled', !snapshot.canRecord);
    this._syncSendState();
    this._renderAutoSendChip(snapshot.settings?.autoSend !== false);
    this._renderChatMeta(snapshot.chatMeta);
    this._renderPendingImages(snapshot.pendingImages ?? []);
    this.emptyState.hidden = (snapshot.messages?.length ?? 0) > 0;
  }

  /** Send is enabled with a non-empty draft or at least one attached image. */
  _syncSendState() {
    const empty = !String(this.textarea.value ?? '').trim();
    const hasImages = Array.isArray(this.pendingImages) && this.pendingImages.length > 0;
    this.sendButton.disabled = (empty && !hasImages) || Boolean(this.textarea.disabled);
  }

  /* ---------------------------------------------- v1.1 top bar & images */

  /** Model / thinking / context row (D-032). Hidden until the server sends meta. */
  _renderChatMeta(meta) {
    this.chatMeta = meta ?? null;
    setHidden(this.metaRow, !meta);
    if (!meta) return;
    setHidden(this.modelChip, !meta.modelLabel);
    this.modelChip.textContent = meta.modelLabel ?? '';
    this.modelChip.title = meta.modelLabel ? t('a11y.modelChip', { model: meta.modelLabel }) : '';
    const effort = meta.reasoningEffort;
    const effortLabels = { low: 'chat.thinkingLow', high: 'chat.thinkingHigh', max: 'chat.thinkingMax' };
    const showEffort = Boolean(effort && effortLabels[effort]);
    setHidden(this.reasoningChip, !showEffort);
    this.reasoningChip.textContent = showEffort ? t(effortLabels[effort]) : '';
    this.reasoningChip.title = showEffort ? t('a11y.reasoningChip', { level: this.reasoningChip.textContent }) : '';
    const percent = Number.isFinite(meta.percent) ? Math.max(0, Math.min(100, meta.percent)) : 0;
    const fill = this.contextBar.firstChild;
    if (fill) fill.style.width = `${percent}%`;
    const label = this.contextMeter.querySelector('.chat__context-label');
    if (label) {
      label.textContent = meta.usedTokens !== null && meta.usedTokens !== undefined
        ? t('chat.contextDetail', {
            used: formatTokenCount(meta.usedTokens),
            total: formatTokenCount(meta.contextWindow),
            percent,
          })
        : t('chat.contextEmpty');
      const base = meta.estimated ? `${t('chat.contextLabel')} · ${t('chat.contextEstimated')}` : t('chat.contextLabel');
      this.contextMeter.title = meta.estimated
        ? t('chat.contextEstimated')
        : t('a11y.contextMeter', { percent });
      this.contextMeter.setAttribute('aria-label', `${base} ${label.textContent}`);
    }
    this.contextMeter.hidden = meta.usedTokens === null && meta.percent === null;
  }

  /** Pending attachment thumbnails above the composer. */
  _renderPendingImages(images) {
    const next = Array.isArray(images) ? images : [];
    const signature = next.map((image) => `${image.id}:${image.mimeType}:${image.data.length}`).join('|');
    if (signature === this._pendingSignature) return;
    this._pendingSignature = signature;
    this.pendingImages = next;
    clear(this.attachments);
    setHidden(this.attachments, this.pendingImages.length === 0);
    for (const image of this.pendingImages) {
      const thumb = h('img', {
        class: 'attachment__image',
        alt: image.name || t('a11y.imagePreview'),
        src: dataUri(image.mimeType, image.data),
      });
      const remove = h(
        'button',
        {
          type: 'button',
          class: 'attachment__remove',
          'aria-label': t('a11y.removeImage'),
          title: t('chat.removeImage'),
          onclick: () => this.controller.removeImage?.(image.id),
        },
        '×',
      );
      this.attachments.appendChild(h('div', { class: 'attachment', 'data-image-id': image.id }, thumb, remove));
    }
    this._syncSendState();
  }

  async _pickImage(source) {
    this.attachButton.disabled = true;
    this.cameraButton.disabled = true;
    try {
      await this.controller.attachImage?.(source);
    } catch {
      this.toast?.show({ key: 'chat.imagePickFailed', vars: { reason: 'unsupported' } });
    } finally {
      this.attachButton.disabled = false;
      this.cameraButton.disabled = false;
      this._syncSendState();
    }
  }

  /** Media bytes arrived: fill any waiting <img> placeholders. */
  _onMedia(mediaId, media) {
    const slots = this.list.querySelectorAll(`[data-media-id="${cssEscape(String(mediaId))}"]`);
    for (const slot of slots) this._applyMediaToSlot(slot, media);
  }

  _applyMediaToSlot(slot, media) {
    if (!media || typeof media.data !== 'string') {
      slot.classList.add('message__image--failed');
      const placeholder = slot.querySelector('.message__image-placeholder');
      if (placeholder) placeholder.textContent = t('chat.imageLoadFailed');
      return;
    }
    const image = slot.querySelector('img');
    if (!image) {
      slot.appendChild(
        h('img', {
          class: 'message__image',
          alt: t('a11y.imagePreview'),
          src: dataUri(media.mimeType, media.data),
          onload: () => slot.classList.add('message__image--loaded'),
          onerror: () => slot.classList.add('message__image--failed'),
        }),
      );
    } else {
      image.src = dataUri(media.mimeType, media.data);
    }
    const placeholder = slot.querySelector('.message__image-placeholder');
    if (placeholder) placeholder.remove();
  }

  /** Render a message's image grid; loads bytes through controller.loadMedia. */
  _renderImages(container, images) {
    const list = Array.isArray(images) ? images : [];
    if (list.length === 0) return;
    const grid = h('div', { class: `message__images message__images--${Math.min(list.length, 4)}` });
    for (const image of list) {
      const mediaId = String(image.mediaId ?? '');
      const slot = h('div', { class: 'message__image-slot', 'data-media-id': mediaId });
      const cached = this.controller.mediaFor?.(mediaId);
      if (cached) {
        this._applyMediaToSlot(slot, cached);
      } else {
        slot.classList.add('message__image--loading');
        slot.appendChild(h('span', { class: 'message__image-placeholder' }, '⏳'));
        Promise.resolve(this.controller.loadMedia?.(mediaId))
          .then((media) => this._applyMediaToSlot(slot, media))
          .catch(() => this._applyMediaToSlot(slot, null));
      }
      grid.appendChild(slot);
    }
    container.appendChild(grid);
  }

  _restoreDraft(draft) {
    if (typeof draft !== 'string') return;
    if (this.textarea.value === draft) return;
    this.textarea.value = draft;
    this._autoResize();
    this._syncSendState();
  }

  _renderHeader(snapshot) {
    const session = snapshot.sessions?.find((item) => item.sessionId === snapshot.activeSessionId);
    this.sessionTitle.textContent = session?.title || t('sessions.untitled');
    this.statusChip.textContent = t(`phase.${snapshot.phase}`);
    this.statusChip.title = t(`phaseHint.${snapshot.phase}`);
    setClass(this.statusChip, 'chip--online', Boolean(snapshot.online));
    setClass(this.statusChip, 'chip--warn', snapshot.phase === 'recording' || snapshot.phase === 'waiting-for-reply');
  }

  _renderAutoSendChip(enabled) {
    this.autoSendChip.textContent = enabled ? t('chat.autoSendOn') : t('chat.autoSendOff');
    setClass(this.autoSendChip, 'chip--on', enabled);
    this.autoSendChip.setAttribute('aria-pressed', String(enabled));
  }

  _rebuildMessages(messages) {
    clear(this.list);
    this.rendered.clear();
    for (const message of messages) this._appendMessage(message, { silent: true });
    this.follow = true;
    this._afterContentChange({ force: true });
  }

  _appendMessage(message, { silent = false } = {}) {
    if (this.rendered.has(message.messageId)) return;
    const node = h('article', { class: `message message--${message.role}`, 'data-message-id': message.messageId });
    const role = h('div', { class: 'message__role' }, this._roleLabel(message.role));
    const body = h('div', { class: 'message__body' });
    const content = h('div', { class: 'message__text' });
    body.appendChild(content);
    const toolLine = h('div', { class: 'message__tool' });
    setHidden(toolLine, true);
    if (message.role === 'tool') setHidden(role, true);
    node.append(role, body);

    const actions = h('div', { class: 'message__actions' });
    if (message.role === 'assistant') {
      actions.appendChild(
        h(
          'button',
          {
            type: 'button',
            class: 'icon-button icon-button--small message__copy',
            'aria-label': t('a11y.copyMessage'),
            title: t('chat.copyMessage'),
            onclick: () => this._copy(message.text),
          },
          '⧉',
        ),
      );
    }
    if (message.role === 'user') {
      actions.appendChild(
        h(
          'button',
          {
            type: 'button',
            class: 'icon-button icon-button--small message__resend',
            'aria-label': t('a11y.resend'),
            title: t('chat.resend'),
            onclick: () => this.controller.sendText(message.text),
          },
          '↻',
        ),
      );
    }
    if (actions.childElementCount > 0) node.appendChild(actions);

    this._renderContent(content, message.text ?? '');
    this._renderImages(content, message.images);
    if (message.status === 'streaming') node.classList.add('message--streaming');
    if (message.status === 'error') node.classList.add('message--error');

    this.list.appendChild(node);
    this.rendered.set(message.messageId, {
      node,
      body: content,
      text: message.text ?? '',
      status: message.status,
      images: Array.isArray(message.images) ? message.images : [],
      toolLine,
    });
    if (!silent) this.emptyState.hidden = true;
  }

  _roleLabel(role) {
    if (role === 'user') return t('common.you');
    if (role === 'assistant') return t('common.assistant');
    if (role === 'tool') return t('common.tool');
    return t('common.system');
  }

  _updateStreaming(messageId, text) {
    let entry = this.rendered.get(messageId);
    if (!entry) {
      // Delta for a message we have not seen yet (e.g. resumed session).
      const message = this.controller.messagesFor(this.sessionId).find((item) => item.messageId === messageId);
      if (!message) return;
      this._appendMessage(message);
      entry = this.rendered.get(messageId);
      if (!entry) return;
    }
    entry.text = text;
    this._renderContent(entry.body, text);
    entry.node.classList.add('message--streaming');
  }

  _updateMessage(message) {
    const entry = this.rendered.get(message.messageId);
    if (!entry) {
      this._appendMessage(message);
      return;
    }
    entry.text = message.text ?? entry.text;
    entry.images = Array.isArray(message.images) ? message.images : entry.images ?? [];
    this._renderContent(entry.body, entry.text);
    this._renderImages(entry.body, entry.images);
    setClass(entry.node, 'message--streaming', message.status === 'streaming');
    setClass(entry.node, 'message--error', message.status === 'error');
    const copyButton = entry.node.querySelector('.message__copy');
    if (copyButton) copyButton.onclick = () => this._copy(entry.text);
  }

  /**
   * Render text with fenced code blocks, inline code and plain paragraphs.
   * Uses text nodes only (never innerHTML) so model output cannot inject HTML.
   */
  _renderContent(container, text) {
    clear(container);
    const pushPlain = (chunk) => {
      if (!chunk) return;
      const paragraphs = chunk.split(/\n{2,}/);
      for (const paragraph of paragraphs) {
        if (!paragraph.trim()) continue;
        const p = h('p', { class: 'message__paragraph' });
        this._renderInline(p, paragraph);
        container.appendChild(p);
      }
    };
    for (const segment of parseMessageSegments(text)) {
      if (segment.type === 'code') {
        const block = h('pre', { class: 'code-block mono' });
        if (segment.language) block.appendChild(h('span', { class: 'code-block__lang' }, segment.language));
        block.appendChild(h('code', null, segment.code));
        container.appendChild(block);
      } else {
        pushPlain(segment.text);
      }
    }
  }

  _renderInline(parent, text) {
    const parts = text.split(/(`[^`\n]+`)/g);
    for (const part of parts) {
      if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
        parent.appendChild(h('code', { class: 'mono inline-code' }, part.slice(1, -1)));
      } else if (part) {
        const lines = part.split('\n');
        lines.forEach((line, index) => {
          if (index > 0) parent.appendChild(document.createElement('br'));
          parent.appendChild(document.createTextNode(line));
        });
      }
    }
  }

  _afterContentChange({ force = false } = {}) {
    if (force || this.follow) this._scrollToBottom(false);
    setHidden(this.scrollButton, this.follow || force);
  }

  _scrollToBottom(smooth) {
    this.follow = true;
    setHidden(this.scrollButton, true);
    const behavior = smooth ? 'smooth' : 'auto';
    try {
      this.list.scrollTo({ top: this.list.scrollHeight, behavior });
    } catch {
      this.list.scrollTop = this.list.scrollHeight;
    }
  }

  async _copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      this.toast?.show({ key: 'common.copied' });
    } catch {
      this.toast?.show({ key: 'toast.error' });
    }
  }

  /* ---------------------------------------------------------------- tools */

  _showToolStatus({ name, state, title }) {
    const labelKey = state === 'running' ? 'chat.toolRunning' : state === 'error' ? 'chat.toolError' : 'chat.toolDone';
    setHidden(this.toolRow, false);
    clear(this.toolRow);
    this.toolRow.appendChild(h('span', { class: `tool-status__dot tool-status__dot--${state}`, 'aria-hidden': 'true' }));
    this.toolRow.appendChild(h('span', { class: 'tool-status__name' }, t(labelKey, { name: name ?? title ?? t('common.tool') })));
    if (state !== 'running' && this.toolTimer === null) {
      this.toolTimer = setTimeout(() => {
        setHidden(this.toolRow, true);
        this.toolTimer = null;
      }, 3000);
    }
  }

  /* ------------------------------------------------------------- partials */

  /**
   * ASR partials may arrive up to 5/s (protocol) and the server may be chatty;
   * coalesce DOM writes to one per animation frame (TC-WEB-09).
   */
  _renderPartial({ text }) {
    this._pendingPartialText = text ?? '';
    if (typeof requestAnimationFrame !== 'function') {
      this._flushPartial();
      return;
    }
    if (this._partialFrame) return;
    this._partialFrame = requestAnimationFrame(() => {
      this._partialFrame = null;
      this._flushPartial();
    });
  }

  _flushPartial() {
    const text = this._pendingPartialText ?? '';
    if (!text) {
      setHidden(this.partialPreview, true);
      this.partialText.textContent = '';
      return;
    }
    this.partialText.textContent = text;
    setHidden(this.partialPreview, false);
  }

  _onAsrFinal({ text }) {
    this._renderPartial({ text: null });
    if (!text) {
      this.toast?.show({ key: 'recording.empty' });
      return;
    }
    if (this.controller.settings.autoSend) {
      // Controller already sent it; nothing to edit.
      this.textarea.value = '';
    } else {
      this.textarea.value = text;
      this.textarea.focus();
      this._autoResize();
      this._syncSendState();
      if (this.sessionId) this.controller.saveDraft?.(this.sessionId, text);
      this.toast?.show({ key: 'toast.autoSendOff' });
    }
  }

  /** Route/device fields for the recording overlay ("via WH-1000XM5"). */
  _routePatch(route = this.controller?.audioRoute) {
    return { route: route?.route ?? null, deviceName: route?.deviceName ?? null };
  }

  _onAudioState({ state }) {
    if (!this.overlay) return;
    if (state === 'preheating') {
      this.overlay.open();
      this.overlay.update({ state: 'preheating', elapsedMs: 0, partial: '', ...this._routePatch() });
    } else if (state === 'streaming') {
      this.overlay.open();
      this.overlay.update({ state: 'recording', elapsedMs: this._elapsed(), level: 0.2, ...this._routePatch() });
    } else if (state === 'stopping') {
      this.overlay.update({ state: 'stopping', elapsedMs: this._elapsed() });
    } else if (state === 'stopped' || state === 'idle') {
      this._stopTimer();
      this.overlay.close();
    }
  }

  _elapsed() {
    return this._recordingStartedAt ? Date.now() - this._recordingStartedAt : 0;
  }

  _startTimer() {
    this._stopTimer();
    this._timerHandle = setInterval(() => {
      this.overlay?.update({ elapsedMs: this._elapsed() });
    }, 200);
  }

  _stopTimer() {
    if (this._timerHandle !== null) {
      clearInterval(this._timerHandle);
      this._timerHandle = null;
    }
  }

  /* ------------------------------------------------------------ push-to-talk */

  _bindPushToTalk() {
    const button = this.micButton;
    const start = (event) => {
      if (button.disabled || this._pressing) return;
      if (event.type === 'pointerdown') {
        event.preventDefault();
        try {
          button.setPointerCapture(event.pointerId);
        } catch {
          /* optional */
        }
      }
      this._pressing = true;
      this._cancelArmed = false;
      this._originY = event.clientY ?? 0;
      this._recordingStartedAt = Date.now();
      if (!this.controller.startRecording()) {
        this._pressing = false;
        return;
      }
      this.overlay?.open();
      this.overlay?.update({ state: 'preheating', elapsedMs: 0, level: 0, partial: '', cancelArmed: false });
      this._startTimer();
      button.classList.add('chat__mic--active');
    };
    const move = (event) => {
      if (!this._pressing) return;
      const deltaY = (event.clientY ?? 0) - this._originY;
      this._cancelArmed = deltaY < -48;
      this.overlay?.update({ cancelArmed: this._cancelArmed });
    };
    const end = (event) => {
      if (!this._pressing) return;
      this._pressing = false;
      button.classList.remove('chat__mic--active');
      if (this._cancelArmed) {
        this.controller.cancelRecording();
        this.toast?.show({ key: 'recording.cancelled' });
      } else {
        this.controller.stopRecording();
      }
      this._cancelArmed = false;
      this.overlay?.update({ cancelArmed: false, state: 'stopping', elapsedMs: this._elapsed() });
      this._recordingStartedAt = null;
      this._stopTimer();
      event.preventDefault?.();
    };
    button.addEventListener('pointerdown', start);
    button.addEventListener('pointermove', move);
    button.addEventListener('pointerup', end);
    button.addEventListener('pointercancel', end);
    button.addEventListener('pointerleave', (event) => {
      if (this._pressing && event.buttons === 0) end(event);
    });
    button.addEventListener('keydown', (event) => {
      if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) start({ type: 'keydown', clientY: 0 });
    });
    button.addEventListener('keyup', (event) => {
      if (event.key === ' ' || event.key === 'Enter') end({ type: 'keyup' });
    });
    this._pttCleanup = () => {
      button.removeEventListener('pointerdown', start);
      button.removeEventListener('pointermove', move);
      button.removeEventListener('pointerup', end);
      button.removeEventListener('pointercancel', end);
    };
  }

  _send() {
    if (this.textarea.disabled) {
      this.toast?.show({ key: 'chat.inputBlockedSending' });
      return;
    }
    const text = this.textarea.value;
    const hasImages = Array.isArray(this.pendingImages) && this.pendingImages.length > 0;
    if (!text.trim() && !hasImages) {
      this.toast?.show({ key: 'chat.emptyBlocked' });
      return;
    }
    if (text.length > 32000) {
      this.toast?.show({ key: 'chat.tooLong', vars: { max: 32000 } });
      return;
    }
    this.textarea.value = '';
    this._autoResize();
    this._syncSendState();
    if (this.sessionId) this.controller.clearDraft?.(this.sessionId);
    this.controller.sendText(text);
  }

  async _toggleAutoSend() {
    const enabled = this.controller.settings.autoSend !== false;
    await this.controller.updateSettings({ autoSend: !enabled });
    this._renderAutoSendChip(!enabled);
    this.toast?.show({ key: enabled ? 'toast.autoSendOff' : 'toast.autoSendOn' });
  }

  _autoResize() {
    this.textarea.style.height = 'auto';
    const lineHeight = 22;
    const rows = Math.min(MAX_INPUT_ROWS, Math.max(1, Math.ceil(this.textarea.scrollHeight / lineHeight)));
    this.textarea.rows = rows;
    this.textarea.style.height = rows > 1 ? `${rows * lineHeight}px` : '';
  }

  /* --------------------------------------------------------------- errors */

  _showError({ code, message, canRetry, scope }) {
    const copy = errorCopy(Number(code));
    setHidden(this.errorBanner, false);
    this.errorTitle.textContent = copy.title;
    this.errorNext.textContent = message ? `${message} · ${copy.next}` : copy.next;
    setHidden(this.errorRetry, !(canRetry || scope === 'close'));
    this.errorRetry.onclick = () => {
      setHidden(this.errorBanner, true);
      if (scope === 'close') this.controller.reconnectBridge();
      else this.controller.retryLastTurn();
    };
  }
}

/** Chat page factory used by app.js. */
export function createChatView(options) {
  const view = new ChatView(options);
  view.mount();
  return view;
}

/** Compact token count for the context-meter label (1200 → 1.2K). */
export function formatTokenCount(value) {
  const count = Number(value);
  if (!Number.isFinite(count) || count < 0) return '0';
  if (count >= 1_000_000) return `${Number((count / 1_000_000).toFixed(1))}M`;
  if (count >= 1_000) return `${Number((count / 1_000).toFixed(1))}K`;
  return String(Math.round(count));
}

/** Build a data URI from base64 media bytes (message images). */
export function dataUri(mimeType, base64) {
  const mime = typeof mimeType === 'string' && mimeType ? mimeType : 'image/jpeg';
  return `data:${mime};base64,${base64}`;
}

/** Minimal CSS.escape fallback for older WebViews. */
function cssEscape(value) {
  if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') return CSS.escape(value);
  return String(value).replace(/["\\]/g, '\\$&');
}

/** Exported for tests: the exact follow threshold used by the scroll logic. */
export const SCROLL_FOLLOW_THRESHOLD_PX = FOLLOW_THRESHOLD_PX;
/** Exported for tests: timer formatting helper reused by the overlay. */
export const _formatClock = formatClock;
