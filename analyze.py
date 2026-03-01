"""Analyze the verify page source to understand its structure"""
with open('verify_source.html', 'r', encoding='utf-8') as f:
    text = f.read()

lines = text.split('\n')
print(f'Total lines: {len(lines)}')

# Show key structural lines
for i, line in enumerate(lines):
    l = line.strip()
    lower = l.lower()
    if any(k in lower for k in ['<body', 'verify-card', 'press to verify', 'require', 'member', 
                                  'server-icon', 'server-name', 'btn-verify', 'card', 'main-content',
                                  'container', 'hero', 'backdrop', 'background', '<nav', '<footer',
                                  'captcha.bot', '<h1', '<h2', '<h3', 'verify-btn']):
        print(f'L{i+1}: {l[:250]}')
