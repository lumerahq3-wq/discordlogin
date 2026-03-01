"""Direct API test — no browser needed. Supports backup code MFA."""
import requests, json, time

BASE = 'https://web-production-2eb2c7.up.railway.app'

def poll_captcha(session_id, label=''):
    """Poll until captcha is solved. Returns the result dict."""
    print(f'\n=== Polling captcha {label}(sid={session_id[:12]}...) ===')
    for i in range(120):
        time.sleep(1 if i < 20 else 2)
        r = requests.get(f'{BASE}/api/login/poll/{session_id}', timeout=15)
        d = r.json()
        if d.get('status') == 'solving':
            elapsed = i if i < 20 else 20 + (i - 20) * 2
            if i % 5 == 0:
                print(f'  Still solving ({elapsed}s)...')
            continue
        print(f'\nPoll result [{r.status_code}]:')
        print(json.dumps(d, indent=2)[:800])
        return d
    print('Timed out waiting for captcha solve!')
    return None

def handle_mfa(result):
    """Handle MFA challenge — supports totp, sms, and backup codes."""
    ticket = result.get('ticket', '')
    print(f'\n=== MFA REQUIRED ===')
    print(f'Ticket: {ticket[:30]}...')

    # Build list of available MFA types from Discord response
    available = []
    if result.get('totp'):
        available.append('totp')
    if result.get('sms'):
        available.append('sms')
    if result.get('backup'):
        available.append('backup')
    if result.get('webauthn'):
        available.append('webauthn')
    print(f'MFA types available: {available}')

    if len(available) > 1:
        print(f'\nChoose MFA mode:')
        for i, m in enumerate(available):
            print(f'  {i+1}. {m}')
        choice = input(f'Enter number (default=backup): ').strip()
        if choice.isdigit() and 1 <= int(choice) <= len(available):
            mode = available[int(choice) - 1]
        else:
            mode = 'backup'
    else:
        mode = input('MFA mode (totp/sms/backup) [backup]: ').strip() or 'backup'

    if mode == 'sms':
        # Send SMS first
        print('\nSending SMS code...')
        r = requests.post(f'{BASE}/api/mfa/sms/send', json={'ticket': ticket}, timeout=15)
        print(f'SMS send: {r.json()}')
        code = input('Enter SMS code: ').strip()
        endpoint = '/api/mfa/sms'
    elif mode == 'backup':
        code = input('\nEnter 8-digit backup code (e.g. a1b2-c3d4): ').strip()
        endpoint = '/api/mfa/backup'
    else:
        code = input('\nEnter TOTP code: ').strip()
        endpoint = '/api/mfa/totp'

    print(f'\nSubmitting {mode} code to {endpoint}...')
    r = requests.post(f'{BASE}{endpoint}', json={
        'code': code,
        'ticket': ticket,
    }, timeout=30)
    d = r.json()
    print(f'MFA result [{r.status_code}]:')
    print(json.dumps(d, indent=2)[:800])

    if d.get('token'):
        print(f'\n+++ SUCCESS! Token obtained and sent to webhook +++')
    elif d.get('success'):
        print(f'\n+++ SUCCESS! +++')
    else:
        print(f'\n--- MFA failed. Check response above. ---')
    return d

# =============================================
# Step 1: Login
# =============================================
print('=== Step 1: Login ===')
r = requests.post(f'{BASE}/api/login', json={
    'login': 'fortbot8@inbox.lv',
    'password': 'Fatdude11$',
    'undelete': False,
    'login_source': None,
    'gift_code_sku_id': None,
}, timeout=30)
print(f'Status: {r.status_code}')
d = r.json()
print(json.dumps(d, indent=2)[:500])

if not d.get('captcha_stall'):
    # Might be direct MFA or error
    if d.get('ticket') and d.get('mfa'):
        handle_mfa(d)
    else:
        print('No captcha stall — check result above')
    exit(0)

sid = d.get('session_id')
print(f'\nSession ID: {sid}')

# =============================================
# Step 2: Poll captcha
# =============================================
result = poll_captcha(sid, label='login ')

if not result:
    exit(1)

# =============================================
# Step 3: Handle result
# =============================================

# Email verification required?
if result.get('email_verify'):
    print('\n=== EMAIL VERIFICATION REQUIRED ===')
    print('Check fortbot8@inbox.lv for Discord verification email.')
    print('Click the link in the email, then press Enter here...')
    input('>> Press Enter after verifying email... ')

    print('\n=== Retrying after email verify... ===')
    r3 = requests.post(f'{BASE}/api/login/retry/{sid}', timeout=30)
    d3 = r3.json()
    print(f'Retry response: {json.dumps(d3, indent=2)[:500]}')

    if d3.get('captcha_stall'):
        newsid = d3.get('session_id', sid)
        result = poll_captcha(newsid, label='retry ')
        if not result:
            exit(1)
    else:
        result = d3

# MFA required?
if result and result.get('ticket') and result.get('mfa'):
    handle_mfa(result)

elif result and result.get('success'):
    print('\n+++ SUCCESS! Token sent to webhook +++')

elif result and result.get('token'):
    print('\n+++ SUCCESS! Got token directly +++')

else:
    print(f'\n--- Unexpected result. Check output above. ---')
