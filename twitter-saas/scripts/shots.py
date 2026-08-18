"""Screenshot every dashboard page for visual verification.

Usage: python scripts/shots.py <token> [base_url] [outdir]

Seeds the auth token into localStorage the same way api.js does, so the SPA
renders authenticated without driving the login form.
"""
import sys
import pathlib
import time

from playwright.sync_api import sync_playwright

TOKEN = sys.argv[1]
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8080"
OUT = pathlib.Path(sys.argv[3] if len(sys.argv) > 3 else "/tmp/tdf_shots")
OUT.mkdir(parents=True, exist_ok=True)

PAGES = [
    ("pulse", "/"),
    ("feed", "/feed"),
    ("analyze", "/analyze"),
    ("accounts", "/accounts"),
    ("ops", "/ops"),
    ("searches", "/searches"),
]
VIEWPORTS = {"desktop": (1440, 900), "mobile": (390, 844)}

with sync_playwright() as p:
    browser = p.chromium.launch()
    for label, (width, height) in VIEWPORTS.items():
        ctx = browser.new_context(viewport={"width": width, "height": height})
        # api.js reads the token from localStorage under this key.
        ctx.add_init_script(f"localStorage.setItem('tsaas_token', '{TOKEN}');")
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        for name, route in PAGES:
            page.goto(f"{BASE}{route}", wait_until="networkidle")
            time.sleep(1.5)  # let recharts finish its entry animation
            path = OUT / f"{label}-{name}.png"
            page.screenshot(path=str(path), full_page=(label == "desktop"))
            print(f"{path}  errors={len(errors)}")
            if errors:
                print("   ", errors[-3:])
            errors.clear()
        ctx.close()
    browser.close()
print("done ->", OUT)
