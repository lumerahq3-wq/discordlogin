"""
Deep probe of Minichat's roulette/chat API to find the guest auth flow.
Uses curl_cffi for proper Chrome TLS fingerprint.
"""
import json, re, time
from curl_cffi import requests as cffi_requests

s = cffi_requests.Session(impersonate="chrome120")
BASE = "https://b.minichat.com/api/v1"
WEB  = "https://minichat.com"

HDR = {
    "Origin":  WEB,
    "Referer": WEB + "/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
}

def p(label, r):
    print(f"\n{'='*60}")
    print(f"{label}  ->  HTTP {r.status_code}")
    try:
        d = r.json()
        print(json.dumps(d, indent=2)[:800])
    except:
        print(r.text[:400])

# 1. Fetch homepage to get SSE/cookies
print("Fetching homepage...")
r = s.get(WEB + "/", headers=HDR)
print(f"Homepage: {r.status_code}, cookies: {list(s.cookies.keys())}")

# 2. Hydration data from __NEXT_DATA__
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
if m:
    nd = json.loads(m.group(1))
    print("\n__NEXT_DATA__ keys:", list(nd.get('props', {}).get('pageProps', {}).get('hydration', {}).keys()))
    cfg = nd['props']['pageProps']['hydration'].get('config', {})
    print("config keys:", list(cfg.keys()))
    print("auth_url:", cfg.get('auth_url'))

# 3. Try GET /sessions (check if we have a session from cookie)
p("GET /sessions", s.get(BASE + "/sessions", headers=HDR))

# 4. Try POST /sessions with empty body
p("POST /sessions {}", s.post(BASE + "/sessions", headers=HDR, json={}))

# 5. Try GET /roulette_sessions
p("GET /roulette_sessions", s.get(BASE + "/roulette_sessions", headers=HDR))

# 6. POST /roulette_sessions
p("POST /roulette_sessions", s.post(BASE + "/roulette_sessions", headers=HDR, json={}))

# 7. Try /users with POST (register anonymous)
p("POST /users (anon)", s.post(BASE + "/users", headers=HDR, json={
    "sex": "male", "country": "us", "client": "web"
}))

# 8. Look at what the roulette iframe loads
r2 = s.get(WEB + "/", headers=HDR)
# Find any API calls in the page JS
api_calls = re.findall(r'["\`](/api/v\d[^"\`]{2,60})["\`]', r2.text)
print("\nAPI paths found in page:")
for a in sorted(set(api_calls))[:40]:
    print(" ", a)
