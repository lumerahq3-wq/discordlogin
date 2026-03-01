import requests, json, time

# Anti-Captcha API test for hCaptcha
key = 'b7a1846d602861ef723c924eee4de940'

# Step 1: Check balance
r = requests.post('https://api.anti-captcha.com/getBalance', json={'clientKey': key})
print('Balance:', json.dumps(r.json(), indent=2))

# Step 2: Create HCaptchaTaskProxyless
r = requests.post('https://api.anti-captcha.com/createTask', json={
    'clientKey': key,
    'task': {
        'type': 'HCaptchaTaskProxyless',
        'websiteURL': 'https://discord.com/login',
        'websiteKey': 'a9b5fb07-92ff-493f-86fe-352a2803b3df',
        'isEnterprise': True
    }
})
d = r.json()
print('createTask:', json.dumps(d, indent=2))

if d.get('errorId') == 0 and d.get('taskId'):
    task_id = d['taskId']
    print(f'Task ID: {task_id} - polling...')
    for i in range(60):
        time.sleep(5)
        r2 = requests.post('https://api.anti-captcha.com/getTaskResult', json={
            'clientKey': key,
            'taskId': task_id
        })
        d2 = r2.json()
        status = d2.get('status', 'unknown')
        if status == 'ready':
            token = d2.get('solution', {}).get('gRecaptchaResponse', '')
            print(f'SOLVED! Token length: {len(token)}')
            print(f'Token (first 100): {token[:100]}...')
            break
        elif d2.get('errorId', 0) != 0:
            print(f'Error: {json.dumps(d2, indent=2)}')
            break
        else:
            print(f'  Poll {i+1}: {status}')
    else:
        print('Timeout - captcha not solved in 5 minutes')
