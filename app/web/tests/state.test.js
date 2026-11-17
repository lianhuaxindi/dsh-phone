import test from 'node:test';
import assert from 'node:assert/strict';

import {
  Phase,
  PHASES,
  Action,
  TRANSITIONS,
  StateTransitionError,
  createInitialState,
  canTransition,
  reduce,
  isOnline,
  canStartTurn,
  canRecord,
  isBusy,
  needsPairing,
  phaseKey,
} from '../js/state.js';

function apply(state, actions) {
  return actions.reduce((current, action) => reduce(current, action), state);
}

const ONLINE_PATH = [
  { type: Action.CONNECT_REQUESTED, at: 1 },
  { type: Action.CONNECTED, needPair: false, at: 2 },
  { type: Action.AUTH_SUCCEEDED, at: 3 },
];

const PAIRING_PATH = [
  { type: Action.CONNECT_REQUESTED, at: 1 },
  { type: Action.CONNECTED, needPair: true, at: 2 },
];

test('initial state is idle with no error', () => {
  const state = createInitialState({ at: 42 });
  assert.equal(state.phase, Phase.IDLE);
  assert.equal(state.error, null);
  assert.equal(state.since, 42);
  assert.equal(state.resumePhase, null);
  assert.deepEqual([...PHASES].sort(), [
    'approval-pending',
    'authenticating',
    'connecting',
    'idle',
    'online',
    'pairing',
    'recording',
    'waiting-for-reply',
  ]);
});

test('connect → auth → online path', () => {
  const online = apply(createInitialState(), ONLINE_PATH);
  assert.equal(online.phase, Phase.ONLINE);
  assert.equal(isOnline(online), true);
  assert.equal(canStartTurn(online), true);
  assert.equal(canRecord(online), true);
  assert.equal(isBusy(online), false);
  assert.equal(needsPairing(online), false);
  assert.equal(phaseKey(online), 'phase.online');
});

test('pairing → pair succeeded → authenticating', () => {
  const pairing = apply(createInitialState(), PAIRING_PATH);
  assert.equal(pairing.phase, Phase.PAIRING);
  assert.equal(needsPairing(pairing), true);
  const auth = reduce(pairing, { type: Action.PAIR_SUCCEEDED, at: 3 });
  assert.equal(auth.phase, Phase.AUTHENTICATING);
  assert.equal(isBusy(auth), true);
});

test('send starts a turn; finish and error return to online', () => {
  const online = apply(createInitialState(), ONLINE_PATH);
  const waiting = reduce(online, { type: Action.SEND_STARTED, at: 4 });
  assert.equal(waiting.phase, Phase.WAITING_FOR_REPLY);
  assert.equal(isOnline(waiting), true);
  assert.equal(canStartTurn(waiting), false);
  assert.equal(isBusy(waiting), true);

  const finished = reduce(waiting, { type: Action.TURN_FINISHED, at: 5 });
  assert.equal(finished.phase, Phase.ONLINE);

  const failed = reduce(waiting, { type: Action.TURN_ERROR, at: 6, error: { code: 3003 } });
  assert.equal(failed.phase, Phase.ONLINE);
  assert.deepEqual(failed.error, { code: 3003 });
});

test('approval suspends the current phase and restores it afterwards', () => {
  const waiting = reduce(apply(createInitialState(), ONLINE_PATH), { type: Action.SEND_STARTED, at: 4 });
  const pending = reduce(waiting, { type: Action.APPROVAL_SHOWN, at: 5 });
  assert.equal(pending.phase, Phase.APPROVAL_PENDING);
  assert.equal(pending.resumePhase, Phase.WAITING_FOR_REPLY);
  const resolved = reduce(pending, { type: Action.APPROVAL_RESOLVED, at: 6 });
  assert.equal(resolved.phase, Phase.WAITING_FOR_REPLY);
  assert.equal(resolved.resumePhase, null);
});

test('recording can start only from online and returns to online', () => {
  const online = apply(createInitialState(), ONLINE_PATH);
  const recording = reduce(online, { type: Action.RECORDING_STARTED, at: 4 });
  assert.equal(recording.phase, Phase.RECORDING);
  assert.equal(canRecord(recording), false);
  const stopped = reduce(recording, { type: Action.RECORDING_STOPPED, at: 5 });
  assert.equal(stopped.phase, Phase.ONLINE);
});

test('failures return to idle and keep the error for the pairing screen', () => {
  const connecting = reduce(createInitialState(), { type: Action.CONNECT_REQUESTED });
  const failed = reduce(connecting, { type: Action.CONNECT_FAILED, at: 2, error: { code: 1002, message: 'bad code' } });
  assert.equal(failed.phase, Phase.IDLE);
  assert.deepEqual(failed.error, { code: 1002, message: 'bad code' });

  const cleared = reduce(failed, { type: Action.CONNECT_REQUESTED, at: 3 });
  assert.equal(cleared.error, null);

  const pairing = apply(createInitialState(), PAIRING_PATH);
  const pairFailed = reduce(pairing, { type: Action.PAIR_FAILED, at: 3, error: { code: 1002 } });
  assert.equal(pairFailed.phase, Phase.IDLE);
  assert.deepEqual(pairFailed.error, { code: 1002 });
});

test('disconnect returns to idle, reset clears the snapshot', () => {
  const online = apply(createInitialState(), ONLINE_PATH);
  const disconnected = reduce(online, { type: Action.DISCONNECTED, at: 9, error: { code: 4408 } });
  assert.equal(disconnected.phase, Phase.IDLE);
  assert.deepEqual(disconnected.error, { code: 4408 });

  const reset = reduce(disconnected, { type: Action.RESET, at: 10 });
  assert.equal(reset.phase, Phase.IDLE);
  assert.equal(reset.error, null);
  assert.equal(reset.since, 10);
});

test('invalid transitions throw StateTransitionError', () => {
  const idle = createInitialState();
  assert.throws(() => reduce(idle, { type: Action.SEND_STARTED, at: 1 }), StateTransitionError);
  assert.throws(() => reduce(idle, { type: Action.AUTH_SUCCEEDED, at: 1 }), StateTransitionError);
  const online = apply(createInitialState(), ONLINE_PATH);
  assert.throws(() => reduce(online, { type: Action.CONNECT_REQUESTED, at: 5 }), StateTransitionError);
  assert.throws(
    () => reduce(online, { type: Action.CONNECT_REQUESTED, at: 5 }),
    (error) => error instanceof StateTransitionError && error.from === Phase.ONLINE,
  );
  assert.equal(canTransition(Phase.IDLE, Phase.CONNECTING), true);
  assert.equal(canTransition(Phase.IDLE, Phase.ONLINE), false);
  assert.equal(canTransition(Phase.ONLINE, Phase.RECORDING), true);
  for (const targets of Object.values(TRANSITIONS)) {
    for (const target of targets) assert.ok(PHASES.includes(target), `unknown target phase ${target}`);
  }
});

test('same-phase transitions only bump the timestamp', () => {
  const online = apply(createInitialState(), ONLINE_PATH);
  const again = reduce(online, { type: Action.AUTH_SUCCEEDED, at: 99 });
  assert.equal(again.phase, Phase.ONLINE);
  assert.equal(again.since, 99);
});
