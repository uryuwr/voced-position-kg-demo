import re
import urllib.request

base = "https://fad9b1015ce041ae9dd149363f74c632.gz2.agentos-app.net/"
for p in ["assets/js/frontend.js", "assets/js/data.js", "assets/js/core.js"]:
    html = urllib.request.urlopen(
        urllib.request.Request(base + p, headers={"User-Agent": "Mozilla/5.0"}),
        timeout=30,
    ).read().decode("utf-8", "replace")
    print("====", p, "len", len(html))
    zh = re.findall(r"['\"]([^'\"]{2,60})['\"]", html)
    zh = [s for s in zh if re.search(r"[\u4e00-\u9fff]", s)]
    # unique preserve order
    seen = set()
    uniq = []
    for s in zh:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    print("zh count", len(uniq))
    for s in uniq[:80]:
        print(" ", s)
    fns = re.findall(r"function\s+(\w+)", html)
    print("fns", fns[:50])
    # id/nav patterns
    ids = re.findall(r"id:\s*['\"](\w+)['\"]", html)
    print("ids", ids[:40])
    print("head", html[:1200].replace("\n", " ")[:1200])
    print()
