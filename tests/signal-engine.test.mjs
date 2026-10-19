import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { checkSignal, SignalJournal } from '../src/signal-engine.mjs';
const now = new Date('2026-10-12T04:05:15Z'); // Mon 09:35:15 IST
function signal(overrides = {}) {
  return { indicator: 'Half Trend Regression [AlgoAlpha]', event: 'REVERSAL', direction: 'BEARISH',
    exchange: 'NSE', ticker: 'NIFTY', interval: '1', index_close: '24000.50',
    bar_time: '2026-10-12T04:04:00Z', fired_at: '2026-10-12T04:05:00Z', ...overrides };
}
test('Accepts a fresh red reversal in IST session, no broker order', () => {
  const result = checkSignal(signal(), now, { interval: '1' });
  assert.equal(result.status, 'SIGNAL_ACCEPTED');
  assert.equal(result.direction, 'BEARISH');
  assert.match(result.note, /No trade/);
});
test('Rejects a steady red color event rather than a reversal', () => {
  assert.equal(checkSignal(signal({ event: 'TREND_STILL_RED' }), now).reason, 'NOT_A_NEW_REVERSAL');
});
test('Rejects unexpected instrument/timeframe', () => {
  assert.equal(checkSignal(signal({ ticker: 'BANKNIFTY' }), now).reason, 'UNEXPECTED_SYMBOL');
  assert.equal(checkSignal(signal(), now, { interval: '5' }).reason, 'UNEXPECTED_TIMEFRAME');
});
test('Accepts only the two configured index symbols', () => {
  const config = { symbols: ['NSE:NIFTY', 'BSE:SENSEX'] };
  assert.equal(checkSignal(signal(), now, config).status, 'SIGNAL_ACCEPTED');
  assert.equal(checkSignal(signal({ exchange: 'BSE', ticker: 'SENSEX' }), now, config).status, 'SIGNAL_ACCEPTED');
  assert.equal(checkSignal(signal({ exchange: 'NSE', ticker: 'BANKNIFTY' }), now, config).reason, 'UNEXPECTED_SYMBOL');
});
test('Rejects stale and market-closed alerts', () => {
  assert.equal(checkSignal(signal({ fired_at: '2026-10-12T04:00:00Z' }), now).reason, 'STALE_ALERT');
  assert.equal(checkSignal(signal(), new Date('2026-10-11T04:05:15Z')).reason, 'STALE_ALERT');
  const sunday = new Date('2026-10-11T04:05:15Z');
  const sundayAlert = signal({ bar_time: '2026-10-11T04:04:00Z', fired_at: '2026-10-11T04:05:00Z' });
  assert.equal(checkSignal(sundayAlert, sunday).reason, 'OUTSIDE_REGULAR_SESSION');
});
test('Deduplicates accepted signals even after process restart', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'nifty-signal-test-'));
  try {
    const file = path.join(dir, 'signals.jsonl');
    const result = checkSignal(signal(), now);
    const a = new SignalJournal(file, 1);
    assert.equal(a.record(result, now).status, 'SIGNAL_ACCEPTED');
    const b = new SignalJournal(file, 1);
    assert.equal(b.record(result, now).reason, 'DUPLICATE_SIGNAL');
    const second = checkSignal(signal({ direction: 'BULLISH' }), now);
    assert.equal(b.record(second, now).reason, 'DAILY_SIGNAL_CAP');
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
