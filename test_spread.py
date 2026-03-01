"""
Test DM spread functionality with a real account.
Logs in with email/pass, gets token, then tests the DM spread logic.
"""
import sys, os, json, base64, re, time, random
from curl_cffi import requests as creq

API = 'https://discord.com/api/v9'
CHROME_VER = '136'
UA = f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_VER}.0.0.0 Safari/537.36'
SEC_CH_UA = f'"Chromium";v="{CHROME_VER}", "Google Chrome";v="{CHROME_VER}", "Not.A/Brand";v="99"'

BUILD = 368827

def sprops():
    return base64.b64encode(json.dumps({
        "os": "Windows", "browser": "Chrome", "device": "",
        "system_locale": "en-US", "browser_user_agent": UA,
        "browser_version": f"{CHROME_VER}.0.0.0", "os_version": "10",
        "referrer": "", "referring_domain": "",
        "referrer_current": "", "referring_domain_current": "",
        "release_channel": "stable",
        "client_build_number": BUILD, "client_event_source": None
    }).encode()).decode()

def fetch_build():
    global BUILD
    try:
        s = creq.Session(impersonate='chrome')
        r = s.get('https://discord.com/login', timeout=15)
        for m in re.findall(r'assets/(\w+)\.js', r.text)[:5]:
            jr = s.get(f'https://discord.com/assets/{m}.js', timeout=10)
            bm = re.search(r'buildNumber["\s:D]+(\d{5,})', jr.text)
            if bm:
                BUILD = int(bm.group(1))
                print(f'[+] Build: {BUILD}')
                return
    except:
        pass
    print(f'[*] Using fallback build: {BUILD}')

def make_nonce():
    return str((int(time.time() * 1000) - 1420070400000) << 22 | random.randint(0, 4194303))

