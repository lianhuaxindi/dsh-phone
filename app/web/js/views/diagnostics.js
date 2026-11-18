/**
 * Diagnostics screen (PRD FR-701..704, §11).
 *
 * Shows versions, connection status, measured latency (first partial / final
 * transcript / first reply chunk / whole turn), recent errors and the sanitized
 * event log. "Export logs" never includes audio, pairing codes, device tokens
 * or message text — the Diagnostics module fingerprints free text instead.
 */

import { h, clear, formatClock } from '../ui/dom.js';
import { t, errorCopy } from '../i18n.js';
import { openSheet } from '../ui/sheet.js';
import { LATENCY_NAMES } from '../diagnostics.js';

const LATENCY_ROWS = [
  ['asr-first-partial', 'diagnostics.latencyAsrFirst'],
  ['asr-final', 'diagnostics.latencyAsrFinal'],
  ['chat-first-delta', 'diagnostics.latencyChatFirst'],
  ['turn-complete', 'diagnostics.latencyTurn'],
];

export class DiagnosticsView {
  /**
   * @param {{root: HTMLElement, controller: object, toast: object,
   *          onBack?: Function}} options
   */
  constructor({ root, controller, toast, onBack = null }) {
    this.root = root;
    this.controller = controller;
    this.toast = toast;
    this.onBack = onBack;
    this.unsubscribers = [];
    this._lastExport = '';
    this._build();
  }

