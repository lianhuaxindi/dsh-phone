/**
 * Approval sheet — dangerous tool call confirmation (PRD FR-601, §12).
 *
 * Requirements baked in here:
 * - tool name, reason and the exact command/arguments in a monospace block
 *   highlighted in the danger color;
 * - the "deny" button is the default focus and keeps the danger styling;
 * - the deadline is shown as a countdown, because an unanswered approval is
 *   auto-denied after ten minutes.
 */

import { h } from './dom.js';
import { openSheet, infoRow } from './sheet.js';
import { t } from '../i18n.js';

/**
 * Format remaining seconds for the footer countdown.
 * @param {number} remainingSeconds
 */
export function formatRemaining(remainingSeconds) {
  const seconds = Math.max(0, Math.floor(remainingSeconds));
  if (seconds >= 60) {
    const minutes = Math.floor(seconds / 60);
    return `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
  }
  return `${seconds}s`;
}

/**
 * Open the approval sheet for one `approval.request`.
 *
 * @param {object} request normalized approval request
 * @param {object} options
 * @param {(id: string, outcome: 'approved'|'denied') => void} options.onRespond
 * @param {(sheet: object) => void} [options.onOpen]
 * @param {number} [options.now] unix-seconds clock
 * @param {object} [options.scheduler] scheduler with setTimeout/clearTimeout
 * @returns {{close: Function}}
 */
export function openApprovalSheet(request, { onRespond, onOpen, now = () => Math.floor(Date.now() / 1000), scheduler = null } = {}) {
  const setTimer = (fn, ms) => (scheduler?.setTimeout ? scheduler.setTimeout(fn, ms) : setTimeout(fn, ms));
  const clearTimer = (handle) => {
    if (handle === null || undefined === handle) return;
    if (scheduler?.clearTimeout) scheduler.clearTimeout(handle);
    else clearTimeout(handle);
  };

  let countdownHandle = null;
  let answered = false;

  const countdown = h('span', { class: 'approval__countdown', 'aria-live': 'off' }, '');
  const command = request.argsPreview
    ? h('pre', { class: 'approval__command mono', 'aria-label': t('approval.command') }, request.argsPreview)
    : null;

  const content = [
    h('div', { class: 'approval__tool' }, h('span', { class: 'chip chip--danger' }, request.tool)),
    request.reason ? h('p', { class: 'approval__reason' }, request.reason) : null,
    command,
    h('p', { class: 'approval__hint' }, t('approval.autoDenyHint')),
    h('p', { class: 'approval__deadline' }, countdown),
  ].filter(Boolean);

  const sheet = openSheet({
    title: t('approval.title'),
    description: t('approval.description'),
    content,
    tone: 'danger',
    dismissible: false,
    actions: [
      {
        label: t('approval.deny'),
        tone: 'danger',
        autofocus: true,
        testId: 'deny',
        onClick: () => respond('denied'),
      },
      {
        label: t('approval.allow'),
        tone: 'secondary',
        testId: 'approve',
        onClick: () => respond('approved'),
      },
    ],
  });

  function respond(outcome) {
    if (answered) return;
    answered = true;
    if (countdownHandle !== null) clearTimer(countdownHandle);
    onRespond?.(request.id, outcome);
    sheet.close('action');
  }

  function updateCountdown() {
    if (answered) return;
    const remaining = request.deadline ? request.deadline - now() : NaN;
    if (!Number.isFinite(remaining)) {
      countdown.textContent = t('approval.noDeadline');
      return;
    }
    if (remaining <= 0) {
      countdown.textContent = t('approval.expired');
      // Deadline reached: options are no longer actionable and the default
      // outcome (denied) applies — disable both buttons (TC-WEB-10).
      const buttons = sheet.sheet?.querySelectorAll ? sheet.sheet.querySelectorAll('.sheet__actions button') : [];
      for (const button of buttons) button.disabled = true;
      // The controller also closes on the local deadline; this is a safety net
      // for the UI only (no response is sent twice).
      return;
    }
    countdown.textContent = t('approval.remaining', { time: formatRemaining(remaining) });
  }

  updateCountdown();
  countdownHandle = setTimer(function tick() {
    countdownHandle = null;
    if (answered) return;
    updateCountdown();
    countdownHandle = setTimer(tick, 1000);
  }, 1000);

  onOpen?.(sheet);
  return sheet;
}
