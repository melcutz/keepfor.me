import argparse
import json
import sys
import urllib.request

def main():
    parser = argparse.ArgumentParser(description="Keepfor.me MCP Stdio Proxy for Claude Desktop & AI Agents")
    parser.add_argument("--url", required=True, help="Base URL of your Keepfor.me Worker (e.g. https://keepfor.me or https://keepfor-me.yourname.workers.dev)")
    parser.add_argument("--token", required=True, help="Keepfor.me Personal Access Token (kfm_live_...)")
    args = parser.parse_args()

    endpoint = args.url.rstrip("/") + "/api/mcp"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {args.token}",
        "User-Agent": "keepfor-me-mcp-cli/0.1.0"
    }

    # Stdio JSON-RPC line-by-line loop
    for line in sys.stdin:
        line_clean = line.strip()
        if not line_clean:
            continue
        try:
            req_data = json.loads(line_clean)
        except json.JSONDecodeError:
            continue

        # Forward request to Worker MCP endpoint
        req_bytes = json.dumps(req_data).encode("utf-8")
        req = urllib.request.Request(endpoint, data=req_bytes, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_data = resp.read().decode("utf-8")
                sys.stdout.write(resp_data + "\n")
                sys.stdout.flush()
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="replace")
            err_resp = {
                "jsonrpc": "2.0",
                "id": req_data.get("id"),
                "error": {
                    "code": -32603,
                    "message": f"Worker HTTP {err.code}: {err_body}"
                }
            }
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()
        except Exception as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": req_data.get("id"),
                "error": {
                    "code": -32603,
                    "message": f"Proxy Error: {str(exc)}"
                }
            }
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
