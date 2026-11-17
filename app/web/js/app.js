/**
 * WebView app bootstrap — screens, navigation, theme and language.
 *
 * This module is the only place that touches `window`/`document`. Everything
 * else (controller, transports, views) is passed in explicitly, which is why
 * `node --test` can import this file and drive the whole UI flow headlessly.
 *
 * Transport selection (all three surfaces share one controller):
 *   - URL `?transport=mock|ws|native` wins (developer/demo shortcut),
 *   - otherwise `settings.transportMode` decides; `auto` prefers the Native
 *     JsBridge when `window.DshNative` exists, else the in-browser mock,
 *   - `mock` is also what plain `python -m http.server` demos use.
 */

import { APP_VERSION, BUILD_CHANNEL, TransportMode } from './config.js';
import { setLang, t } from './i18n.js';
import { createStore } from './store.js';
import { Diagnostics } from './diagnostics.js';
import { Controller } from './controller.js';
import { realScheduler } from './scheduler.js';
import {
  NativeTransport,
  WsTransport,
  TransportKind,
  TransportEvent,
  defaultWsUrl,
  hasNativeBridge,
  resolveWebTransportMode,
} from './transport.js';
import { MockTransport } from './mock.js';
import { h, clear, setClass } from './ui/dom.js';
import { ToastHost } from './ui/toast.js';
import { RecordingOverlay } from './ui/recording-overlay.js';
import { openApprovalSheet } from './ui/approval-sheet.js';
import { openQuestionSheet } from './ui/question-sheet.js';
import { closeTopSheet, hasOpenSheet } from './ui/sheet.js';
import { PairingView } from './views/pairing.js';
import { SessionsView } from './views/sessions.js';
import { ChatView } from './views/chat.js';
import { SettingsView } from './views/settings.js';
import { DiagnosticsView } from './views/diagnostics.js';

/** Screens addressable by the router. */
export const SCREEN = Object.freeze({
  PAIRING: 'pairing',
  SESSIONS: 'sessions',
  CHAT: 'chat',
  SETTINGS: 'settings',
  DIAGNOSTICS: 'diagnostics',
});

const NAV_ITEMS = [
  { screen: SCREEN.SESSIONS, key: 'nav.sessions', icon: '💬' },
  { screen: SCREEN.SETTINGS, key: 'nav.settings', icon: '⚙' },
  { screen: SCREEN.DIAGNOSTICS, key: 'nav.diagnostics', icon: '🩺' },
];

/**
 * Build the app once the DOM is ready.
 *
 * @param {{document: Document, window: Window, search?: string, autoStart?: boolean}} scope
 */
export async function createApp({ document: doc, window: win, search = '', autoStart = true } = {}) {
  const app = new WebApp({ document: doc, window: win, search });
  await app.init();
  if (autoStart) app.start();
  return app;
}

export class WebApp {
  constructor({ document: doc, window: win, search = '' }) {
    this.document = doc;
    this.window = win;
    this.search = search || win?.location?.search || '';
    this.screen = null;
    this.view = null;
    this.unsubscribers = [];
    this._previousScreen = SCREEN.SESSIONS;
    this._mediaQuery = null;
  }

  /* ------------------------------------------------------------------ boot */

  async init() {
    const doc = this.document;
    const win = this.window;
    this.root = doc.getElementById('app');
    if (!this.root) throw new Error('index.html is missing #app');

    this.store = createStore({ indexedDB: win?.indexedDB ?? null });
    this.settings = await this.store.getSettings();
    this.diagnostics = new Diagnostics({
      appVersion: APP_VERSION,
      protocolVersion: 1,
      buildChannel: BUILD_CHANNEL,
      scheduler: realScheduler,
    });
    this.transport = this._createTransport(this.settings);
    this.transportKind = this.transport.kind;
    this.controller = new Controller({
      transport: this.transport,
      store: this.store,
      diagnostics: this.diagnostics,
      scheduler: realScheduler,
    });

    this._buildShell();

    this.toast = new ToastHost({ root: this.toastRoot, scheduler: realScheduler });
    this.recordingOverlay = new RecordingOverlay({ root: this.overlayRoot });

    this._bindController();
    this._bindWindow();

    await this.controller.init();
    this._applyLanguage(this.controller.settings.lang);
    this._applyTheme(this.controller.settings.theme);
    this._applyDemoMode();
    return this;
  }

