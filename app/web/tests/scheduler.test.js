import test from 'node:test';
import assert from 'node:assert/strict';

import { Scheduler, RealScheduler, ManualScheduler, realScheduler } from '../js/scheduler.js';

test('RealScheduler delegates to platform timers', async () => {
  const scheduler = new RealScheduler();
  assert.ok(scheduler instanceof Scheduler);
  assert.ok(realScheduler instanceof RealScheduler);
  assert.ok(Math.abs(scheduler.now() - Date.now()) < 5000);
  const fired = await new Promise((resolve) => {
    scheduler.setTimeout(() => resolve('fired'), 0);
  });
  assert.equal(fired, 'fired');
  const handle = scheduler.setTimeout(() => assert.fail('should have been cleared'), 5);
  scheduler.clearTimeout(handle);
  await new Promise((resolve) => setTimeout(resolve, 20));
});

test('ManualScheduler runs timers deterministically in due order', () => {
  const scheduler = new ManualScheduler(1000);
  const order = [];
  scheduler.setTimeout(() => order.push('b'), 20);
  scheduler.setTimeout(() => order.push('a'), 10);
  scheduler.setTimeout(() => order.push('c'), 30);
  assert.equal(scheduler.pendingCount(), 3);
  assert.equal(scheduler.now(), 1000);
  assert.equal(order.length, 0, 'nothing runs before time advances');

  scheduler.advanceBy(9);
  assert.deepEqual(order, []);
  scheduler.advanceBy(1);
  assert.deepEqual(order, ['a']);
  assert.equal(scheduler.now(), 1010);
  scheduler.advanceBy(20);
  assert.deepEqual(order, ['a', 'b', 'c']);
  assert.equal(scheduler.pendingCount(), 0);
  assert.equal(scheduler.now(), 1030);
});

test('ManualScheduler runs nested timers within the advanced window', () => {
  const scheduler = new ManualScheduler(0);
  const order = [];
  scheduler.setTimeout(() => {
    order.push('first');
    scheduler.setTimeout(() => order.push('nested'), 5);
  }, 5);
  scheduler.advanceBy(9);
  assert.deepEqual(order, ['first']);
  scheduler.advanceBy(6);
  assert.deepEqual(order, ['first', 'nested']);

  // A timer scheduled beyond the window only runs in a later advance.
  scheduler.setTimeout(() => order.push('later'), 100);
  scheduler.advanceBy(50);
  assert.deepEqual(order, ['first', 'nested']);
  scheduler.advanceBy(50);
  assert.deepEqual(order, ['first', 'nested', 'later']);
  assert.equal(scheduler.pendingCount(), 0);
});

test('runAll() drains everything; guard budget prevents infinite loops', () => {
  const scheduler = new ManualScheduler(0);
  let count = 0;
  const tick = () => {
    count += 1;
    if (count < 50) scheduler.setTimeout(tick, 1);
  };
  scheduler.setTimeout(tick, 1);
  scheduler.runAll();
  assert.equal(count, 50);
  assert.equal(scheduler.pendingCount(), 0);

  const looper = new ManualScheduler(0);
  const forever = () => looper.setTimeout(forever, 1);
  looper.setTimeout(forever, 1);
  assert.throws(() => looper.runAll(100), /step budget/);
});

test('ManualScheduler.clearTimeout removes pending work', () => {
  const scheduler = new ManualScheduler(10);
  const handle = scheduler.setTimeout(() => assert.fail('cancelled timer fired'), 5);
  scheduler.clearTimeout(handle);
  scheduler.advanceBy(10);
  assert.equal(scheduler.pendingCount(), 0);

  scheduler.setTimeout(() => {}, 5);
  scheduler.clearTimeout(999999);
  assert.equal(scheduler.pendingCount(), 1, 'unknown handles are ignored');
});

test('delay() schedules the callback through the same clock', () => {
  const scheduler = new ManualScheduler(0);
  let fired = false;
  scheduler.delay(25, () => {
    fired = true;
  });
  scheduler.advanceBy(24);
  assert.equal(fired, false);
  scheduler.advanceBy(1);
  assert.equal(fired, true);
});

test('flush() runs timers due at the current virtual time', () => {
  const scheduler = new ManualScheduler(100);
  const order = [];
  scheduler.setTimeout(() => order.push('now'), 0);
  scheduler.setTimeout(() => order.push('later'), 10);
  scheduler.flush();
  assert.deepEqual(order, ['now']);
  scheduler.advanceBy(10);
  assert.deepEqual(order, ['now', 'later']);
});
