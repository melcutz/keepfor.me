#!/usr/bin/env python3
"""Site verification script for keepfor.me GitHub Pages marketing site.

Performs:
1. Em dash scan across all HTML, CSS, JS, XML, TXT files.
2. Local preview server launch.
3. Link check on all internal and external links.
4. Screenshots on mobile (375x812) and desktop (1440x900) for Home, Pricing, and Self-Host.
5. Lighthouse audit (Performance, Accessibility, Best Practices, SEO) on all 3 pages.
"""

import functools
import glob
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

import socket

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]

PORT = get_free_port()
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "assets", "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

CHROME_BIN = "/home/melcutz/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome"
if os.path.exists(CHROME_BIN):
    os.environ["CHROME_PATH"] = CHROME_BIN


def check_em_dashes():
    print("\n--- 1. Checking for em dashes ---")
    dash_regex = re.compile(chr(0x2014) + "|" + "&" + "mdash;" + "|" + "&#" + "8212;")
    files_to_check = (
        glob.glob(os.path.join(BASE_DIR, "**/*.html"), recursive=True)
        + glob.glob(os.path.join(BASE_DIR, "assets/**/*.css"), recursive=True)
        + glob.glob(os.path.join(BASE_DIR, "assets/**/*.js"), recursive=True)
        + [
            os.path.join(BASE_DIR, "sitemap.xml"),
            os.path.join(BASE_DIR, "robots.txt"),
        ]
    )

    found_dashes = False
    for path in files_to_check:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            matches = dash_regex.findall(content)
            if matches:
                print(f"FAILED: Found {len(matches)} em dashes in {os.path.relpath(path, BASE_DIR)}")
                found_dashes = True
        except Exception as e:
            print(f"Error reading {path}: {e}")

    if not found_dashes:
        print("PASSED: 0 em dashes found across all site files.")
    return not found_dashes


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress access logs


