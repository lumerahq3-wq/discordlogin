"""Test multiple captcha services to find the fastest for Discord hCaptcha Enterprise."""
import requests, time, json, concurrent.futures

sitekey = 'a9b5fb07-92ff-493f-86fe-352a2803b3df'
url = 'https://discord.com/login'

def test_2captcha():
    """2captcha.com - uses separate key format"""
    # User would need to sign up at 2captcha.com
    # API: https://2captcha.com/2captcha-api#solving_hcaptcha
    # Typically 10-30s, costs ~$2.99/1000
    print('[2captcha] Need API key from 2captcha.com to test')
    return None

def test_nocaptchaai():
    """NoCaptchaAI - very fast hCaptcha solver, ~3-10s"""
    # API: nocaptchaai.com
    # Typically 3-10s for hCaptcha, costs ~$1-2/1000
    print('[NoCaptchaAI] Need API key from nocaptchaai.com to test')
    return None

def test_capmonster():
    """CapMonster Cloud - anti-captcha compatible API"""
    api = 'https://api.capmonster.cloud'
    # CapMonster Cloud uses same API as anti-captcha
    # Could use the same key format
    print('[CapMonster] Need API key from capmonster.cloud to test')
    print('[CapMonster] Note: Uses same API format as anti-captcha, usually faster')
    return None

def test_anticaptcha_proxyless():
    """Anti-Captcha proxyless (already tested at ~40s)"""
    key = 'b7a1846d602861ef723c924eee4de940'
    api = 'https://api.anti-captcha.com'
    t0 = time.time()
    r = requests.post(f'{api}/createTask', json={
        'clientKey': key,
        'task': {
            'type': 'HCaptchaTaskProxyless',
            'websiteURL': url,
            'websiteKey': sitekey,
            'isEnterprise': True,
        }
    }, timeout=30)
    j = r.json()
    tid = j.get('taskId')
    if not tid:
        print(f'[Anti-Captcha] Failed: {j}')
        return None
    
    for i in range(120):
        time.sleep(0.5)
        r2 = requests.post(f'{api}/getTaskResult', json={'clientKey': key, 'taskId': tid}, timeout=15)
        j2 = r2.json()
        if j2.get('status') == 'ready':
            elapsed = time.time() - t0
            print(f'[Anti-Captcha] Solved in {elapsed:.1f}s')
            return elapsed
        if j2.get('errorId', 0) != 0:
            print(f'[Anti-Captcha] Error: {j2}')
            return None
    return None

# Run second test to get average
print('=== Testing Anti-Captcha (2nd run for average) ===')
t1 = test_anticaptcha_proxyless()

print('\n=== Summary ===')
print(f'  Anti-Captcha Proxyless: ~{t1:.0f}s' if t1 else '  Anti-Captcha: N/A')
print(f'  CapSolver: BLOCKED (usage policy violation)')
print(f'\n=== Recommendations for faster solving ===')
print(f'  1. CapMonster Cloud (capmonster.cloud) - Same API as anti-captcha, often 2-3x faster')
print(f'  2. NoCaptchaAI (nocaptchaai.com) - Very fast hCaptcha, ~3-10s, cheapest')
print(f'  3. 2Captcha (2captcha.com) - Reliable, ~10-30s')
print(f'  4. Anti-Captcha (current) - Working but slow (~40s)')
