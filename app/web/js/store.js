/**
 * Local persistence (ARCHITECTURE §3.4, D-010).
 *
 * On a device, sessions/messages/settings live in WebView IndexedDB; the device
 * token stays in Android Keystore behind the JsBridge and is never handled
 * here. In Node (tests) and browsers without IndexedDB a memory backend with
 * the same async interface is used.
 *
 * Both backends expose the same methods and are covered by the same tests:
 *   getSettings() / saveSettings(partial)
 *   getDeviceId()
 *   listSessions() / saveSession() / deleteSession()
 *   getMessages(sessionId) / saveMessage() / saveMessages() / clearSessionMessages()
 *   getDraft(sessionId) / saveDraft(sessionId, text) / deleteDraft(sessionId)
 *   getMedia(mediaId) / saveMedia(media) / deleteMedia(mediaId)  (v1.1 image cache)
 *   clearAll()
 */

import { DEFAULT_SETTINGS, LANGUAGES, THEMES, SILENCE_OPTIONS, AUDIO_DEVICES, AUDIO_MODES, TransportMode } from './config.js';
import { PROTOCOL, randomUuid } from './protocol.js';

const DB_NAME = 'dsh-phone';
const DB_VERSION = 1;
const STORE_META = 'meta';
const STORE_SESSIONS = 'sessions';
const STORE_MESSAGES = 'messages';

const VALID_TRANSPORTS = new Set(Object.values(TransportMode));

function clampNumber(value, min, max, fallback) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, Math.round(n)));
}

/** The screens a persisted `lastScreen` may point at. */
export const SCREENS = Object.freeze(['pairing', 'sessions', 'chat', 'settings', 'diagnostics']);

/**
 * Merge `partial` settings over the defaults, validating every field so a
 * corrupt store can never break the UI.
 */
export function normalizeSettings(partial) {
  const p = partial && typeof partial === 'object' ? partial : {};
  const merged = { ...DEFAULT_SETTINGS, ...p };

  const lang = LANGUAGES.includes(merged.lang) ? merged.lang : DEFAULT_SETTINGS.lang;
  const theme = THEMES.includes(merged.theme) ? merged.theme : DEFAULT_SETTINGS.theme;
  const silence = Number(merged.silenceMs);
  const silenceMs = SILENCE_OPTIONS.includes(silence)
    ? silence
    : clampNumber(silence, 200, 3000, DEFAULT_SETTINGS.silenceMs);
  const preferAudioDevice = AUDIO_DEVICES.includes(merged.preferAudioDevice)
    ? merged.preferAudioDevice
    : DEFAULT_SETTINGS.preferAudioDevice;
  const transportMode = VALID_TRANSPORTS.has(merged.transportMode) ? merged.transportMode : DEFAULT_SETTINGS.transportMode;
  const host = typeof merged.host === 'string' && merged.host.trim() ? merged.host.trim() : DEFAULT_SETTINGS.host;
  const port = Number.isInteger(Number(merged.port)) && Number(merged.port) >= 1 && Number(merged.port) <= 65535
    ? Number(merged.port)
    : DEFAULT_SETTINGS.port;
  const fp = typeof merged.fp === 'string' && /^[0-9a-fA-F]{0,64}$/.test(merged.fp.trim()) ? merged.fp.trim().toLowerCase() : DEFAULT_SETTINGS.fp;
  const deviceName =
    typeof merged.deviceName === 'string' && merged.deviceName.trim() ? merged.deviceName.trim() : DEFAULT_SETTINGS.deviceName;
  const lastScreen = SCREENS.includes(merged.lastScreen) ? merged.lastScreen : DEFAULT_SETTINGS.lastScreen;
  const audioMode = AUDIO_MODES.includes(merged.audioMode) ? merged.audioMode : AUDIO_MODES[0];

  return {
    lang,
    theme,
    autoSend: Boolean(merged.autoSend),
    silenceMs,
    preferAudioDevice,
    transportMode,
    host,
    port,
    fp,
    deviceName,
    audioMode,
    paired: Boolean(merged.paired),
    lastScreen,
    lastActiveSessionId: typeof merged.lastActiveSessionId === 'string' ? merged.lastActiveSessionId : null,
    model: typeof merged.model === 'string' ? merged.model : DEFAULT_SETTINGS.model ?? 'default',
  };
}

