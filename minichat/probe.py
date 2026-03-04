import requests, json

BASE = 'https://b.minichat.com/api/v1'
HEADERS = {
    'Origin': 'https://minichat.com',
    'Referer': 'https://minichat.com/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Content-Type': 'application/json',
}

endpoints = [
    ('GET',  '/users/guest',        None),
    ('POST', '/auth/guest',         {}),
    ('POST', '/sessions',           {'guest': True}),
    ('POST', '/sessions/guest',     {}),
    ('GET',  '/config',             None),
    ('POST', '/users',              {'guest': True}),
    ('GET',  '/roulette',           None),
    ('POST', '/roulette/sessions',  {}),
]

for method, path, body in endpoints:
    url = BASE + path
    try:
        if method == 'GET':
            r = requests.get(url, headers=HEADERS, timeout=10)
        else:
            r = requests.post(url, headers=HEADERS, json=body, timeout=10)
        print(f'{method} {path} -> {r.status_code}')
        try:
            data = r.json()
            print(' ', json.dumps(data)[:300])
        except:
            print(' ', r.text[:200])
    except Exception as e:
        print(f'{method} {path} -> ERROR: {e}')
    print()
