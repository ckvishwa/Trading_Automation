/**
 * A trading signal collector, NOT a trading strategy or an order execution engine.
 * Accepts only fresh NSE:NIFTY AlgoAlpha reversal alerts during Indian market hours.
 * The index itself is not tradable; instrument mapping is deliberately absent.
 */
import fs from 'node:fs';
import path from 'node:path';

const ZONE = 'Asia/Kolkata';
function istParts(date) {
  const formatter = new Intl.DateTimeFormat('en-GB', {
    timeZone: ZONE, weekday: 'short', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
  });
  const parts = Object.fromEntries(formatter.formatToParts(date).map(x => [x.type, x.value]));
  return { weekday: parts.weekday, date: `${parts.year}-${parts.month}-${parts.day}`,
    minuteOfDay: Number(parts.hour) * 60 + Number(parts.minute) };
}
function indianSessionOpen(date) {
  const x = istParts(date);
  return x.weekday !== 'Sat' && x.weekday !== 'Sun' &&
    x.minuteOfDay >= 9 * 60 + 15 && x.minuteOfDay < 15 * 60 + 30;
  // NSE holiday calendar must also be checked before any future execution.
}
function normalizeDirection(direction) {
  const s = String(direction ?? '').trim().toUpperCase();
  if (s === 'BEARISH' || s === 'RED') return 'BEARISH';
  if (s === 'BULLISH' || s === 'GREEN') return 'BULLISH';
  return null;
}
const error = reason => ({ status: 'REJECTED', reason });
export function checkSignal(input, now, config = {}) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return error('INVALID_PAYLOAD');
  const direction = normalizeDirection(input.direction);
  if (!direction) return error('UNKNOWN_DIRECTION');
  if (input.indicator !== 'Half Trend Regression [AlgoAlpha]') return error('UNEXPECTED_INDICATOR');
  if (input.event !== 'REVERSAL') return error('NOT_A_NEW_REVERSAL');
  const allowedSymbols = config.symbols ?? [`${config.exchange ?? 'NSE'}:${config.ticker ?? 'NIFTY'}`];
  if (!allowedSymbols.includes(`${input.exchange}:${input.ticker}`)) {
    return error('UNEXPECTED_SYMBOL');
  }
  const interval = String(input.interval ?? '');
  if (!interval || (config.interval && interval !== String(config.interval))) return error('UNEXPECTED_TIMEFRAME');
  const barTime = new Date(input.bar_time);
  const firedAt = new Date(input.fired_at);
  if (!Number.isFinite(barTime.getTime()) || !Number.isFinite(firedAt.getTime())) return error('INVALID_TIME');
  const deliveryAge = now.getTime() - firedAt.getTime();
  if (deliveryAge < -10_000 || deliveryAge > (config.maxAgeSeconds ?? 120) * 1000) return error('STALE_ALERT');
  if (barTime.getTime() > firedAt.getTime() + 10_000) return error('BAR_AFTER_FIRE_TIME');
  const indexValue = Number(input.index_close);
  if (!Number.isFinite(indexValue) || indexValue <= 0) return error('BAD_INDEX_PRICE');
  if (!config.allowAfterhours && !indianSessionOpen(now)) return error('OUTSIDE_REGULAR_SESSION');
  const day = istParts(now).date;
  const key = `${input.exchange}:${input.ticker}:${interval}:${direction}:${barTime.toISOString()}`;
  return { status: 'SIGNAL_ACCEPTED', key, day, direction, interval, indexValue,
    barTime: barTime.toISOString(), firedAt: firedAt.toISOString(),
    note: 'SIGNAL ONLY. No trade, simulated fill, or Kite order.' };
}

export class SignalJournal {
  constructor(filepath, maxPerDay = 6) {
    this.filepath = filepath;
    this.maxPerDay = maxPerDay;
    this.seen = new Set();
    this.counts = new Map();
    if (fs.existsSync(filepath)) {
      for (const line of fs.readFileSync(filepath, 'utf8').split('\n')) {
        if (!line.trim()) continue;
        try {
          const item = JSON.parse(line);
          if (item.status === 'SIGNAL_ACCEPTED' && item.key) {
            this.seen.add(item.key);
            this.counts.set(item.day, (this.counts.get(item.day) ?? 0) + 1);
          }
        } catch { throw new Error('Corrupt journal; refusing to start'); }
      }
    }
  }
  record(result, now) {
    if (result.status !== 'SIGNAL_ACCEPTED') return result;
    if (this.seen.has(result.key)) return error('DUPLICATE_SIGNAL');
    if ((this.counts.get(result.day) ?? 0) >= this.maxPerDay) return error('DAILY_SIGNAL_CAP');
    // Synchronous append keeps the small read-only collector simple, durable, and responsive.
    fs.mkdirSync(path.dirname(this.filepath), { recursive: true });
    fs.appendFileSync(this.filepath, JSON.stringify({ ...result, receivedAt: now.toISOString() }) + '\n');
    this.seen.add(result.key);
    this.counts.set(result.day, (this.counts.get(result.day) ?? 0) + 1);
    return result;
  }
}
