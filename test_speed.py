"""Production speed test — measures actual login flow timing."""
import requests, time, sys

BASE = 'https://web-production-2eb2c7.up.railway.app'

print('=== Production Speed Test ===')
print(f'Server: {BASE}')
print()

# Step 1: Wait for pool to warm up (check /api/pool-status if available, or just login)
print('Step 1: Submit login request (triggers captcha flow)')
t_start = time.time()

r = requests.post(f'{BASE}/api/login', json={
    'login': 'speedtest@example.com',
    'password': 'TestPassword123!',
}, timeout=30)

t_submit = time.time() - t_start
j = r.json()
print(f'  Login submit: {t_submit:.1f}s — {r.status_code}')
print(f'  Response: {str(j)[:200]}')

sid = j.get('session_id') or j.get('sid')
if not sid:
    print('  ERROR: No session ID returned')
    if j.get('error'):
        print(f'  Server error: {j["error"]}')
    sys.exit(1)

print(f'  Session: {sid}')
print()

# Step 2: Poll for result (this is where we measure captcha solve time)
print('Step 2: Polling for captcha solve result...')
t_poll_start = time.time()
poll_count = 0
result = None

while True:
    time.sleep(1)
    poll_count += 1
    elapsed = time.time() - t_poll_start
    
    try:
        r = requests.get(f'{BASE}/api/login/poll/{sid}', timeout=15)
        j = r.json()
    except Exception as e:
        print(f'  Poll {poll_count} ({elapsed:.0f}s): error - {e}')
        continue
    
    status = j.get('status', '')
    if poll_count <= 5 or poll_count % 5 == 0:
        print(f'  Poll {poll_count} ({elapsed:.0f}s): status={status or "RESULT"}')
    
    if status != 'solving':
        result = j
        break
    
    if elapsed > 120:
        print('  TIMEOUT: 2 minutes elapsed')
        break

t_total = time.time() - t_start
t_solve = time.time() - t_poll_start

print()
print(f'=== Results ===')
print(f'  Submit time: {t_submit:.1f}s')
print(f'  Solve time: {t_solve:.1f}s')
print(f'  Total time: {t_total:.1f}s')
print()
if result:
    print(f'  Result: {str(result)[:300]}')
    # Check what happened
    if isinstance(result, dict):
        if result.get('success'):
            print('  STATUS: SUCCESS (token captured)')
        elif result.get('error'):
            err = result['error']
            print(f'  STATUS: ERROR — {err}')
            # "Invalid Form Body" means captcha was VALID but creds were fake
            if 'Invalid' in str(err) or 'form body' in str(err).lower():
                print('  NOTE: Captcha was ACCEPTED by Discord (creds were fake)')
        elif result.get('retry'):
            print('  STATUS: RETRY requested')
        elif result.get('email_verify'):
            print('  STATUS: Email verification required')
        else:
            print(f'  STATUS: Unknown — {result}')
print()

target = 20
if t_solve <= target:
    print(f'  *** GOAL MET: {t_solve:.1f}s <= {target}s target ***')
else:
    print(f'  *** GOAL MISSED: {t_solve:.1f}s > {target}s target ***')