def main():
    email = 'fortbot8@inbox.lv'
    password = 'Fatdude11$'
    test_message = 'https://discord.gg/qBCJKm8S bro join she is stripping on cam'

    print('[*] Fetching build number...')
    fetch_build()

    # Create stealth session
    s = creq.Session(impersonate='chrome')

    # Step 1: Visit login page for cookies
    print('[*] Getting cookies from login page...')
    r = s.get('https://discord.com/login', headers={
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': UA,
        'Sec-CH-UA': SEC_CH_UA,
        'Sec-CH-UA-Mobile': '?0',
        'Sec-CH-UA-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
    }, timeout=15)
    print(f'    Status: {r.status_code}, Cookies: {list(s.cookies.keys())}')

    # Step 2: Get fingerprint
    print('[*] Getting fingerprint...')
    r2 = s.get(f'{API}/experiments', headers={
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Origin': 'https://discord.com',
        'Referer': 'https://discord.com/login',
        'User-Agent': UA,
        'Sec-CH-UA': SEC_CH_UA,
        'Sec-CH-UA-Mobile': '?0',
        'Sec-CH-UA-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'X-Track': sprops(),
    }, timeout=15)
    fingerprint = r2.json().get('fingerprint', '') if r2.status_code == 200 else ''
    print(f'    Fingerprint: {fingerprint[:30]}...' if fingerprint else '    No fingerprint')

    def api_hdrs(extra=None):
        h = {
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Content-Type': 'application/json',
            'Origin': 'https://discord.com',
            'Referer': 'https://discord.com/login',
            'User-Agent': UA,
            'X-Discord-Locale': 'en-US',
            'X-Discord-Timezone': 'America/Los_Angeles',
            'X-Debug-Options': 'bugReporterEnabled',
            'X-Super-Properties': sprops(),
            'Sec-CH-UA': SEC_CH_UA,
            'Sec-CH-UA-Mobile': '?0',
            'Sec-CH-UA-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        }
        if fingerprint:
            h['X-Fingerprint'] = fingerprint
        if extra:
            h.update(extra)
        return h

    # Step 3: Login
    print('[*] Logging in...')
    r3 = s.post(f'{API}/auth/login', headers=api_hdrs(),
                json={'login': email, 'password': password, 'undelete': False, 'login_source': None, 'gift_code_sku_id': None},
                timeout=30)
    j = r3.json()
    print(f'    Status: {r3.status_code}')

    token = j.get('token')
    if not token:
        # Might need MFA or captcha
        if j.get('mfa'):
            print('[!] Account needs MFA — enter TOTP code:')
            code = input('    TOTP> ').strip()
            ticket = j.get('ticket', '')
            r4 = s.post(f'{API}/auth/mfa/totp', headers=api_hdrs(),
                        json={'code': code, 'ticket': ticket, 'login_source': None, 'gift_code_sku_id': None},
                        timeout=30)
            token = r4.json().get('token')
        elif j.get('captcha_key'):
            print(f'[!] Captcha required: {j.get("captcha_sitekey")}')
            print('    Cannot proceed without captcha solve in test mode.')
            sys.exit(1)
        else:
            print(f'[!] Login failed: {json.dumps(j, indent=2)}')
            sys.exit(1)

    if not token:
        print('[!] No token obtained')
        sys.exit(1)

    print(f'[+] Token: {token[:30]}...')

    # Now switch referer to channels/@me (like navigating after login)
    def dm_hdrs():
        h = api_hdrs()
        h['Authorization'] = token
        h['Referer'] = 'https://discord.com/channels/@me'
        return h

    # Navigate to channels (like real client)
    print('[*] Navigating to channels/@me...')
    try:
        s.get('https://discord.com/channels/@me', headers={
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'User-Agent': UA,
            'Sec-CH-UA': SEC_CH_UA,
            'Sec-CH-UA-Mobile': '?0',
            'Sec-CH-UA-Platform': '"Windows"',
        }, timeout=15)
    except:
        pass

    # Get user info
    r_me = s.get(f'{API}/users/@me', headers=dm_hdrs(), timeout=15)
    if r_me.status_code == 200:
        me = r_me.json()
        print(f'[+] Logged in as: {me.get("username")}#{me.get("discriminator")} ({me.get("id")})')
    else:
        print(f'[!] /users/@me returned {r_me.status_code}')

    # Get open DMs
    print('[*] Fetching open DM channels...')
    r_dms = s.get(f'{API}/users/@me/channels', headers=dm_hdrs(), timeout=15)
    dm_channels = []
    if r_dms.status_code == 200:
        for ch in r_dms.json():
            if ch.get('type') in (1, 3):
                recipients = [u.get('username', '?') for u in ch.get('recipients', [])]
                dm_channels.append({'id': ch['id'], 'type': ch['type'], 'recipients': recipients})
        print(f'    Found {len(dm_channels)} DM channels')
        for dc in dm_channels[:10]:
            print(f'      {dc["id"]} -> {", ".join(dc["recipients"])}')
    else:
        print(f'    Failed: {r_dms.status_code} {r_dms.text[:200]}')

    # Get friends
    print('[*] Fetching friends...')
    r_friends = s.get(f'{API}/users/@me/relationships', headers=dm_hdrs(), timeout=15)
    friends = []
    if r_friends.status_code == 200:
        for rel in r_friends.json():
            if rel.get('type') == 1:
                friends.append({'id': rel['id'], 'username': rel.get('user', {}).get('username', '?')})
        print(f'    Found {len(friends)} friends')
        for f in friends[:10]:
            print(f'      {f["id"]} -> {f["username"]}')
    else:
        print(f'    Failed: {r_friends.status_code} {r_friends.text[:200]}')

    # Collect all channel IDs to message
    all_channel_ids = [dc['id'] for dc in dm_channels]

    # Open DMs for friends not already in open DMs
    existing_dm_channel_ids = set(all_channel_ids)
    for friend in friends:
        time.sleep(random.uniform(0.5, 1.5))
        try:
            r_open = s.post(f'{API}/users/@me/channels', headers=dm_hdrs(),
                            json={'recipients': [friend['id']]}, timeout=15)
            if r_open.status_code == 200:
                ch_id = r_open.json().get('id')
                if ch_id and ch_id not in existing_dm_channel_ids:
                    all_channel_ids.append(ch_id)
                    existing_dm_channel_ids.add(ch_id)
                    print(f'    Opened DM with {friend["username"]}: {ch_id}')
        except Exception as e:
            print(f'    Failed to open DM with {friend["username"]}: {e}')

    print(f'\n[*] Total channels to message: {len(all_channel_ids)}')
    print(f'[*] Message: {test_message}')

    confirm = input('\n    Send messages? (y/n) > ').strip().lower()
    if confirm != 'y':
        print('[*] Aborted.')
        sys.exit(0)

    # Send messages with human-like delays
    sent = 0
    failed = 0
    for ch_id in all_channel_ids:
        try:
            # Delay
            delay = random.uniform(2.0, 5.0)
            print(f'    Waiting {delay:.1f}s...', end=' ', flush=True)
            time.sleep(delay)

            # Typing indicator
            try:
                s.post(f'{API}/channels/{ch_id}/typing', headers=dm_hdrs(), timeout=5)
            except:
                pass
            time.sleep(random.uniform(0.8, 2.0))

            payload = {
                'content': test_message,
                'nonce': make_nonce(),
                'tts': False,
                'flags': 0
            }
            r_msg = s.post(f'{API}/channels/{ch_id}/messages',
                           headers=dm_hdrs(), json=payload, timeout=15)

            if r_msg.status_code == 200:
                sent += 1
                print(f'SENT to {ch_id} ({sent})')
            elif r_msg.status_code == 429:
                retry = r_msg.json().get('retry_after', 5)
                print(f'RATE LIMITED ({retry}s)')
                time.sleep(retry + random.uniform(1, 3))
                r2 = s.post(f'{API}/channels/{ch_id}/messages',
                            headers=dm_hdrs(), json=payload, timeout=15)
                if r2.status_code == 200:
                    sent += 1
                    print(f'    SENT on retry')
                else:
                    failed += 1
                    print(f'    FAILED on retry: {r2.status_code}')
            elif r_msg.status_code == 403:
                failed += 1
                print(f'FORBIDDEN (DMs closed/blocked)')
            else:
                failed += 1
                print(f'ERROR {r_msg.status_code}: {r_msg.text[:200]}')
        except Exception as e:
            failed += 1
            print(f'EXCEPTION: {e}')

    print(f'\n[+] DONE — Sent: {sent}, Failed: {failed}')


if __name__ == '__main__':
    main()
