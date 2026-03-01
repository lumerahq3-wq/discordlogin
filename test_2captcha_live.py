import requests, time, json

KEY = 'dbfdba3b6d1a7c256969070e942687c1'
API = 'https://api.2captcha.com'

# Step 1: Check balance
print('=== Step 1: Check Balance ===')
r = requests.post(f'{API}/getBalance', json={'clientKey': KEY}, timeout=15)
print(f'Status: {r.status_code}')
print(f'Response: {r.text}')
j = r.json()
if j.get('errorId', 0) != 0:
    print(f'ERROR: {j.get("errorCode")} - {j.get("errorDescription")}')
    exit(1)
print(f'Balance: ${j.get("balance", "?")}')
print()

# Step 2: Create task
print('=== Step 2: Create HCaptcha Task ===')
payload = {
    'clientKey': KEY,
    'task': {
        'type': 'HCaptchaTaskProxyless',
        'websiteURL': 'https://discord.com/login',
        'websiteKey': 'a5f74b19-9e45-40e0-b45d-47ff91b7a6c2',
        'isEnterprise': True,
    }
}
print(f'Sending to {API}/createTask')
r = requests.post(f'{API}/createTask', json=payload, timeout=30)
print(f'Status: {r.status_code}')
print(f'Response: {r.text[:500]}')
j = r.json()

if j.get('errorId', 0) != 0:
    code = j.get('errorCode', '?')
    desc = j.get('errorDescription', '?')
    print(f'CREATE ERROR: {code} - {desc}')
    
    # Try alternative format without isEnterprise
    print('\n=== Retry WITHOUT isEnterprise ===')
    payload['task'] = {
        'type': 'HCaptchaTaskProxyless',
        'websiteURL': 'https://discord.com/login',
        'websiteKey': 'a5f74b19-9e45-40e0-b45d-47ff91b7a6c2',
    }
    r = requests.post(f'{API}/createTask', json=payload, timeout=30)
    print(f'Status: {r.status_code}')
    print(f'Response: {r.text[:500]}')
    j = r.json()
    if j.get('errorId', 0) != 0:
        print(f'STILL ERROR: {j.get("errorCode")} - {j.get("errorDescription")}')
        
        # Try the old-style API
        print('\n=== Retry with OLD API (in.php) ===')
        r = requests.get(f'{API}/in.php', params={
            'key': KEY,
            'method': 'hcaptcha',
            'sitekey': 'a5f74b19-9e45-40e0-b45d-47ff91b7a6c2',
            'pageurl': 'https://discord.com/login',
            'json': 1,
        }, timeout=30)
        print(f'Status: {r.status_code}')
        print(f'Response: {r.text[:500]}')
        exit(1)

task_id = j.get('taskId')
if not task_id:
    print('ERROR: No taskId in response')
    exit(1)

print(f'TaskId: {task_id}')

# Step 3: Poll for result
print(f'\n=== Step 3: Polling ===')
t0 = time.time()
for i in range(120):
    time.sleep(2)
    r = requests.post(f'{API}/getTaskResult', json={'clientKey': KEY, 'taskId': task_id}, timeout=15)
    j = r.json()
    elapsed = time.time() - t0
    status = j.get('status', '?')
    if status == 'ready':
        token = j.get('solution', {}).get('gRecaptchaResponse', '')
        print(f'\nSOLVED in {elapsed:.1f}s! Token length: {len(token)}')
        print(f'Token: {token[:80]}...')
        print('\n=== SUCCESS ===')
        exit(0)
    if j.get('errorId', 0) != 0:
        code = j.get('errorCode', '?')
        desc = j.get('errorDescription', '?')
        print(f'POLL ERROR at {elapsed:.1f}s: {code} - {desc}')
        exit(1)
    if i % 5 == 0:
        print(f'  Waiting... {elapsed:.0f}s (status={status})')
    if elapsed > 120:
        print('TIMEOUT after 120s')
        exit(1)
