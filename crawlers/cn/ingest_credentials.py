"""
Ingest 1+X 职业技能等级证书目录 → credential nodes (CN).

Sources under data/raw/CN/credential/:
  - 1x_batch4_公示名单.pdf  第四批公示名单（可提取文本）
  - 可选：1x_batch1_3_list.txt  第一～三批名单（从公开转载页整理）
  - 可选：1x_batch3_名单.doc   第三批官方附件（Word）

主键：batch:序号:证书名 的稳定 hash → source_id = f\"{batch}:{seq}:{name}\"
去重：证书名称规范化后，后批覆盖前批同名（保留 batch/org 最新）

Usage:
  python -m crawlers.cn.ingest_credentials --dry-run
  python -m crawlers.cn.ingest_credentials
  python -m backend.kg.neo4j_store.migrate
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, stats, upsert_nodes
from backend.kg.paths import RAW, REPORTS, ensure_dirs
from backend.kg.provenance import make_node_id, utc_now_iso

SOURCE_SYSTEM = "ONE_PLUS_X"
REGION = "CN"
LICENSE = "教育部1+X证书制度试点公开名单；使用时请注明出处"
SOURCE_HOME = "http://www.moe.gov.cn/"

# 内嵌：第一～三批（教育部职业技术教育中心研究所公开名单的院校转载整理）
# 仅作底稿；若目录中有 1x_batch1_3_list.txt 则优先读文件
EMBEDDED_BATCHES: dict[int, list[tuple[str, str]]] = {
    1: [
        ("廊坊市中科建筑产业化创新研究中心（中国建设教育协会人才评价中心）", "建筑信息模型（BIM）职业技能等级证书"),
        ("工业和信息化部教育与考试中心", "Web前端开发职业技能等级证书"),
        ("北京中物联物流采购培训中心", "物流管理职业技能等级证书"),
        ("中国社会福利与养老服务协会北京中福长者文化科技有限公司", "老年照护职业技能等级证书"),
        ("北京中车行高新技术有限公司", "汽车运用与维修职业技能等级证书"),
        ("北京中车行高新技术有限公司", "智能新能源汽车职业技能等级证书"),
    ],
    2: [
        ("北京博导前程信息技术股份有限公司", "电子商务数据分析职业技能等级证书"),
        ("北京鸿科经纬科技有限公司", "网店运营推广职业技能等级证书"),
        ("北京新奥时代科技有限责任公司", "工业机器人操作与运维职业技能等级证书"),
        ("北京赛育达科教有限责任公司", "工业机器人应用编程职业技能等级证书"),
        ("中船舰客教育科技（北京）有限公司", "特殊焊接技术职业技能等级证书"),
        ("中联集团教育科技有限公司", "智能财税职业技能等级证书"),
        ("济南阳光大姐服务有限责任公司", "母婴护理职业技能等级证书"),
        ("北京新大陆时代教育科技有限公司", "传感网应用开发职业技能等级证书"),
        ("北京中民福祉教育科技有限责任公司", "失智老年人照护职业技能等级证书"),
        ("南京第五十五所技术开发有限公司", "云计算平台运维与开发职业技能等级证书"),
    ],
}


def cred_dir() -> Path:
    return RAW / "CN" / "credential"


def norm_space(s: str) -> str:
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"\s+", "", s)
    return s.strip()


def clean_cert_name(name: str) -> str:
    name = norm_space(name)
    # 统一后缀
    if name and not name.endswith("证书") and "职业技能等级" not in name:
        # 已有「职业技能等级证书」或短名均可
        pass
    # 去掉尾部星号备注
    name = name.rstrip("*＊").strip()
    return name


def clean_org(name: str) -> str:
    name = re.sub(r"\s+", "", name.replace("\u3000", ""))
    name = name.rstrip("*＊").strip()
    return name


def make_cred_node(
    *,
    cert_name: str,
    org: str,
    batch: int,
    seq: int,
    source_url: str,
    source_file: str,
    fetched_at: str,
    list_kind: str = "official_list",
) -> dict:
    cert_name = clean_cert_name(cert_name)
    org = clean_org(org)
    sid = f"batch{batch}:{seq}:{cert_name}"
    # id 长度控制
    if len(sid) > 180:
        sid = f"batch{batch}:{seq}:{cert_name[:80]}"
    attrs = {
        "batch": batch,
        "seq": seq,
        "issuer_org": org,
        "certificate_type": "1+X_skill_level",
        "list_kind": list_kind,
        "source_file": source_file,
    }
    return {
        "id": make_node_id(REGION, "credential", SOURCE_SYSTEM, sid),
        "region": REGION,
        "type": "credential",
        "name": cert_name,
        "name_en": None,
        "name_zh": cert_name,
        "description": f"1+X 第{batch}批 · {org}",
        "attrs": attrs,
        "source_system": SOURCE_SYSTEM,
        "source_id": sid,
        "source_url": source_url,
        "license": LICENSE,
        "fetched_at": fetched_at,
        "confidence": "official",
    }


def parse_numbered_triplets(text: str) -> list[tuple[int, str, str]]:
    """
    解析「序号 / 组织 / 证书」三行一组或 PDF 流式结构。
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # 去表头
    skip = {
        "序号",
        "培训评价组织名称",
        "证书名称",
        "附件",
        "备注",
    }
    cleaned: list[str] = []
    for ln in lines:
        if ln in skip:
            continue
        if ln.startswith("备注") or ln.startswith("第四批") and "公示" in ln:
            continue
        if re.match(r"^附件", ln):
            continue
        cleaned.append(ln)

    rows: list[tuple[int, str, str]] = []
    i = 0
    while i < len(cleaned):
        m = re.fullmatch(r"(\d{1,3})", cleaned[i])
        if not m:
            i += 1
            continue
        seq = int(m.group(1))
        if i + 2 >= len(cleaned):
            break
        org = cleaned[i + 1]
        cert = cleaned[i + 2]
        # 若 org/cert 又是数字则错位
        if re.fullmatch(r"\d{1,3}", org) or re.fullmatch(r"\d{1,3}", cert):
            i += 1
            continue
        # 证书名可能跨行（少见）——若下一行不像序号且不像机构，拼接
        j = i + 3
        while j < len(cleaned) and not re.fullmatch(r"\d{1,3}", cleaned[j]):
            # 仅拼接很短的续行（如「职业技能等级证书」被拆开）
            nxt = cleaned[j]
            if len(nxt) <= 12 and ("证书" in nxt or "职业技能" in nxt):
                cert = cert + nxt
                j += 1
            else:
                break
        rows.append((seq, org, cert))
        i = j if j > i + 3 else i + 3
    return rows


