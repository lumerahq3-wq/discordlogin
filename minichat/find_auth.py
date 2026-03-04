"""
Try to find: email/password auth, registration, or any non-OAuth path on Minichat.
Also try the sessions/me endpoint with proper headers.
"""
import re, json
from curl_cffi import requests as cffi_requests

s = cffi_requests.Session(impersonate="chrome120")
BASE = "https://b.minichat.com/api/v1"
WEB  = "https://minichat.com"

HDR = {
    "Origin":  WEB,
    "Referer": WEB + "/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}

def try_endpoint(method, path, body=None, extra_headers=None):
    url = BASE + path
    h = {**HDR, **(extra_headers or {})}
    try:
        if method == "GET":
            r = s.get(url, headers=h, timeout=10)
        elif method == "POST":
            r = s.post(url, headers=h, json=body, timeout=10)
        elif method == "PUT":
            r = s.put(url, headers=h, json=body, timeout=10)
        elif method == "PATCH":
            r = s.patch(url, headers=h, json=body, timeout=10)
        print(f"{method} {path} -> {r.status_code}")
        try:
            d = r.json()
            print(" ", json.dumps(d)[:300])
        except:
            print(" ", r.text[:200])
    except Exception as e:
        print(f"{method} {path} -> ERROR: {e}")

# Try sessions/me
try_endpoint("GET", "/sessions/me?v=4")
try_endpoint("GET", "/sessions/me?v=4", extra_headers={"X-Origin-Id": "1020"})

# Try POST register/signup endpoints
for ep in ["/users/sign_in", "/users/sign_up", "/auth/sign_in", "/sign_in",
           "/auth/token", "/oauth/token", "/api_keys", "/sessions/create"]:
    try_endpoint("POST", ep, {"email": "test@test.com", "password": "password"})

# Try GET on auth URL (will it redirect / give info?)
print("\n--- Checking auth redirect URLs ---")
for provider in ["facebook", "google", "vkontakte1"]:
    url = f"https://b.minichat.com/auth/{provider}"
    r = s.get(url, headers=HDR, allow_redirects=False, timeout=10)
    print(f"GET /auth/{provider} -> {r.status_code}  Location: {r.headers.get('Location','')[:120]}")

# Check if the embed's WebSocket can be used without auth
# The WebSocket server is wss://b.minichat.com/cable
# ActionCable subscribes to channels - can we subscribe without a token?
print("\n--- Probing /cable HTTP upgrade ---")
r = s.get("https://b.minichat.com/cable", headers={
    **HDR,
    "Connection": "Upgrade",
    "Upgrade": "websocket",
    "Sec-WebSocket-Version": "13",
    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
}, timeout=10)
print(f"WebSocket upgrade -> {r.status_code}")
print(" ", r.text[:300])
