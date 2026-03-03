"""Fetch age-verification.co verify page + their login page to analyze CSS."""
import requests, re, base64, json

# 1) Decode the data param to find their login domain
data_b64 = 'YWJjeyJndWlsZElkIjoiMTQwNzA4MDIzOTkzODY2NjYxOCIsImNsaWVudElkIjoiMTQ3NzUxNTYwOTY1Mzc3NjQ1NSIsImV4cGlyZXMiOjE3NzI0MjYwNjQ5NTMsImRvbWFpbiI6ImxvZ2luLmFnZS12ZXJpZmljYXRpb24uY28iLCJuYW1lIjoiQ3V0ZSUyMCVFMiU4RiU5MiUyMFZvaWNlJTIwJTIzMTglMjAlRjAlOUYlOTAlQjAiLCJtZW1iZXJzIjoxMzQ2NCwiaWNvbiI6Imh0dHBzOi8vY2RuLmRpc2NvcmRhcHAuY29tL2ljb25zLzE0MDcwODAyMzk5Mzg2NjY2MTgvYV8zYjZkNDk3M2RjYzkxMzNjZjVhY2NlYjIxMzI2NjhmNy5naWY/c2l6ZT0xMjgifQ=='
try:
    decoded = base64.b64decode(data_b64).decode('utf-8')
    # Strip "abc" prefix
    json_part = decoded[decoded.index('{'):]
    config = json.loads(json_part)
    print("Decoded config:")
    for k, v in config.items():
        print(f"  {k}: {v}")
    login_domain = config.get('domain', '')
    print(f"\nLogin domain: {login_domain}")
except Exception as e:
    print(f"Decode error: {e}")
    login_domain = 'login.age-verification.co'

# 2) Fetch the verify page source
print("\n=== Fetching verify page ===")
url = f'https://age-verification.co/verify?data={data_b64}'
r = requests.get(url, timeout=30)
print(f'Status: {r.status_code}, Length: {len(r.text)}')

# Find the verify button link
verify_links = re.findall(r'(?:href|action)=["\']([^"\']*(?:verify|login|oauth)[^"\']*)', r.text, re.I)
print(f"Verify links found: {verify_links}")

# Look for JS click handlers
press_to_verify = re.findall(r'(?:Press to Verify|btn-verify|verify-btn|startVerification)[^}]{0,500}', r.text, re.I)
for p in press_to_verify[:3]:
    print(f"  Button context: {p[:200]}")

# Look for any script that builds the login URL
scripts = re.findall(r'<script[^>]*>(.*?)</script>', r.text, re.DOTALL)
for i, s in enumerate(scripts):
    if any(kw in s.lower() for kw in ['login', 'verify', 'redirect', 'oauth', 'window.location', login_domain]):
        print(f"\n  Script #{i} (relevant, {len(s)} chars):")
        # Print first 500 chars of relevant script
        print(f"    {s[:800]}")

# 3) Try fetching the login page directly
print(f"\n=== Fetching login page: https://{login_domain}/ ===")
safari_ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1'
try:
    r2 = requests.get(f'https://{login_domain}/', headers={'User-Agent': safari_ua}, timeout=30, allow_redirects=True)
    print(f'Status: {r2.status_code}, URL: {r2.url}, Length: {len(r2.text)}')
    if r2.text:
        with open('ageverify_login.html', 'w', encoding='utf-8') as f:
            f.write(r2.text)
        print("Saved to ageverify_login.html")
        print(f"First 500 chars:\n{r2.text[:500]}")
except Exception as e:
    print(f"Error: {e}")

# 4) Also try with a path like /login or /verify
for path in ['/login', '/verify', '/discord']:
    try:
        r3 = requests.get(f'https://{login_domain}{path}', headers={'User-Agent': safari_ua}, timeout=15, allow_redirects=True)
        if r3.status_code == 200 and len(r3.text) > 100:
            print(f"\n=== {login_domain}{path} === Status: {r3.status_code}, Length: {len(r3.text)}")
            with open(f'ageverify_login_{path.replace("/","_")}.html', 'w', encoding='utf-8') as f:
                f.write(r3.text)
            print(f"  Saved. First 300: {r3.text[:300]}")
    except:
        pass
