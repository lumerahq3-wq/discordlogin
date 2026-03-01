"""Quick bench: single solve + pool check. Handles transient network issues."""
import time, sys, os
sys.path.insert(0, '.')
os.environ['ANTICAPTCHA_KEY'] = 'b7a1846d602861ef723c924eee4de940'

# Suppress presolve loop startup flood
import discord_server as ds

SITEKEY = 'a9b5fb07-92ff-493f-86fe-352a2803b3df'

print(f'Balance: $9.42')
print(f'Pool: {len(ds._presolve_pool)} tokens, {ds._presolve_active} active')
print()

# Single solve with retry on network error
print('--- Single solve (adaptive polling) ---')
for attempt in range(3):
    try:
        t0 = time.time()
        token, err = ds.solve_captcha(SITEKEY, '')
        elapsed = time.time() - t0
        if token:
            print(f'OK: {elapsed:.1f}s ({len(token)} chars)')
        else:
            print(f'FAIL: {elapsed:.1f}s - {err}')
        break
    except Exception as e:
        print(f'Attempt {attempt+1} error: {e}')
        time.sleep(2)

print()

# Race solve
print('--- Race solve (2 parallel) ---')
for attempt in range(3):
    try:
        t0 = time.time()
        token2, err2 = ds._solve_race(SITEKEY, '', n=2)
        elapsed2 = time.time() - t0
        if token2:
            print(f'OK: {elapsed2:.1f}s ({len(token2)} chars)')
        else:
            print(f'FAIL: {elapsed2:.1f}s - {err2}')
        break
    except Exception as e:
        print(f'Attempt {attempt+1} error: {e}')
        time.sleep(2)

print()

# Check pool
print(f'Pool: {len(ds._presolve_pool)} tokens ready, {ds._presolve_active} active')
presolved = ds._get_presolved(SITEKEY)
print(f'Presolved: {"YES" if presolved else "NO"}')
print()
print('Done.')
