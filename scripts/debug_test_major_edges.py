from backend.kg.pg_store.client import connect
from backend.kg.pg_store.query import explore_graph

with connect() as c:
    rows = c.execute(
        "SELECT id, name, status, type FROM kg_node WHERE name LIKE %s",
        ("%测试专业%",),
    ).fetchall()
    print("nodes:", [dict(r) for r in rows])
    for r in rows:
        edges = c.execute(
            "SELECT id, src_id, dst_id, rel_type, status FROM kg_edge WHERE src_id=%s OR dst_id=%s",
            (r["id"], r["id"]),
        ).fetchall()
        print("edges:", [dict(e) for e in edges])
        for e in edges:
            for nid in (e["src_id"], e["dst_id"]):
                n = c.execute(
                    "SELECT id, name, type, status FROM kg_node WHERE id=%s", (nid,)
                ).fetchone()
                print("  peer", dict(n) if n else nid)

if rows:
    data = explore_graph("测试专业", node_type="major", region="CN", depth=2, max_nodes=50)
    print(
        "explore nodes",
        [(n.get("name"), n.get("type")) for n in data.get("nodes", [])],
    )
    print("explore edges", len(data.get("edges", [])), data.get("meta"))