  /** Create the transport implementation for the current settings/URL. */
  _createTransport(settings) {
    const params = new URLSearchParams(this.search);
    const requested = params.get('transport');
    const mode = requested ?? settings.transportMode ?? TransportMode.AUTO;

    if (mode === TransportMode.MOCK) return new MockTransport({ scheduler: realScheduler });
    if (mode === TransportMode.WS) {
      return new WsTransport({
        WebSocketImpl: this.window?.WebSocket,
        storage: this.window?.localStorage,
        scheduler: realScheduler,
        urlFactory: ({ host, port }) => defaultWsUrl(host, port, false),
      });
    }
    if (mode === TransportMode.NATIVE) {
      if (!hasNativeBridge(this.window)) {
        this.diagnostics?.warn?.('transport', 'native transport requested without DshNative bridge');
      }
      return new NativeTransport({ scope: this.window, scheduler: realScheduler });
    }

    const resolved = resolveWebTransportMode(TransportMode.AUTO, this.window);
    if (resolved === TransportKind.NATIVE) return new NativeTransport({ scope: this.window, scheduler: realScheduler });
    if (resolved === TransportKind.WS) {
      return new WsTransport({
        WebSocketImpl: this.window?.WebSocket,
        storage: this.window?.localStorage,
        scheduler: realScheduler,
        urlFactory: ({ host, port }) => defaultWsUrl(host, port, false),
      });
    }
    // AUTO in a plain browser demo: run the local mock backend.
    return new MockTransport({ scheduler: realScheduler });
  }

