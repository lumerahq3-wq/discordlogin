"""Debug poll endpoint."""
import requests, time

BASE = 'https://web-production-2eb2c7.up.railway.app'

# Fresh login
r = requests.post(f'{BASE}/api/login', json={'login':'test@test.com','password':'Test1234!'}, timeout=30)
print(f'Login: {r.status_code} {r.text[:200]}')
sid = r.json().get('session_id')
print(f'SID: {sid}')

# Wait 2s
time.sleep(2)

# Poll
r2 = requests.get(f'{BASE}/api/login/poll/{sid}', timeout=15)
print(f'Poll status: {r2.status_code}')
print(f'Poll content-type: {r2.headers.get("content-type", "?")}')
print(f'Poll body repr: {repr(r2.text[:300])}')
print(f'Poll body len: {len(r2.text)}')
