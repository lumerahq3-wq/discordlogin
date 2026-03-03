"""Find the JS redirect logic in the verify page."""
import re

with open('ageverify_source.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Find all script blocks > 50 chars
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
scripts = [s for s in scripts if len(s.strip()) > 50]

print(f"Found {len(scripts)} scripts with content")

for i, s in enumerate(scripts):
    # Check if it mentions login, verify, redirect, oauth, or the domain
    keywords = ['login', 'verify', 'redirect', 'oauth', 'window.location', 'age-verification', '.co/', 'discord.com/api', 'discord.com/oauth']
    has_kw = [kw for kw in keywords if kw.lower() in s.lower()]
    if has_kw:
        print(f"\n=== Script #{i} ({len(s)} chars) — Keywords: {has_kw} ===")
        print(s[:2000])
        print("...")
        if len(s) > 2000:
            print(s[-500:])