  _buildShell() {
    const doc = this.document;
    clear(this.root);
    this.brandTitle = h('span', { class: 'brand__title' }, t('app.title'));
    this.brandSub = h('span', { class: 'brand__sub' }, t('app.subtitle'));
    this.connChip = h('span', { class: 'chip chip--status mono', role: 'status', 'aria-live': 'polite' });
    // Bluetooth / audio route status (PRD §12): shows the active SCO route and
    // device once the native bridge reports it, hidden while unknown.
    this.routeChip = h('span', { class: 'chip chip--status route-chip', role: 'status', 'aria-live': 'polite' });
    this.routeChip.hidden = true;

    this.navButtons = new Map();
    const nav = h('nav', { class: 'tabbar', role: 'tablist', 'aria-label': t('nav.menu') });
    for (const item of NAV_ITEMS) {
      const button = h(
        'button',
        {
          type: 'button',
          class: 'tabbar__item',
          role: 'tab',
          'data-screen': item.screen,
          onclick: () => this.show(item.screen),
        },
        h('span', { class: 'tabbar__icon', 'aria-hidden': 'true' }, item.icon),
        h('span', { class: 'tabbar__label' }, t(item.key)),
      );
      this.navButtons.set(item.screen, button);
      nav.appendChild(button);
    }

    this.demoBanner = h(
      'div',
      { class: 'demo-banner', role: 'note' },
      h('span', { class: 'demo-banner__title' }, t('app.demoBanner')),
      h('span', { class: 'demo-banner__hint' }, t('app.demoHint')),
      this._buildDemoTools(),
    );
    this.demoBanner.hidden = true;

    this.screenHost = h('main', { class: 'screen-host' });

    // Global connection banner (FR-105/106, TC-WEB-01): host offline / reconnecting.
    this.connBannerTitle = h('strong', { class: 'banner__title' });
    this.connBannerNext = h('span', { class: 'banner__next' });
    this.connBannerRetry = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--secondary btn--small',
        'data-action': 'conn-retry',
        onclick: () => this.controller.reconnectBridge(),
      },
      t('diagnostics.reconnect'),
    );
    this.connBanner = h(
      'div',
      { class: 'banner banner--danger conn-banner', role: 'status', 'aria-live': 'polite' },
      this.connBannerTitle,
      this.connBannerNext,
      h('div', { class: 'banner__actions' }, this.connBannerRetry),
    );
    this.connBanner.hidden = true;

    this.toastRoot = h('div', { class: 'toast-root' });
    this.overlayRoot = h('div', { class: 'overlay-root' });
    this.sheetRoot = h('div', { class: 'sheet-root' });

    this.root.append(
      h(
        'div',
        { class: 'app' },
        h('header', { class: 'topbar' }, h('div', { class: 'brand' }, this.brandTitle, this.brandSub), h('div', { class: 'topbar__chips' }, this.routeChip, this.connChip)),
        this.connBanner,
        this.demoBanner,
        this.screenHost,
        nav,
        this.overlayRoot,
        this.sheetRoot,
        this.toastRoot,
      ),
    );

    this.root.setAttribute('data-app-ready', 'false');
  }

  _buildDemoTools() {
    const tools = h('div', { class: 'demo-banner__tools' });
    this.demoButtons = [
      { key: 'demo.approval', text: '运行命令：清理构建目录', title: 'demo.approval' },
      { key: 'demo.question', text: '请选择一个方案', title: 'demo.question' },
    ].map((item) => {
      const button = h(
        'button',
        {
          type: 'button',
          class: 'btn btn--ghost btn--small',
          'data-demo': item.key,
          title: t('demo.tools'),
          onclick: async () => {
            await this._demoSend(item.text);
          },
        },
        t(item.title),
      );
      tools.appendChild(button);
      return button;
    });
    this.demoDisconnectButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--ghost btn--small',
        'data-demo': 'demo.disconnect',
        onclick: () => this.transport?.simulateClose?.({ code: 4408 }),
      },
      t('demo.disconnect'),
    );
    this.demoReconnectButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--ghost btn--small',
        'data-demo': 'demo.reconnect',
        onclick: () => this.controller?.reconnectBridge?.(),
      },
      t('demo.reconnect'),
    );
    this.demoResetButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--ghost btn--small',
        'data-demo': 'demo.reset',
        onclick: () => this.window?.location?.reload?.(),
      },
      t('demo.reset'),
    );
    tools.append(this.demoDisconnectButton, this.demoReconnectButton, this.demoResetButton);
    return tools;
  }

  /* ------------------------------------------------------------- controller */

  _bindController() {
    const controller = this.controller;
    this.unsubscribers.push(
      controller.on('ready', () => this._onReady()),
      controller.on('state', (snapshot) => this._onState(snapshot)),
      controller.on('approval', (request) => this._openApproval(request)),
      controller.on('question', (request) => this._openQuestion(request)),
      controller.on('error', (payload) => this._onError(payload)),
      controller.on('toast', (payload) => this.toast?.show(payload)),
      controller.on('reconnecting', () => this.toast?.show({ key: 'toast.reconnecting' })),
      controller.on('settings', (settings) => this._onSettings(settings)),
      controller.on('logs-exported', (payload) => this._onLogsExported(payload)),
      controller.on('scan-result', (result) => this.toast?.showText(result?.message ?? t('pairing.scanHint'))),
      controller.on('pair-error', (payload) => this._onPairError(payload)),
      controller.on('audio-route', (route) => this._onAudioRoute(route)),
    );
  }

  _bindWindow() {
    const win = this.window;
    if (!win) return;
    this.transport.setBackHandler?.(() => this.handleBack());
    // Native hardware back key.
    try {
      win.DshBridge = win.DshBridge ?? {};
      const previous = win.DshBridge.onBack;
      win.DshBridge.onBack = () => {
        if (typeof previous === 'function' && previous !== this._backHandler) {
          const consumed = previous();
          if (consumed) return true;
        }
        return this.handleBack();
      };
    } catch {
      /* non-fatal */
    }
    // Re-apply the system theme when the OS flips light/dark.
    if (win.matchMedia) {
      this._mediaQuery = win.matchMedia('(prefers-color-scheme: dark)');
      const onChange = () => this._applyTheme(this.controller?.settings?.theme ?? 'system');
      if (this._mediaQuery.addEventListener) this._mediaQuery.addEventListener('change', onChange);
      else if (this._mediaQuery.addListener) this._mediaQuery.addListener(onChange);
    }
    win.addEventListener?.('popstate', () => this.handleBack());
  }

  start() {
    this.root.setAttribute('data-app-ready', 'true');
    const snapshot = this.controller.snapshot();
    const initial = snapshot.online ? SCREEN.SESSIONS : SCREEN.PAIRING;
    this.show(initial);
  }

  /* ----------------------------------------------------------------- router */

  /** Switch to a screen, creating the view and disposing the previous one. */
  show(screen) {
    if (!Object.values(SCREEN).includes(screen)) return;
    if (this.screen === screen && this.view) return;
    this._previousScreen = this.screen ?? SCREEN.SESSIONS;
    this.view?.destroy?.();
    this.view = null;
    clear(this.screenHost);
    this.screen = screen;
    for (const [name, button] of this.navButtons) {
      const active = name === screen;
      setClass(button, 'tabbar__item--active', active);
      button.setAttribute('aria-selected', String(active));
    }
    this.navButtons.get(screen)?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });

    const common = { root: this.screenHost, controller: this.controller, toast: this.toast };
    try {
      switch (screen) {
        case SCREEN.PAIRING:
          this.view = new PairingView({ ...common, onPaired: () => this.show(SCREEN.SESSIONS) });
          break;
        case SCREEN.SESSIONS:
          this.view = new SessionsView({
            ...common,
            onOpen: (sessionId) => this._openChat(sessionId),
            onNew: () => this._newChat(),
          });
          break;
        case SCREEN.CHAT:
          this.view = new ChatView({
            ...common,
            overlay: this.recordingOverlay,
            onBack: () => this.show(SCREEN.SESSIONS),
            onOpenSessions: () => this.show(SCREEN.SESSIONS),
          });
          break;
        case SCREEN.SETTINGS:
          this.view = new SettingsView({
            ...common,
            onOpenDiagnostics: () => this.show(SCREEN.DIAGNOSTICS),
            onOpenPairing: () => this.show(SCREEN.PAIRING),
          });
          break;
        case SCREEN.DIAGNOSTICS:
          this.view = new DiagnosticsView({ ...common, onBack: () => this.show(SCREEN.PAIRING) });
          break;
        default:
          break;
      }
      this.view?.mount?.();
    } catch (error) {
      this.toast?.showText(String(error?.message ?? error));
    }
    this.controller.updateSettings?.({ lastScreen: screen }).catch?.(() => {});
    this._syncShell();
  }

  /** Open an existing session in the chat screen. */
  async _openChat(sessionId) {
    this.show(SCREEN.CHAT);
    await this.controller.openSession(sessionId);
  }

  /** Create an empty session and enter the chat screen. */
  async _newChat() {
    const session = await this.controller.createSession({});
    if (!session) {
      this.show(SCREEN.SESSIONS);
      return;
    }
    this.show(SCREEN.CHAT);
    await this.controller.openSession(session.sessionId);
  }

  /** Browser/Android back handling: sheets first, then up one screen. */
  handleBack() {
    if (hasOpenSheet()) {
      closeTopSheet('back');
      return true;
    }
    if (this.screen && this.screen !== SCREEN.SESSIONS) {
      this.show(this.screen === SCREEN.CHAT ? SCREEN.SESSIONS : SCREEN.SETTINGS);
      return true;
    }
    return false;
  }

  /* ---------------------------------------------------------------- events */

  _onReady() {
    const snapshot = this.controller.snapshot();
    this._applyLanguage(this.controller.settings.lang);
    this._applyTheme(this.controller.settings.theme);
    this._syncShell(snapshot);
    if (!snapshot.online && this.screen === null) this.show(SCREEN.PAIRING);
  }

  _onState(snapshot) {
    this._syncShell(snapshot);
    // Pairing completed (or a stored token reconnected) → sessions list.
    if (snapshot.online && (this.screen === SCREEN.PAIRING || this.screen === null)) {
      this.toast?.show({ key: 'toast.connected' });
      this.show(SCREEN.SESSIONS);
    }
    if (!snapshot.online && this.screen === null) this.show(SCREEN.PAIRING);
  }

  _onSettings(settings) {
    const langChanged = settings.lang !== this._lang;
    const themeChanged = settings.theme !== this._theme;
    this._applyLanguage(settings.lang);
    if (themeChanged) this._applyTheme(settings.theme);
    if (langChanged) {
      this._rebuildShellLabels();
      if (this.screen) {
        const current = this.screen;
        this.screen = null; // force re-creation
        this.show(current);
      }
    } else {
      this._syncShell();
    }
  }

  _onError(payload) {
    if (this.screen === SCREEN.CHAT) return; // the chat view renders an inline banner
    this.toast?.show({ key: 'toast.error' });
    if (payload?.code) this.diagnostics?.recordError?.({ code: payload.code, message: payload.message, retryable: payload.retryable, source: payload.scope });
  }

  _onPairError(payload) {
    if (this.screen !== SCREEN.PAIRING) this.show(SCREEN.PAIRING);
    const code = Number(payload?.code ?? 1002);
    this.toast?.showText(`${t(`error.${code}.title`)} · ${t(`error.${code}.next`)}`);
  }

  _openApproval(request) {
    if (!request) {
      // Local deadline elapsed (deadline-based default deny, FR-602): the
      // controller emits a null request so the sheet must close.
      closeTopSheet('expired');
      return;
    }
    if (hasOpenSheet()) closeTopSheet('replaced');
    const controller = this.controller;
    openApprovalSheet(request, {
      onRespond: (id, outcome) => controller.respondApproval(id, outcome),
    });
  }

  _openQuestion(request) {
    if (!request) {
      closeTopSheet('expired');
      return;
    }
    if (hasOpenSheet()) closeTopSheet('replaced');
    const controller = this.controller;
    openQuestionSheet(request, {
      onRespond: (id, response) => controller.respondQuestion(id, response.answers, { cancelled: response.cancelled }),
    });
  }

  _onLogsExported(payload) {
    if (this.screen !== SCREEN.DIAGNOSTICS) {
      this.toast?.show({ key: 'toast.exported' });
    }
    if (payload?.text && this.screen === SCREEN.DIAGNOSTICS) {
      this.view?._showExportedLogs?.(payload);
    }
  }

  /* ------------------------------------------------------------------- demo */

  async _demoSend(text) {
    if (this.screen !== SCREEN.CHAT) this.show(SCREEN.SESSIONS);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const message = await this.controller.sendText(text);
    if (message && this.controller.activeSessionId) {
      this.show(SCREEN.CHAT);
      await this.controller.openSession(this.controller.activeSessionId);
    }
  }

  _applyDemoMode() {
    const isMock = this.transportKind === TransportKind.MOCK;
    this.demoBanner.hidden = !isMock;
    this.root.setAttribute('data-transport', this.transportKind);
  }

  _applyLanguage(lang) {
    this._lang = lang;
    setLang(lang);
    this.document?.documentElement?.setAttribute('lang', lang);
  }

  _applyTheme(theme) {
    this._theme = theme;
    const root = this.document?.documentElement;
    if (!root) return;
    if (theme === 'light' || theme === 'dark') root.setAttribute('data-theme', theme);
    else root.removeAttribute('data-theme');
    const dark = theme === 'dark' || (theme === 'system' && Boolean(this._mediaQuery?.matches));
    this.document?.querySelector?.('meta[name="theme-color"]')?.setAttribute('content', dark ? '#0b0f14' : '#f6f7f9');
  }

  _rebuildShellLabels() {
    this.brandTitle.textContent = t('app.title');
    this.brandSub.textContent = t('app.subtitle');
    this.demoBanner.querySelector('.demo-banner__title').textContent = t('app.demoBanner');
    this.demoBanner.querySelector('.demo-banner__hint').textContent = t('app.demoHint');
    for (const item of NAV_ITEMS) {
      const button = this.navButtons.get(item.screen);
      button.querySelector('.tabbar__label').textContent = t(item.key);
    }
  }

  _syncShell(snapshot = this.controller.snapshot()) {
    const phase = snapshot.phase;
    this.connChip.textContent = `${t(`phase.${phase}`)}${snapshot.online ? ' · online' : ''}`;
    this.connChip.title = t(`phaseHint.${phase}`);
    this.connChip.className = `chip chip--status ${snapshot.online ? 'chip--online' : phase === 'idle' ? '' : 'chip--warn'}`;
    this._onAudioRoute(snapshot.audioRoute);
    this._syncConnectionBanner(snapshot);
    for (const [name, button] of this.navButtons) {
      const active = name === this.screen;
      setClass(button, 'tabbar__item--active', active);
    }
  }

  /** Bluetooth status bar: active route + device name once known (PRD §12). */
  _onAudioRoute(route) {
    if (!this.routeChip) return;
    const device = route?.deviceName ?? null;
    this.routeChip.hidden = !device;
    this.routeChip.textContent = device ? t('audio.route', { device }) : '';
    this.routeChip.title = route?.preferredUnavailable ? t('audio.preferDevice') : '';
    const headset = route?.route === 'hfp' || route?.route === 'le';
    this.routeChip.className = `chip chip--status route-chip ${headset ? 'chip--online' : ''}`.trim();
  }

  /** Offline / reconnecting banner with an explicit retry action (TC-WEB-01). */
  _syncConnectionBanner(snapshot) {
    if (!this.connBanner) return;
    const phase = snapshot.phase;
    const state = snapshot.connection?.state ?? 'idle';
    const offline = state === 'failed' || state === 'closed' || state === 'reconnecting';
    const connecting = !snapshot.online && (phase === 'connecting' || phase === 'authenticating');
    const visible = offline || connecting;
    this.connBanner.hidden = !visible;
    if (!visible) return;
    const titleKey = state === 'reconnecting' ? 'toast.reconnecting' : `phase.${phase}`;
    this.connBannerTitle.textContent = t(titleKey);
    this.connBannerNext.textContent = t(`phaseHint.${phase}`);
    this.connBannerRetry.hidden = state === 'reconnecting';
  }
}

/* ------------------------------------------------------------------------- */
/* Auto-boot when loaded as a plain page (index.html).                       */
/* ------------------------------------------------------------------------- */

if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  const boot = async () => {
    try {
      const app = await createApp({ document, window });
      window.__dshPhoneApp = app;
      document.documentElement.setAttribute('data-app-booted', 'true');
    } catch (error) {
      const root = document.getElementById('app');
      if (root) {
        root.textContent = `启动失败 / Failed to start: ${error?.message ?? error}`;
      }
      throw error;
    }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true });
  else boot();
}

export { hasNativeBridge, TransportEvent };
