# Security Policy

## About this project

Keepfor.me is a **self-hosted, single-tenant** application. There is no hosted
SaaS, no multi-tenant data store, and no shared user database — each deployment
has exactly one owner account (the first user to register claims admin, and
registration then closes).

That shapes everything below: the realistic threat model is "someone obtained
access to *my* deployment," not "an attacker is targeting all users of a public
service."

## Supported Versions

There are no tagged releases; the project is deployed straight from `main`.

| Version | Supported |
| ------- | --------- |
| `main` (current `HEAD`) | :white_check_mark: |
| Anything older than `HEAD` | :x: |

Fixes land on `main` and reach a deployment on the next push (see
`deploy.yml`). If you are self-hosting, update by pulling and redeploying.

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

- **No rate limiting on authentication.** `/auth/login` has no attempt
  counter, lockout, or delay. Passwords are strong-hashed, but an attacker with
  network access to your deployment can make unlimited guesses. Restrict access
  at the network layer (Cloudflare Access, IP allowlist) if the deployment is
  reachable from the public internet.
- **CORS reflects any origin.** The API middleware is configured with
  `allow_origins=["*"]` *and* `allow_credentials=True`
  (`src/app.py:136`), which causes the server to echo back whatever `Origin`
  the caller sends, with credentials allowed. Session-cookie reads are
  currently blocked only because the cookies are `SameSite=Lax` — verified
  2026-10-03: a cross-origin `fetch` from an attacker-controlled page reaches
  the API but receives `{"detail":"Authentication required"}`. Treat
  `SameSite=Lax` as the only thing standing between this and data
  exfiltration; do not relax it, and prefer tightening the CORS allowlist.
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
  "Report a vulnerability"). This opens a private advisory visible only to the
  maintainer.
- Expect an acknowledgement within a few days and a fix or mitigation plan
  shortly after. Because there are no releases, a fix means a commit on `main` —
  tell me if you self-host and need the patch sooner.

Please do not open a public issue for an unpatched vulnerability.

## Verifying a Deployment

A few things worth checking on your own instance:

- `ALLOW_PUBLIC_SIGNUPS` is `"false"` in `wrangler.jsonc`.
- The deployment is reachable only over HTTPS and the zone has HSTS enabled.
- Your PATs are treated as secrets — they grant full read/write/delete access to
  the entire library.
- Review **Settings → Personal Access Tokens** and revoke anything you don't
  recognize.