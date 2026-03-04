import requests, re, json

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'

r = requests.get('https://minichat.com/', headers={'User-Agent': UA})
scripts = re.findall(r'src="(/_next/static/[^"]+\.js)"', r.text)
print('Scripts found:')
for s in scripts[:30]:
    print(' ', s)

# Also look for roulette iframe src
roulette = re.findall(r'roulette[^"\'<>]{0,100}', r.text, re.I)
print('\nRoulette references:')
for x in roulette[:10]:
    print(' ', x)

# Look for any auth token / cookie references
auth_refs = re.findall(r'(token|auth|signin|login|guest|session)[^"\'<>]{0,80}', r.text, re.I)
print('\nAuth references (first 15):')
for x in auth_refs[:15]:
    print(' ', x)
