#!/usr/bin/env python3
"""Final comprehensive test: 2Captcha hCaptcha solving using official library + raw API"""
import requests
import time
import json

API_KEY = "dbfdba3b6d1a7c256969070e942687c1"
SITEKEY = "a5f74b19-9e45-40e0-b45d-47ff91b7a6c2"
PAGEURL = "https://discord.com/login"

def test_raw_v1_hcaptcha():
    """Test V1 API in.php with method=hcaptcha — the format used by official 2captcha-python library"""
    print("\n=== TEST 1: V1 in.php with method=hcaptcha (official library format) ===")
    
    params = {
        'key': API_KEY,
        'method': 'hcaptcha',
        'sitekey': SITEKEY,
        'pageurl': PAGEURL,
        'json': 1,
    }
    
    print(f"POST https://2captcha.com/in.php")
    print(f"Params: {json.dumps(params, indent=2)}")
    
    resp = requests.post('https://2captcha.com/in.php', data=params)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text}")
    
    try:
        data = resp.json()
        if data.get('status') == 1:
            captcha_id = data['request']
            print(f"SUCCESS! Captcha ID: {captcha_id}")
            return captcha_id
        else:
            print(f"FAILED: {data.get('request', 'unknown error')}")
            return None
    except:
        print(f"Raw response: {resp.text}")
        if resp.text.startswith('OK|'):
            captcha_id = resp.text.split('|')[1]
            print(f"SUCCESS! Captcha ID: {captcha_id}")
            return captcha_id
        return None

def test_raw_v1_hcaptcha_enterprise():
    """Test V1 API with enterprise=1"""
    print("\n=== TEST 2: V1 in.php with method=hcaptcha + enterprise=1 ===")
    
    params = {
        'key': API_KEY,
        'method': 'hcaptcha',
        'sitekey': SITEKEY,
        'pageurl': PAGEURL,
        'enterprise': 1,
        'json': 1,
    }
    
    print(f"POST https://2captcha.com/in.php")
    print(f"Params: {json.dumps(params, indent=2)}")
    
    resp = requests.post('https://2captcha.com/in.php', data=params)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text}")
    
    try:
        data = resp.json()
        if data.get('status') == 1:
            captcha_id = data['request']
            print(f"SUCCESS! Captcha ID: {captcha_id}")
            return captcha_id
        else:
            print(f"FAILED: {data.get('request', 'unknown error')}")
            return None
    except:
        if resp.text.startswith('OK|'):
            captcha_id = resp.text.split('|')[1]
            print(f"SUCCESS! Captcha ID: {captcha_id}")
            return captcha_id
        return None

def test_raw_v1_get():
    """Test V1 API using GET instead of POST"""
    print("\n=== TEST 3: V1 in.php GET request ===")
    
    url = f'https://2captcha.com/in.php?key={API_KEY}&method=hcaptcha&sitekey={SITEKEY}&pageurl={PAGEURL}&json=1'
    
    print(f"GET {url}")
    
    resp = requests.get(url)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text}")
    
    try:
        data = resp.json()
        if data.get('status') == 1:
            captcha_id = data['request']
            print(f"SUCCESS! Captcha ID: {captcha_id}")
            return captcha_id
        else:
            print(f"FAILED: {data.get('request', 'unknown error')}")
            return None
    except:
        if resp.text.startswith('OK|'):
            captcha_id = resp.text.split('|')[1]
            print(f"SUCCESS! Captcha ID: {captcha_id}")
            return captcha_id
        return None

def test_official_library():
    """Test using the official 2captcha-python library"""
    print("\n=== TEST 4: Official 2captcha-python library ===")
    
    try:
        from twocaptcha import TwoCaptcha
        solver = TwoCaptcha(API_KEY)
        
        print(f"Calling solver.hcaptcha(sitekey='{SITEKEY}', url='{PAGEURL}')")
        result = solver.hcaptcha(sitekey=SITEKEY, url=PAGEURL)
        print(f"SUCCESS! Result: {json.dumps(result, indent=2)[:200]}...")
        return result
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return None

def test_v2_createtask():
    """Test V2 createTask API at api.2captcha.com"""
    print("\n=== TEST 5: V2 createTask HCaptchaTaskProxyless ===")
    
    payload = {
        "clientKey": API_KEY,
        "task": {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": PAGEURL,
            "websiteKey": SITEKEY,
        }
    }
    
    print(f"POST https://api.2captcha.com/createTask")
    print(f"Payload: {json.dumps(payload, indent=2)}")
    
    resp = requests.post('https://api.2captcha.com/createTask', json=payload)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text}")
    
    try:
        data = resp.json()
        if data.get('errorId') == 0:
            task_id = data['taskId']
            print(f"SUCCESS! Task ID: {task_id}")
            return task_id
        else:
            print(f"FAILED: errorId={data.get('errorId')}, code={data.get('errorCode')}, desc={data.get('errorDescription')}")
            return None
    except:
        return None

