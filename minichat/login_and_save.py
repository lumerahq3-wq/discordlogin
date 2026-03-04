"""
minichat/login_and_save.py

Opens a real Chrome browser, lets you log in with Google/Facebook/VK,
intercepts the accessToken from the API call, and saves it to token.json.
Run once, then use the token in bot scripts.
"""
import json, re, time, os
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities

OUTPUT = os.path.join(os.path.dirname(__file__), "token.json")

def get_token_from_logs(driver):
    """Scan Chrome performance logs for the accessToken in API calls."""
    try:
        logs = driver.get_log("performance")
    except:
        return None
    for entry in logs:
        try:
            msg = json.loads(entry["message"])["message"]
            if msg.get("method") == "Network.requestWillBeSent":
                url = msg.get("params", {}).get("request", {}).get("url", "")
                if "sessions/me" in url or "accessToken" in url:
                    # Try to get from query params
                    m = re.search(r'accessToken=([^&]+)', url)
                    if m:
                        return m.group(1)
                    # Try post body
                    body = msg.get("params", {}).get("request", {}).get("postData", "")
                    if body:
                        try:
                            d = json.loads(body)
                            if "accessToken" in d:
                                return d["accessToken"]
                        except:
                            pass
            elif msg.get("method") == "Network.responseReceived":
                url = msg.get("params", {}).get("response", {}).get("url", "")
                if "sessions/me" in url:
                    pass  # token comes from the request side
        except:
            pass
    return None

def get_token_from_cookies(driver):
    """Try to get auth token from storage/cookies."""
    try:
        # Try localStorage
        token = driver.execute_script(
            "return localStorage.getItem('accessToken') || "
            "localStorage.getItem('token') || "
            "localStorage.getItem('api_key') || "
            "localStorage.getItem('minichat_token') || null;"
        )
        if token:
            return token
    except:
        pass
    # Try cookies
    cookies = driver.get_cookies()
    for c in cookies:
        if "token" in c["name"].lower() or "api" in c["name"].lower():
            return c["value"]
    return None

def scan_all_storage(driver):
    """Dump all localStorage and sessionStorage entries."""
    try:
        data = driver.execute_script("""
            var out = {};
            for (var i = 0; i < localStorage.length; i++) {
                var k = localStorage.key(i);
                out['ls:'+k] = localStorage.getItem(k);
            }
            for (var i = 0; i < sessionStorage.length; i++) {
                var k = sessionStorage.key(i);
                out['ss:'+k] = sessionStorage.getItem(k);
            }
            return out;
        """)
        return data
    except:
        return {}

def main():
    print("[*] Setting up Chrome...")
    opts = Options()
    opts.add_argument("--start-maximized")
    # Enable performance logging to capture network requests
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=opts)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    print("[*] Opening Minichat...")
    driver.get("https://minichat.com/")
    time.sleep(2)

    print("\n" + "="*60)
    print("ACTION REQUIRED:")
    print("1. Click the LOGIN button on Minichat")
    print("2. Choose Google, Facebook, or VK")
    print("3. Complete the login")
    print("After you're logged in, press ENTER here.")
    print("="*60 + "\n")
    input("Press ENTER after you've logged in > ")

    # Wait a moment for the app to stabilize
    time.sleep(3)

    # Try to get token from localStorage/cookies
    storage = scan_all_storage(driver)
    print("\n[*] Storage entries found:")
    for k, v in storage.items():
        if v:
            print(f"  {k}: {str(v)[:100]}")

    # Try performance logs
    token = get_token_from_logs(driver)
    if token:
        print(f"\n[+] Found token in network logs: {token[:40]}...")
    else:
        token = get_token_from_cookies(driver)
        if token:
            print(f"\n[+] Found token in storage/cookies: {token[:40]}...")

    # Try to get it from the Redux state in the page
    try:
        state_token = driver.execute_script("""
            // Try to find the token from the React/Next.js state
            try {
                var store = window.__NEXT_DATA__;
                if (store && store.props && store.props.pageProps) {
                    var s = store.props.pageProps.hydration;
                    if (s && s.session && s.session.apiKey) return s.session.apiKey;
                }
            } catch(e) {}
            // Try to find in any global variable
            for (var k in window) {
                try {
                    var v = window[k];
                    if (typeof v === 'string' && v.length > 20 && v.length < 200 && /^[a-zA-Z0-9_-]+$/.test(v)) {
                        if (k.toLowerCase().includes('token') || k.toLowerCase().includes('key') || k.toLowerCase().includes('auth')) {
                            return JSON.stringify({key: k, val: v.substring(0, 40)});
                        }
                    }
                } catch(e) {}
            }
            return null;
        """)
        if state_token:
            print(f"[+] Found from page state: {state_token}")
            # Try parse
            try:
                obj = json.loads(state_token)
                token = obj.get("val")
            except:
                token = state_token
    except Exception as e:
        print(f"[!] State extraction error: {e}")

    # Get all cookies for the domain
    all_cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
    print(f"\n[*] All cookies: {list(all_cookies.keys())}")

    if not token:
        print("\n[!] Could not auto-extract token.")
        print("    Try navigating to any API URL manually:")
        print("    Paste this in the browser address bar:")
        print("    https://b.minichat.com/api/v1/sessions/me?v=4")
        input("    Press ENTER after you've noted any token info > ")

    # Navigate to sessions/me to see the live session
    print("\n[*] Navigating to /api/v1/sessions/me?v=4 to inspect...")
    driver.get("https://b.minichat.com/api/v1/sessions/me?v=4")
    time.sleep(2)
    body = driver.find_element(By.TAG_NAME, "body").text
    print(f"[*] /sessions/me response: {body[:300]}")
    try:
        session_data = json.loads(body)
        print(f"[+] Session JSON: {json.dumps(session_data, indent=2)[:300]}")
    except:
        pass

    # Save what we have
    result = {
        "token": token,
        "cookies": all_cookies,
        "storage": storage,
    }
    with open(OUTPUT, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n[+] Saved to {OUTPUT}")

    driver.quit()
    print("[+] Done.")

if __name__ == "__main__":
    main()
