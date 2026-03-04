"""Probe ome.tv to understand the guest session mechanism."""
import requests, json, re

s = requests.Session()
s.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
    'Origin': 'https://ome.tv',
    'Referer': 'https://ome.tv/',
    'Accept-Language': 'en-US,en;q=0.9',
})

print("=== GET https://ome.tv/ ===")
r = s.get('https://ome.tv/', timeout=10)
print(f"Status: {r.status_code}  len={len(r.text)}")
print(f"Cookies set: {dict(r.cookies)}")

# Extract key JS/config from HTML
html = r.text
for pattern, label in [
    (r'csrf.?token["\s:=]+(["\w\-]+)', 'CSRF token'),
    (r'_token["\s:=]+["\']([\w\-]+)', '_token'),
    (r'window\.__(?:INITIAL|STATE|CONFIG|APP)[^=]+=\s*(\{[^;]+)', 'window state'),
    (r'<meta name="csrf-token" content="([^"]+)"', 'meta csrf'),
    (r'var\s+token\s*=\s*["\']([^"\']+)', 'var token'),
    (r'"accessToken"\s*:\s*"([^"]+)"', 'accessToken'),
    (r'guest["\s:=]+["\']([\w\-]+)', 'guest value'),
    (r'<script[^>]+src="([^"]*(?:app|main|bundle|chat)[^"]*\.js[^"]*)"', 'main JS'),
]:
    m = re.search(pattern, html, re.IGNORECASE)
    if m:
        print(f"[{label}]: {m.group(1)[:120]}")

print("\n=== Key cookies after homepage load ===")
for c in s.cookies:
    print(f"  {c.name}={c.value[:60]}  domain={c.domain}")

print("\n=== Probing API endpoints ===")
endpoints = [
    ('GET',  'https://ome.tv/api/'),
    ('POST', 'https://ome.tv/guest'),
    ('POST', 'https://ome.tv/api/guest'),
    ('GET',  'https://ome.tv/api/session'),
    ('POST', 'https://ome.tv/api/session'),
    ('GET',  'https://ome.tv/api/user'),
    ('GET',  'https://ome.tv/api/config'),
    ('POST', 'https://ome.tv/login'),
    ('POST', 'https://ome.tv/api/login'),
    ('GET',  'https://ome.tv/chat'),
]
for method, url in endpoints:
    try:
        if method == 'GET':
            resp = s.get(url, timeout=6)
        else:
            resp = s.post(url, json={}, timeout=6)
        ct = resp.headers.get('content-type', '')
        print(f"  {method} {url.replace('https://ome.tv','')} -> {resp.status_code}  ct={ct[:30]}  body={resp.text[:120]}")
    except Exception as e:
        print(f"  {method} {url.replace('https://ome.tv','')} -> ERROR: {e}")

print("\n=== Inline scripts (checking for guest/session/token logic) ===")
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
for i, sc in enumerate(scripts):
    sc = sc.strip()
    if sc and len(sc) > 20:
        hits = re.findall(r'(?:guest|token|session|csrf|auth|api)[^;]{0,80}', sc, re.IGNORECASE)
        if hits:
            print(f"Script {i}: {hits[:4]}")
