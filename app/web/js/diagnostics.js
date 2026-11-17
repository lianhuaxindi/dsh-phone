/**
 * Diagnostics: sanitized event log, latency statistics and log export.
 *
 * Privacy rules (PRD FR-703/704, NFR): exported logs never contain audio,
 * pairing codes, device tokens, message text or tool arguments. Free text is
 * reduced to a length + non-cryptographic fingerprint so failures can still be
 * correlated, and secret-shaped fields are replaced with `[redacted]`.
 *
 * Pure module: no DOM, injectable clock. The UI renders `snapshot()` and
 * copies `exportText()`.
 */

import { PROTOCOL, textFingerprint } from './protocol.js';
import { realScheduler } from './scheduler.js';

/** Latency sample names shown on the diagnostics page. */
export const LATENCY = Object.freeze({
  ASR_FIRST_PARTIAL: 'asr-first-partial',
  ASR_FINAL: 'asr-final',
  CHAT_FIRST_DELTA: 'chat-first-delta',
  TURN_COMPLETE: 'turn-complete',
});

/** All latency names, in display order. */
export const LATENCY_NAMES = Object.freeze(Object.values(LATENCY));

/** Log levels. */
export const LOG_LEVELS = Object.freeze(['debug', 'info', 'warn', 'error']);

/**
 * Field keys that are always dropped/redacted at ingestion. These may carry
 * user content, credentials, audio or pair codes.
 */
const SECRET_KEY_PATTERN = /(token|secret|password|passwd|hmac|nonce|signature|paircode|pair_code|fingerprint|devicekey|device_key|apikey|api_key)/i;

/** Free-text keys: never stored verbatim, only length + hash. */
const FREE_TEXT_KEY_PATTERN = /^(text|message|detail|reason|content|transcript|preview|args|argspreview|command|answer|title|label|question)$/i;

/** Scalar keys retained verbatim (numbers/booleans/enums). */
const PASS_KEYS = new Set([
  'code',
  'retryable',
  'seq',
  'replyTo',
  'lastSeq',
  'fromSeq',
  'uptoS',
  'phase',
  'state',
  'name',
  'tool',
  'toolCallId',
  'sessionId',
  'messageId',
  'id',
  'route',
  'device',
  'durationMs',
  'finishReason',
  'index',
  'attempt',
  'attemptsLeft',
  'count',
  'level',
  'status',
  'event',
  'source',
  'transport',
  'host',
  'port',
  'lang',
  'mode',
  'needPair',
  'path',
  'bytes',
]);

const MAX_STRING_LEN = 160;
const MAX_ENTRIES = 500;
const MAX_ERRORS = 20;
/** Samples kept per latency metric for p50/p95 (diagnostics page only). */
const LATENCY_SAMPLE_WINDOW = 200;

/**
 * Nearest-rank percentile over an array of numbers (samples may be unsorted).
 * Returns 0 for an empty array.
 * @param {number[]} samples
 * @param {number} ratio 0 < ratio <= 1
 */
export function percentile(samples, ratio) {
  if (!Array.isArray(samples) || samples.length === 0) return 0;
  const sorted = [...samples].sort((a, b) => a - b);
  const rank = Math.min(sorted.length, Math.max(1, Math.ceil(ratio * sorted.length)));
  return sorted[rank - 1];
}