def test_v2_createtask_enterprise():
    """Test V2 createTask with isEnterprise"""
    print("\n=== TEST 6: V2 createTask HCaptchaTaskProxyless + isEnterprise ===")
    
    payload = {
        "clientKey": API_KEY,
        "task": {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": PAGEURL,
            "websiteKey": SITEKEY,
            "isEnterprise": True,
        }
    }
    
    print(f"POST https://api.2captcha.com/createTask")
    print(f"Payload: {json.dumps(payload, indent=2)}")
    
    resp = requests.post('https://api.2captcha.com/createTask', json=payload)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text}")
    
    try:
        data = resp.json()
        if data.get('errorId') == 0:
            task_id = data['taskId']
            print(f"SUCCESS! Task ID: {task_id}")
            return task_id
        else:
            print(f"FAILED: errorId={data.get('errorId')}, code={data.get('errorCode')}, desc={data.get('errorDescription')}")
            return None
    except:
        return None

def poll_v1_result(captcha_id, max_wait=120):
    """Poll V1 res.php for result"""
    print(f"\n--- Polling V1 res.php for captcha {captcha_id} ---")
    start = time.time()
    
    while time.time() - start < max_wait:
        time.sleep(10)
        resp = requests.get(f'https://2captcha.com/res.php?key={API_KEY}&action=get&id={captcha_id}&json=1')
        data = resp.json()
        elapsed = time.time() - start
        
        if data.get('request') == 'CAPCHA_NOT_READY':
            print(f"  [{elapsed:.0f}s] Not ready...")
            continue
        elif data.get('status') == 1:
            token = data['request']
            print(f"  [{elapsed:.0f}s] SOLVED! Token: {token[:80]}...")
            return token
        else:
            print(f"  [{elapsed:.0f}s] Error: {data}")
            return None
    
    print(f"  Timeout after {max_wait}s")
    return None

def poll_v2_result(task_id, max_wait=120):
    """Poll V2 getTaskResult"""
    print(f"\n--- Polling V2 getTaskResult for task {task_id} ---")
    start = time.time()
    
    while time.time() - start < max_wait:
        time.sleep(10)
        resp = requests.post('https://api.2captcha.com/getTaskResult', json={
            'clientKey': API_KEY,
            'taskId': task_id
        })
        data = resp.json()
        elapsed = time.time() - start
        
        if data.get('status') == 'processing':
            print(f"  [{elapsed:.0f}s] Processing...")
            continue
        elif data.get('status') == 'ready':
            token = data.get('solution', {}).get('gRecaptchaResponse', data.get('solution', {}).get('token', ''))
            print(f"  [{elapsed:.0f}s] SOLVED! Token: {token[:80]}...")
            return token
        else:
            print(f"  [{elapsed:.0f}s] Error: {data}")
            return None
    
    print(f"  Timeout after {max_wait}s")
    return None

if __name__ == '__main__':
    print("=" * 70)
    print("2CAPTCHA HCAPTCHA COMPREHENSIVE TEST")
    print(f"API Key: {API_KEY}")
    print(f"Sitekey: {SITEKEY}")
    print(f"PageURL: {PAGEURL}")
    print("=" * 70)
    
    # Check balance first
    print("\n--- Balance Check ---")
    resp = requests.get(f'https://2captcha.com/res.php?key={API_KEY}&action=getbalance&json=1')
    print(f"Balance: {resp.text}")
    
    # Run submission tests (no polling yet, just see which accept the task)
    results = {}
    
    # Test 1: V1 POST
    results['v1_post'] = test_raw_v1_hcaptcha()
    
    # Test 2: V1 POST enterprise
    results['v1_enterprise'] = test_raw_v1_hcaptcha_enterprise()
    
    # Test 3: V1 GET
    results['v1_get'] = test_raw_v1_get()
    
    # Test 5: V2 createTask
    results['v2_proxyless'] = test_v2_createtask()
    
    # Test 6: V2 createTask enterprise
    results['v2_enterprise'] = test_v2_createtask_enterprise()
    
    # Summary
    print("\n" + "=" * 70)
    print("SUBMISSION RESULTS SUMMARY:")
    print("=" * 70)
    
    successful = []
    for name, result in results.items():
        status = "SUCCESS" if result else "FAILED"
        print(f"  {name}: {status} -> {result}")
        if result:
            successful.append((name, result))
    
    # Poll for the FIRST successful result
    if successful:
        name, captcha_id = successful[0]
        print(f"\nPolling first success: {name} (ID: {captcha_id})")
        if name.startswith('v2'):
            token = poll_v2_result(captcha_id)
        else:
            token = poll_v1_result(captcha_id)
        
        if token:
            print(f"\n{'='*70}")
            print(f"FINAL RESULT: hCaptcha token obtained!")
            print(f"Method: {name}")
            print(f"Token length: {len(token)}")
            print(f"Token preview: {token[:100]}...")
            print(f"{'='*70}")
        else:
            print("\nFailed to get solution even though task was accepted")
    else:
        print("\nNO METHOD SUCCEEDED. Trying official library as last resort...")
        test_official_library()