/** Validate/normalize a session record before persisting. */
export function normalizeSessionRecord(raw) {
  if (!raw || typeof raw.sessionId !== 'string' || !raw.sessionId) throw new Error('session.sessionId is required');
  return {
    sessionId: raw.sessionId,
    title: typeof raw.title === 'string' ? raw.title : '',
    updatedAt: Number(raw.updatedAt) || 0,
    lastPreview: typeof raw.lastPreview === 'string' ? raw.lastPreview : '',
  };
}

/** Validate/normalize a message record before persisting. */
export function normalizeMessageRecord(raw) {
  if (!raw || typeof raw.messageId !== 'string' || !raw.messageId) throw new Error('message.messageId is required');
  if (typeof raw.sessionId !== 'string' || !raw.sessionId) throw new Error('message.sessionId is required');
  return {
    messageId: raw.messageId,
    sessionId: raw.sessionId,
    role: raw.role === 'user' || raw.role === 'assistant' || raw.role === 'system' || raw.role === 'tool' ? raw.role : 'assistant',
    text: typeof raw.text === 'string' ? raw.text : '',
    status: raw.status === 'streaming' || raw.status === 'error' ? raw.status : 'done',
    createdAt: Number(raw.createdAt) || 0,
    ...(raw.clientId ? { clientId: String(raw.clientId) } : {}),
  };
}

/**
 * Normalize a composer draft: trimmed of trailing whitespace, capped at the
 * protocol text limit; an empty draft means "no draft".
 * @param {unknown} text
 */
export function normalizeDraft(text) {
  const value = typeof text === 'string' ? text : '';
  if (!value.trim()) return '';
  return value.length > PROTOCOL.maxTextChars ? value.slice(0, PROTOCOL.maxTextChars) : value;
}

/**
 * Normalize a cached media record (v1.1 `media.get` result, D-029). Records
 * without a media id or base64 payload are dropped by the callers.
 * @param {unknown} raw
 * @returns {{mediaId: string, mimeType: string, data: string, width: number, height: number, sizeBytes: number} | null}
 */
export function normalizeMediaCacheRecord(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const mediaId = typeof raw.mediaId === 'string' ? raw.mediaId.trim() : '';
  const data = typeof raw.data === 'string' ? raw.data : '';
  if (!mediaId || !data) return null;
  return {
    mediaId,
    mimeType: typeof raw.mimeType === 'string' && /^image\/[a-z0-9.+-]+$/i.test(raw.mimeType) ? raw.mimeType.toLowerCase() : 'image/jpeg',
    data,
    width: Number.isInteger(raw.width) && raw.width > 0 ? raw.width : 0,
    height: Number.isInteger(raw.height) && raw.height > 0 ? raw.height : 0,
    sizeBytes: Math.max(0, Math.round(Number(raw.sizeBytes) || Math.round((data.length * 3) / 4))),
  };
}

function byUpdatedDesc(a, b) {
  return b.updatedAt - a.updatedAt;
}

function byCreatedAsc(a, b) {
  return a.createdAt - b.createdAt;
}

/* ------------------------------------------------------------------------- */
/* Memory backend                                                             */
/* ------------------------------------------------------------------------- */

/** In-memory store with the same async API as the IndexedDB backend. */
export class MemoryStore {
  /** @param {{settings?: object, sessions?: object[], messages?: object[], drafts?: object, media?: object[]}} [seed] */
  constructor(seed = {}) {
    this.backend = 'memory';
    this._settings = normalizeSettings(seed.settings);
    this._deviceId = seed.deviceId ?? null;
    this._sessions = new Map();
    this._messages = new Map();
    this._drafts = new Map();
    this._media = new Map();
    for (const [sessionId, text] of Object.entries(seed.drafts ?? {})) {
      const draft = normalizeDraft(text);
      if (draft) this._drafts.set(String(sessionId), draft);
    }
    for (const raw of seed.media ?? []) {
      const record = normalizeMediaCacheRecord(raw);
      if (record) this._media.set(record.mediaId, record);
    }
    for (const session of seed.sessions ?? []) {
      const record = normalizeSessionRecord(session);
      this._sessions.set(record.sessionId, record);
    }
    for (const message of seed.messages ?? []) {
      const record = normalizeMessageRecord(message);
      this._messages.set(record.messageId, record);
    }
  }

  async getSettings() {
    return { ...this._settings };
  }