def parse_batch4_pdf(path: Path) -> list[tuple[int, str, str]]:
    import fitz

    doc = fitz.open(path)
    try:
        text = "\n".join(doc[i].get_text("text") for i in range(doc.page_count))
    finally:
        doc.close()
    return parse_numbered_triplets(text)


def parse_batch3_doc(path: Path) -> list[tuple[int, str, str]]:
    """Word .doc → text via COM if available, else fail empty."""
    try:
        from win32com.client import Dispatch

        w = Dispatch("Word.Application")
        w.Visible = False
        doc = w.Documents.Open(str(path.resolve()))
        try:
            text = doc.Content.Text
        finally:
            doc.Close(False)
            try:
                w.Quit()
            except Exception:
                pass
    except Exception:
        # 尝试已导出的 txt
        txt = path.with_suffix(".txt")
        if txt.is_file():
            text = txt.read_text(encoding="utf-8")
        else:
            return []

    # 压缩：序号组织证书连在一起 → 用正则切
    t = re.sub(r"\s+", "", text)
    # 去掉标题
    t = re.sub(r"^.*?证书名单", "", t)
    pat = re.compile(
        r"(\d{1,3})([\u4e00-\u9fffA-Za-z0-9（）()、，,\-·\.\&]+?)([\u4e00-\u9fffA-Za-z0-9（）()、，,\-·\.]+?职业技能等级证书)"
    )
    rows: list[tuple[int, str, str]] = []
    for m in pat.finditer(t):
        rows.append((int(m.group(1)), m.group(2), m.group(3)))
    if rows:
        return rows
    # 回退三行解析
    return parse_numbered_triplets(text)


