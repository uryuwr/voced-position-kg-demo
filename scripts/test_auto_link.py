from backend.kg.pg_store.client import connect
from backend.kg.pg_store.write import create_node, extract_link_ids

with connect() as c:
    inds = list(
        c.execute(
            """
            SELECT id FROM kg_node
            WHERE type='industry' AND COALESCE(status,'published')='published'
            LIMIT 2
            """
        )
    )
print("inds", [i["id"] for i in inds])
payload = {
    "type": "major",
    "name": "边测试专业X",
    "region": "CN",
    "status": "published",
    "industry_ids": [i["id"] for i in inds],
}
print("links", extract_link_ids(payload))
n = create_node(payload, user_id="1", user_name="t")
print("created", n["id"], "n_linked", len(n.get("linked_edges") or []))
with connect() as c:
    e = list(
        c.execute(
            "SELECT rel_type, src_id, dst_id FROM kg_edge WHERE src_id=%s OR dst_id=%s",
            (n["id"], n["id"]),
        )
    )
    print("db edges", e)
    c.execute("DELETE FROM kg_edge WHERE src_id=%s OR dst_id=%s", (n["id"], n["id"]))
    c.execute("DELETE FROM kg_node WHERE id=%s", (n["id"],))
    c.commit()
