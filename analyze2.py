"""Write analysis output to file to avoid PSReadLine issues"""
with open('verify_source.html', 'r', encoding='utf-8') as f:
    text = f.read()

lines = text.split('\n')
out = [f'Total lines: {len(lines)}', f'Total chars: {len(text)}', '']

# Key structural keywords
keywords = ['<body', 'verify-card', 'press to verify', 'require', 'member', 
            'server-icon', 'server-name', 'btn', 'card', 'main',
            'container', 'hero', 'backdrop', 'background', '<nav', '<footer',
            'captcha.bot', '<h1', '<h2', '<h3', 'verify-btn', 'iframe',
            'modal', 'overlay', 'discord', '<img', 'font-family', '@import',
            'google', 'gradient', '#2b2d31', '#1e1f22', 'svg', 'icon']

for i, line in enumerate(lines):
    l = line.strip()
    lower = l.lower()
    if any(k in lower for k in keywords):
        out.append(f'L{i+1}: {l[:300]}')

# Also extract first 200 lines raw
out.append('\n\n=== FIRST 200 LINES ===')
for i, line in enumerate(lines[:200]):
    out.append(f'{i+1}: {line}')

# Extract last 100 lines
out.append('\n\n=== LAST 100 LINES ===')
for i, line in enumerate(lines[-100:]):
    out.append(f'{len(lines)-100+i+1}: {line}')

with open('analyze_output.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))

print('Done! Output in analyze_output.txt')
