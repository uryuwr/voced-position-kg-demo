import re
import urllib.request

base = "https://fad9b1015ce041ae9dd149363f74c632.gz2.agentos-app.net/"
html = urllib.request.urlopen(
    urllib.request.Request(base + "backend.html", headers={"User-Agent": "Mozilla/5.0"}),
    timeout=30,
).read().decode("utf-8", "replace")
print("html", html[:1500])
print("scripts", re.findall(r'src=["\']([^"\']+)["\']', html))

for p in re.findall(r'src=["\'](assets/js/[^"\']+)["\']', html):
    js = urllib.request.urlopen(
        urllib.request.Request(base + p, headers={"User-Agent": "Mozilla/5.0"}),
        timeout=60,
    ).read().decode("utf-8", "replace")
    print("====", p, len(js))
    # views / nav
    zh = re.findall(r"['\"]([^'\"]{2,50})['\"]", js)
    zh = [s for s in zh if re.search(r"[\u4e00-\u9fff]", s)]
    seen = set()
    uniq = []
    for s in zh:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    for s in uniq[:100]:
        print(" ", s)
    fns = re.findall(r"function\s+(\w+)|(\w+)\s*:\s*function|const\s+v(\w+)\s*=", js)
    print("fns sample", fns[:40])
