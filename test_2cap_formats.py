import requests, time, json

KEY = 'dbfdba3b6d1a7c256969070e942687c1'
API = 'https://api.2captcha.com'

print(f'Balance: ${requests.post(f"{API}/getBalance", json={"clientKey": KEY}).json().get("balance")}')
print()

# Test various task type formats
tests = [
    ("HCaptchaTaskProxyless + isEnterprise", {
        'type': 'HCaptchaTaskProxyless',
        'websiteURL': 'https://discord.com/login',
        'websiteKey': 'a5f74b19-9e45-40e0-b45d-47ff91b7a6c2',
        'isEnterprise': True,
    }),
    ("HCaptchaTaskProxyless plain", {
        'type': 'HCaptchaTaskProxyless',
        'websiteURL': 'https://discord.com/login',
        'websiteKey': 'a5f74b19-9e45-40e0-b45d-47ff91b7a6c2',
    }),
    ("HCaptchaTask plain", {
        'type': 'HCaptchaTask',
        'websiteURL': 'https://discord.com/login',
        'websiteKey': 'a5f74b19-9e45-40e0-b45d-47ff91b7a6c2',
    }),
    ("NoCaptchaTaskProxyless (reCAPTCHA style)", {
        'type': 'NoCaptchaTaskProxyless',
        'websiteURL': 'https://discord.com/login',
        'websiteKey': 'a5f74b19-9e45-40e0-b45d-47ff91b7a6c2',
    }),
]

for name, task in tests:
    r = requests.post(f'{API}/createTask', json={'clientKey': KEY, 'task': task}, timeout=15)
    j = r.json()
    eid = j.get('errorId', 0)
    tid = j.get('taskId', '-')
    err = j.get('errorDescription', j.get('errorCode', ''))
    status = 'OK' if eid == 0 else f'ERR({eid})'
    print(f'{status}: {name}')
    print(f'  taskId={tid} error={err}')
    print(f'  raw: {r.text[:200]}')
    print()

# Also try the old-style in.php with full params
print('=== OLD API in.php ===')
for label, params in [
    ("hcaptcha method", {
        'key': KEY, 'method': 'hcaptcha',
        'sitekey': 'a5f74b19-9e45-40e0-b45d-47ff91b7a6c2',
        'pageurl': 'https://discord.com/login', 'json': 1,
    }),
    ("hcaptcha enterprise", {
        'key': KEY, 'method': 'hcaptcha',
        'sitekey': 'a5f74b19-9e45-40e0-b45d-47ff91b7a6c2',
        'pageurl': 'https://discord.com/login',
        'enterprise_type': 'hcaptcha', 'json': 1,
    }),
]:
    r = requests.post(f'{API}/in.php', data=params, timeout=15)
    print(f'{label}: {r.text[:300]}')
    print()
