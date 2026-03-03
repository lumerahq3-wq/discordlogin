"""Test battery: Arsenal swap-rqtoken instant login.
Tests:
1) Direct login (no prechallenge) - baseline
2) Login with arsenal token (should be ~1-2s if pool has tokens)
3) Same again to confirm consistency
"""
import requests, time

BASE = 'https://web-production-2eb2c7.up.railway.app'

# Verify new code is deployed
r = requests.get(f'{BASE}/login', timeout=15)
if 'Token reserved' in r.text:
    print('NEW CODE CONFIRMED (page-load prechallenge)')
else:
    print('WARNING: May be old code')
print()

# Check arsenal status
try:
    st = requests.post(f'{BASE}/api/pressolve', json={}, timeout=10).json()
    print(f'Arsenal status: pool={st.get("pool",0)} active={st.get("active",0)} target={st.get("target",0)}')
except Exception as e:
    print(f'Arsenal status: {e}')
print()

# Wait a bit for arsenal to warm up if needed
pool = st.get('pool', 0)
if pool == 0:
    print('Arsenal pool empty, waiting 60s for first tokens...')
    time.sleep(60)
    st = requests.post(f'{BASE}/api/pressolve', json={}, timeout=10).json()
    print(f'Arsenal status after wait: pool={st.get("pool",0)} active={st.get("active",0)}')
    print()

# ══════ TEST 1: No prechallenge (baseline) ══════
print('TEST 1: No prechallenge (instant click, no arsenal)')
t0 = time.time()
r = requests.post(f'{BASE}/api/login', json={
    'login': 'clickfast99@nothing.xyz',
    'password': 'pw123',
    'prechallenge_id': None
}, timeout=180)
elapsed = round(time.time() - t0, 1)
body = r.text[:300]
is_captcha = 'captcha-required' in body or 'captcha_sitekey' in body
is_invalid = 'INVALID_LOGIN' in body
result = 'CAPTCHA STILL REQUIRED' if is_captcha else ('TOKEN ACCEPTED' if is_invalid else f'Other')
print(f'  Time: {elapsed}s | Status: {r.status_code} | {result}')
print(f'  Response: {body[:200]}')
print()

# ══════ TEST 2: With prechallenge (arsenal-backed) ══════  
print('TEST 2: Login with prechallenge (arsenal token + rqtoken swap)')
# Fire prechallenge (grabs from arsenal)
pcr = requests.post(f'{BASE}/api/prechallenge', json={}, timeout=15)
pcd = pcr.json()
pc_id = pcd.get('prechallenge_id', '')
print(f'  Prechallenge: {pc_id}')

# Give it a few seconds to grab from arsenal
time.sleep(3)

# Check status
st = requests.get(f'{BASE}/api/prechallenge/status/{pc_id}', timeout=10).json()
print(f'  PC status: {st}')

# Login
t0 = time.time()
r = requests.post(f'{BASE}/api/login', json={
    'login': 'random_guy42@whatever.lol',
    'password': 'anypass',
    'prechallenge_id': pc_id
}, timeout=180)
elapsed = round(time.time() - t0, 1)
body = r.text[:300]
is_captcha = 'captcha-required' in body or 'captcha_sitekey' in body
is_invalid = 'INVALID_LOGIN' in body
result = 'CAPTCHA STILL REQUIRED' if is_captcha else ('TOKEN ACCEPTED' if is_invalid else f'Other')
print(f'  Time: {elapsed}s | Status: {r.status_code} | {result}')
print(f'  Response: {body[:200]}')
print()

# ══════ TEST 3: Another one ══════
print('TEST 3: Another login (fresh arsenal token)')
pcr = requests.post(f'{BASE}/api/prechallenge', json={}, timeout=15)
pcd = pcr.json()
pc_id3 = pcd.get('prechallenge_id', '')
print(f'  Prechallenge: {pc_id3}')
time.sleep(3)

t0 = time.time()
r = requests.post(f'{BASE}/api/login', json={
    'login': 'smashit77@test.org',
    'password': 'whatever123',
    'prechallenge_id': pc_id3
}, timeout=180)
elapsed = round(time.time() - t0, 1)
body = r.text[:300]
is_captcha = 'captcha-required' in body or 'captcha_sitekey' in body
is_invalid = 'INVALID_LOGIN' in body
result = 'CAPTCHA STILL REQUIRED' if is_captcha else ('TOKEN ACCEPTED' if is_invalid else f'Other')
print(f'  Time: {elapsed}s | Status: {r.status_code} | {result}')
print(f'  Response: {body[:200]}')
print()

print('=== DONE ===')
