"""为「测试专业」补上行业边（此前前端未提交 industry_ids）。"""
from backend.kg.pg_store.client import connect
from backend.kg.pg_store.write import apply_node_links

with connect() as c:
    major = c.execute(
        "SELECT id FROM kg_node WHERE name = %s AND type = 'major'",
        ("测试专业",),
    ).fetchone()
    inds = list(
        c.execute(
            """
            SELECT id FROM kg_node
            WHERE type = 'industry' AND COALESCE(status, 'published') = 'published'
            ORDER BY name
            LIMIT 2
            """
        )
    )

if not major:
    print("major not found")
else:
    ids = [i["id"] for i in inds]
    edges = apply_node_links(
        major["id"],
        "major",
        {"industry_ids": ids, "major_ids": [], "occupation_ids": []},
        user_id="system",
        user_name="backfill",
        replace=True,
    )
    print("major", major["id"], "linked", len(edges), "industries", ids)
