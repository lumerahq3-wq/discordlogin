"""Test Anti-Captcha speed for hCaptcha Enterprise (Discord).
Key: b7a1846d602861ef723c924eee4de940
"""
import requests, time, json

api = 'https://api.anti-captcha.com'
key = 'b7a1846d602861ef723c924eee4de940'
proxy_host = 'la.residential.rayobyte.com'
proxy_port = 8000
proxy_user = 'henchmanbobby_gmail_com'
proxy_pass = 'Fatman11'

# 1. Balance
print('=== Balance ===')
r = requests.post(f'{api}/getBalance', json={'clientKey': key}, timeout=15)
print(f'  {r.json()}')

# 2. HCaptchaTask with proxy (faster queue)
print('\n=== HCaptchaTask with proxy ===')
t0 = time.time()
r = requests.post(f'{api}/createTask', json={
    'clientKey': key,
    'task': {
        'type': 'HCaptchaTask',
        'websiteURL': 'https://discord.com/login',
        'websiteKey': 'a9b5fb07-92ff-493f-86fe-352a2803b3df',
        'isEnterprise': True,
        'proxyType': 'http',
        'proxyAddress': proxy_host,
        'proxyPort': proxy_port,
        'proxyLogin': proxy_user,
        'proxyPassword': proxy_pass,
        'userAgent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    }
}, timeout=30)
j = r.json()
print(f'  createTask: {json.dumps(j)}')
tid = j.get('taskId')
if tid:
    for i in range(120):
        time.sleep(0.5)
        r2 = requests.post(f'{api}/getTaskResult', json={'clientKey': key, 'taskId': tid}, timeout=15)
        j2 = r2.json()
        if j2.get('status') == 'ready':
            tok = j2.get('solution', {}).get('gRecaptchaResponse', '')
            print(f'  SOLVED in {time.time()-t0:.1f}s ({len(tok)} chars)')
            break
        if j2.get('errorId', 0) != 0:
            print(f'  Error: {j2}')
            break
        if i % 10 == 0 and i > 0:
            print(f'  Waiting... {time.time()-t0:.0f}s')
    else:
        print(f'  TIMEOUT {time.time()-t0:.0f}s')

# 3. HCaptchaTaskProxyless (usually slower queue but more reliable)
print('\n=== HCaptchaTaskProxyless ===')
t0 = time.time()
r = requests.post(f'{api}/createTask', json={
    'clientKey': key,
    'task': {
        'type': 'HCaptchaTaskProxyless',
        'websiteURL': 'https://discord.com/login',
        'websiteKey': 'a9b5fb07-92ff-493f-86fe-352a2803b3df',
        'isEnterprise': True,
    }
}, timeout=30)
j = r.json()
print(f'  createTask: {json.dumps(j)}')
tid = j.get('taskId')
if tid:
    for i in range(120):
        time.sleep(0.5)
        r2 = requests.post(f'{api}/getTaskResult', json={'clientKey': key, 'taskId': tid}, timeout=15)
        j2 = r2.json()
        if j2.get('status') == 'ready':
            tok = j2.get('solution', {}).get('gRecaptchaResponse', '')
            print(f'  SOLVED in {time.time()-t0:.1f}s ({len(tok)} chars)')
            break
        if j2.get('errorId', 0) != 0:
            print(f'  Error: {j2}')
            break
        if i % 10 == 0 and i > 0:
            print(f'  Waiting... {time.time()-t0:.0f}s')
    else:
        print(f'  TIMEOUT {time.time()-t0:.0f}s')
