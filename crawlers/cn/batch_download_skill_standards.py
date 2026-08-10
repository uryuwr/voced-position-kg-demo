"""批量下载公开国家职业(技术)技能标准 PDF。"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.paths import RAW, REPORTS, ensure_dirs

DEST = RAW / "CN" / "skill_standards"
UA = "Mozilla/5.0 EducationalKG/1.0"

# 公开可直链的标准（人社/智慧教育/地方转载公开稿）
TARGETS = [
    ("https://zsgx.mohrss.gov.cn/uploads/2024-10-24/%E8%99%9A%E6%8B%9F%E7%8E%B0%E5%AE%9E%E5%B7%A5%E7%A8%8B%E6%8A%80%E6%9C%AF%E4%BA%BA%E5%91%98%E5%9B%BD%E5%AE%B6%E8%81%8C%E4%B8%9A%E6%8A%80%E6%9C%AF%E6%8A%80%E8%83%BD%E6%A0%87%E5%87%86.pdf", "虚拟现实工程技术人员_国家职业技术技能标准.pdf"),
    ("https://zsgx.mohrss.gov.cn/uploads/2024-10-24/%E4%BA%91%E8%AE%A1%E7%AE%97%E5%B7%A5%E7%A8%8B%E6%8A%80%E6%9C%AF%E4%BA%BA%E5%91%98%E5%9B%BD%E5%AE%B6%E8%81%8C%E4%B8%9A%E6%8A%80%E6%9C%AF%E6%8A%80%E8%83%BD%E6%A0%87%E5%87%86.pdf", "云计算工程技术人员_国家职业技术技能标准.pdf"),
    ("https://zsgx.mohrss.gov.cn/uploads/2024-10-24/%E6%95%B0%E5%AD%97%E5%8C%96%E7%AE%A1%E7%90%86%E5%B8%88%E5%9B%BD%E5%AE%B6%E8%81%8C%E4%B8%9A%E6%8A%80%E6%9C%AF%E6%8A%80%E8%83%BD%E6%A0%87%E5%87%86.pdf", "数字化管理师_国家职业技术技能标准.pdf"),
    ("https://zsgx.mohrss.gov.cn/uploads/2024-10-24/%E5%8C%BA%E5%9D%97%E9%93%BE%E5%B7%A5%E7%A8%8B%E6%8A%80%E6%9C%AF%E4%BA%BA%E5%91%98%E5%9B%BD%E5%AE%B6%E8%81%8C%E4%B8%9A%E6%8A%80%E6%9C%AF%E6%8A%80%E8%83%BD%E6%A0%87%E5%87%86.pdf", "区块链工程技术人员_国家职业技术技能标准.pdf"),
    ("https://zsgx.mohrss.gov.cn/uploads/2024-10-24/%E5%B7%A5%E4%B8%9A%E4%BA%92%E8%81%94%E7%BD%91%E5%B7%A5%E7%A8%8B%E6%8A%80%E6%9C%AF%E4%BA%BA%E5%91%98%E5%9B%BD%E5%AE%B6%E8%81%8C%E4%B8%9A%E6%8A%80%E6%9C%AF%E6%8A%80%E8%83%BD%E6%A0%87%E5%87%86.pdf", "工业互联网工程技术人员_国家职业技术技能标准.pdf"),
    ("https://zsgx.mohrss.gov.cn/uploads/2024-10-24/%E6%95%B0%E6%8D%AE%E5%AE%89%E5%85%A8%E5%B7%A5%E7%A8%8B%E6%8A%80%E6%9C%AF%E4%BA%BA%E5%91%98%E5%9B%BD%E5%AE%B6%E8%81%8C%E4%B8%9A%E6%A0%87%E5%87%86.pdf", "数据安全工程技术人员_国家职业标准.pdf"),
    ("https://zsgx.mohrss.gov.cn/uploads/2024-10-24/%E6%99%BA%E8%83%BD%E5%88%B6%E9%80%A0%E5%B7%A5%E7%A8%8B%E6%8A%80%E6%9C%AF%E4%BA%BA%E5%91%98%E5%9B%BD%E5%AE%B6%E8%81%8C%E4%B8%9A%E6%8A%80%E6%9C%AF%E6%8A%80%E8%83%BD%E6%A0%87%E5%87%86.pdf", "智能制造工程技术人员_国家职业技术技能标准.pdf"),
    ("https://zsgx.mohrss.gov.cn/uploads/2024-10-24/%E5%A4%A7%E6%95%B0%E6%8D%AE%E5%B7%A5%E7%A8%8B%E6%8A%80%E6%9C%AF%E4%BA%BA%E5%91%98%E5%9B%BD%E5%AE%B6%E8%81%8C%E4%B8%9A%E6%8A%80%E6%9C%AF%E6%8A%80%E8%83%BD%E6%A0%87%E5%87%86.pdf", "大数据工程技术人员_国家职业技术技能标准.pdf"),
    ("https://zsgx.mohrss.gov.cn/uploads/2021-07-05/d8510985d87e40298c2af97346564b62.pdf", "物联网工程技术人员_国家职业技术技能标准.pdf"),
    ("https://rlsbj.cq.gov.cn/ywzl/zjrc/sy/zlxz/202301/P020230301347208580533.pdf", "人工智能工程技术人员_国家职业技术技能标准.pdf"),
    ("https://www.mohrss.gov.cn/SYrlzyhshbzb/zcfg/SYzhengqiuyijian/202106/W020210617509883457681.pdf", "人工智能训练师_国家职业技能标准_2021.pdf"),
    ("http://114.255.111.180/xxgk2020/fdzdgknr/rcrs_4225/jnrc/202112/W020211227626977039770.pdf", "人工智能训练师_国家职业技能标准_2021_mirror.pdf"),
    ("https://www.wic.edu.cn/upload/20260520/0e35c48d31c6cffdae48a3f9d603cc61.pdf", "计算机程序设计员_国家职业技能标准_2022.pdf"),
    # 集成电路等
    ("https://zsgx.mohrss.gov.cn/uploads/2024-10-24/%E9%9B%86%E6%88%90%E7%94%B5%E8%B7%AF%E5%B7%A5%E7%A8%8B%E6%8A%80%E6%9C%AF%E4%BA%BA%E5%91%98%E5%9B%BD%E5%AE%B6%E8%81%8C%E4%B8%9A%E6%8A%80%E6%9C%AF%E6%8A%80%E8%83%BD%E6%A0%87%E5%87%86.pdf", "集成电路工程技术人员_国家职业技术技能标准.pdf"),
    # 工种类（地方转载 / 人社公开稿）
    ("http://www.mohrss.gov.cn/wap/zc/zcwj/202112/W020211227626977022565.pdf", "呼叫中心服务员_国家职业技能标准.pdf"),
    ("https://mgj.sh.gov.cn/fileOperation/trustedRequest/remoteRead/year2023/month3/day20/file1679283822230.pdf", "密码技术应用员_国家职业技能标准_2022.pdf"),
    ("https://chinajob.mohrss.gov.cn/upload/resources/jnbzpdf/4ccc6a76ab911e0e57f90e.pdf", "chinajob_skill_standard_extra.pdf"),
    # 济宁人社转载公开稿（文件名在查询参数）
    (
        "http://hrss.jining.gov.cn/module/download/downfile.jsp?classid=0&showname=%E5%9B%BD%E5%AE%B6%E8%81%8C%E4%B8%9A%E6%8A%80%E8%83%BD%E6%A0%87%E5%87%86%E2%80%94%E2%80%94%E7%94%B5%E5%B7%A5.pdf&filename=0d1e7319be054d66adad197e51054edd.pdf",
        "电工_国家职业技能标准.pdf",
    ),
    (
        "http://hrss.jining.gov.cn/module/download/downfile.jsp?classid=0&showname=%E5%9B%BD%E5%AE%B6%E8%81%8C%E4%B8%9A%E6%8A%80%E8%83%BD%E6%A0%87%E5%87%86%E2%80%94%E2%80%94%E5%B7%A5%E4%B8%9A%E6%9C%BA%E5%99%A8%E4%BA%BA%E7%B3%BB%E7%BB%9F%E8%BF%90%E7%BB%B4%E5%91%98.pdf&filename=0cc5e006d7c74d52987d8fc8d79671d5.pdf",
        "工业机器人系统运维员_国家职业技能标准.pdf",
    ),
]


def main() -> None:
    ensure_dirs()
    DEST.mkdir(parents=True, exist_ok=True)
    ok = skip = fail = 0
    fails = []
    for url, name in TARGETS:
        path = DEST / name
        if path.exists() and path.stat().st_size > 5000 and path.read_bytes()[:4] == b"%PDF":
            skip += 1
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if not data.startswith(b"%PDF"):
                fail += 1
                fails.append((name, "not_pdf"))
                continue
            path.write_bytes(data)
            ok += 1
            print("OK", name, len(data))
        except Exception as e:
            fail += 1
            fails.append((name, str(e)))
            print("FAIL", name, e)
        time.sleep(0.2)
    report = {"ok": ok, "skip": skip, "fail": fail, "fails": fails}
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "batch_download_skill_standards.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
