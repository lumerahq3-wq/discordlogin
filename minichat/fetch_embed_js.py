from curl_cffi import requests as cffi_requests
import re, json

s = cffi_requests.Session(impersonate="chrome120")
HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://minichat.com/embed/index.html"
}

r = s.get("https://r.minichat.com/scripts/main.js?t=1770126287291", headers=HDR)
print(f"Status: {r.status_code}  Size: {len(r.text)}")
js = r.text

with open("minichat/main_embed.js", "w", encoding="utf-8") as f:
    f.write(js)
print("Saved to minichat/main_embed.js")

# Find session/auth related code
print("\n=== Session/auth patterns ===")
for m in re.finditer(r'.{0,60}(session|apiKey|token|auth|login|guest|signIn|signUp|roulette|start|connect).{0,60}', js, re.I):
    print(m.group(0)[:130])

print("\n=== URL patterns ===")
urls = re.findall(r'"(https?://[^"]{5,100})"', js)
for u in sorted(set(urls))[:40]:
    print(" ", u)

print("\n=== API paths ===")
paths = re.findall(r'"(/[a-z][a-z0-9_/]{3,60})"', js)
for p in sorted(set(paths))[:40]:
    print(" ", p)
