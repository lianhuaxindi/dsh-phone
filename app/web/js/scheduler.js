/**
 * Injectable timing primitives.
 *
 * Production code uses `RealScheduler` (setTimeout/performance.now). Tests use
 * `ManualScheduler` so streaming, reconnects and debounce logic are fully
 * deterministic without sleeping. Nothing here touches the DOM.
 */

/** Interface implemented by both schedulers. */
export class Scheduler {
  /** Monotonic-ish current time in milliseconds. */
  now() {
    return Date.now();
  }

  /**
   * @param {() => void} fn
   * @param {number} delayMs
   * @returns {number} handle usable with `clearTimeout`
   */
  setTimeout(fn, delayMs) {
    return globalThis.setTimeout(fn, delayMs);
  }

  /** @param {number} handle */
  clearTimeout(handle) {
    globalThis.clearTimeout(handle);
  }

  /** Run `fn` once after `delayMs`, ignoring cancellation (convenience). */
  delay(delayMs, fn) {
    return this.setTimeout(fn, delayMs);
  }
}

/** Wall-clock scheduler backed by the platform. */
export class RealScheduler extends Scheduler {
  now() {
    return Date.now();
  }
}

/**
 * Deterministic virtual-clock scheduler for tests.
 * Time only moves when `advance()` / `advanceBy()` is called.
 */
export class ManualScheduler extends Scheduler {
  constructor(startMs = 1000) {
    super();
    this.currentMs = startMs;
    this.nextHandle = 1;
    this.timers = new Map();
  }

  now() {
    return this.currentMs;
  }

  setTimeout(fn, delayMs) {
    const handle = this.nextHandle++;
    const delay = Math.max(0, Number(delayMs) || 0);
    this.timers.set(handle, { fn, dueAt: this.currentMs + delay });
    return handle;
  }

  clearTimeout(handle) {
    this.timers.delete(handle);
  }

  /** Number of timers still pending. */
  pendingCount() {
    return this.timers.size;
  }

  /** Move the virtual clock by `deltaMs`, running due timers in order. */
  advanceBy(deltaMs) {
    const target = this.currentMs + Math.max(0, Number(deltaMs) || 0);
    let guard = 0;
    for (;;) {
      const due = this.#nextDue(target);
      if (!due) break;
      guard += 1;
      if (guard > 10000) throw new Error('ManualScheduler: too many timers in one advance (loop?)');
      this.currentMs = due.dueAt;
      this.timers.delete(due.handle);
      due.fn();
    }
    this.currentMs = target;
  }

  /** Run all timers whose due time is exactly now or earlier. */
  flush() {
    this.advanceBy(0);
  }

  /** Advance until no timers remain (bounded). */
  runAll(maxSteps = 10000) {
    let steps = 0;
    while (this.timers.size > 0) {
      steps += 1;
      if (steps > maxSteps) throw new Error('ManualScheduler: runAll exceeded step budget');
      const due = this.#nextDue(Number.POSITIVE_INFINITY);
      if (!due) break;
      this.currentMs = Math.max(this.currentMs, due.dueAt);
      this.timers.delete(due.handle);
      due.fn();
    }
  }

  #nextDue(limit) {
    let best = null;
    for (const [handle, timer] of this.timers) {
      if (timer.dueAt > limit) continue;
      if (!best || timer.dueAt < best.dueAt || (timer.dueAt === best.dueAt && handle < best.handle)) {
        best = { handle, dueAt: timer.dueAt, fn: timer.fn };
      }
    }
    return best;
  }
}

/** Shared real-time scheduler instance (stateless; safe to reuse). */
export const realScheduler = new RealScheduler();
