/**
 * Recording overlay — press-to-talk feedback (PRD FR-401, §12).
 *
 * Shows the Bluetooth/SCO preheat ("连接耳机中…"), a waveform driven by
 * `audio.level`, the elapsed timer, the incremental ASR text and the
 * swipe-up-to-cancel hint. It owns no gestures itself; the chat view tracks the
 * pointer and calls `setCancelArmed()`.
 */

import { h, clear, clamp, formatClock } from './dom.js';
import { t } from '../i18n.js';

const BAR_COUNT = 27;

/** Overlay states. */
export const RecordingViewState = Object.freeze({
  IDLE: 'idle',
  PREHEATING: 'preheating',
  RECORDING: 'recording',
  STOPPING: 'stopping',
});

/**
 * Full-screen recording overlay.
 */
export class RecordingOverlay {
  /** @param {{root?: HTMLElement|null}} [options] */
  constructor({ root = null } = {}) {
    this.root = root;
    this.visible = false;
    this._level = 0;
    this._elapsedMs = 0;
    this._state = RecordingViewState.IDLE;
    this._partial = '';
    this._cancelArmed = false;
    this._raf = null;
    this._phase = 0;
    this._deviceName = null;
    this._route = null;
    this._build();
  }

  _build() {
    if (typeof document === 'undefined') return;
    this.bars = [];
    this.waveform = h('div', { class: 'recording__waveform', 'aria-hidden': 'true' });
    for (let index = 0; index < BAR_COUNT; index += 1) {
      const bar = h('span', { class: 'recording__bar' });
      this.bars.push(bar);
      this.waveform.appendChild(bar);
    }
    this.status = h('p', { class: 'recording__status' });
    this.timer = h('p', { class: 'recording__timer mono', 'aria-live': 'off' });
    this.partial = h('p', { class: 'recording__partial', 'aria-live': 'polite' });
    this.hint = h('p', { class: 'recording__hint' });
    this.route = h('p', { class: 'recording__route' });
    this.card = h(
      'div',
      { class: 'recording__card', role: 'dialog', 'aria-modal': 'true', 'aria-label': t('recording.overlayLabel') },
      h('div', { class: 'recording__spinner', 'aria-hidden': 'true' }),
      this.status,
      this.waveform,
      this.timer,
      this.partial,
      this.route,
      this.hint,
    );
    this.overlay = h('div', { class: 'recording' }, this.card);
    if (this.root) clear(this.root).appendChild(this.overlay);
    else document.body.appendChild(this.overlay);
    this.overlay.classList.remove('recording--visible');
  }

  /** Mount into a host element (or body once when no host was provided). */
  mount(root) {
    this.root = root;
    if (this.overlay && this.overlay.parentNode !== root) {
      root.appendChild(this.overlay);
    }
  }

  /** True while the overlay is on screen. */
  get isOpen() {
    return this.visible;
  }

  open() {
    if (typeof document === 'undefined' || !this.overlay) return;
    this.visible = true;
    this.overlay.classList.add('recording--visible');
    this.overlay.setAttribute('aria-hidden', 'false');
    this._startAnimation();
  }

  close() {
    this.visible = false;
    if (!this.overlay) return;
    this.overlay.classList.remove('recording--visible');
    this.overlay.setAttribute('aria-hidden', 'true');
    this._level = 0;
    this._stopAnimation();
  }

  /**
   * Render a complete overlay state.
   * @param {{state?: string, elapsedMs?: number, level?: number, partial?: string, cancelArmed?: boolean, deviceName?: string|null, route?: string|null}} patch
   */
  update(patch = {}) {
    if (patch.state !== undefined) this._state = patch.state;
    if (patch.elapsedMs !== undefined) this._elapsedMs = patch.elapsedMs;
    if (patch.level !== undefined) this._level = clamp(Number(patch.level) || 0, 0, 1);
    if (patch.partial !== undefined) this._partial = patch.partial;
    if (patch.cancelArmed !== undefined) this._cancelArmed = Boolean(patch.cancelArmed);
    if (patch.deviceName !== undefined) this._deviceName = patch.deviceName;
    if (patch.route !== undefined) this._route = patch.route;
    this._render();
  }

  _render() {
    if (typeof document === 'undefined' || !this.overlay) return;
    const state = this._state;
    const preheating = state === RecordingViewState.PREHEATING;
    this.overlay.classList.toggle('recording--preheating', preheating);
    this.overlay.classList.toggle('recording--recording', state === RecordingViewState.RECORDING);
    this.overlay.classList.toggle('recording--cancel-armed', this._cancelArmed);

    if (preheating) {
      this.status.textContent = t('recording.connectingHeadset');
      this.status.hidden = false;
      this.timer.textContent = formatClock(this._elapsedMs);
    } else if (state === RecordingViewState.RECORDING || state === RecordingViewState.STOPPING) {
      this.status.textContent = state === RecordingViewState.STOPPING ? t('recording.finishing') : t('recording.listening');
      this.status.hidden = false;
      this.timer.textContent = formatClock(this._elapsedMs);
    } else {
      this.status.textContent = '';
      this.status.hidden = true;
    }

    this.timer.hidden = preheating;
    this.partial.textContent = this._partial;
    this.partial.hidden = !this._partial;
    this.hint.textContent = this._cancelArmed ? t('recording.releaseToCancel') : t('recording.cancelHint');
    this.hint.classList.toggle('recording__hint--cancel', this._cancelArmed);

    const routeKey = this._route === 'hfp' || this._route === 'le' ? 'recording.viaHeadset' : 'recording.viaPhone';
    this.route.textContent = this._deviceName ? t(routeKey, { device: this._deviceName }) : '';
    this.route.hidden = !this._deviceName;

    if (state === RecordingViewState.IDLE) this.close();
  }

  _startAnimation() {
    if (typeof requestAnimationFrame !== 'function' || this._raf !== null) return;
    const tick = () => {
      if (!this.visible) {
        this._raf = null;
        return;
      }
      this._phase += 0.12;
      const base = this._state === RecordingViewState.RECORDING ? 0.16 + this._level * 0.84 : 0.06;
      for (let index = 0; index < this.bars.length; index += 1) {
        const wave = Math.sin(this._phase + index * 0.55) * 0.5 + 0.5;
        const spike = Math.sin(this._phase * 2.3 + index * 1.7) * 0.5 + 0.5;
        const scale = clamp(base * (0.35 + wave * 0.65) + spike * base * 0.35, 0.08, 1);
        this.bars[index].style.transform = `scaleY(${scale.toFixed(3)})`;
      }
      this._raf = requestAnimationFrame(tick);
    };
    this._raf = requestAnimationFrame(tick);
  }

  _stopAnimation() {
    if (this._raf !== null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(this._raf);
    this._raf = null;
  }

  /** Remove the overlay from the DOM. */
  destroy() {
    this._stopAnimation();
    this.overlay?.remove();
    this.overlay = null;
  }
}