  async saveSettings(partial) {
    this._settings = normalizeSettings({ ...this._settings, ...(partial ?? {}) });
    return { ...this._settings };
  }

  async getDeviceId() {
    if (!this._deviceId) this._deviceId = randomUuid();
    return this._deviceId;
  }

  async setDeviceId(deviceId) {
    this._deviceId = String(deviceId);
    return this._deviceId;
  }

  async listSessions() {
    return [...this._sessions.values()].map((s) => ({ ...s })).sort(byUpdatedDesc);
  }

  async saveSession(session) {
    const record = normalizeSessionRecord(session);
    this._sessions.set(record.sessionId, record);
    return { ...record };
  }

  async deleteSession(sessionId) {
    this._sessions.delete(sessionId);
    this._drafts.delete(sessionId);
    for (const [messageId, message] of this._messages) {
      if (message.sessionId === sessionId) this._messages.delete(messageId);
    }
  }

  async getMessages(sessionId) {
    return [...this._messages.values()]
      .filter((message) => message.sessionId === sessionId)
      .map((m) => ({ ...m }))
      .sort(byCreatedAsc);
  }

  async saveMessage(message) {
    const record = normalizeMessageRecord(message);
    this._messages.set(record.messageId, record);
    return { ...record };
  }

  async saveMessages(messages) {
    for (const message of messages) await this.saveMessage(message);
    return messages.length;
  }

  async clearSessionMessages(sessionId) {
    let removed = 0;
    for (const [messageId, message] of this._messages) {
      if (message.sessionId === sessionId) {
        this._messages.delete(messageId);
        removed += 1;
      }
    }
    return removed;
  }

  /** Composer draft for a session (empty string when none). */
  async getDraft(sessionId) {
    return this._drafts.get(String(sessionId)) ?? '';
  }

  /** Persist a composer draft; an empty text clears it. */
  async saveDraft(sessionId, text) {
    const draft = normalizeDraft(text);
    if (draft) this._drafts.set(String(sessionId), draft);
    else this._drafts.delete(String(sessionId));
    return draft;
  }

  async deleteDraft(sessionId) {
    this._drafts.delete(String(sessionId));
  }

  /** Cached image bytes for a message `mediaId` (null when not cached). */
  async getMedia(mediaId) {
    const record = this._media.get(String(mediaId));
    return record ? { ...record } : null;
  }

  /** Cache image bytes; invalid records are ignored. */
  async saveMedia(media) {
    const record = normalizeMediaCacheRecord(media);
    if (!record) return null;
    this._media.set(record.mediaId, record);
    return { ...record };
  }

  async deleteMedia(mediaId) {
    this._media.delete(String(mediaId));
  }

  async clearAll() {
    this._sessions.clear();
    this._messages.clear();
    this._drafts.clear();
    this._media.clear();
    this._settings = normalizeSettings();
    this._deviceId = null;
  }
}

/* ------------------------------------------------------------------------- */
/* IndexedDB backend                                                          */
/* ------------------------------------------------------------------------- */

function requestToPromise(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('IndexedDB request failed'));
  });
}

function txDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error('IndexedDB transaction failed'));
    transaction.onabort = () => reject(transaction.error ?? new Error('IndexedDB transaction aborted'));
  });
}

/** IndexedDB store used inside the Android WebView. */
export class IndexedDbStore {
  /** @param {{indexedDB?: IDBFactory, fallback?: MemoryStore}} [options] */
  constructor({ indexedDB = globalThis.indexedDB, fallback = new MemoryStore() } = {}) {
    this.backend = 'indexeddb';
    this._factory = indexedDB;
    this._fallback = fallback;
    this._dbPromise = null;
    this._degraded = false;
  }

  /** True when IndexedDB was unavailable and the memory fallback is in use. */
  get degraded() {
    return this._degraded;
  }

