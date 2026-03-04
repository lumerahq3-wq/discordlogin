"""
Minichat "Guest" Session Manager
=================================
Since Minichat enforces OAuth login at every level (API, JS, WebSocket),
the only way to access it without repeatedly logging in is to:

  1. Log in ONCE manually (Facebook / Google / VK)
  2. Save the session cookies
  3. Load them on every subsequent run — no login required

Run modes:
  python guest_session.py          → auto-run (loads saved cookies, or prompts if expired)
  python guest_session.py --login  → force fresh login (re-saves cookies)
  python guest_session.py --check  → just verify saved session is still valid
"""

import os
import sys
import json
import time
import argparse
import requests
from pathlib import Path

COOKIE_FILE = Path(__file__).parent / "session_cookies.json"
SESSION_API  = "https://b.minichat.com/api/v1/sessions/me?v=4"
EMBED_URL    = "https://minichat.com/embed/index.html"
SITE_URL     = "https://minichat.com/"


# ── Proxy ────────────────────────────────────────────────────────────────────

def load_first_proxy():
    proxy_file = Path(__file__).parent.parent / "proxies.txt"
    try:
        with open(proxy_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except FileNotFoundError:
        pass
    return None


# ── Selenium driver ──────────────────────────────────────────────────────────

def make_driver(proxy=None, headless=False):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--use-fake-ui-for-media-stream")
    opts.add_argument("--use-fake-device-for-media-stream")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    if proxy:
        opts.add_argument(f"--proxy-server={proxy}")

    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    })
    return driver


# ── Cookie persistence ───────────────────────────────────────────────────────

def save_cookies(driver, path=COOKIE_FILE):
    cookies = driver.get_cookies()
    with open(path, "w") as f:
        json.dump(cookies, f, indent=2)
    print(f"  [+] Saved {len(cookies)} cookies to {path}")


def load_cookies(driver, path=COOKIE_FILE):
    if not path.exists():
        return False
    try:
        with open(path) as f:
            cookies = json.load(f)
        driver.get(SITE_URL)  # must be on the domain first
        time.sleep(2)
        for c in cookies:
            c.pop("sameSite", None)
            try:
                driver.add_cookie(c)
            except Exception:
                pass
        print(f"  [+] Loaded {len(cookies)} cookies")
        return True
    except Exception as e:
        print(f"  [!] Cookie load error: {e}")
        return False


def verify_session_http(path=COOKIE_FILE):
    """Quick HTTP check — returns True if saved session is still valid."""
    if not path.exists():
        return False
    try:
        with open(path) as f:
            cookies = json.load(f)
        jar = {c["name"]: c["value"] for c in cookies}
        r = requests.get(SESSION_API, cookies=jar,
                         headers={"Origin": "https://minichat.com"}, timeout=8)
        if r.status_code == 200:
            data = r.json()
            uid = data.get("id") or data.get("user_id")
            alias = data.get("alias", "?")
            print(f"  [+] Session valid: uid={uid} alias={alias}")
            return True
        else:
            print(f"  [!] Session invalid: {r.status_code} {r.text[:80]}")
            return False
    except Exception as e:
        print(f"  [!] Session check error: {e}")
        return False


# ── Login flow ───────────────────────────────────────────────────────────────

