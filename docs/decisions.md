# Architectural Decisions

## ADR 001: Outbound Egress Redirect Handling in Workers and CPython

### Context
Outbound HTTP fetching occurs during item extraction and reader proxy fallback. In malicious or adversarial scenarios, an attacker can provide a URL that redirects from a benign public domain to a sensitive internal resource (e.g., `127.0.0.1`, cloud metadata `169.254.169.254`, or private RFC 1918 subnets).

### Decision
In `keepfor/utils/egress.py`:
1. **CPython Runtime:**
   We enforce manual redirection via a custom `urllib.request.HTTPRedirectHandler` (`_NoRedirectHandler`) that disables automatic redirection in `urllib`. Each redirect hop (HTTP 301, 302, 303, 307, 308) is intercepted individually, resolved via `urllib.parse.urljoin`, validated against `validate_url(next_url, policy)`, and verified against the `FetchBudget` hook before proceeding to the next hop. Bounded to `policy.max_redirects` (default 5).

2. **Cloudflare Workers (Pyodide) Runtime:**
   We request `redirect="manual"` in `pyodide.http.pyfetch` where supported by the underlying fetch implementation, inspecting `Location` headers and applying `validate_url` at every hop. If `redirect="manual"` is unsupported by a given Worker runtime release, we fallback to `redirect="follow"`, but strictly validate `response.url` post-fetch and abort/discard the response body immediately if the final destination is blocked by `validate_url`. Spike S6 will verify the exact behavior of `pyfetch(..., redirect="manual")` on Cloudflare Workers.

## ADR 002: Spike S6 - Streaming Body Cap and Local Runtime Limitation

### Context
Spike S6 tests whether streaming size cap enforcement works under workerd (Pyodide).

### Findings & Decision
1. In local sandbox development, running `pywrangler dev --config wrangler.local.jsonc` encounters `ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'` because Pyodide in workerd requires wasm-compiled packages for native C extensions, which are not packaged locally in `python_modules`. Per the execution plan, this is recorded as `BLOCKED-ON-HUMAN: pywrangler dev --config wrangler.local.jsonc`.
2. For the streaming reader loop implementation in `keepfor/utils/egress.py`:
   - Pyodide implementation uses `resp.js_response.body.getReader()`, iterating `await reader.read()` and accumulating chunk lengths, calling `await reader.cancel()` and `reader.releaseLock()` as soon as `total_read > policy.max_bytes`.
   - Pre-check on `Content-Length` header rejects oversized payloads before streaming begins.
   - CPython fallback streams in 64 KiB chunks via `resp.read()`, cleanly terminating early and closing the socket before downloading the remainder of the payload.
