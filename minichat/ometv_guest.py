"""
ome.tv Guest Bot
================
ome.tv auto-issues a signed guest token in localStorage['ld'] on first visit.
  {"UserId":"...","CreatedAt":"...","InitalAddr":"...","Hmac":"..."}

This script:
  1. Loads ome.tv 
  2. Clicks Start → guest session granted immediately, no login
  3. Watches the chat state
  4. Saves the guest token for reuse
  5. Optionally uses a proxy (from proxies.txt)

Usage:
  python ometv_guest.py               # fresh run, no proxy
  python ometv_guest.py --proxy       # use proxy from proxies.txt
  python ometv_guest.py --headless    # headless Chrome
  python ometv_guest.py --reuse       # reuse saved guest token (no proxy needed for token)
  python ometv_guest.py --watch 120   # watch for 120 seconds
"""

import argparse
import json
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

TOKEN_FILE = Path(__file__).parent / "ometv_token.json"
OMETV_URL  = "https://ome.tv/"


def load_first_proxy():
    try:
        p = Path(__file__).parent.parent / "proxies.txt"
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except:
        pass
    return None


def make_driver(proxy=None, headless=False):
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--use-fake-ui-for-media-stream")         # auto-grant camera
    opts.add_argument("--use-fake-device-for-media-stream")     # fake webcam feed
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if proxy:
        opts.add_argument(f"--proxy-server={proxy}")
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    })
    return driver


