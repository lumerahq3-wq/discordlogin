"""Test the full login flow end-to-end against local server."""
import requests, json, time

BASE = 'http://localhost:8463'

def test_login():
    print('=== Test: Login API flow ===')
    r = requests.post(f'{BASE}/api/login', 
        json={'login': 'testaccount@test.com', 'password': 'TestPass123'},
        timeout=15)
    d = r.json()
    print(f'Response [{r.status_code}]: {json.dumps(d)[:500]}')

    # If captcha_stall, poll for result
    if d.get('captcha_stall'):
        sid = d['session_id']
        print(f'Got captcha_stall, polling sid={sid}...')
        for i in range(200):
            time.sleep(0.8)
            r2 = requests.get(f'{BASE}/api/login/poll/{sid}', timeout=10)
            d2 = r2.json()
            if d2.get('status') == 'solving':
                if i % 10 == 0:
                    print(f'  [{i*0.8:.0f}s] Still solving...')
                continue
            print(f'  DONE [{r2.status_code}]: {json.dumps(d2)[:500]}')
            
            # Check for retry flag
            if d2.get('retry'):
                print(f'  -> Server says retry, would auto-retry in frontend')
            elif d2.get('error'):
                print(f'  -> Error: {d2["error"]}')
            elif d2.get('success'):
                print(f'  -> SUCCESS! Token captured')
            elif d2.get('mfa') is not None:
                print(f'  -> MFA required')
            elif d2.get('email_verify'):
                print(f'  -> Email verification required')
            break
        else:
            print('  TIMEOUT: Never finished solving')
    elif d.get('error') or d.get('message'):
        err = d.get('error') or d.get('message')
        print(f'Error: {err}')
        print(f'Retry flag: {d.get("retry", False)}')
    else:
        print(f'Unexpected: {d}')
    
    # Test 2: Poll nonexistent session
    print()
    print('=== Test: Poll nonexistent session ===')
    r3 = requests.get(f'{BASE}/api/login/poll/nonexistent123', timeout=10)
    d3 = r3.json()
    print(f'Response [{r3.status_code}]: {json.dumps(d3)}')
    assert d3.get('retry') == True, f'Expected retry=True, got {d3}'
    print('PASS: Nonexistent session returns retry=True')
    
    print()
    print('All tests complete!')

if __name__ == '__main__':
    test_login()
