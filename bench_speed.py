"""Benchmark captcha solve speeds with all optimizations."""
import time, threading, sys, os
sys.path.insert(0, '.')
os.environ['ANTICAPTCHA_KEY'] = 'b7a1846d602861ef723c924eee4de940'
import discord_server as ds

SITEKEY = 'a9b5fb07-92ff-493f-86fe-352a2803b3df'

print('=== Speed Benchmark: Anti-Captcha ===')
print(f'Pool: {len(ds._presolve_pool)} tokens, {ds._presolve_active} active workers')
print()

# Test 1: Single solve speed (adaptive polling)
print('Test 1: Single solve (adaptive polling)')
t0 = time.time()
token, err = ds.solve_captcha(SITEKEY, '')
t1 = time.time() - t0
ok1 = 'OK' if token else 'FAIL'
print(f'  {ok1} in {t1:.1f}s' + (f' - {err}' if err else ''))
print()

# Test 2: Race solve (2 parallel)
print('Test 2: Race solve (2 parallel)')
t0 = time.time()
token2, err2 = ds._solve_race(SITEKEY, '', n=2)
t2 = time.time() - t0
ok2 = 'OK' if token2 else 'FAIL'
print(f'  {ok2} in {t2:.1f}s' + (f' - {err2}' if err2 else ''))
print()

# Wait for pool to fill
print('Waiting 5s for pool to fill...')
time.sleep(5)
print(f'Pool state: {len(ds._presolve_pool)} tokens, {ds._presolve_active} active')

# Test 3: Get presolved token
print()
print('Test 3: Get presolved token')
t0 = time.time()
presolved = ds._get_presolved(SITEKEY)
t3 = (time.time() - t0) * 1000
if presolved:
    print(f'  GOT presolved in {t3:.0f}ms ({len(presolved)} chars)')
else:
    print(f'  No presolved available ({t3:.0f}ms)')
print()

# Summary
print(f'=== Summary ===')
print(f'  Single solve: {t1:.1f}s')
print(f'  Race solve (2x): {t2:.1f}s')
print(f'  Presolved: {"instant" if presolved else "empty pool"}')
print(f'  Pool: {len(ds._presolve_pool)} tokens ready')
