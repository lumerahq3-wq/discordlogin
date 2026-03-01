"""Run 3 consecutive speed tests to measure distribution."""
import requests, time, sys

BASE = 'https://web-production-2eb2c7.up.railway.app'
results = []

for run in range(3):
    print(f'\n=== Run {run+1}/3 ===')
    
    t_start = time.time()
    r = requests.post(f'{BASE}/api/login', json={
        'login': f'test{run}@test.com',
        'password': 'TestPassword123!',
    }, timeout=30)
    j = r.json()
    sid = j.get('session_id')
    
    if not sid:
        print(f'  No session: {j}')
        results.append(('FAIL', 0))
        time.sleep(5)
        continue
    
    # Poll
    while True:
        time.sleep(1)
        elapsed = time.time() - t_start
        try:
            r = requests.get(f'{BASE}/api/login/poll/{sid}', timeout=15)
            j = r.json()
        except:
            continue
        
        if j.get('status') != 'solving':
            total = time.time() - t_start
            accepted = 'Invalid' in str(j.get('error', ''))
            status = 'ACCEPTED' if accepted else 'RESULT'
            print(f'  {status}: {total:.1f}s')
            results.append((status, total))
            break
        
        if elapsed > 90:
            print(f'  TIMEOUT: >90s')
            results.append(('TIMEOUT', elapsed))
            break
    
    time.sleep(3)  # Brief pause between runs

print(f'\n=== Summary ===')
times = [t for s, t in results if s in ('ACCEPTED', 'RESULT')]
for i, (s, t) in enumerate(results):
    marker = ' ***' if t <= 20 else ''
    print(f'  Run {i+1}: {t:.1f}s ({s}){marker}')
if times:
    print(f'  Avg: {sum(times)/len(times):.1f}s')
    print(f'  Min: {min(times):.1f}s')
    print(f'  Max: {max(times):.1f}s')
    under_20 = sum(1 for t in times if t <= 20)
    print(f'  Sub-20s: {under_20}/{len(times)}')
