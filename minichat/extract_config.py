"""
Extract full __NEXT_DATA__ to understand session structure and config.
Then try to find the right headers/auth to call the API.
"""
import json, re, sys
from curl_cffi import requests as cffi_requests

s = cffi_requests.Session(impersonate="chrome120")
WEB  = "https://minichat.com"
BASE = "https://b.minichat.com/api/v1"

HDR = {
    "Origin":  WEB,
    "Referer": WEB + "/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

r = s.get(WEB + "/", headers=HDR)
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
nd = json.loads(m.group(1))
hydration = nd['props']['pageProps']['hydration']

# Print session data
print("=== SESSION ===")
print(json.dumps(hydration.get('session'), indent=2)[:1000])

print("\n=== VIDEOCHAT CONFIG ===")
print(json.dumps(hydration['config'].get('videochat'), indent=2)[:1000])

print("\n=== EMBED CONFIG ===")
print(json.dumps(hydration['config'].get('embed'), indent=2)[:500])

print("\n=== GUEST RESTRICTIONS ===")
print(json.dumps(hydration['config'].get('guest_restrictions'), indent=2)[:500])

# Find all API endpoint patterns in the JS bundle
print("\n=== Fetching main app JS for API endpoints ===")
app_js_r = s.get(WEB + "/_next/static/chunks/pages/_app-cf15ed69bfec7fa5.js", headers=HDR)
app_js = app_js_r.text

# Find fetch/axios calls
api_paths = re.findall(r'["\`](/\w[^"\`\s\)]{3,60})["\`]', app_js)
api_paths = sorted(set(p for p in api_paths if 'api' in p.lower() or 'session' in p.lower() or 'auth' in p.lower() or 'roulette' in p.lower()))
print("API-related paths in _app.js:")
for p in api_paths[:50]:
    print(" ", p)

# Try the main chunk too
chunk_r = s.get(WEB + "/_next/static/chunks/990.2f8b239282459181.js", headers=HDR)
chunk_js = chunk_r.text
print(f"\nchunk 990 size: {len(chunk_js)}")
# Look for roulette or session create patterns
patterns = re.findall(r'(?:roulette|session|auth|token|guest)[^;\n]{0,120}', chunk_js, re.I)
for p in patterns[:20]:
    print(" ", p[:120])
