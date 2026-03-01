"""Diagnose CapSolver key status and test different approaches."""
import requests, time, json

api = 'https://api.capsolver.com'
key = 'CAP-081ECA8ECC5191C4E96A5AB50E5D7534'

# 1. Check balance
print('=== Balance Check ===')
r = requests.post(f'{api}/getBalance', json={'clientKey': key}, timeout=15)
j = r.json()
print(f'  Response: {json.dumps(j, indent=2)}')

# 2. Try HCaptchaClassification (different approach)
print('\n=== Try HCaptchaTask with full enterprise payload ===')
task = {
    'type': 'HCaptchaTask',
    'websiteURL': 'https://discord.com/login',
    'websiteKey': 'a9b5fb07-92ff-493f-86fe-352a2803b3df',
    'isEnterprise': True,
    'proxy': 'http://henchmanbobby_gmail_com:Fatman11@la.residential.rayobyte.com:8000',
    'userAgent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
}
r = requests.post(f'{api}/createTask', json={'clientKey': key, 'task': task}, timeout=30)
j = r.json()
print(f'  Response: {json.dumps(j, indent=2)}')

# 3. Try without isEnterprise
print('\n=== Try HCaptchaTaskProxyLess without isEnterprise ===')
task2 = {
    'type': 'HCaptchaTaskProxyLess',
    'websiteURL': 'https://discord.com/login',
    'websiteKey': 'a9b5fb07-92ff-493f-86fe-352a2803b3df',
}
r = requests.post(f'{api}/createTask', json={'clientKey': key, 'task': task2}, timeout=30)
j = r.json()
print(f'  Response: {json.dumps(j, indent=2)}')

# 4. Try a simple reCaptcha to verify the key works at all
print('\n=== Try simple hCaptcha on different site (verification) ===')
task3 = {
    'type': 'HCaptchaTaskProxyLess',
    'websiteURL': 'https://accounts.hcaptcha.com/demo',
    'websiteKey': 'a5f74b19-9e45-40e0-b45d-47ff91b7a6c2',
}
r = requests.post(f'{api}/createTask', json={'clientKey': key, 'task': task3}, timeout=30)
j = r.json()
print(f'  Response: {json.dumps(j, indent=2)}')
if j.get('taskId'):
    tid = j['taskId']
    t0 = time.time()
    for i in range(60):
        time.sleep(0.5)
        r2 = requests.post(f'{api}/getTaskResult', json={'clientKey': key, 'taskId': tid}, timeout=15)
        j2 = r2.json()
        if j2.get('status') == 'ready':
            print(f'  SOLVED demo in {time.time()-t0:.1f}s')
            break
        if j2.get('errorId', 0) != 0:
            print(f'  Error: {j2}')
            break
