/**
 * UI state machine (ARCHITECTURE §3.3, PROTOCOL §3).
 *
 * Pure reducer: no DOM, no timers, no I/O. The controller dispatches actions
 * and the UI renders from the resulting snapshot. Invalid transitions throw a
 * StateTransitionError so bugs surface in tests instead of silently drifting.
 */

/** Connection / interaction phases the UI can be in. */
export const Phase = Object.freeze({
  IDLE: 'idle',
  CONNECTING: 'connecting',
  PAIRING: 'pairing',
  AUTHENTICATING: 'authenticating',
  ONLINE: 'online',
  RECORDING: 'recording',
  WAITING_FOR_REPLY: 'waiting-for-reply',
  APPROVAL_PENDING: 'approval-pending',
});

/** All phases, ordered by their position in the happy path. */
export const PHASES = Object.freeze(Object.values(Phase));

/** Actions accepted by `reduce`. */
export const Action = Object.freeze({
  CONNECT_REQUESTED: 'connect-requested',
  CONNECT_FAILED: 'connect-failed',
  CONNECTED: 'connected',
  PAIR_REQUIRED: 'pair-required',
  PAIR_SUCCEEDED: 'pair-succeeded',
  PAIR_FAILED: 'pair-failed',
  AUTH_SUCCEEDED: 'auth-succeeded',
  AUTH_FAILED: 'auth-failed',
  DISCONNECTED: 'disconnected',
  SEND_STARTED: 'send-started',
  TURN_FINISHED: 'turn-finished',
  TURN_ERROR: 'turn-error',
  APPROVAL_SHOWN: 'approval-shown',
  APPROVAL_RESOLVED: 'approval-resolved',
  RECORDING_STARTED: 'recording-started',
  RECORDING_STOPPED: 'recording-stopped',
  PAIRING_STARTED: 'pairing-started',
  REPAIR_REQUESTED: 'repair-requested',
  RESET: 'reset',
});

/**
 * Allowed phase transitions. `RESET` (and UI-only actions) are handled outside
 * this map. A transition not listed here is a programming error.
 */
export const TRANSITIONS = Object.freeze({
  [Phase.IDLE]: [Phase.CONNECTING],
  [Phase.CONNECTING]: [Phase.PAIRING, Phase.AUTHENTICATING, Phase.IDLE],
  [Phase.PAIRING]: [Phase.AUTHENTICATING, Phase.IDLE],
  [Phase.AUTHENTICATING]: [Phase.ONLINE, Phase.PAIRING, Phase.IDLE],
  [Phase.ONLINE]: [Phase.RECORDING, Phase.WAITING_FOR_REPLY, Phase.APPROVAL_PENDING, Phase.IDLE],
  [Phase.RECORDING]: [Phase.ONLINE, Phase.WAITING_FOR_REPLY, Phase.APPROVAL_PENDING, Phase.IDLE],
  [Phase.WAITING_FOR_REPLY]: [Phase.ONLINE, Phase.APPROVAL_PENDING, Phase.IDLE],
  [Phase.APPROVAL_PENDING]: [Phase.ONLINE, Phase.WAITING_FOR_REPLY, Phase.IDLE],
});

/** Error thrown for a disallowed transition (or disallowed action). */
export class StateTransitionError extends Error {
  constructor(from, to, action) {
    super(`invalid transition ${from} -> ${to} (action: ${action})`);
    this.name = 'StateTransitionError';
    this.from = from;
    this.to = to;
    this.action = action;
  }
}

/** Create the initial snapshot. `at` is an injectable timestamp (ms). */
export function createInitialState({ at = 0 } = {}) {
  return {
    phase: Phase.IDLE,
    /** Phase to return to after a transient overlay (approval sheet). */
    resumePhase: null,
    /** Last error surfaced to the user, cleared on successful connect. */
    error: null,
    /** Timestamp (ms) of the last phase change. */
    since: at,
  };
}

/** True when `from` may legally move to `to`. */
export function canTransition(from, to) {
  return (TRANSITIONS[from] ?? []).includes(to);
}

function transition(state, to, action, at) {
  if (state.phase === to) return { ...state, since: at };
  if (!canTransition(state.phase, to)) throw new StateTransitionError(state.phase, to, action.type);
  const next = { ...state, phase: to, since: at };
  if (to !== Phase.APPROVAL_PENDING) next.resumePhase = null;
  return next;
}

