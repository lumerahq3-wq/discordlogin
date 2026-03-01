"""Test predictive pre-challenge: start solving on page load, use token on login."""
import requests, time

BASE = 'https://web-production-2eb2c7.up.railway.app'

print('=== Predictive Pre-Challenge Speed Test ===')
print(f'Server: {BASE}')
print()

# Step 1: Simulate page load (frontend calls /api/prechallenge)
print('Step 1: Start pre-challenge (simulating page load)...')
t0 = time.time()
r = requests.post(f'{BASE}/api/prechallenge', timeout=30)
j = r.json()
t_prechallenge = time.time() - t0
pc_id = j.get('prechallenge_id')
print(f'  Response: {j}')
print(f'  Pre-challenge started in {t_prechallenge:.1f}s, ID: {pc_id}')
print()

# Step 2: Simulate user typing (wait for captcha to solve in background)
TYPING_TIME = 20  # seconds a user spends typing email + password
print(f'Step 2: Simulating user typing for {TYPING_TIME}s...')
time.sleep(TYPING_TIME)
print(f'  Done waiting. Captcha should be solved by now.')
print()

# Step 3: Submit login with pre-challenge ID
print('Step 3: Submit login with pre-challenge ID...')
t_login_start = time.time()
r = requests.post(f'{BASE}/api/login', json={
    'login': 'test@test.com',
    'password': 'TestPassword123!',
    'prechallenge_id': pc_id,
}, timeout=30)
j = r.json()
t_login = time.time() - t_login_start

print(f'  Login response in {t_login:.1f}s: {r.status_code}')
print(f'  Body: {str(j)[:300]}')
print()

# Check if it was instant (pre-challenge worked) or stalled (fell back)
if j.get('captcha_stall'):
    # Pre-challenge token was rejected, fell back to normal captcha solve
    sid = j.get('session_id')
    print(f'  Pre-challenge REJECTED — fell back to normal flow (session={sid})')
    print(f'  Polling for result...')
    while True:
        time.sleep(1)
        elapsed = time.time() - t_login_start
        try:
            r = requests.get(f'{BASE}/api/login/poll/{sid}', timeout=15)
            j = r.json()
        except:
            continue
        if j.get('status') != 'solving':
            break
        if elapsed > 90:
            print('  TIMEOUT')
            break
    t_total = time.time() - t_login_start
    print(f'  Result after {t_total:.1f}s: {str(j)[:200]}')
elif j.get('success'):
    print(f'  *** INSTANT SUCCESS via pre-challenge! ***')
elif j.get('error'):
    err = j.get('error', '')
    if 'Invalid' in err:
        print(f'  ** INSTANT result via pre-challenge ({t_login:.1f}s)! Captcha ACCEPTED by Discord **')
    else:
        print(f'  Error: {err}')
elif j.get('ticket'):
    print(f'  MFA required (pre-challenge worked!)')
else:
    print(f'  Unknown: {j}')

t_total_all = time.time() - t0
print()
print(f'=== Summary ===')
print(f'  Pre-challenge init: {t_prechallenge:.1f}s')
print(f'  User typing time: {TYPING_TIME}s')
print(f'  Login response time: {t_login:.1f}s')
print(f'  Total wall time: {t_total_all:.1f}s')
print(f'  Perceived wait (from login click): {t_login:.1f}s')
target = 5
if t_login <= target:
    print(f'  *** PRE-CHALLENGE GOAL MET: {t_login:.1f}s <= {target}s ***')
elif t_login <= 20:
    print(f'  *** UNDER 20s GOAL MET: {t_login:.1f}s <= 20s ***')
else:
    print(f'  Goal missed: {t_login:.1f}s > 20s')
