# Security Policy

## About this project

Keepfor.me core is single-tenant by default. A separate hosted service is built
on it through documented provider seams; multi-tenant isolation guarantees are
enforced by `tests/isolation/` and `tests/test_sql_tenant_scope.py` (to be
linked in T1.8).

That shapes the baseline threat model: in self-hosted deployments, each
deployment has exactly one owner account (the first user to register claims
admin, and registration then closes).

## Supported Versions

The latest minor release tag receives security fixes; older minor versions are
unsupported. Fixes are developed on `main` and backported to the supported
release.

| Version | Supported |
| ------- | --------- |
| `1.1.x` (once tagged) | :white_check_mark: |
| Older minor versions | :x: |
| `main` | Best effort |

If you are self-hosting, update by pulling the latest release tag and
redeploying.

## Security Model

| Concern | Implementation |
| ------- | -------------- |
| Passwords | PBKDF2-HMAC-SHA256, 100,000 iterations, stored as `salt$hex`. There is a pure-Python fallback for runtimes without `hashlib.pbkdf2_hmac`, using the same parameters. |
| Sessions | Opaque random tokens (`secrets.token_hex(32)`, in `src/auth/crypto.py`) stored server-side in the `sessions` table. Cookies are `HttpOnly`, `Secure`, `SameSite=Lax`. **Not** signed — the `SESSION_SECRET` var in `wrangler.jsonc` is never read. |
| Personal Access Tokens | Prefixed `kfm_live_` / `rk_live_`; only a SHA-256 hash is stored. The raw token is displayed exactly once, at creation. |
| Password reset | **Not implemented.** There is no recovery flow; rotate the PATs and re-register on a fresh instance if you lose access. |
| Privilege | Single tenant. Every authenticated user can read, tag, and delete the whole library. There is no per-item authorization. |

## Known Limitations

These are real and current. None are hidden, and none are silently mitigated in
production.

- **Login attempts are rate limited.** `/auth/login` and `/auth/register` throttle
  failed attempts, tracked in D1 so the limit is shared across all Worker
  isolates (see `src/utils/rate_limit.py`). Two independent caps apply: **20
  failures per IP** and **10 failures per account** per 15-minute window. The
  account cap is what stops distributed credential stuffing, and it holds even
  when the attacker rotates IPs. Only failures count, and a successful sign-in
  clears the account counter, so ordinary typos never lock anyone out. Blocked
  attempts return `429` with `Retry-After`, and the check runs *before* PBKDF2 so
  a throttled client cannot burn CPU on password hashing.

  Rate limiting raises the cost of guessing; it does not eliminate it. If the
  deployment must not be brute-forced at all, put Cloudflare Access in front of
  it.
- **Cross-origin requests are refused by default.** The app sends **no CORS
  headers**, so the browser's same-origin policy applies: another site cannot
  read this API. An earlier version ran `CORSMiddleware` with
  `allow_origins=["*"]` together with `allow_credentials=True`, which made the
  server echo any `Origin` back with credentials allowed (verified against
  production). Session-cookie reads were blocked only because cookies are
  `SameSite=Lax`; that was a single config change away from a full library
  exfiltration. `tests/test_endpoints.py::test_no_cross_origin_cors_headers`
  pins the safe behavior.

  If you self-host behind a separate frontend origin, it must proxy through the
  same origin rather than calling the API directly. The browser extension does
  not need CORS: Manifest V3 grants its cross-origin fetch through
  `host_permissions` in `browser-extension/manifest.json`.
- **Third-party requests disclose saved URLs.** Two features contact external
  services with data derived from your library:
  - Item favicons are fetched from `google.com` / `icons.duckduckgo.com`,
    which discloses every domain you have saved, at page-render time.
  - On HTTP 403 only, the article URL is sent to `r.jina.ai` (reader proxy) to
    recover bot-walled pages. The URL, never your credentials.
- **Article content is sanitized, not sandboxed.** Extracted HTML passes
  through an allowlist (`sanitize_clean_html`) that strips all scripts, styles,
  and event handlers, but reader output is rendered into the same origin as the
  app. Do not treat it as a hard boundary against a novel parser bypass.
- **`ALLOW_PUBLIC_SIGNUPS` gates registration only.** If set to the string
  `"true"`, anyone who can reach the instance can create an account and, since
  the library is single-tenant, read everything already saved. Leave it
  `false`.

## Reporting a Vulnerability

This is a personal, single-maintainer project, so please be pragmatic — but do
report real issues.

- **Use GitHub's private vulnerability reporting** on
  [`melcutz/keepfor.me`](https://github.com/melcutz/keepfor.me) (Security tab →
  "Report a vulnerability") as the preferred channel when enabled. This opens a
  private advisory visible only to the maintainer.
- Expect an acknowledgement within a few days and a fix or mitigation plan
  shortly after. Security fixes will be tagged in a patch release —
  tell me if you self-host and need the patch sooner.

Please do not open a public issue for an unpatched vulnerability.

## Verifying a Deployment

A few things worth checking on your own instance:

- `ALLOW_PUBLIC_SIGNUPS` is `"false"` in `wrangler.jsonc`.
- The deployment is reachable only over HTTPS and the zone has HSTS enabled.
- A response to `curl -sI -H 'Origin: https://example.com' <your-domain>/api/items`
  contains **no** `Access-Control-Allow-Origin` header. If one appears, a CORS
  allowlist has been reintroduced.
- Your PATs are treated as secrets — they grant full read/write/delete access to
  the entire library.
- Review **Settings → Personal Access Tokens** and revoke anything you don't
  recognize.