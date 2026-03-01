"""Direct API test — no browser needed."""
import requests, json, time

BASE = 'https://web-production-2eb2c7.up.railway.app'

# Step 1: Login
print('=== Step 1: Login ===')
r = requests.post(f'{BASE}/api/login', json={
    'login': 'fortbot7@inbox.lv',
    'password': 'Fatman11$',
    'undelete': False,
    'login_source': None,
    'gift_code_sku_id': None,
}, timeout=30)
print(f'Status: {r.status_code}')
d = r.json()
print(json.dumps(d, indent=2)[:500])

if not d.get('captcha_stall'):
    print('No captcha stall — check result above')
    exit(1)

sid = d.get('session_id')
print(f'\nSession ID: {sid}')

# Step 2: Poll until done
print('\n=== Step 2: Polling... ===')
result = None
for i in range(80):
    time.sleep(3)
    r2 = requests.get(f'{BASE}/api/login/poll/{sid}', timeout=15)
    d2 = r2.json()
    if d2.get('status') == 'solving':
        if i % 3 == 0:
            print(f'  Still solving ({i*3}s)...')
        continue
    print(f'\nPoll result [{r2.status_code}]:')
    print(json.dumps(d2, indent=2)[:800])
    result = d2
    break

if not result:
    print('Timed out waiting for solve!')
    exit(1)

# Check for email verify → retry
if result.get('email_verify'):
    print('\n=== EMAIL VERIFICATION REQUIRED ===')
    print('Check fortbot7@inbox.lv for Discord verification email.')
    print('Click the link, then press Enter here...')
    input('>> ')
    
    print('\n=== Step 3: Retrying after email verify... ===')
    r3 = requests.post(f'{BASE}/api/login/retry/{sid}', timeout=30)
    d3 = r3.json()
    print(f'Retry response: {json.dumps(d3, indent=2)[:500]}')
    
    if d3.get('captcha_stall'):
        newsid = d3.get('session_id', sid)
        print(f'Polling retry (sid={newsid})...')
        for i in range(80):
            time.sleep(3)
            r4 = requests.get(f'{BASE}/api/login/poll/{newsid}', timeout=15)
            d4 = r4.json()
            if d4.get('status') == 'solving':
                if i % 3 == 0:
                    print(f'  Still solving ({i*3}s)...')
                continue
            print(f'\nRetry result [{r4.status_code}]:')
            print(json.dumps(d4, indent=2)[:800])
            break

elif result.get('success'):
    print('\n+++ SUCCESS! Token sent to webhook +++')

elif result.get('ticket') and result.get('mfa') is not None:
    print(f'\n+++ MFA REQUIRED! ticket={result["ticket"][:20]}... +++')

else:
    print(f'\n--- Other result (check above) ---')
