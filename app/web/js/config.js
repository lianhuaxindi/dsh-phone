/**
 * Static app configuration and defaults.
 *
 * Everything here is data only — no DOM access — so the module can be imported
 * by Node unit tests and by the WebView at runtime.
 *
 * @see docs/PRD.md §7.7 (settings), docs/ARCHITECTURE.md §3.4 (storage)
 */

/** Human readable app version, shown on the diagnostics screen. */
export const APP_VERSION = '1.0.0-web.1';

/** Build channel marker used by the diagnostics export header. */
export const BUILD_CHANNEL = 'webview';

/** Devices can talk to the bridge in one of four ways. */
export const TransportMode = Object.freeze({
  /** Pick native when the JsBridge exists, otherwise mock (browser demo). */
  AUTO: 'auto',
  /** Android WebView: WSS/TLS/audio are owned by the Kotlin shell. */
  NATIVE: 'native',
  /** Browser/WebView direct WebSocket (desktop debug against a real bridge). */
  WS: 'ws',
  /** Scripted fake bridge, used for the browser demo and unit tests. */
  MOCK: 'mock',
});

/** Supported UI languages (BCP-47 primary subtags). */
export const LANGUAGES = Object.freeze(['zh', 'en']);

/** Theme preference values; `system` follows `prefers-color-scheme`. */
export const THEMES = Object.freeze(['system', 'light', 'dark']);

/** Choices for the VAD silence threshold, milliseconds (PRD FR-404). */
export const SILENCE_OPTIONS = Object.freeze([300, 500, 800, 1200]);

/** Preferred audio route values (PRD FR-505). */
export const AUDIO_DEVICES = Object.freeze(['auto', 'phone', 'hfp', 'le']);

/** Recording input modes (PROTOCOL §10.1). */
export const AUDIO_MODES = Object.freeze(['ptt', 'continuous']);

/** Default settings persisted in IndexedDB (merged with stored values). */
export const DEFAULT_SETTINGS = Object.freeze({
  lang: 'zh',
  theme: 'system',
  autoSend: true,
  silenceMs: 500,
  preferAudioDevice: 'auto',
  transportMode: TransportMode.AUTO,
  host: '127.0.0.1',
  port: 8765,
  fp: '',
  deviceName: 'DSH Phone Web',
  paired: false,
  lastScreen: 'sessions',
});

/** Delay before auto reconnect retry (protocol §7): 1,2,4,8,15,15… seconds. */
export const RECONNECT_DELAYS_MS = Object.freeze([1000, 2000, 4000, 8000, 15000]);

/** Jitter factor applied to reconnect delays (±20%). */
export const RECONNECT_JITTER = 0.2;
