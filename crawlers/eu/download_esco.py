"""
Fetch ESCO occupations, skills, and occupation→skill relations via official Web API.

API base: https://ec.europa.eu/esco/api
Bulk CSV portal (manual): https://esco.ec.europa.eu/en/use-esco/download

Produces under data/raw/EU/esco/api_harvest/:
  - occupations_{lang}.json
  - skills_{lang}.json
  - relations_{lang}.json   # {occupation_uri, skill_uri, relation, skill_title}
  - manifest.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.paths import RAW, REPORTS, ensure_dirs
from backend.kg.provenance import utc_now_iso

API = "https://ec.europa.eu/esco/api"
UA = "VocEdKG-Bot/0.2 (+official ESCO API; contact: local-dev)"
LICENSE = "ESCO open data — see esco.ec.europa.eu terms; attribution required"


def http_get_json(url: str) -> dict:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl:
        time.sleep(0.3)
        proc = subprocess.run(
            [
                curl,
                "-sS",
                "-A",
                UA,
                "-H",
                "Accept: application/json",
                "--max-time",
                "90",
                url,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or f"curl exit {proc.returncode}")
        return json.loads(proc.stdout)

    import requests

    time.sleep(0.3)
    resp = requests.get(
        url,
        headers={"User-Agent": UA, "Accept": "application/json"},
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()


def search(resource_type: str, language: str, text: str, limit: int, offset: int) -> dict:
    params = {
        "text": text,
        "language": language,
        "type": resource_type,
        "limit": str(limit),
        "offset": str(offset),
        "full": "false",
        "viewObsolete": "false",
    }
    url = f"{API}/search?{urllib.parse.urlencode(params)}"
    return http_get_json(url)


def collect(
    resource_type: str,
    language: str,
    max_items: int,
    queries: list[str],
    page_size: int = 20,
) -> list[dict]:
    by_uri: dict[str, dict] = {}
    for q in queries:
        if len(by_uri) >= max_items:
            break
        offset = 0
        print(f"  {resource_type} query={q!r} (have {len(by_uri)})")
        while len(by_uri) < max_items:
            limit = min(page_size, max_items - len(by_uri))
            try:
                data = search(resource_type, language, q, limit, offset)
            except Exception as e:
                print(f"  API error: {e}")
                break
            batch = (data.get("_embedded") or {}).get("results") or []
            if not batch:
                break
            for item in batch:
                uri = item.get("uri") or ""
                if uri and uri not in by_uri:
                    by_uri[uri] = item
            total = int(data.get("total") or 0)
            offset += len(batch)
            if offset >= total or len(batch) < limit:
                break
            time.sleep(0.15)
    return list(by_uri.values())[:max_items]


def fetch_occupation_detail(uri: str, language: str) -> dict:
    params = {
        "uri": uri,
        "language": language,
        "viewObsolete": "false",
    }
    url = f"{API}/resource/occupation?{urllib.parse.urlencode(params)}"
    return http_get_json(url)


def extract_skill_links(detail: dict) -> list[dict]:
    """From occupation resource _links hasEssentialSkill / hasOptionalSkill."""
    links = detail.get("_links") or {}
    out: list[dict] = []
    for rel_key, rel_type in (
        ("hasEssentialSkill", "essential"),
        ("hasOptionalSkill", "optional"),
    ):
        items = links.get(rel_key) or []
        if isinstance(items, dict):
            items = [items]
        for it in items:
            uri = it.get("uri") or ""
            if not uri:
                continue
            out.append(
                {
                    "occupation_uri": detail.get("uri"),
                    "skill_uri": uri,
                    "skill_title": it.get("title") or "",
                    "skill_type": it.get("skillType") or "",
                    "relation": rel_type,
                }
            )
    return out


# Broad keyword coverage (API requires non-empty text)
OCC_QUERIES = [
    "software", "developer", "engineer", "technician", "teacher", "nurse",
    "mechanic", "analyst", "manager", "designer", "accountant", "driver",
    "chef", "electrician", "carpenter", "welder", "sales", "marketing",
    "data", "security", "healthcare", "construction", "agriculture",
    "logistics", "finance", "legal", "social", "science", "operator",
    "assistant", "clerk", "supervisor", "specialist", "consultant",
    "architect", "researcher", "therapist", "pharmacist", "plumber",
    "painter", "baker", "butcher", "cleaner", "guard", "pilot",
    "journalist", "translator", "musician", "coach", "librarian",
]

SKILL_QUERIES = [
    "programming", "communication", "python", "teamwork", "analysis",
    "mathematics", "safety", "customer", "machine learning", "project management",
    "writing", "leadership", "accounting", "welding", "nursing",
    "marketing", "design", "database", "cloud", "security",
    "language", "teaching", "maintenance", "quality", "logistics",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Harvest ESCO via official API (+ relations)")
    parser.add_argument("--language", default="en")
    parser.add_argument("--max-occupations", type=int, default=400)
    parser.add_argument("--max-skills", type=int, default=600)
    parser.add_argument(
        "--with-relations",
        action="store_true",
        default=True,
        help="Fetch occupation detail links for essential/optional skills (default on)",
    )
    parser.add_argument(
        "--no-relations",
        action="store_true",
        help="Skip relation harvest (faster, nodes only)",
    )
    parser.add_argument(
        "--max-relation-occupations",
        type=int,
        default=0,
        help="Cap occupations for detail fetch (0 = all harvested occupations)",
    )
    args = parser.parse_args()
    ensure_dirs()

    with_rel = args.with_relations and not args.no_relations
    out_dir = RAW / "EU" / "esco" / "api_harvest"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Fetching occupations...")
    occupations = collect(
        "occupation", args.language, args.max_occupations, OCC_QUERIES
    )
    print("Fetching skills (search)...")
    skills = collect("skill", args.language, args.max_skills, SKILL_QUERIES)

    relations: list[dict] = []
    skill_from_rel: dict[str, dict] = {}
    if with_rel and occupations:
        cap = args.max_relation_occupations or len(occupations)
        subset = occupations[:cap]
        print(f"Fetching occupation→skill relations for {len(subset)} occupations...")
        for i, occ in enumerate(subset, 1):
            uri = occ.get("uri") or ""
            if not uri:
                continue
            try:
                detail = fetch_occupation_detail(uri, args.language)
            except Exception as e:
                print(f"  skip {uri}: {e}")
                continue
            links = extract_skill_links(detail)
            relations.extend(links)
            for lk in links:
                su = lk["skill_uri"]
                if su not in skill_from_rel:
                    skill_from_rel[su] = {
                        "uri": su,
                        "preferredLabel": {args.language: lk["skill_title"]},
                        "title": lk["skill_title"],
                        "className": "Skill",
                        "skillType": lk.get("skill_type"),
                    }
            if i % 25 == 0:
                print(f"  … {i}/{len(subset)} occ, {len(relations)} rels")

        # merge relation skills into skills list
        by_uri = {s.get("uri"): s for s in skills if s.get("uri")}
        for uri, item in skill_from_rel.items():
            if uri not in by_uri:
                by_uri[uri] = item
                skills.append(item)

    fetched_at = utc_now_iso()
    occ_path = out_dir / f"occupations_{args.language}.json"
    skill_path = out_dir / f"skills_{args.language}.json"
    rel_path = out_dir / f"relations_{args.language}.json"
    meta = {
        "source_system": "ESCO",
        "api": API,
        "download_portal": "https://esco.ec.europa.eu/en/use-esco/download",
        "license": LICENSE,
        "language": args.language,
        "fetched_at": fetched_at,
        "with_relations": with_rel,
        "counts": {
            "occupations": len(occupations),
            "skills": len(skills),
            "relations": len(relations),
            "skills_from_relations": len(skill_from_rel),
        },
        "note": (
            "Official ESCO Web API harvest. For full classification dump use Download portal CSV. "
            "Relations from hasEssentialSkill / hasOptionalSkill on occupation resource."
        ),
        "paths": {
            "occupations": str(occ_path),
            "skills": str(skill_path),
            "relations": str(rel_path),
        },
    }
    occ_path.write_text(json.dumps(occupations, ensure_ascii=False, indent=2), encoding="utf-8")
    skill_path.write_text(json.dumps(skills, ensure_ascii=False, indent=2), encoding="utf-8")
    rel_path.write_text(json.dumps(relations, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "manifest.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_path = REPORTS / "download_esco_api_harvest.json"
    report_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"saved → {out_dir}")
    if not occupations:
        sys.exit(1)


if __name__ == "__main__":
    main()