/**
 * Apply one action to a snapshot and return the next snapshot.
 * @param {object} state snapshot from `createInitialState`
 * @param {{type: string, at?: number, [key: string]: unknown}} action
 */
export function reduce(state, action) {
  const at = Number.isFinite(action.at) ? action.at : state.since;

  switch (action.type) {
    case Action.RESET:
      return { ...createInitialState({ at }), error: action.error ?? null };

    case Action.CONNECT_REQUESTED:
      return transition({ ...state, error: null }, Phase.CONNECTING, action, at);

    case Action.CONNECT_FAILED:
      return { ...transition(state, Phase.IDLE, action, at), error: action.error ?? null };

    case Action.CONNECTED:
      // The transport is up; protocol decides whether pairing is still needed.
      return action.needPair
        ? transition(state, Phase.PAIRING, action, at)
        : transition(state, Phase.AUTHENTICATING, action, at);

    case Action.PAIR_REQUIRED:
      return state.phase === Phase.PAIRING ? { ...state, since: at } : transition(state, Phase.PAIRING, action, at);

    case Action.PAIRING_STARTED:
      return state.phase === Phase.PAIRING ? { ...state, since: at } : transition(state, Phase.PAIRING, action, at);

    case Action.PAIR_SUCCEEDED:
      return transition({ ...state, error: null }, Phase.AUTHENTICATING, action, at);

    case Action.PAIR_FAILED:
      return {
        ...(canTransition(state.phase, Phase.IDLE) ? transition(state, Phase.IDLE, action, at) : { ...state, since: at }),
        error: action.error ?? null,
      };

    case Action.AUTH_SUCCEEDED:
      return transition({ ...state, error: null }, Phase.ONLINE, action, at);

    case Action.AUTH_FAILED:
      return { ...transition(state, Phase.IDLE, action, at), error: action.error ?? null };

    case Action.DISCONNECTED:
      if (state.phase === Phase.IDLE) return { ...state, error: action.error ?? state.error };
      return { ...transition(state, Phase.IDLE, action, at), error: action.error ?? null };

    case Action.SEND_STARTED:
      return transition(state, Phase.WAITING_FOR_REPLY, action, at);

    case Action.TURN_FINISHED:
    case Action.TURN_ERROR: {
      const next = transition(state, Phase.ONLINE, action, at);
      return action.type === Action.TURN_ERROR ? { ...next, error: action.error ?? null } : next;
    }

    case Action.APPROVAL_SHOWN: {
      const resumePhase = [Phase.ONLINE, Phase.RECORDING, Phase.WAITING_FOR_REPLY].includes(state.phase)
        ? state.phase
        : Phase.WAITING_FOR_REPLY;
      const next = transition(state, Phase.APPROVAL_PENDING, action, at);
      return { ...next, resumePhase };
    }

    case Action.APPROVAL_RESOLVED:
      return transition(state, state.resumePhase ?? Phase.ONLINE, action, at);

    case Action.RECORDING_STARTED:
      return transition(state, Phase.RECORDING, action, at);

    case Action.RECORDING_STOPPED:
      return transition(state, Phase.ONLINE, action, at);

    default:
      return state;
  }
}

/* ------------------------------------------------------------------------- */
/* Selectors                                                                  */
/* ------------------------------------------------------------------------- */

/** Connection is established and the socket is usable. */
export function isOnline(state) {
  return state.phase === Phase.ONLINE || state.phase === Phase.WAITING_FOR_REPLY;
}

/** The user may start a new turn (single in-flight turn per session). */
export function canStartTurn(state) {
  return state.phase === Phase.ONLINE;
}

/** The user may start recording (PTT) right now. */
export function canRecord(state) {
  return state.phase === Phase.ONLINE;
}

/** The UI should show the "busy" (waiting/blocked) affordances. */
export function isBusy(state) {
  return (
    state.phase === Phase.CONNECTING ||
    state.phase === Phase.AUTHENTICATING ||
    state.phase === Phase.WAITING_FOR_REPLY ||
    state.phase === Phase.APPROVAL_PENDING
  );
}

/** Phase needs the user's attention in the pairing screen. */
export function needsPairing(state) {
  return state.phase === Phase.PAIRING;
}

/** i18n key describing the current phase (status bar / diagnostics). */
export function phaseKey(state) {
  return `phase.${state.phase}`;
}
