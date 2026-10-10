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
