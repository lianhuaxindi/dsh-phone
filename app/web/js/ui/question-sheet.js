/**
 * Question sheet — `ask_user_question` equivalent (PRD FR-602).
 *
 * Supports single-select, multi-select, required questions, custom free-text
 * answers and cancellation (which tells the server the question was cancelled).
 */

import { h, setHidden } from './dom.js';
import { openSheet } from './sheet.js';
import { t } from '../i18n.js';

/**
 * Open a question sheet for one `question.request`.
 *
 * @param {object} request normalized question request
 * @param {object} options
 * @param {(id: string, payload: {answers: object[], cancelled: boolean}) => void} options.onRespond
 * @returns {{close: Function}}
 */
export function openQuestionSheet(request, { onRespond } = {}) {
  /** @type {Map<string, {question: object, selected: Set<string>, custom: HTMLInputElement, error: HTMLElement}>} */
  const state = new Map();
  const content = [];

  for (const question of request.questions) {
    const selected = new Set();
    const fieldset = h('fieldset', {
      class: 'question',
      'data-question-id': question.id,
    });
    const legend = h('legend', { class: 'question__title' }, question.question);
    if (question.required) legend.appendChild(h('span', { class: 'question__required', 'aria-hidden': 'true' }, ' *'));
    fieldset.appendChild(legend);

    const hintKey = question.multiSelect ? 'question.multiHint' : 'question.singleHint';
    fieldset.appendChild(h('p', { class: 'question__hint' }, t(hintKey)));

    for (const option of question.options) {
      const input = h('input', {
        type: question.multiSelect ? 'checkbox' : 'radio',
        name: `q-${question.id}`,
        value: option.id,
        'data-option-id': option.id,
        onchange: () => {
          if (question.multiSelect) {
            if (input.checked) selected.add(option.id);
            else selected.delete(option.id);
          } else {
            selected.clear();
            selected.add(option.id);
          }
          setHidden(error, true);
        },
      });
      fieldset.appendChild(
        h('label', { class: 'option' }, input, h('span', { class: 'option__label' }, option.label)),
      );
    }

    let customInput = null;
    if (question.allowCustom) {
      customInput = h('input', {
        type: 'text',
        class: 'input question__custom',
        placeholder: t('question.customPlaceholder'),
        'aria-label': t('question.customPlaceholder'),
        oninput: () => setHidden(error, true),
      });
      fieldset.appendChild(customInput);
    }

    const error = h('p', { class: 'question__error', role: 'alert' }, t('question.requiredError'));
    setHidden(error, true);
    fieldset.appendChild(error);

    state.set(question.id, { question, selected, custom: customInput, error });
    content.push(fieldset);
  }

  const sheet = openSheet({
    title: t('question.title'),
    description: t('question.description'),
    content,
    dismissible: false,
    actions: [
      {
        label: t('question.cancel'),
        tone: 'secondary',
        testId: 'cancel',
        onClick: () => {
          onRespond?.(request.id, { answers: [], cancelled: true });
          sheet.close('action');
        },
      },
      {
        label: t('question.submit'),
        tone: 'primary',
        autofocus: true,
        testId: 'submit',
        onClick: () => submit(),
      },
    ],
  });

  function collectAnswers() {
    const answers = [];
    let valid = true;
    /** @type {HTMLElement|null} */
    let firstInvalid = null;
    for (const [questionId, entry] of state) {
      const custom = entry.custom?.value?.trim() ? entry.custom.value.trim() : null;
      const selected = [...entry.selected];
      const answered = selected.length > 0 || Boolean(custom);
      if (entry.question.required && !answered) {
        valid = false;
        setHidden(entry.error, false);
        firstInvalid ??= entry.custom ?? entry.error;
      } else {
        setHidden(entry.error, true);
      }
      answers.push({ questionId, selected, custom });
    }
    return { valid, answers, firstInvalid };
  }

  function submit() {
    const { valid, answers, firstInvalid } = collectAnswers();
    if (!valid) {
      firstInvalid?.focus?.();
      return;
    }
    onRespond?.(request.id, { answers, cancelled: false });
    sheet.close('action');
  }

  return sheet;
}
