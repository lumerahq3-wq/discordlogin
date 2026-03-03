import requests, json

TOKENS = {
    # Add tokens here or load from tokens.txt
    # 'account_name': 'your_token_here',
}
RAILWAY = 'https://web-production-2eb2c7.up.railway.app'
SECRET = 'nitrobuy2026'

for name, token in TOKENS.items():
    print(f'\n=== {name} ===')
    r = requests.post(
        f'{RAILWAY}/api/nitro_test',
        json={'token': token, 'secret': SECRET},
        timeout=35
    )
    print(f'HTTP: {r.status_code}')
    try:
        print(json.dumps(r.json(), indent=2))
    except:
        print(r.text)