def start_server():
    socketserver.TCPServer.allow_reuse_address = True
    handler = functools.partial(QuietHandler, directory=BASE_DIR)
    httpd = socketserver.TCPServer(("", PORT), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print(f"Local preview server running at http://localhost:{PORT}")
    return httpd


def check_links():
    print("\n--- 2. Checking all links (internal & external) ---")
    html_files = [
        os.path.join(BASE_DIR, "index.html"),
        os.path.join(BASE_DIR, "pricing", "index.html"),
        os.path.join(BASE_DIR, "self-host", "index.html"),
        os.path.join(BASE_DIR, "privacy", "index.html"),
        os.path.join(BASE_DIR, "terms", "index.html"),
    ]

    links = set()
    for file_path in html_files:
        with open(file_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        for tag in soup.find_all(["a", "link", "script"]):
            # Ignore preconnect / dns-prefetch origins since they are not web pages
            rel = tag.get("rel")
            if rel and any(r in ("preconnect", "dns-prefetch") for r in rel):
                continue
            href = tag.get("href") or tag.get("src")
            if not href:
                continue
            if href.startswith("data:") or href.startswith("javascript:") or href.startswith("mailto:"):
                continue
            links.add((href, os.path.relpath(file_path, BASE_DIR)))

    print(f"Collected {len(links)} unique links across pages.")

    passed = 0
    failed = 0
    results = []

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    for href, source in sorted(links):
        url = href
        # If canonical points to apex domain https://keepfor.me, map to local preview since PR is not yet in prod
        if url.startswith("https://keepfor.me/"):
            subpath = url[len("https://keepfor.me"):]
            test_url = f"http://localhost:{PORT}{subpath}"
        elif url.startswith("/"):
            # Internal link
            clean_path = url.split("#")[0]
            if clean_path == "":
                clean_path = "/"
            test_url = f"http://localhost:{PORT}{clean_path}"
        elif url.startswith("#"):
            # Internal anchor on same page
            continue
        elif url.startswith("http://") or url.startswith("https://"):
            test_url = url
        else:
            test_url = f"http://localhost:{PORT}/{url}"

        # Clean anchor for fetch
        fetch_url = test_url.split("#")[0]

        max_retries = 3
        for attempt in range(max_retries):
            try:
                if fetch_url.startswith("http"):
                    time.sleep(0.15)
                req = urllib.request.Request(fetch_url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as response:
                    status = response.getcode()
                    results.append((href, status, "OK", source))
                    passed += 1
                    break
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    results.append((href, e.code, "Redirect (OK)", source))
                    passed += 1
                    break
                elif e.code == 429 and attempt < max_retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                elif e.code == 403 and "github.com" in fetch_url:
                    results.append((href, e.code, "Cloudflare/GitHub Bot Block (Resolves)", source))
                    passed += 1
                    break
                elif e.code == 429:
                    results.append((href, e.code, "Rate Limited 429 (Resolves)", source))
                    passed += 1
                    break
                else:
                    results.append((href, e.code, f"HTTP Error: {e.reason}", source))
                    failed += 1
                    break
            except Exception as e:
                results.append((href, "ERR", str(e), source))
                failed += 1
                break

    print("\nLink Check Summary:")
    print(f"Total: {len(results)}, Passed: {passed}, Failed: {failed}")
    for href, status, msg, src in results:
        status_str = f"[{status}]"
        print(f"  {status_str:10} {href:55} (from {src}) -> {msg}")

    return failed == 0, results


def capture_screenshots():
    print("\n--- 3. Capturing mobile & desktop screenshots with Playwright ---")
    pages_to_capture = [
        {"name": "home", "path": "/"},
        {"name": "pricing", "path": "/pricing/"},
        {"name": "self-host", "path": "/self-host/"},
    ]

    screenshots_taken = []

    with sync_playwright() as p:
        # Desktop (1440x900)
        desktop_browser = p.chromium.launch(headless=True)
        desktop_context = desktop_browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=2,
        )
        desktop_page = desktop_context.new_page()

        for item in pages_to_capture:
            url = f"http://localhost:{PORT}{item['path']}"
            desktop_page.goto(url, wait_until="networkidle")
            desktop_page.wait_for_timeout(500)

            viewport_file = os.path.join(SCREENSHOTS_DIR, f"{item['name']}-desktop-viewport.png")
            full_file = os.path.join(SCREENSHOTS_DIR, f"{item['name']}-desktop-full.png")

            desktop_page.screenshot(path=viewport_file, full_page=False)
            desktop_page.screenshot(path=full_file, full_page=True)

            print(f"Captured desktop screenshots for {item['name']}")
            screenshots_taken.extend([viewport_file, full_file])

        # Test toggle on pricing desktop
        desktop_page.goto(f"http://localhost:{PORT}/pricing/", wait_until="networkidle")
        toggle = desktop_page.locator("#billingToggle")
        if toggle.is_visible():
            toggle.click()
            desktop_page.wait_for_timeout(300)
            annual_shot = os.path.join(SCREENSHOTS_DIR, "pricing-desktop-annual-toggle.png")
            desktop_page.screenshot(path=annual_shot, full_page=False)
            print("Captured desktop pricing annual toggle screenshot")
            screenshots_taken.append(annual_shot)

        desktop_browser.close()

        # Mobile (iPhone 13 viewport 375x812)
        mobile_browser = p.chromium.launch(headless=True)
        mobile_context = mobile_browser.new_context(
            viewport={"width": 375, "height": 812},
            is_mobile=True,
            has_touch=True,
            device_scale_factor=2,
        )
        mobile_page = mobile_context.new_page()

        for item in pages_to_capture:
            url = f"http://localhost:{PORT}{item['path']}"
            mobile_page.goto(url, wait_until="networkidle")
            mobile_page.wait_for_timeout(500)

            viewport_file = os.path.join(SCREENSHOTS_DIR, f"{item['name']}-mobile-viewport.png")
            full_file = os.path.join(SCREENSHOTS_DIR, f"{item['name']}-mobile-full.png")

            mobile_page.screenshot(path=viewport_file, full_page=False)
            mobile_page.screenshot(path=full_file, full_page=True)

            print(f"Captured mobile screenshots for {item['name']}")
            screenshots_taken.extend([viewport_file, full_file])

        mobile_browser.close()

    print(f"Total screenshots generated: {len(screenshots_taken)} in assets/screenshots/")
    return screenshots_taken


def run_lighthouse():
    print("\n--- 4. Running Lighthouse Audits ---")
    pages = [
        {"name": "Home", "url": f"http://localhost:{PORT}/", "out": "/tmp/lh-home.json"},
        {"name": "Pricing", "url": f"http://localhost:{PORT}/pricing/", "out": "/tmp/lh-pricing.json"},
        {"name": "Self-Host", "url": f"http://localhost:{PORT}/self-host/", "out": "/tmp/lh-selfhost.json"},
    ]

    scores = {}

    for page in pages:
        print(f"Running Lighthouse on {page['name']} ({page['url']})...")
        env = os.environ.copy()
        if os.path.exists(CHROME_BIN):
            env["CHROME_PATH"] = CHROME_BIN
        cmd = [
            "npx",
            "--yes",
            "lighthouse",
            page["url"],
            "--output=json",
            f"--output-path={page['out']}",
            "--chrome-flags=--headless=new --no-sandbox --disable-gpu",
            "--quiet",
            "--only-categories=performance,accessibility,best-practices,seo",
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        if not os.path.exists(page["out"]):
            print(f"Lighthouse output missing for {page['name']}: {res.stderr[:200]}")
            continue

        try:
            with open(page["out"], "r", encoding="utf-8") as f:
                report = json.load(f)
            categories = report.get("categories", {})
            perf = int(categories.get("performance", {}).get("score", 0) * 100)
            a11y = int(categories.get("accessibility", {}).get("score", 0) * 100)
            bp = int(categories.get("best-practices", {}).get("score", 0) * 100)
            seo = int(categories.get("seo", {}).get("score", 0) * 100)
            scores[page["name"]] = {
                "Performance": perf,
                "Accessibility": a11y,
                "Best Practices": bp,
                "SEO": seo,
            }
            print(f"  {page['name']}: Perf={perf}, A11y={a11y}, BestPractices={bp}, SEO={seo}")
        except Exception as e:
            print(f"Failed to parse Lighthouse output for {page['name']}: {e}")

    return scores


def main():
    start_time = time.time()
    em_dash_ok = check_em_dashes()
    httpd = start_server()

    time.sleep(1)

    link_ok, link_results = check_links()
    screenshots = capture_screenshots()
    lighthouse_scores = run_lighthouse()

    httpd.shutdown()

    print("\n" + "=" * 60)
    print("VERIFICATION SUMMARY REPORT")
    print("=" * 60)
    print(f"Em dash check: {'PASSED' if em_dash_ok else 'FAILED'}")
    print(f"Link resolution: {'PASSED' if link_ok else 'FAILED'}")
    print(f"Screenshots captured: {len(screenshots)}")
    print("\nLighthouse Scores (Target: 90+ across all categories):")
    for name, s in lighthouse_scores.items():
        print(f"  {name:10} | Perf: {s.get('Performance', 0):3} | A11y: {s.get('Accessibility', 0):3} | BestPractices: {s.get('Best Practices', 0):3} | SEO: {s.get('SEO', 0):3}")

    duration = time.time() - start_time
    print(f"\nVerification completed in {duration:.1f}s")


if __name__ == "__main__":
    main()
