import 'dotenv/config';
import http from 'node:http';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { checkSignal, SignalJournal } from './signal-engine.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const key = process.env.ALERT_KEY ?? '';
if (key.length < 32 || key.startsWith('REPLACE')) {
  throw new Error('Set ALERT_KEY in .env to 32+ random characters before starting');
}
const port = Number(process.env.PORT ?? 8787);
const config = {
  symbols: (process.env.EXPECTED_SYMBOLS || `${process.env.EXPECTED_EXCHANGE || 'NSE'}:${process.env.EXPECTED_TICKER || 'NIFTY'}`)
    .split(',').map(x => x.trim()).filter(Boolean),
  exchange: process.env.EXPECTED_EXCHANGE || 'NSE',
  ticker: process.env.EXPECTED_TICKER || 'NIFTY',
  interval: process.env.EXPECTED_INTERVAL || '',
  maxAgeSeconds: Number(process.env.MAX_DELIVERY_AGE_SECONDS ?? 120),
  allowAfterhours: process.env.ALLOW_AFTERHOURS_PAPER === 'true'
};
const journal = new SignalJournal(path.join(root, 'data', 'accepted-signals.jsonl'),
  Number(process.env.MAX_SIGNALS_PER_DAY ?? 6));
function secureMatch(actual, expected) {
  const a = Buffer.from(actual), b = Buffer.from(expected);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
function send(res, code, payload) {
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  res.end(JSON.stringify(payload));
}
const server = http.createServer((req, res) => {
  if (req.url === '/health' && req.method === 'GET') {
    return send(res, 200, { status: 'ok', mode: 'SIGNAL_ONLY', liveOrders: false });
  }
  const supplied = req.url?.startsWith('/alert/') ? req.url.slice('/alert/'.length) : '';
  if (req.method !== 'POST' || !secureMatch(supplied, key)) return send(res, 404, { status: 'NOT_FOUND' });
  let body = '';
  req.on('data', chunk => {
    body += chunk;
    if (body.length > 8192) req.destroy();
  });
  req.on('end', () => {
    try {
      const input = JSON.parse(body);
      const now = new Date();
      const result = journal.record(checkSignal(input, now, config), now);
      // Always acknowledge validly authenticated messages; rejected signals are normal.
      // Never record the secret/token or account cookies in logs.
      console.log(`[${now.toISOString()}] ${result.status}: ${result.reason || result.direction}`);
      return send(res, 200, result);
    } catch {
      return send(res, 400, { status: 'REJECTED', reason: 'INVALID_JSON' });
    }
  });
});
server.listen(port, '127.0.0.1', () => {
  console.log(`Local signal collector: http://127.0.0.1:${port}/health`);
  console.log('Mode: SIGNAL_ONLY; no simulated fills or broker orders.');
  console.log('To receive TradingView webhooks use your own secured HTTPS reverse proxy/tunnel on port 443.');
});