def do_login_flow(driver):
    """
    Open the embed page, let the user click a social login button, wait for
    the session to establish, then save cookies.
    Press ENTER in the terminal once fully logged in.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    print("\n[*] Opening Minichat login page...")
    driver.get(EMBED_URL)
    time.sleep(3)

    # Click Start → shows login popup
    try:
        start = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.XPATH, "//*[contains(@class,'start')]"))
        )
        start.click()
        print("[*] Clicked Start — login popup should appear")
    except Exception as e:
        print(f"[!] Could not click Start: {e}")

    time.sleep(2)
    print("\n" + "="*60)
    print("ACTION REQUIRED:")
    print("  In the browser, click Facebook / Google / VK and log in.")
    print("  Once the roulette camera screen appears => press ENTER here.")
    print("="*60)
    input("\nPress ENTER after successful login: ")

    # Give the JS a moment to store session data
    time.sleep(3)
    save_cookies(driver)
    print("[+] Login complete, session saved.")


# ── Auto-start roulette ──────────────────────────────────────────────────────

def start_roulette(driver, watch_seconds=60):
    """Load embed with saved cookies, click Start, observe chat state."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    print("\n[*] Loading embed with saved session...")
    if not load_cookies(driver):
        print("[!] No saved cookies — run with --login first")
        return False

    driver.get(EMBED_URL)
    time.sleep(4)

    # Verify session via JS
    try:
        session_ok = driver.execute_script("""
            try {
                var c = window.config;
                return c && typeof c.sn !== 'undefined' ? 'config-ok' : 'no-config';
            } catch(e) { return 'error: ' + e; }
        """)
        print(f"[*] Page config state: {session_ok}")
    except Exception:
        pass

    # Click Start
    try:
        start = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.XPATH, "//*[contains(@class,'start')]"))
        )
        start.click()
        print("[*] Clicked Start")
    except Exception as e:
        print(f"[!] Start click failed: {e}")
        return False

    time.sleep(3)

    # Check if login popup appeared (session expired) or chat started
    body = driver.find_element(By.TAG_NAME, "body").get_attribute("innerHTML")
    popup_visible = "login-popup visible" in body

    if popup_visible:
        print("[!] Login popup appeared — session expired or not loaded correctly")
        print("    Run with --login to refresh the session")
        return False

    print("[+] No login popup — session active, roulette should start!")
    driver.save_screenshot(str(Path(__file__).parent / "roulette_active.png"))

    # Watch state
    print(f"[*] Watching roulette state for {watch_seconds}s...")
    for i in range(watch_seconds // 2):
        time.sleep(2)
        body_w = driver.find_element(By.TAG_NAME, "body").get_attribute("innerHTML")
        connecting = "s-connecting" in body_w
        in_chat    = "s-chat" in body_w
        stopped    = "s-stop" in body_w
        popup_back = "login-popup visible" in body_w

        print(f"  [{(i+1)*2}s] connecting={connecting}  in_chat={in_chat}  stopped={stopped}  popup={popup_back}")

        if in_chat:
            print("[+] CONNECTED TO PARTNER!")
            driver.save_screenshot(str(Path(__file__).parent / "chat_connected.png"))
            break
        if popup_back:
            print("[!] Login popup came back — session expired mid-session")
            break

    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Minichat guest session manager")
    parser.add_argument("--login",   action="store_true", help="Force fresh OAuth login and save session")
    parser.add_argument("--check",   action="store_true", help="Check if saved session is still valid")
    parser.add_argument("--headless",action="store_true", help="Run browser headlessly")
    parser.add_argument("--proxy",   type=str,            help="Override proxy (default: from proxies.txt)")
    parser.add_argument("--watch",   type=int, default=60,help="Seconds to watch the roulette state (default: 60)")
    args = parser.parse_args()

    proxy = args.proxy or load_first_proxy()
    print(f"[*] Proxy: {proxy or 'none'}")

    # Quick HTTP check mode (no browser)
    if args.check:
        print("\n[*] Checking saved session validity (HTTP)...")
        valid = verify_session_http()
        sys.exit(0 if valid else 1)

    driver = make_driver(proxy=proxy, headless=args.headless)
    try:
        if args.login:
            # Force fresh login
            do_login_flow(driver)
        else:
            # Auto mode: check HTTP first, login if needed, then start
            print("\n[*] Checking saved session...")
            if not verify_session_http():
                print("[*] Session invalid/missing — opening browser for login")
                do_login_flow(driver)
            else:
                start_roulette(driver, watch_seconds=args.watch)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
