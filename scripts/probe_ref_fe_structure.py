"""Extract structure from Open-Q frontend mock for API alignment."""
import re
import urllib.request

base = "https://fad9b1015ce041ae9dd149363f74c632.gz2.agentos-app.net/"
fe = urllib.request.urlopen(
    urllib.request.Request(base + "assets/js/frontend.js", headers={"User-Agent": "Mozilla/5.0"}),
    timeout=60,
).read().decode("utf-8", "replace")
data = urllib.request.urlopen(
    urllib.request.Request(base + "assets/js/data.js", headers={"User-Agent": "Mozilla/5.0"}),
    timeout=60,
).read().decode("utf-8", "replace")

print("=== views/routes ===")
for m in re.finditer(r"route\s*[:=]\s*['\"](\w+)['\"]|setView\(['\"](\w+)|case\s+['\"](\w+)['\"]", fe):
    pass
# tab/menu
for pat in [r"tab:\s*'(\w+)'", r"route:\s*'(\w+)'", r"setView\('([^']+)'", r"v(\w+)\s*\("]:
    found = sorted(set(re.findall(pat, fe)))
    print(pat, found[:40])

print("\n=== state keys ===")
sm = re.search(r"const state\s*=\s*\{([^}]+)\}", fe, re.S)
if sm:
    print(sm.group(1)[:800])

print("\n=== LS keys ===")
print(sorted(set(re.findall(r"LS\.(?:get|set)\('([^']+)'", fe))))

print("\n=== DATA top-level consts ===")
print(re.findall(r"const ([A-Z_]+)\s*=", data)[:40])

# sample profession/position fields
for name in ["PROFESSIONS", "POSITIONS", "SKILLS", "RESOURCES"]:
    m = re.search(rf"const {name}\s*=\s*\[", data)
    if not m:
        print(name, "not found")
        continue
    start = m.end()
    snippet = data[start : start + 600]
    print("\n", name, snippet[:500])