def inject_saved_token(driver, token_data):
    """Pre-load a saved guest token into localStorage before page scripts run."""
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": f"""
            try {{
                localStorage.setItem('ld', JSON.stringify({json.dumps(token_data)}));
                console.log('[ometv-bot] Pre-loaded guest token UserId=' + {json.dumps(token_data)}.UserId);
            }} catch(e) {{}}
        """
    })
    print(f"  [+] Pre-injected guest token: UserId={token_data.get('UserId')}")


def get_guest_token(driver):
    """Read the guest token from localStorage after page load."""
    try:
        raw = driver.execute_script("return localStorage.getItem('ld');")
        if raw:
            return json.loads(raw)
    except:
        pass
    return None


def get_network_api_calls(driver):
    """Get all network requests — filter for API/token calls."""
    calls = []
    try:
        import json as _json
        logs = driver.get_log("performance")
        for entry in logs:
            msg = _json.loads(entry["message"])["message"]
            if msg.get("method") == "Network.requestWillBeSent":
                url = msg["params"]["request"]["url"]
                method = msg["params"]["request"]["method"]
                if any(x in url for x in ["/token", "/guest", "/session", "/api", "/auth", "/users"]):
                    body = msg["params"]["request"].get("postData", "")
                    calls.append({"method": method, "url": url, "body": body[:120]})
    except:
        pass
    return calls


def run_guest_session(driver, watch=60, save_token=True):
    """
    Main flow: load ome.tv, click Start, confirm guest session, watch chat.
    Returns the guest token dict.
    """
    print(f"\n[*] Loading {OMETV_URL}")
    driver.get(OMETV_URL)
    time.sleep(4)

    print(f"[*] Title: {driver.title}")

    # Read token generated on first load (before clicking Start)
    token_before = get_guest_token(driver)
    if token_before:
        print(f"[*] Token already in localStorage: {token_before}")
    else:
        print("[*] No token in localStorage yet")

    # Find + click Start
    print("[*] Looking for Start button...")
    try:
        start = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.ID, "start-button"))
        )
    except TimeoutException:
        # Fallback selectors
        for sel in [".ip-btn-start", ".btn-main"]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                start = els[0]
                break
        else:
            print("[!] No start button found!")
            driver.save_screenshot("minichat/ometv_no_start.png")
            return None

    print(f"[*] Clicking: '{start.text.strip()}'")
    try:
        start.click()
    except:
        driver.execute_script("arguments[0].click();", start)
    time.sleep(4)

    driver.save_screenshot("minichat/ometv_started.png")
    print("[*] Screenshot: ometv_started.png")
    print(f"[*] URL: {driver.current_url}")

    # Read token after click
    token = get_guest_token(driver)
    if token:
        print(f"[+] Guest token issued:")
        print(f"    UserId    = {token.get('UserId')}")
        print(f"    CreatedAt = {token.get('CreatedAt')}")
        print(f"    IP in token = {token.get('InitalAddr')}")
        print(f"    Hmac      = {token.get('Hmac', '')[:20]}...")
        if save_token:
            TOKEN_FILE.write_text(json.dumps(token, indent=2))
            print(f"    Saved to {TOKEN_FILE}")
    else:
        print("[!] No guest token found after click")

    # Check if login popup appeared and dismiss it
    # ome.tv sometimes shows a "sign up" nudge popup even for guests — we can close it
    body = driver.find_element(By.TAG_NAME, "body").get_attribute("innerHTML")
    login_popup = "login-popup visible" in body or (
        "facebook" in body.lower() and 
        "sign in" in body.lower() and 
        "block" in body.lower()
    )
    print(f"[*] Login popup detected: {login_popup}")

    if login_popup:
        # Try to close/dismiss the popup so chat can proceed
        dismissed = driver.execute_script("""
            // Try clicking any close/dismiss button inside the login popup
            var popup = document.querySelector('.login-popup');
            if (!popup) return 'no-popup';
            // Look for close button: .close, .popup-close, [data-dismiss], X button, etc.
            var closers = popup.querySelectorAll('.close, .popup-close, [data-dismiss], .btn-close, .login-popup__close');
            if (closers.length > 0) { closers[0].click(); return 'clicked-close'; }
            // Try pressing Escape via key event
            document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', keyCode:27, bubbles:true}));
            return 'sent-escape';
        """)
        print(f"  [*] Popup dismiss attempt: {dismissed}")
        time.sleep(1.5)

        # Check if still showing
        body2 = driver.find_element(By.TAG_NAME, "body").get_attribute("innerHTML")
        still_open = "login-popup visible" in body2
        print(f"  [*] Popup still open after dismiss: {still_open}")

        if still_open:
            # Force-hide via style
            driver.execute_script("""
                var p = document.querySelector('.login-popup');
                if (p) { p.style.display = 'none'; p.classList.remove('visible'); }
            """)
            time.sleep(0.5)
            print("  [*] Force-hidden popup via style")

        # Re-click Start now that popup is gone
        try:
            start2 = driver.find_element(By.ID, "start-button")
            driver.execute_script("arguments[0].click();", start2)
            print("  [*] Re-clicked Start after popup dismiss")
            time.sleep(3)
        except Exception as e:
            print(f"  [!] Re-click failed: {e}")

    # Any API calls that reveal how the token was issued
    api_calls = get_network_api_calls(driver)
    if api_calls:
        print(f"[*] Token-related API calls:")
        for c in api_calls:
            print(f"    {c['method']} {c['url']}  body={c['body']}")
    else:
        print("[*] No API calls for token — token generated client-side")

    # Watch chat state
    print(f"\n[*] Watching chat for {watch}s...")
    connected = False
    for i in range(watch // 2):
        time.sleep(2)
        try:
            b = driver.find_element(By.TAG_NAME, "body").get_attribute("innerHTML")
        except Exception:
            print(f"  [!] Browser closed early at {(i+1)*2}s")
            break
        # ome.tv uses roulette state classes on #roulette div
        connecting = "s-connecting" in b or "searching" in b.lower()
        in_chat    = "s-chat" in b
        stopped    = "s-stop" in b
        # Also check for Stop/Next buttons visible = active session
        try:
            has_controls = driver.execute_script("""
                var btns = document.querySelectorAll('.buttons__button');
                return Array.from(btns).map(b => b.textContent.trim()).join(',');
            """)
        except Exception:
            has_controls = "?"

        elapsed = (i + 1) * 2
        print(f"  [{elapsed:3d}s] connecting={connecting}  in_chat={in_chat}  stopped={stopped}  controls=[{has_controls}]")

        if in_chat and not connected:
            connected = True
            print(f"\n  [++++] CONNECTED TO PARTNER AT {elapsed}s!")
            try:
                driver.save_screenshot("minichat/ometv_chat_active.png")
                print("  [*] Screenshot: ometv_chat_active.png")
            except Exception:
                pass

    try:
        driver.save_screenshot("minichat/ometv_session_end.png")
    except Exception:
        pass
    print(f"\n[*] Session result: connected={connected}")
    return token


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy",    action="store_true",  help="Use proxy from proxies.txt")
    ap.add_argument("--headless", action="store_true",  help="Headless Chrome")
    ap.add_argument("--reuse",    action="store_true",  help="Pre-inject saved guest token")
    ap.add_argument("--watch",    type=int, default=60, help="Seconds to watch (default: 60)")
    ap.add_argument("--proxy-url",type=str, default=None, help="Direct proxy URL override")
    ap.add_argument("--test-both",action="store_true",  help="Test both direct and proxy")
    args = ap.parse_args()

    proxy = None
    if args.proxy_url:
        proxy = args.proxy_url
    elif args.proxy or args.test_both:
        proxy = load_first_proxy()
    print(f"[*] Proxy: {proxy or 'none (direct)'}")

    if args.test_both:
        # Run direct first, then proxy
        print("\n>>> TEST 1: Direct connection")
        d1 = make_driver(proxy=None, headless=args.headless)
        try:
            t1 = run_guest_session(d1, watch=args.watch)
            print(f"[*] Direct token: {t1}")
            input("\nPress ENTER to run proxy test...")
        finally:
            d1.quit()

        print(f"\n>>> TEST 2: Proxy ({proxy})")
        d2 = make_driver(proxy=proxy, headless=args.headless)
        try:
            if args.reuse and TOKEN_FILE.exists():
                saved = json.loads(TOKEN_FILE.read_text())
                inject_saved_token(d2, saved)
            t2 = run_guest_session(d2, watch=args.watch)
            print(f"[*] Proxy token: {t2}")
            input("\nPress ENTER to close...")
        finally:
            d2.quit()
    else:
        driver = make_driver(proxy=proxy, headless=args.headless)
        try:
            if args.reuse and TOKEN_FILE.exists():
                saved = json.loads(TOKEN_FILE.read_text())
                print(f"[*] Reusing saved token: UserId={saved.get('UserId')}")
                inject_saved_token(driver, saved)

            token = run_guest_session(driver, watch=args.watch)

            if token:
                print("\n[+] Guest session complete!")
                print(f"    Token: {json.dumps(token)}")
            else:
                print("\n[!] No guest token acquired")

            input("\nPress ENTER to close browser...")
        except Exception as e:
            print(f"[!] Error: {e}")
            import traceback; traceback.print_exc()
        finally:
            driver.quit()


if __name__ == "__main__":
    main()
