import re
import urllib.request

url = "https://fad9b1015ce041ae9dd149363f74c632.gz2.agentos-app.net/frontend.html"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
print("len", len(html))
print("srcs", re.findall(r'src=["\']([^"\']+)["\']', html)[:30])
print("hrefs", re.findall(r'href=["\']([^"\']+)["\']', html)[:30])
# try sibling assets
base = url.rsplit("/", 1)[0]
for path in [
    "/assets/index.js",
    "/frontend.js",
    "/static/js/main.js",
    "/js/app.js",
]:
    pass
# dump raw head
print(html[:2500])
print("---")
for kw in [
    "诊断",
    "简历",
    "路径",
    "技能",
    "专业",
    "岗位",
    "行业",
    "探索",
    "成就",
    "进度",
    "画像",
    "搜索",
    "课程",
    "nav",
    "tab",
    "fetch",
    "api",
]:
    print(kw, html.lower().find(kw.lower()) if kw.isascii() else html.find(kw))