  _build() {
    clear(this.root);
    this.backButton = h(
      'button',
      {
        type: 'button',
        class: 'icon-button',
        'aria-label': t('a11y.back'),
        onclick: () => this.onBack?.(),
      },
      '‹',
    );
    this.refreshButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--ghost btn--small',
        'data-action': 'diag-refresh',
        onclick: () => this.refresh(),
      },
      t('common.refresh'),
    );

    this.versionValues = {};
    this.connectionValues = {};
    const versionRows = h(
      'div',
      { class: 'info-grid' },
      this._infoRow('appVersion', t('settings.version')),
      this._infoRow('protocolVersion', t('settings.protocolVersion')),
      this._infoRow('buildChannel', t('settings.buildChannel')),
      this._infoRow('transportKind', t('settings.transportMode')),
    );
    const connectionRows = h(
      'div',
      { class: 'info-grid' },
      this._infoRow('connectionState', t('diagnostics.connectionState')),
      this._infoRow('server', t('settings.server')),
      this._infoRow('serverId', t('diagnostics.serverId')),
      this._infoRow('phase', t('diagnostics.connection')),
    );

    this.latencyBody = h('tbody');
    this.latencyTable = h(
      'table',
      { class: 'table' },
      h(
        'thead',
        null,
        h(
          'tr',
          null,
          h('th', null, t('diagnostics.latency')),
          h('th', null, 'n'),
          h('th', null, 'last'),
          h('th', null, 'p50'),
          h('th', null, 'p95'),
          h('th', null, 'min'),
          h('th', null, 'max'),
          h('th', null, 'avg'),
        ),
      ),
      this.latencyBody,
    );
    this.latencyEmpty = h('p', { class: 'muted' }, t('diagnostics.noData'));

    this.errorList = h('ul', { class: 'error-list' });
    this.errorEmpty = h('p', { class: 'muted' }, t('diagnostics.noErrors'));

    // v1.1 (D-031): per-address connect diagnostics when host + hosts[] all fail.
    this.connectDiagMessage = h('p', { class: 'muted' }, '');
    this.connectDiagList = h('ul', { class: 'error-list' });
    this.connectDiagEmpty = h('p', { class: 'muted' }, t('diagnostics.connectDiagNone'));

    this.logPre = h('pre', { class: 'diagnostics__log mono', tabindex: '0' });
    this.logMeta = h('span', { class: 'muted' });

    this.exportButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--primary',
        'data-action': 'diag-export',
        onclick: () => this._export(),
      },
      t('diagnostics.exportLogs'),
    );
    this.copyButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--secondary',
        'data-action': 'diag-copy',
        onclick: () => this._copyLogs(),
      },
      t('diagnostics.copyLogs'),
    );
    this.clearButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--ghost',
        'data-action': 'diag-clear',
        onclick: () => {
          this.controller.clearLogs();
          this.refresh();
          this.toast?.showText(t('diagnostics.clearLogs'));
        },
      },
      t('diagnostics.clearLogs'),
    );
    this.reconnectButton = h(
      'button',
      {
        type: 'button',
        class: 'btn btn--secondary',
        'data-action': 'diag-reconnect',
        onclick: () => this._reconnect(),
      },
      t('diagnostics.restartBridge'),
    );

    this.root.append(
      h(
        'section',
        { class: 'screen screen--diagnostics' },
        h('header', { class: 'screen__header' }, this.backButton, h('h1', { class: 'screen__title' }, t('diagnostics.title')), this.refreshButton),
        this._section(t('diagnostics.versions'), versionRows),
        this._section(t('diagnostics.connection'), connectionRows),
        this._section(t('diagnostics.connectDiagTitle'), this.connectDiagMessage, this.connectDiagList, this.connectDiagEmpty),
        this._section(t('diagnostics.latency'), this.latencyTable, this.latencyEmpty),
        this._section(t('diagnostics.lastError'), this.errorList, this.errorEmpty),
        this._section(
          t('diagnostics.eventLog'),
          h('div', { class: 'diagnostics__actions' }, this.exportButton, this.copyButton, this.reconnectButton, this.clearButton),
          h('p', { class: 'muted' }, t('diagnostics.sanitized')),
          this.logMeta,
          this.logPre,
        ),
      ),
    );
  }

  _section(title, ...children) {
    return h('section', { class: 'settings__section' }, h('h2', { class: 'settings__section-title' }, title), ...children);
  }

  _infoRow(key, label) {
    const value = h('span', { class: 'info-value mono' }, t('diagnostics.noData'));
    this.versionValues[key] = value;
    this.connectionValues[key] = value;
    return h('div', { class: 'info-row' }, h('span', { class: 'info-label' }, label), value);
  }

  mount() {
    this.unsubscribers.push(
      this.controller.on('state', () => this.refresh()),
      this.controller.on('latency', () => this.refresh()),
      this.controller.on('error', () => this.refresh()),
      this.controller.on('logs-exported', (payload) => this._showExportedLogs(payload)),
    );
    this.refresh();
  }

  destroy() {
    for (const unsubscribe of this.unsubscribers) unsubscribe?.();
    this.unsubscribers = [];
    clear(this.root);
  }

  refresh() {
    const data = this.controller.getDiagnostics();
    const snapshot = this.controller.snapshot();
    this.versionValues.appVersion.textContent = data.appVersion;
    this.versionValues.protocolVersion.textContent = `v${data.protocolVersion}`;
    this.versionValues.buildChannel.textContent = data.buildChannel;
    this.versionValues.transportKind.textContent = data.transportKind;
    this.connectionValues.connectionState.textContent = t(`phase.${snapshot.phase}`);
    this.connectionValues.server.textContent = data.settings?.host ? `${data.settings.host}:${data.settings.port}` : t('diagnostics.noData');
    this.connectionValues.serverId.textContent = data.serverId ?? t('diagnostics.noData');
    this.connectionValues.phase.textContent = data.connection?.state ?? t('diagnostics.noData');

    clear(this.latencyBody);
    const stats = data.latency ?? {};
    let rowCount = 0;
    for (const [name, labelKey] of LATENCY_ROWS) {
      const entry = stats[name];
      const row = h(
        'tr',
        null,
        h('th', { scope: 'row' }, t(labelKey)),
        h('td', null, entry ? String(entry.count) : '—'),
        h('td', null, entry ? `${entry.last} ms` : '—'),
        h('td', null, entry ? `${entry.p50} ms` : '—'),
        h('td', null, entry ? `${entry.p95} ms` : '—'),
        h('td', null, entry ? `${entry.min} ms` : '—'),
        h('td', null, entry ? `${entry.max} ms` : '—'),
        h('td', null, entry ? `${entry.avg} ms` : '—'),
      );
      this.latencyBody.appendChild(row);
      if (entry) rowCount += 1;
    }
    this.latencyTable.hidden = rowCount === 0;
    this.latencyEmpty.hidden = rowCount > 0;

    clear(this.connectDiagList);
    const connectDiagnostics = data.connectDiagnostics ?? null;
    const tried = connectDiagnostics?.tried ?? [];
    this.connectDiagMessage.textContent = connectDiagnostics?.message ?? '';
    for (const entry of tried) {
      const host = entry?.host ?? '—';
      const port = entry?.port ?? '';
      const elapsed = Number.isFinite(Number(entry?.elapsedMs)) ? `${Math.round(Number(entry.elapsedMs))} ms` : '—';
      this.connectDiagList.appendChild(
        h(
          'li',
          { class: 'error-list__item' },
          h('span', { class: 'chip chip--danger mono' }, port ? `${host}:${port}` : String(host)),
          h('div', { class: 'error-list__body' }, h('span', { class: 'muted mono' }, `${elapsed} · ${entry?.error ?? '—'}`)),
        ),
      );
    }
    this.connectDiagList.hidden = tried.length === 0;
    this.connectDiagEmpty.hidden = tried.length > 0;
    this.connectDiagMessage.hidden = !connectDiagnostics?.message;

    clear(this.errorList);
    const errors = data.errors ?? [];
    for (const error of errors) {
      const copy = errorCopy(Number(error.code));
      this.errorList.appendChild(
        h(
          'li',
          { class: 'error-list__item' },
          h('span', { class: 'chip chip--danger mono' }, String(error.code ?? '—')),
          h('div', { class: 'error-list__body' }, h('strong', null, copy.title), h('span', { class: 'muted' }, copy.next), h('span', { class: 'muted mono' }, error.message ?? '')),
        ),
      );
    }
    this.errorList.hidden = errors.length === 0;
    this.errorEmpty.hidden = errors.length > 0;

    const diagnostics = this.controller.diagnostics;
    const entries = diagnostics?.entries ?? [];
    this.logMeta.textContent = `${entries.length} / ${diagnostics?.maxEntries ?? 0}`;
    this.logPre.textContent = entries
      .slice(-80)
      .map((entry) => `${formatClock(entry.at ?? 0)} ${entry.level ?? 'info'} ${entry.scope ?? ''} ${entry.message ?? ''}`)
      .join('\n');
  }

  async _export() {
    this.controller.exportLogs();
    this.toast?.showText(t('diagnostics.exportLogs'));
  }

  _showExportedLogs({ text = '', truncated = false }) {
    this._lastExport = text;
    const area = h('textarea', { class: 'diagnostics__export mono', rows: 12, readonly: 'readonly' });
    area.value = text;
    openSheet({
      title: t('diagnostics.eventLog'),
      description: t('diagnostics.sanitized'),
      content: area,
      dismissible: true,
      actions: [
        {
          label: t('diagnostics.copyLogs'),
          tone: 'primary',
          testId: 'copy-exported',
          onClick: () => this._copyText(text),
        },
        { label: t('common.close'), tone: 'secondary', onClick: () => {} },
      ],
    });
    if (truncated) this.toast?.showText(t('diagnostics.exported').replace('{path}', '…'));
  }

  async _copyLogs() {
    const text = this._lastExport || this.controller.diagnostics?.exportText?.() || '';
    await this._copyText(text);
  }

  async _copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      this.toast?.showText(t('common.copied'));
    } catch {
      this.toast?.showText(t('toast.error'));
    }
  }

  async _reconnect() {
    this.toast?.showText(t('diagnostics.reconnect'));
    await this.controller.reconnectBridge();
    this.refresh();
  }
}

export function createDiagnosticsView(options) {
  const view = new DiagnosticsView(options);
  view.mount();
  return view;
}

export { LATENCY_ROWS };
export const LATENCY_NAMES_EXPORT = LATENCY_NAMES;
