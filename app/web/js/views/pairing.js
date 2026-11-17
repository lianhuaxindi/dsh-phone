/**
 * Pairing screen (PRD FR-101..107).
 *
 * M2 pairing is manual: host / port / 64-hex fingerprint / 6-digit code, plus
 * a "scan" placeholder that calls the native `scanPairing` op (unsupported in
 * M2 → we tell the user to enter the fields manually). A full
 * `dsphone://pair?...` URI can also be pasted into the "server" field.
 */

import { h, byId, setClass, setHidden, clear } from '../ui/dom.js';
import { t, errorCopy } from '../i18n.js';
import { validatePairingField } from '../protocol.js';
import { MOCK_PAIRING } from '../mock.js';
import { TransportKind } from '../transport.js';

export class PairingView {
  /**
   * @param {{root: HTMLElement, controller: object, toast: object, onPaired?: Function}} options
   */
  constructor({ root, controller, toast, onPaired = null }) {
    this.root = root;
    this.controller = controller;
    this.toast = toast;
    this.onPaired = onPaired;
    this.unsubscribers = [];
    this._build();
  }

  _build() {
    clear(this.root);
    this.status = h('p', { class: 'pairing__status', role: 'status' });
    this.hint = h('p', { class: 'pairing__hint' });
    this.error = h('div', { class: 'banner banner--danger', role: 'alert' });
    setHidden(this.error, true);
    this.errorTitle = h('strong', { class: 'banner__title' });
    this.errorNext = h('span', { class: 'banner__next' });
    this.error.append(this.errorTitle, this.errorNext);

    const demo = this.controller.transportKind === TransportKind.MOCK;
    this.host = this._field('pairing.host', 'text', demo ? MOCK_PAIRING.host : this.controller.settings.host ?? '');
    this.port = this._field('pairing.port', 'number', String(demo ? MOCK_PAIRING.port : this.controller.settings.port ?? ''));
    this.fp = this._field('pairing.fingerprint', 'text', demo ? MOCK_PAIRING.fp : this.controller.settings.fp ?? '');
    this.fp.input.addEventListener('input', () => this._validateLive());
    this.code = this._field('pairing.code', 'text', demo ? MOCK_PAIRING.code : '', {
      inputmode: 'numeric',
      maxlength: '6',
      autocomplete: 'one-time-code',
    });
    this.code.input.addEventListener('input', () => this._validateLive());
    this.deviceName = this._field('settings.deviceName', 'text', this.controller.settings.deviceName ?? '');

    this.liveError = h('p', { class: 'field__error', role: 'alert' });
    setHidden(this.liveError, true);

    this.connectButton = h(
      'button',
      {
        type: 'submit',
        class: 'btn btn--primary btn--large',
        'data-action': 'connect',
      },
      t('pairing.connect'),
    );
    this.scanButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--secondary',
        'data-action': 'scan',
        onclick: () => this._scan(),
      },
      t('pairing.scan'),
    );
    this.scanHint = h('p', { class: 'pairing__hint' }, t('pairing.scanHint'));

    this.form = h(
      'form',
      {
        class: 'pairing__form',
        onsubmit: (event) => {
          event.preventDefault();
          this._submit();
        },
      },
      h('h2', { class: 'section__title' }, t('pairing.manualTitle')),
      this.host.field,
      this.port.field,
      this.fp.field,
      this.code.field,
      this.deviceName.field,
      this.liveError,
      h('div', { class: 'pairing__actions' }, this.connectButton, this.scanButton),
      this.scanHint,
    );

    this.savedHint = h('p', { class: 'pairing__saved' }, t('pairing.savedHint'));
    if (demo) {
      this.savedHint.textContent = `${t('pairing.savedHint')} · ${t('app.demoHint')}`;
    }

    this.root.append(
      h(
        'section',
        { class: 'screen screen--pairing' },
        h('h1', { class: 'screen__title' }, t('pairing.title')),
        h('p', { class: 'screen__subtitle' }, t('pairing.subtitle')),
        this.status,
        this.hint,
        this.error,
        this.form,
        this.savedHint,
      ),
    );
  }

  _field(labelKey, type, value, extra = {}) {
    const id = `pairing-${labelKey.replace(/\W/g, '-')}`;
    const input = h('input', {
      id,
      type,
      class: 'input',
      value,
      ...extra,
    });
    const field = h(
      'div',
      { class: 'field' },
      h('label', { class: 'field__label', for: id }, t(labelKey)),
      input,
    );
    return { field, input };
  }

  _validateLive() {
    const problems = [];
    try {
      validatePairingField('fp', this.fp.input.value.trim());
    } catch {
      if (this.fp.input.value.trim()) problems.push(t('pairing.fingerprintHint'));
    }
    try {
      validatePairingField('code', this.code.input.value.trim());
    } catch {
      if (this.code.input.value.trim()) problems.push(t('pairing.codeHint'));
    }
    if (problems.length > 0) {
      this.liveError.textContent = problems.join(' · ');
      setHidden(this.liveError, false);
    } else {
      setHidden(this.liveError, true);
    }
  }

  async _submit() {
    this.connectButton.disabled = true;
    const uriLike = this.host.input.value.trim();
    const result = await this.controller.connectPairing({
      uri: uriLike.startsWith('dsphone://') ? uriLike : undefined,
      host: uriLike.startsWith('dsphone://') ? undefined : uriLike,
      port: this.port.input.value.trim(),
      fp: this.fp.input.value.trim(),
      code: this.code.input.value.trim(),
      deviceName: this.deviceName.input.value.trim() || undefined,
    });
    if (!result?.ok) {
      this.connectButton.disabled = false;
      setHidden(this.error, false);
      this.errorTitle.textContent = t('pairing.invalidFields');
      this.errorNext.textContent = t('pairing.help');
    }
  }

  async _scan() {
    const result = await this.controller.scanPairing();
    if (!result?.supported) {
      this.scanHint.textContent = t('pairing.scanHint');
      this.toast?.showText(t('pairing.scanHint'));
    }
  }

  mount() {
    this.unsubscribers.push(
      this.controller.on('pair-error', (payload) => this._showError(payload)),
      this.controller.on('state', (snapshot) => this.render(snapshot)),
      this.controller.on('toast', (payload) => this.toast?.show(payload)),
    );
  }

  _showError({ code, message, attemptsLeft }) {
    const copy = errorCopy(code);
    setHidden(this.error, false);
    this.errorTitle.textContent = copy.title;
    this.errorNext.textContent = attemptsLeft !== undefined && attemptsLeft >= 0
      ? `${copy.next} (${t('pairing.codeHint')}: ${attemptsLeft})`
      : copy.next;
    if (message) this.controller.diagnostics?.warn('pairing', message);
    this.connectButton.disabled = false;
  }

  /** Render connection status. */
  render(snapshot) {
    const phase = snapshot.phase;
    const online = snapshot.online;
    this.status.textContent = t(`phase.${phase}`);
    setClass(this.status, 'pairing__status--online', online);
    this.hint.textContent = t(`phaseHint.${phase}`);
    this.connectButton.textContent = phase === 'connecting' || phase === 'authenticating' ? t('pairing.connecting') : t('pairing.connect');
    this.connectButton.disabled = phase === 'connecting' || phase === 'authenticating';
    setClass(this.form, 'pairing__form--busy', phase === 'connecting' || phase === 'authenticating');
    setHidden(this.error, phase === 'pairing' || phase === 'idle' ? !this.error.matches('.banner:not([hidden])') : false);
    if (online) this.onPaired?.();
  }

  destroy() {
    for (const unsubscribe of this.unsubscribers) unsubscribe?.();
    this.unsubscribers = [];
    clear(this.root);
  }
}

/** Pairing page factory used by app.js. */
export function createPairingView(options) {
  const view = new PairingView(options);
  view.mount();
  return view;
}
