import requests, json
API = 'https://api.ez-captcha.com'
types = [
    'HCaptchaTaskProxyless',
    'HCaptchaTask',
    'HCaptchaEnterpriseTaskProxyless',
    'HCaptchaClassification',
    'ReCaptchaV2TaskProxyless',
]
for t in types:
    r = requests.post(
        f'{API}/createTask',
        json={'clientKey':'test_fake_key','task':{'type':t,'websiteURL':'https://discord.com','websiteKey':'sitekey'}},
        timeout=15
    )
    j = r.json()
    print(f'{t}: {j.get("errorCode","OK")} - {j.get("errorDescription","")}')
