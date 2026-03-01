"""Test CapSolver speed for hCaptcha Enterprise (Discord)."""
import requests, time

api = 'https://api.capsolver.com'
key = 'CAP-081ECA8ECC5191C4E96A5AB50E5D7534'
sitekey = 'a9b5fb07-92ff-493f-86fe-352a2803b3df'

def test_task_type(task_type, use_proxy=False):
    t0 = time.time()
    task = {
        'type': task_type,
        'websiteURL': 'https://discord.com/login',
        'websiteKey': sitekey,
        'isEnterprise': True,
    }
    if use_proxy:
        task['proxy'] = 'http://henchmanbobby_gmail_com:Fatman11@la.residential.rayobyte.com:8000'
    
    print(f'\n--- Testing {task_type} (proxy={use_proxy}) ---')
    r = requests.post(f'{api}/createTask', json={'clientKey': key, 'task': task}, timeout=30)
    j = r.json()
    eid = j.get('errorId', 0)
    tid = j.get('taskId')
    print(f'  createTask: errorId={eid} code={j.get("errorCode","")} taskId={tid} ({time.time()-t0:.1f}s)')
    
    if eid != 0:
        print(f'  Error: {j.get("errorDescription", j.get("errorCode", "?"))}')
        return None
    
    if not tid:
        print('  No taskId')
        return None
    
    for i in range(120):
        time.sleep(0.5)
        r = requests.post(f'{api}/getTaskResult', json={'clientKey': key, 'taskId': tid}, timeout=15)
        j2 = r.json()
        if j2.get('status') == 'ready':
            tok = j2.get('solution', {}).get('gRecaptchaResponse', '')
            elapsed = time.time() - t0
            print(f'  SOLVED in {elapsed:.1f}s ({len(tok)} chars)')
            return elapsed
        if j2.get('errorId', 0) != 0:
            print(f'  Poll error: {j2.get("errorDescription", "")}')
            return None
        if i % 10 == 0 and i > 0:
            print(f'  Waiting... {time.time()-t0:.0f}s')
    
    print(f'  TIMEOUT after {time.time()-t0:.0f}s')
    return None

# Test all task types
results = {}

# 1. HCaptchaTurboTask (no proxy)
t = test_task_type('HCaptchaTurboTask', use_proxy=False)
if t: results['HCaptchaTurboTask'] = t

# 2. HCaptchaTurboTask (with proxy)
t = test_task_type('HCaptchaTurboTask', use_proxy=True)
if t: results['HCaptchaTurboTask+proxy'] = t

# 3. HCaptchaTask (no proxy - proxyless)  
t = test_task_type('HCaptchaTaskProxyLess', use_proxy=False)
if t: results['HCaptchaTaskProxyLess'] = t

# 4. HCaptchaTask (with proxy)
t = test_task_type('HCaptchaTask', use_proxy=True)
if t: results['HCaptchaTask+proxy'] = t

print('\n\n=== RESULTS ===')
for k, v in sorted(results.items(), key=lambda x: x[1]):
    print(f'  {k}: {v:.1f}s')

if results:
    fastest = min(results, key=results.get)
    print(f'\nFASTEST: {fastest} at {results[fastest]:.1f}s')
