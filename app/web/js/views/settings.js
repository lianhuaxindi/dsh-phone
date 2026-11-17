/**
 * Settings screen (PRD FR-701..704).
 *
 * Every control writes through `controller.updateSettings`, which normalizes and
 * persists the value. Re-pairing asks for confirmation because it clears the
 * local token (native Keystore / debug token store) and forces a fresh pairing.
 */

import { h, clear, setHidden, setText } from '../ui/dom.js';
import { t } from '../i18n.js';
import { openSheet } from '../ui/sheet.js';
import { APP_VERSION, SILENCE_OPTIONS } from '../config.js';

export class SettingsView {
  /**
   * @param {{root: HTMLElement, controller: object, toast: object,
   *          onOpenDiagnostics?: Function, onOpenPairing?: Function}} options
   */
  constructor({ root, controller, toast, onOpenDiagnostics = null, onOpenPairing = null }) {
    this.root = root;
    this.controller = controller;
    this.toast = toast;
    this.onOpenDiagnostics = onOpenDiagnostics;
    this.onOpenPairing = onOpenPairing;
    this.unsubscribers = [];
    this._build();
  }

  _build() {
    clear(this.root);
    this.title = h('h1', { class: 'screen__title' }, t('settings.title'));

    /* ------------------------------------------------------------ connection */
    this.serverValue = h('span', { class: 'info-value mono' }, '—');
    this.pairedValue = h('span', { class: 'chip' });
    this.transportSelect = this._select(
      [
        ['auto', t('settings.transportAuto')],
        ['mock', t('settings.transportMock')],
        ['native', t('settings.transportNative')],
        ['ws', t('settings.transportWs')],
      ],
      (value) => this._changeTransport(value),
      t('settings.transportMode'),
    );
    this.rePairButton = h(
      'button',
      { type: 'button', class: 'btn btn--secondary', 'data-action': 're-pair', onclick: () => this._confirmRePair() },
      t('settings.rePair'),
    );

    const connectionSection = this._section(
      t('settings.connection'),
      this._row(t('settings.server'), this.serverValue),
      this._row(t('settings.paired'), this.pairedValue),
      this._row(t('settings.transportMode'), this.transportSelect, 'transport'),
      h('div', { class: 'settings__actions' }, this.rePairButton),
    );

    /* ------------------------------------------------------------ appearance */
    this.langSelect = this._select(
      [
        ['zh', t('settings.languageZh')],
        ['en', t('settings.languageEn')],
      ],
      (value) => this._update({ lang: value }),
      t('settings.language'),
    );
    this.themeSelect = this._select(
      [
        ['system', t('settings.themeSystem')],
        ['light', t('settings.themeLight')],
        ['dark', t('settings.themeDark')],
      ],
      (value) => this._update({ theme: value }),
      t('settings.theme'),
    );
    const appearanceSection = this._section(
      t('settings.appearance'),
      this._row(t('settings.language'), this.langSelect),
      this._row(t('settings.theme'), this.themeSelect),
    );

    /* ------------------------------------------------------------------ chat */
    this.autoSendInput = h('input', {
      type: 'checkbox',
      class: 'switch__input',
      onchange: () => this._update({ autoSend: this.autoSendInput.checked }),
    });
    this.autoSendSwitch = h(
      'label',
      { class: 'switch' },
      this.autoSendInput,
      h('span', { class: 'switch__track' }, h('span', { class: 'switch__thumb' })),
    );
    const chatSection = this._section(
      t('settings.chat'),
      this._row(
        h('span', null, t('settings.autoSend'), h('span', { class: 'field__hint' }, t('settings.autoSendHint'))),
        this.autoSendSwitch,
      ),
    );

    /* ----------------------------------------------------------------- voice */
    this.silenceSelect = this._select(
      SILENCE_OPTIONS.map((ms) => [String(ms), `${ms} ms`]),
      (value) => this._update({ silenceMs: Number(value) }),
      t('settings.silenceMs'),
    );
    this.audioDeviceSelect = this._select(
      [
        ['auto', t('settings.audioAuto')],
        ['phone', t('settings.audioPhone')],
        ['hfp', t('settings.audioHfp')],
        ['le', t('settings.audioLe')],
      ],
      (value) => this._update({ preferAudioDevice: value }),
      t('settings.audioDevice'),
    );
    const voiceSection = this._section(
      t('settings.voice'),
      this._row(h('span', null, t('settings.silenceMs'), h('span', { class: 'field__hint' }, t('settings.silenceHint'))), this.silenceSelect),
      this._row(h('span', null, t('settings.audioDevice'), h('span', { class: 'field__hint' }, t('settings.audioHint'))), this.audioDeviceSelect),
    );

    /* --------------------------------------------------------- diagnostics */
    this.diagnosticsButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--secondary',
        'data-action': 'open-diagnostics',
        onclick: () => this.onOpenDiagnostics?.(),
      },
      t('settings.diagnosticsLink'),
    );
    const demoSection = this._section(t('settings.demo'), h('div', { class: 'settings__actions' }, this.diagnosticsButton));

    /* ----------------------------------------------------------------- about */
    this.versionValue = h('span', { class: 'info-value mono' });
    const aboutSection = this._section(
      t('settings.about'),
      this._row(t('settings.version'), this.versionValue),
      this._row(t('settings.protocolVersion'), h('span', { class: 'info-value mono' }, 'v1')),
      this._row(t('settings.buildChannel'), h('span', { class: 'info-value mono' }, 'webview')),
      this._row(t('settings.deviceName'), h('span', { class: 'info-value' }, this.controller.deviceId ?? '—')),
    );

    this.root.append(
      h(
        'section',
        { class: 'screen screen--settings' },
        this.title,
        connectionSection,
        appearanceSection,
        chatSection,
        voiceSection,
        demoSection,
        aboutSection,
      ),
    );

    this.versionValue.textContent = APP_VERSION;
  }

  mount() {
    this.unsubscribers.push(
      this.controller.on('settings', () => this._sync()),
      this.controller.on('state', () => this._sync()),
      this.controller.on('token', () => this._sync()),
    );
    this._sync();
  }

  destroy() {
    for (const unsubscribe of this.unsubscribers) unsubscribe?.();
    this.unsubscribers = [];
    clear(this.root);
  }

  /** Re-read controller state into the controls. */
  _sync() {
    const settings = this.controller.settings ?? {};
    const snapshot = this.controller.snapshot();
    this.serverValue.textContent = settings.host ? `${settings.host}:${settings.port}` : t('diagnostics.noData');
    this.pairedValue.textContent = settings.paired ? t('phase.online') : t('phase.pairing');
    this.pairedValue.className = `chip ${settings.paired ? 'chip--online' : 'chip--warn'}`;
    this.transportSelect.value = settings.transportMode ?? 'auto';
    this.langSelect.value = settings.lang ?? 'zh';
    this.themeSelect.value = settings.theme ?? 'system';
    this.autoSendInput.checked = settings.autoSend !== false;
    this.silenceSelect.value = String(settings.silenceMs ?? 500);
    this.audioDeviceSelect.value = settings.preferAudioDevice ?? 'auto';
    this.versionValue.textContent = APP_VERSION;
    setHidden(this.rePairButton, false);
    this._phaseHint = snapshot.phase;  }

  _section(title, ...children) {
    return h(
      'section',
      { class: 'settings__section' },
      h('h2', { class: 'settings__section-title' }, title),
      h('div', { class: 'settings__body' }, ...children),
    );
  }

  _row(label, control, extraClass = '') {
    return h('div', { class: `settings__row ${extraClass}`.trim() }, label, control);
  }

  _select(options, onChange, label) {
    const select = h(
      'select',
      { class: 'select', 'aria-label': label ?? '' },
      ...options.map(([value, text]) => h('option', { value }, text)),
    );
    select.addEventListener('change', () => onChange(select.value));
    return select;
  }

  async _update(patch) {
    const before = this.controller.settings.lang;
    await this.controller.updateSettings(patch);
    if (patch.lang && patch.lang !== before) {
      // app.js re-renders every screen when the language changes.
      this.toast?.show({ key: 'toast.langChanged' });
    }
    this._sync();
  }

  async _changeTransport(mode) {
    await this._update({ transportMode: mode });
    this.toast?.showText(t('settings.transportMode'));
  }

  async _confirmRePair() {
    openSheet({
      title: t('settings.rePair'),
      description: t('settings.rePairConfirm'),
      tone: 'danger',
      dismissible: true,
      actions: [
        { label: t('common.cancel'), tone: 'secondary', onClick: () => {} },
        {
          label: t('settings.rePair'),
          tone: 'danger',
          testId: 'confirm-re-pair',
          onClick: () => {
            this.controller.reconnectBridge();
            this.onOpenPairing?.();
          },
        },
      ],
    });
  }
}

export function createSettingsView(options) {
  const view = new SettingsView(options);
  view.mount();
  return view;
}
