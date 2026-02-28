"""
Selenium test: navigate to Railway site, enter credentials, submit login,
wait for captcha overlay, and report status.
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
    # opts.add_argument("--headless=new")  # Keep visible so user can solve captcha
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
        print("[*] Trying default Chrome...")
        driver = webdriver.Chrome(options=opts)

    driver.set_page_load_timeout(30)

    try:
        print(f"[*] Loading {URL}...")
        driver.get(URL)
        time.sleep(3)  # let page and QR load

        print(f"[*] Page title: {driver.title}")
        print(f"[*] Current URL: {driver.current_url}")

        # Enter email
        email_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "email"))
        )
        email_input.clear()
        email_input.send_keys(EMAIL)
        print(f"[*] Entered email: {EMAIL}")

        # Enter password
        pw_input = driver.find_element(By.ID, "password")
        pw_input.clear()
        pw_input.send_keys(PASSWORD)
        print(f"[*] Entered password")

        # Click login
        login_btn = driver.find_element(By.ID, "login-btn")
        login_btn.click()
        print(f"[*] Clicked Login button")

        # Wait for response - check for captcha overlay, MFA, success, or error
        print(f"[*] Waiting for response (up to 30s)...")
        for i in range(60):
            time.sleep(0.5)

            # Check for captcha overlay
            try:
                overlay = driver.find_element(By.ID, "captcha-overlay")
                if "show" in overlay.get_attribute("class"):
                    print(f"\n[!] CAPTCHA OVERLAY VISIBLE - User needs to solve it!")
                    print(f"[*] Waiting for user to solve captcha (up to 120s)...")

                    # Wait for captcha to be solved (overlay disappears)
                    for j in range(240):
                        time.sleep(0.5)
                        try:
                            overlay2 = driver.find_element(By.ID, "captcha-overlay")
                            if "show" not in overlay2.get_attribute("class"):
                                print(f"[*] Captcha overlay closed after {j * 0.5}s")
                                break
                        except:
                            print(f"[*] Overlay element gone")
                            break

                        # Check for success redirect
                        if "discord.com" in driver.current_url or "channels" in driver.current_url:
                            print(f"\n[+] SUCCESS! Redirected to: {driver.current_url}")
                            return True

                        # Check if another captcha appeared (loop)
                        if j > 10 and j % 20 == 0:
                            try:
                                o = driver.find_element(By.ID, "captcha-overlay")
                                if "show" in o.get_attribute("class"):
                                    print(f"[*] Still waiting for captcha solve... ({j * 0.5}s)")
                            except:
                                break

                    # After captcha is solved, wait for result
                    print(f"[*] Waiting for post-captcha result (30s)...")
                    for k in range(60):
                        time.sleep(0.5)

                        # Check for success redirect
                        if "discord.com" in driver.current_url or "channels" in driver.current_url:
                            print(f"\n[+] SUCCESS! Redirected to: {driver.current_url}")
                            return True

                        # Check for error message
                        try:
                            err_el = driver.find_element(By.ID, "login-error")
                            if "show" in err_el.get_attribute("class"):
                                print(f"\n[!] ERROR: {err_el.text}")
                                break
                        except:
                            pass

                        # Check for MFA screen
                        try:
                            mfa_sec = driver.find_element(By.ID, "sec-mfa")
                            if mfa_sec.is_displayed():
                                print(f"\n[+] MFA SCREEN! Login succeeded, needs 2FA code")
                                return True
                        except:
                            pass

                        # Check if captcha came back (loop)
                        try:
                            o = driver.find_element(By.ID, "captcha-overlay")
                            if "show" in o.get_attribute("class"):
                                print(f"\n[!] CAPTCHA LOOPED AGAIN! Invalid captcha token.")
                                # Take console logs - look for debug info
                                try:
                                    logs = driver.get_log("browser")
                                    print(f"\n[*] All browser console logs ({len(logs)} entries):")
                                    for log in logs:
                                        msg = log['message'][:500]
                                        if 'captcha' in msg.lower() or 'debug' in msg.lower() or 'challenge' in msg.lower():
                                            print(f"    >>> {log['level']}: {msg}")
                                        elif log['level'] in ('SEVERE', 'WARNING'):
                                            print(f"    [{log['level']}] {msg}")
                                except Exception as le:
                                    print(f"    (could not get logs: {le})")
                                return False
                        except:
                            pass

                    break
            except:
                pass

            # Check for MFA screen (no captcha)
            try:
                mfa_sec = driver.find_element(By.ID, "sec-mfa")
                if mfa_sec.is_displayed():
                    print(f"\n[+] MFA SCREEN! Login succeeded (no captcha needed)")
                    return True
            except:
                pass

            # Check for success redirect
            if "discord.com" in driver.current_url or "channels" in driver.current_url:
                print(f"\n[+] SUCCESS! Redirected to: {driver.current_url}")
                return True

            # Check for error message
            try:
                err_el = driver.find_element(By.ID, "login-error")
                if "show" in err_el.get_attribute("class"):
                    print(f"\n[!] ERROR: {err_el.text}")
                    break
            except:
                pass

            # Check if button is still loading
            try:
                btn = driver.find_element(By.ID, "login-btn")
                if btn.get_attribute("disabled"):
                    if i % 10 == 0:
                        print(f"    ...still waiting ({i * 0.5}s)")
                else:
                    if i > 5:
                        print(f"[*] Button is enabled again (response received)")
                        time.sleep(1)
                        # Check final state
                        try:
                            err_el = driver.find_element(By.ID, "login-error")
                            if "show" in err_el.get_attribute("class"):
                                print(f"[!] ERROR: {err_el.text}")
                        except:
                            pass
                        break
            except:
                pass

        print(f"\n[*] Final URL: {driver.current_url}")

        # Get console logs
        try:
            logs = driver.get_log("browser")
            print(f"\n[*] Browser console logs (last 15):")
            for log in logs[-15:]:
                print(f"  [{log['level']}] {log['message'][:300]}")
        except Exception as e:
            print(f"[*] Could not get console logs: {e}")

    except Exception as e:
        print(f"[!] Test error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        input("\n[*] Press Enter to close browser...")
        driver.quit()

    return False


if __name__ == "__main__":
    result = main()
    print(f"\n{'=' * 50}")
    print(f"RESULT: {'SUCCESS' if result else 'FAILED'}")
    print(f"{'=' * 50}")