def load_embedded_or_txt() -> dict[int, list[tuple[int, str, str]]]:
    """返回 {batch: [(seq, org, cert), ...]}"""
    out: dict[int, list[tuple[int, str, str]]] = {}
    txt = cred_dir() / "1x_batch1_3_list.txt"
    if txt.is_file():
        # 简单格式：batch\tseq\torg\tcert
        for line in txt.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 4:
                b, s, org, cert = int(parts[0]), int(parts[1]), parts[2], parts[3]
                out.setdefault(b, []).append((s, org, cert))
        return out

    for b, pairs in EMBEDDED_BATCHES.items():
        out[b] = [(i + 1, org, cert) for i, (org, cert) in enumerate(pairs)]
    return out


def collect(fetched_at: str) -> tuple[list[dict], dict]:
    d = cred_dir()
    report: dict = {"files": {}, "by_batch": {}}
    nodes_by_name: dict[str, dict] = {}
    order: list[str] = []

    def absorb(batch: int, rows: list[tuple[int, str, str]], *, url: str, file: str, kind: str):
        report["files"][file] = {"batch": batch, "rows": len(rows), "kind": kind}
        report["by_batch"][str(batch)] = report["by_batch"].get(str(batch), 0) + len(rows)
        for seq, org, cert in rows:
            node = make_cred_node(
                cert_name=cert,
                org=org,
                batch=batch,
                seq=seq,
                source_url=url,
                source_file=file,
                fetched_at=fetched_at,
                list_kind=kind,
            )
            key = clean_cert_name(cert)
            if key not in nodes_by_name:
                order.append(key)
            nodes_by_name[key] = node  # 后写覆盖

    # 第一～三批底稿
    emb = load_embedded_or_txt()
    for b, rows in sorted(emb.items()):
        absorb(
            b,
            rows,
            url="https://www.chinazy.org/",
            file="embedded_or_1x_batch1_3_list.txt",
            kind="official_list_republish",
        )

    # 第三批 .doc：仅当解析行数充足时覆盖（避免 COM 正则失败覆盖完整 tsv）
    b3 = d / "1x_batch3_名单.doc"
    if b3.is_file() and b3.stat().st_size > 10000:
        rows = parse_batch3_doc(b3)
        if len(rows) >= 50:
            absorb(
                3,
                rows,
                url="https://www.chinazy.org/info/1015/2981.htm",
                file=b3.name,
                kind="official_attachment",
            )
        else:
            report["files"][b3.name] = {
                "batch": 3,
                "rows": len(rows),
                "kind": "skipped_sparse_parse",
                "note": "use 1x_batch1_3_list.txt instead",
            }

    # 第四批公示 PDF
    b4 = d / "1x_batch4_公示名单.pdf"
    if not b4.is_file():
        # 备选文件名
        hits = list(d.glob("*batch4*.pdf")) + list(d.glob("*第四批*.pdf"))
        # 优先可抽取文本的公示版
        for h in hits:
            if "培训评价组织及证书名单" in h.name and "公示" not in h.name:
                continue
            b4 = h
            break
        else:
            b4 = hits[0] if hits else b4
    if b4.is_file():
        rows = parse_batch4_pdf(b4)
        absorb(
            4,
            rows,
            url="https://www.chinazy.org/info/1006/5304.htm",
            file=b4.name,
            kind="public_notice_list",
        )

    nodes = [nodes_by_name[k] for k in order if k in nodes_by_name]
    # 补上仅在覆盖中出现的
    for k, n in nodes_by_name.items():
        if k not in order:
            nodes.append(n)

    report["unique_certs"] = len(nodes)
    report["dedupe"] = "by normalized certificate name; later batch overwrites"
    return nodes, report


def ingest(dry_run: bool = False, db_path: Path | None = None) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    nodes, parse_report = collect(fetched_at)
    result = {
        "source_system": SOURCE_SYSTEM,
        "source_home": SOURCE_HOME,
        "fetched_at": fetched_at,
        "dry_run": dry_run,
        "nodes_parsed": len(nodes),
        "parse": parse_report,
        "sample": [
            {
                "id": n["id"],
                "name": n["name"],
                "batch": n["attrs"]["batch"],
                "org": n["attrs"]["issuer_org"],
            }
            for n in nodes[:8]
        ],
    }
    if dry_run:
        return result

    conn = connect(db_path)
    try:
        n = upsert_nodes(conn, nodes)
        conn.commit()
        s = stats(conn)
    finally:
        conn.close()

    result["nodes_upserted"] = n
    result["db_stats"] = s
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "ingest_cn_credentials.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest CN 1+X credentials")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    out = ingest(dry_run=args.dry_run, db_path=Path(args.db) if args.db else None)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