  async #db() {
    if (this._degraded) return null;
    if (!this._dbPromise) {
      this._dbPromise = new Promise((resolve) => {
        let request;
        try {
          request = this._factory.open(DB_NAME, DB_VERSION);
        } catch {
          resolve(null);
          return;
        }
        request.onupgradeneeded = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains(STORE_META)) db.createObjectStore(STORE_META, { keyPath: 'key' });
          if (!db.objectStoreNames.contains(STORE_SESSIONS)) db.createObjectStore(STORE_SESSIONS, { keyPath: 'sessionId' });
          if (!db.objectStoreNames.contains(STORE_MESSAGES)) {
            const messages = db.createObjectStore(STORE_MESSAGES, { keyPath: 'messageId' });
            messages.createIndex('bySession', 'sessionId', { unique: false });
          }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => resolve(null);
        request.onblocked = () => resolve(null);
      });
    }
    const db = await this._dbPromise;
    if (!db) {
      this._degraded = true;
      return null;
    }
    return db;
  }

  async #withStore(name, mode, fn) {
    const db = await this.#db();
    if (!db) return fn(null);
    const transaction = db.transaction(name, mode);
    const store = transaction.objectStore(name);
    const result = fn(store);
    await txDone(transaction);
    return result;
  }

  async getSettings() {
    const db = await this.#db();
    if (!db) return this._fallback.getSettings();
    try {
      const row = await requestToPromise(db.transaction(STORE_META, 'readonly').objectStore(STORE_META).get('settings'));
      return normalizeSettings(row?.value);
    } catch {
      return this._fallback.getSettings();
    }
  }

  async saveSettings(partial) {
    const current = await this.getSettings();
    const next = normalizeSettings({ ...current, ...(partial ?? {}) });
    const db = await this.#db();
    if (!db) return this._fallback.saveSettings(next);
    try {
      const transaction = db.transaction(STORE_META, 'readwrite');
      transaction.objectStore(STORE_META).put({ key: 'settings', value: next });
      await txDone(transaction);
    } catch {
      return this._fallback.saveSettings(next);
    }
    return next;
  }

  async getDeviceId() {
    const db = await this.#db();
    if (!db) {
      const existing = await this._fallback.getDeviceId();
      return existing;
    }
    try {
      const row = await requestToPromise(db.transaction(STORE_META, 'readonly').objectStore(STORE_META).get('deviceId'));
      if (row?.value) return String(row.value);
      const created = randomUuid();
      const transaction = db.transaction(STORE_META, 'readwrite');
      transaction.objectStore(STORE_META).put({ key: 'deviceId', value: created });
      await txDone(transaction);
      return created;
    } catch {
      return this._fallback.getDeviceId();
    }
  }

  async setDeviceId(deviceId) {
    const db = await this.#db();
    if (!db) return this._fallback.setDeviceId(deviceId);
    const transaction = db.transaction(STORE_META, 'readwrite');
    transaction.objectStore(STORE_META).put({ key: 'deviceId', value: String(deviceId) });
    await txDone(transaction);
    return String(deviceId);
  }

  async listSessions() {
    const db = await this.#db();
    if (!db) return this._fallback.listSessions();
    try {
      const rows = await requestToPromise(db.transaction(STORE_SESSIONS, 'readonly').objectStore(STORE_SESSIONS).getAll());
      return rows.map(normalizeSessionRecord).sort(byUpdatedDesc);
    } catch {
      return this._fallback.listSessions();
    }
  }

  async saveSession(session) {
    const record = normalizeSessionRecord(session);
    const db = await this.#db();
    if (!db) return this._fallback.saveSession(record);
    const transaction = db.transaction(STORE_SESSIONS, 'readwrite');
    transaction.objectStore(STORE_SESSIONS).put(record);
    await txDone(transaction);
    return { ...record };
  }

  async deleteSession(sessionId) {
    const db = await this.#db();
    if (!db) return this._fallback.deleteSession(sessionId);
    await this.#withStore(STORE_SESSIONS, 'readwrite', (store) => store.delete(sessionId));
    await this.clearSessionMessages(sessionId);
    await this.deleteDraft(sessionId);
  }

  async getMessages(sessionId) {
    const db = await this.#db();
    if (!db) return this._fallback.getMessages(sessionId);
    try {
      const store = db.transaction(STORE_MESSAGES, 'readonly').objectStore(STORE_MESSAGES);
      const index = store.index('bySession');
      const rows = await requestToPromise(index.getAll(sessionId));
      return rows.map(normalizeMessageRecord).sort(byCreatedAsc);
    } catch {
      return this._fallback.getMessages(sessionId);
    }
  }

  async saveMessage(message) {
    const record = normalizeMessageRecord(message);
    const db = await this.#db();
    if (!db) return this._fallback.saveMessage(record);
    const transaction = db.transaction(STORE_MESSAGES, 'readwrite');
    transaction.objectStore(STORE_MESSAGES).put(record);
    await txDone(transaction);
    return { ...record };
  }

  async saveMessages(messages) {
    const db = await this.#db();
    if (!db) return this._fallback.saveMessages(messages);
    const transaction = db.transaction(STORE_MESSAGES, 'readwrite');
    const store = transaction.objectStore(STORE_MESSAGES);
    for (const message of messages) store.put(normalizeMessageRecord(message));
    await txDone(transaction);
    return messages.length;
  }

  async clearSessionMessages(sessionId) {
    const db = await this.#db();
    if (!db) return this._fallback.clearSessionMessages(sessionId);
    try {
      const db2 = db;
      const transaction = db2.transaction(STORE_MESSAGES, 'readwrite');
      const store = transaction.objectStore(STORE_MESSAGES);
      const index = store.index('bySession');
      const keys = await requestToPromise(index.getAllKeys(sessionId));
      let removed = 0;
      for (const key of keys) {
        store.delete(key);
        removed += 1;
      }
      await txDone(transaction);
      return removed;
    } catch {
      return this._fallback.clearSessionMessages(sessionId);
    }
  }

  async clearAll() {
    const db = await this.#db();
    if (!db) return this._fallback.clearAll();
    for (const name of [STORE_META, STORE_SESSIONS, STORE_MESSAGES]) {
      await this.#withStore(name, 'readwrite', (store) => store.clear());
    }
    this._deviceId = null;
  }

  /* -------------------------------------------------------------- drafts */

  async getDraft(sessionId) {
    const key = `draft:${String(sessionId)}`;
    const db = await this.#db();
    if (!db) return this._fallback.getDraft(sessionId);
    try {
      const row = await requestToPromise(db.transaction(STORE_META, 'readonly').objectStore(STORE_META).get(key));
      return typeof row?.value === 'string' ? row.value : '';
    } catch {
      return this._fallback.getDraft(sessionId);
    }
  }

  async saveDraft(sessionId, text) {
    const draft = normalizeDraft(text);
    const key = `draft:${String(sessionId)}`;
    const db = await this.#db();
    if (!db) return this._fallback.saveDraft(sessionId, draft);
    try {
      const transaction = db.transaction(STORE_META, 'readwrite');
      const store = transaction.objectStore(STORE_META);
      if (draft) store.put({ key, value: draft });
      else store.delete(key);
      await txDone(transaction);
    } catch {
      return this._fallback.saveDraft(sessionId, draft);
    }
    return draft;
  }

  async deleteDraft(sessionId) {
    return this.saveDraft(sessionId, '');
  }

  /* --------------------------------------------------------------- media */

  async getMedia(mediaId) {
    const key = `media:${String(mediaId)}`;
    const db = await this.#db();
    if (!db) return this._fallback.getMedia(mediaId);
    try {
      const row = await requestToPromise(db.transaction(STORE_META, 'readonly').objectStore(STORE_META).get(key));
      return normalizeMediaCacheRecord(row?.value);
    } catch {
      return this._fallback.getMedia(mediaId);
    }
  }

  async saveMedia(media) {
    const record = normalizeMediaCacheRecord(media);
    if (!record) return null;
    const key = `media:${record.mediaId}`;
    const db = await this.#db();
    if (!db) return this._fallback.saveMedia(record);
    try {
      const transaction = db.transaction(STORE_META, 'readwrite');
      transaction.objectStore(STORE_META).put({ key, value: record });
      await txDone(transaction);
    } catch {
      return this._fallback.saveMedia(record);
    }
    return { ...record };
  }

  async deleteMedia(mediaId) {
    const key = `media:${String(mediaId)}`;
    const db = await this.#db();
    if (!db) return this._fallback.deleteMedia(mediaId);
    try {
      await this.#withStore(STORE_META, 'readwrite', (store) => store.delete(key));
    } catch {
      return this._fallback.deleteMedia(mediaId);
    }
  }
}

/**
 * Create the best available store.
 * @param {{indexedDB?: IDBFactory}} [options]
 * @returns {MemoryStore|IndexedDbStore}
 */
export function createStore({ indexedDB = globalThis.indexedDB } = {}) {
  if (indexedDB && typeof indexedDB.open === 'function') return new IndexedDbStore({ indexedDB });
  return new MemoryStore();
}
