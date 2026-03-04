"""Probe ome.tv guest mechanism in depth."""
import requests, re, json

s = requests.Session()
s.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin': 'https://ome.tv',
    'Referer': 'https://ome.tv/',
})

r = s.get('https://ome.tv/', timeout=10)
html = r.text

# Find script src tags
script_srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
print("Script files:")
for sc in script_srcs:
    print(f"  {sc}")

# End of html
print("\n--- End of HTML (last 3000 chars) ---")
print(html[-3000:])

# Try to fetch main app JS if found
for sc in script_srcs:
    url = sc if sc.startswith('http') else 'https://ome.tv' + sc
    if any(x in sc for x in ['app', 'main', 'bundle', 'chat', 'vendor']):
        print(f"\n=== JS: {url} (first 3000 chars) ===")
        try:
            jr = s.get(url, timeout=15)
            print(jr.text[:3000])
        except Exception as e:
            print(f"Error: {e}")
