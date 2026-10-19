# ADR 0003 -- Webhook authentication and what each status code means

* Status: Accepted
* Date: 2026-10-19
* Milestone: M2

## Context

TradingView posts to a fixed URL, cannot attach custom headers, accepts ports 80
and 443 only, cancels any delivery that takes longer than three seconds, and
requires account 2FA for webhooks (verified against TradingView's webhook help
article on 2026-10-19). It also exposes a per-alert delivery log, and treats a
non-2xx response as a failed delivery.

## Decision

**Authentication.** A high-entropy shared secret is carried as the final URL path
segment and compared with `hmac.compare_digest`. An `X-Webhook-Token` header is
also accepted so local tooling and tests need not put the secret in a URL. The
token is never persisted and never logged; a contract test asserts no stored
request body contains it. The app refuses to start without a token, and
rejects one shorter than 32 characters.

**Nothing is written for an unauthenticated request.** Recording anonymous
requests would turn a public endpoint into a disk-exhaustion surface.

**Status codes.**

| Code | Meaning | Why |
|---|---|---|
| 401 | Not authenticated | Not a delivery failure to retry; the URL is wrong |
| 413 | Body over the ceiling | Rejected before parsing |
| 400 | Unparseable JSON | The alert message template is malformed; the operator must fix it, and TradingView's log should show the failure |
| 200 + `status: REJECTED` | Valid delivery, signal fails the contract | A *correctly rejected* alert is not a delivery failure. Marking it failed in TradingView's log would train the operator to ignore that log |
| 200 + `status: SIGNAL_RECORDED` | Committed to disk | Means "recorded", never "will be traded" |

**Ordering.** Authenticate, then size-check, then store the raw body, then
validate, then commit the normalized event, then acknowledge. The event is
durable before the acknowledgement is produced.

**Work off the request path.** The handler only validates and commits. Accepted
events are left in `signal_events.status = PENDING_DECISION`, a durable queue the
worker reads. An in-process queue was rejected: it loses events on restart, and
restart recovery is a stated requirement.

## Consequences

* TradingView's alert log stays meaningful: it shows failures for genuine
  transport and template problems, not for business rejections.
* Reason codes must be read from the response body and from
  `ingest_rejections`, not inferred from status codes.
* Public exposure remains the operator's responsibility: the app binds to
  127.0.0.1 and needs an authenticated HTTPS proxy on 443 in front of it.
