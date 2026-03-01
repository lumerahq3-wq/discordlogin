"""Benchmark captcha solve times — test multiple approaches."""
import time, sys, os, threading
sys.path.insert(0, '.')
os.environ['ANTICAPTCHA_KEY'] = 'b7a1846d602861ef723c924eee4de940'

import discord_server as ds

def bench_solve(label, sitekey, rqdata=''):
    t0 = time.time()
    token, err = ds.solve_captcha(sitekey, rqdata)
    elapsed = time.time() - t0
    if token:
        print(f'  {label}: OK {elapsed:.1f}s ({len(token)} chars)')
    else:
        print(f'  {label}: FAIL {elapsed:.1f}s — {err}')
    return elapsed, bool(token)

print('=== Anti-Captcha Solve Speed Benchmark ===')
print(f'Key: {ds.ANTICAPTCHA_KEY[:8]}***')
print()

# Test 1: 3 parallel solves without rqdata  
print('Test 1: 3x parallel solve (no rqdata)')
results = []
def _solve(idx):
    e, ok = bench_solve(f'Solve-{idx}', 'a9b5fb07-92ff-493f-86fe-352a2803b3df', '')
    results.append((e, ok))

threads = [threading.Thread(target=_solve, args=(i,)) for i in range(3)]
t0 = time.time()
for t in threads: t.start()
for t in threads: t.join()
total = time.time() - t0
ok_count = sum(1 for _, ok in results if ok)
avg = sum(e for e, ok in results if ok) / max(ok_count, 1)
print(f'  Wall time: {total:.1f}s, Success: {ok_count}/3, Avg solve: {avg:.1f}s')
print()

# Test 2: Check if pool has tokens after the solves above stopped
print(f'Test 2: Pool state — {len(ds._presolve_pool)} tokens ready, {ds._presolve_active} active')
print()

# Test 3: Get presolved token  
print('Test 3: Get presolved token (should be instant if pool has tokens)')
t0 = time.time()
tok = ds._get_presolved(required_sitekey='a9b5fb07-92ff-493f-86fe-352a2803b3df')
e = time.time() - t0
print(f'  Got token: {bool(tok)} in {e*1000:.0f}ms')
print()

print('=== Benchmark Complete ===')
