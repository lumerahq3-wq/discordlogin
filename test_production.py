"""Test production endpoints."""
import requests, json

BASE = 'https://web-production-2eb2c7.up.railway.app'

# Test 1: Root page
r = requests.get(f'{BASE}/', timeout=15)
print(f'GET / -> {r.status_code} ({len(r.text)} chars)')

# Test 2: Login page
r2 = requests.get(f'{BASE}/login', timeout=15)
print(f'GET /login -> {r2.status_code} ({len(r2.text)} chars)')
has_autoretry = '_autoRetryLogin' in r2.text
has_maxretry4 = 'MAX_AUTO_RETRIES = 4' in r2.text
has_rounded = 'border-radius:8px' in r2.text
print(f'  _autoRetryLogin present: {has_autoretry}')
print(f'  MAX_AUTO_RETRIES=4: {has_maxretry4}')
print(f'  Rounded inputs (8px): {has_rounded}')

# Test 3: Pre-solve API
r3 = requests.post(f'{BASE}/api/pressolve', timeout=15)
print(f'POST /api/pressolve -> {r3.status_code}: {r3.text[:200]}')

# Test 4: Poll nonexistent session (should return retry:true)
r4 = requests.get(f'{BASE}/api/login/poll/nonexistent123', timeout=15)
d4 = r4.json()
print(f'GET /api/login/poll/nonexistent -> {r4.status_code}: {json.dumps(d4)}')
assert d4.get('retry') == True, f'Expected retry=True!'
print('  PASS: retry=True returned')

# Test 5: Login with test credentials (should trigger captcha flow)
print('\nTesting login flow...')
r5 = requests.post(f'{BASE}/api/login',
    json={'login': 'test@test.com', 'password': 'test123'},
    timeout=30)
d5 = r5.json()
print(f'POST /api/login -> {r5.status_code}: {json.dumps(d5)[:300]}')
if d5.get('captcha_stall'):
    sid = d5['session_id']
    print(f'  Got captcha_stall, sid={sid}')
    
    # Poll a few times
    import time
    for i in range(100):
        time.sleep(1)
        rp = requests.get(f'{BASE}/api/login/poll/{sid}', timeout=10)
        dp = rp.json()
        if dp.get('status') == 'solving':
            if i % 10 == 0:
                print(f'  [{i}s] Still solving...')
            continue
        print(f'  [{i}s] DONE [{rp.status_code}]: {json.dumps(dp)[:400]}')
        
        # Verify no captcha-related error messages leak
        err = dp.get('error', '') or dp.get('message', '')
        bad_msgs = ['Captcha verification expired', 'Session not found', 'captcha_key']
        for bad in bad_msgs:
            if bad.lower() in err.lower():
                print(f'  FAIL: Bad error message found: "{err}"')
                break
        else:
            print(f'  PASS: No bad error messages')
        break
    else:
        print('  TIMEOUT after 100s')
else:
    print(f'  Response: {json.dumps(d5)[:300]}')

print('\nAll production tests complete!')