/** Sanitize a free-form string: strip credential/code shapes, cap length. */
export function sanitizeText(value) {
  let text = String(value ?? '');
  // `key=value` pairs that look like credentials or pair codes.
  text = text.replace(
    /\b(token|secret|password|hmac|nonce|signature|pair[_-]?code)\s*[=:]\s*["']?[A-Za-z0-9+/=._-]{4,}/gi,
    '$1=[redacted]',
  );
  // Standalone 6-digit codes in obvious positions.
  text = text.replace(/\b(code)\s*[=:]\s*\d{6}\b/gi, '$1=[redacted]');
  if (text.length > MAX_STRING_LEN) text = `${text.slice(0, MAX_STRING_LEN)}…`;
  return text;
}

function fingerprint(value) {
  const fp = textFingerprint(value);
  return { len: fp.length, hash: fp.hashPrefix };
}

/**
 * Sanitize one field value for storage.
 * @returns {unknown} sanitized primitive, fingerprint object, or `[redacted]`
 */
export function sanitizeField(key, value) {
  if (key === 'code' && typeof value === 'string') {
    // A string "code" is ambiguous: 6-digit pair codes are secret, error codes
    // are not. Keep only the latter.
    return /^\d{1,5}$/.test(value) ? Number(value) : '[redacted]';
  }
  if (SECRET_KEY_PATTERN.test(key)) return '[redacted]';
  if (FREE_TEXT_KEY_PATTERN.test(key)) return fingerprint(value);
  if (PASS_KEYS.has(key)) {
    if (value === null || value === undefined) return null;
    if (typeof value === 'number' || typeof value === 'boolean') return value;
    if (typeof value === 'string') return sanitizeText(value);
    return '[redacted]';
  }
  if (typeof value === 'number' || typeof value === 'boolean') return value;
  if (typeof value === 'string') return sanitizeText(value);
  return '[redacted]';
}

/**
 * Final redaction applied to a stored entry before export. Free text never
 * leaves the device, even though ingestion already reduced it.
 */
export function redactForExport(fields) {
  const out = {};
  for (const [key, value] of Object.entries(fields)) {
    if (SECRET_KEY_PATTERN.test(key)) {
      out[key] = '[redacted]';
      continue;
    }
    if (FREE_TEXT_KEY_PATTERN.test(key)) {
      if (value && typeof value === 'object' && typeof value.hash === 'string') {
        out[`${key}Hash`] = value.hash;
        out[`${key}Len`] = value.len;
      }
      continue;
    }
    out[key] = value;
  }
  return out;
}

/** Format one log entry as a single export line. */
export function formatEntryLine(entry) {
  const time = new Date(entry.at).toISOString();
  const fields = redactForExport(entry.fields);
  const parts = Object.entries(fields).map(([key, value]) => {
    if (value === null || value === undefined) return `${key}=null`;
    if (typeof value === 'object') return `${key}=${JSON.stringify(value)}`;
    return `${key}=${String(value)}`;
  });
  return [time, entry.level.toUpperCase(), sanitizeText(entry.event), ...parts].join(' ');
}

/** Sanitized log + latency + recent errors; the diagnostics page's model. */
export class Diagnostics {
  /**
   * @param {object} [options]
   * @param {string} [options.appVersion]
   * @param {number} [options.protocolVersion]
   * @param {string} [options.buildChannel]
   * @param {import('./scheduler.js').Scheduler} [options.scheduler]
   * @param {number} [options.maxEntries]
   * @param {number} [options.maxErrors]
   */
  constructor({
    appVersion = '',
    protocolVersion = PROTOCOL.version,
    buildChannel = '',
    scheduler = realScheduler,
    maxEntries = MAX_ENTRIES,
    maxErrors = MAX_ERRORS,
  } = {}) {
    this.appVersion = appVersion;
    this.protocolVersion = protocolVersion;
    this.buildChannel = buildChannel;
    this.scheduler = scheduler;
    this.maxEntries = maxEntries;
    this.maxErrors = maxErrors;
    /** @type {Array<{at:number,level:string,event:string,fields:object}>} */
    this._entries = [];
    /** @type {Array<{at:number,code:number,source:string,retryable:boolean,messageFp:object|null}>} */
    this._errors = [];
    /** @type {Map<string,{count:number,last:number,min:number,max:number,sum:number}>} */
    this._latency = new Map();
  }

  /* ---------------------------------------------------------------- logging */

  /**
   * Record one sanitized event.
   * @param {'debug'|'info'|'warn'|'error'} level
   * @param {string} event short snake/kebab-case event name
   * @param {Record<string, unknown>} [fields]
   */
  log(level, event, fields = {}) {
    const safeLevel = LOG_LEVELS.includes(level) ? level : 'info';
    const safeFields = {};
    for (const [key, value] of Object.entries(fields)) {
      safeFields[key] = sanitizeField(key, value);
    }
    const entry = { at: this.scheduler.now(), level: safeLevel, event: String(event), fields: safeFields };
    this._entries.push(entry);
    if (this._entries.length > this.maxEntries) this._entries.splice(0, this._entries.length - this.maxEntries);
    return entry;
  }

  debug(event, fields) {
    return this.log('debug', event, fields);
  }

  info(event, fields) {
    return this.log('info', event, fields);
  }

  warn(event, fields) {
    return this.log('warn', event, fields);
  }

  error(event, fields) {
    return this.log('error', event, fields);
  }

  /**
   * Record a user-visible error (kept separately for the diagnostics page).
   * Only the protocol code and a fingerprint of the message are retained.
   */
  recordError({ code = 0, message = '', retryable = false, source = 'protocol', at } = {}) {
    const entry = {
      at: Number.isFinite(at) ? at : this.scheduler.now(),
      code: Number(code) || 0,
      source: sanitizeText(source),
      retryable: Boolean(retryable),
      messageFp: message ? fingerprint(message) : null,
    };
    this._errors.push(entry);
    if (this._errors.length > this.maxErrors) this._errors.splice(0, this._errors.length - this.maxErrors);
    this.warn('error.recorded', { code: entry.code, retryable: entry.retryable, source: entry.source });
    return entry;
  }

  /* ---------------------------------------------------------------- latency */

  /** Record a latency sample in milliseconds. */
  latency(name, ms) {
    const value = Number(ms);
    if (!Number.isFinite(value) || value < 0) return null;
    const current = this._latency.get(name) ?? { count: 0, last: 0, min: Infinity, max: 0, sum: 0, samples: [] };
    current.count += 1;
    current.last = value;
    current.min = Math.min(current.min, value);
    current.max = Math.max(current.max, value);
    current.sum += value;
    // Keep a bounded window for percentiles (p50/p95, PRD FR-702 / TC-WEB-13).
    current.samples.push(value);
    if (current.samples.length > LATENCY_SAMPLE_WINDOW) current.samples.splice(0, current.samples.length - LATENCY_SAMPLE_WINDOW);
    this._latency.set(name, current);
    this.debug('latency.sample', { name, durationMs: Math.round(value) });
    return current;
  }

  /** Latency statistics map, `{name: {count,last,min,max,avg,p50,p95}}`. */
  latencyStats() {
    const out = {};
    for (const [name, stats] of this._latency) {
      out[name] = {
        count: stats.count,
        last: Math.round(stats.last),
        min: Math.round(stats.min),
        max: Math.round(stats.max),
        avg: Math.round(stats.sum / stats.count),
        p50: Math.round(percentile(stats.samples, 0.5)),
        p95: Math.round(percentile(stats.samples, 0.95)),
      };
    }
    return out;
  }

  /* ------------------------------------------------------------------ query */

  /** Recent log entries (copy). */
  entries() {
    return this._entries.map((entry) => ({ ...entry, fields: { ...entry.fields } }));
  }

  /** Recent errors (copy). */
  recentErrors() {
    return this._errors.map((entry) => ({ ...entry, messageFp: entry.messageFp ? { ...entry.messageFp } : null }));
  }

  /** Last recorded error, or null. */
  lastError() {
    return this._errors.length > 0 ? { ...this._errors[this._errors.length - 1] } : null;
  }

  /** Everything the diagnostics page needs in one object. */
  snapshot() {
    return {
      appVersion: this.appVersion,
      protocolVersion: this.protocolVersion,
      buildChannel: this.buildChannel,
      latency: this.latencyStats(),
      errors: this.recentErrors(),
      entries: this.entries(),
      entryCount: this._entries.length,
      lastError: this.lastError(),
    };
  }

  /* ----------------------------------------------------------------- export */

  /**
   * Sanitized plain-text log bundle for export/paste.
   * Contains versions, latency summary, recent error codes and event lines.
   */
  exportText() {
    const lines = [];
    lines.push('# DSH Phone diagnostics export');
    lines.push(`# generated=${new Date(this.scheduler.now()).toISOString()}`);
    lines.push(`# app=${this.appVersion || 'unknown'} protocolVersion=${this.protocolVersion}`);
    if (this.buildChannel) lines.push(`# buildChannel=${sanitizeText(this.buildChannel)}`);
    lines.push('# Note: audio, pairing codes, tokens and message text are never logged.');
    lines.push('# latency (name count avg p50 p95 min max last ms):');
    const stats = this.latencyStats();
    const names = Object.keys(stats);
    if (names.length === 0) lines.push('#   (no samples)');
    for (const name of names) {
      const s = stats[name];
      lines.push(`#   ${name} ${s.count} ${s.avg} ${s.p50} ${s.p95} ${s.min} ${s.max} ${s.last}`);
    }
    lines.push('# errors (time code retryable source messageHash messageLen):');
    if (this._errors.length === 0) lines.push('#   (none)');
    for (const error of this._errors) {
      const fp = error.messageFp ?? { hash: '-', len: 0 };
      lines.push(`#   ${new Date(error.at).toISOString()} ${error.code} ${error.retryable} ${error.source} ${fp.hash} ${fp.len}`);
    }
    lines.push('# events:');
    for (const entry of this._entries) lines.push(formatEntryLine(entry));
    return lines.join('\n');
  }

  /** Clear all logs/latency/errors (export happens first when the user asks). */
  clear() {
    this._entries = [];
    this._errors = [];
    this._latency = new Map();
  }
}
