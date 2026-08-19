#!/usr/bin/env bash
# 对全部 BOSS 门类重跑晋升链（advance）。
# 提示词已改为逐岗位枚举三类方向，且 stage_advance 现在会先删本门类旧边再重建，
# 所以这是一次「全量重建」而不是叠加。
set -u
tail -n +2 .l1_list.txt | while IFS= read -r l1; do
  [ -z "$l1" ] && continue
  echo "=== $l1 ==="
  timeout 3000 python -X utf8 -m crawlers.cn.link_boss_skill_chain \
    --stage advance --l1 "$l1" --batch 40 --sleep 0.3 2>&1 \
    | grep -E '"(edges_valid|llm_paths_proposed|stale_advances_removed|cross_l1_targets|dropped)"' || true
done
echo "=== 全部门类 advance 重跑完成 ==="
