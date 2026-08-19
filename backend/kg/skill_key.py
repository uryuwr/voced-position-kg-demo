"""`skill_key` 的唯一真源：生成、校验，以及**与 SQL 完全等价**的那份表达式。

## 为什么 skill_key 不能是技能名（2026-08-19 改）

原来 `attrs.skill_key` 存的就是中文技能名，问题有三层：

1. **它会跑到 URL 里**：`/v1/admin/skills/{skill_key}`、`?skill_key=`。名字里带
   `/`（26 个，如 `Python/R编程`）、`#`（`C#…`）、`%`、空格、`+` 的都有，一次编码
   失误就变成另一个 key。库里真的因此长出过一个幽灵技能
   `3D%25E5%259C%25BA%25E6%2599%25AF%25E6%2590%25AD%25E5%25BB%25BA`
   —— 二次解码就是 `3D场景搭建`，即前端把已编码的串又编码了一遍，服务端把它
   当成了新技能名。**主键一旦要走 URL，就不能是自由文本。**
2. **与 `skill_name` 职责重合**：一个字段同时当主键和展示名，改名就等于换主键。
3. **改名 = 断链**：先修表、测评题库都按 key 存，运营改个错别字就把关联改没了。

## 形态

`SK` + md5(规范化后的名字) 前 10 位十六进制，例如 `SKabd68031c5`。
规范化 = `NFC` + 去首尾空白（**只做这两件事**，见下）。

三条约束决定了这个形态：

- **必须能从名字稳定推出来**。采集端是按名字重复入库的：随机 uuid 会让重跑采集
  找不到已有技能、再造一个，而这类错不报错，只表现为「同一岗位两个数字」
  （CLAUDE.md 里那条重复 `requires` 边的坑就是同一族）。
- **SQL 侧要能算出同一个值**。`SKILL_KEY_SQL` 的兜底分支用 `SQL_DERIVE_KEY`
  现算，于是**任何还没写 key 的行（新采集、直连改库）读出来也是 ASCII code**，
  不会回落成中文。md5 与 normalize(NFC) 是 PG 与 Python 都原生有、且实测逐位
  相同的两个函数 —— 换成 sha1/sha256 也行，但换成「小写化」「折叠空格」这类
  两边实现不一定一致的规范化就会漂移，所以规范化**刻意只做 NFC + strip**。
- **人得能一眼看出这是生成的**。`SK` 前缀把它和运营手填的业务 code 区分开。

碰撞：10 位十六进制 ≈ 1.1e12，5911 个 key 的生日碰撞概率约 1.6e-5。写路径仍然
显式查重（`assert_key_free`），撞上就报错让人换一个，不静默覆盖。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# 运营手填的 code：只允许英文字母 + 数字（产品要求），长度 2–64。
# 不放开下划线/连字符：一旦放开就会有人填 `c#-dev` 这种，又把 URL 敏感字符带回来。
KEY_RE = re.compile(r"^[A-Za-z0-9]{2,64}$")

GENERATED_PREFIX = "SK"
_HASH_LEN = 10


def normalize_name(name: str | None) -> str:
    """名字规范化 —— **只做 NFC + 去首尾空白**，与 SQL 侧逐字对应。

    别在这里加小写化、折叠内部空格、去标点：SQL 兜底要算出同一个值，
    每多一步就多一处两边实现可能不一致的地方，而不一致的表现是
    「同一个技能两个 key」，且不报错。
    """
    return unicodedata.normalize("NFC", (name or "").strip())


def derive_key(name: str | None) -> str:
    """名字 → 生成式 key。同名必同 key，跨进程、跨语言、跨重跑都一样。"""
    n = normalize_name(name)
    if not n:
        raise ValueError("技能名为空，无法生成 skill_key")
    return GENERATED_PREFIX + hashlib.md5(n.encode("utf-8")).hexdigest()[:_HASH_LEN]


def is_generated(key: str | None) -> bool:
    k = (key or "").strip()
    return bool(k.startswith(GENERATED_PREFIX) and KEY_RE.match(k))


def is_valid_key(key: str | None) -> bool:
    return bool(KEY_RE.match((key or "").strip()))


def assert_valid_key(key: str | None) -> str:
    k = (key or "").strip()
    if not is_valid_key(k):
        raise ValueError(
            f"skill_key 只允许英文字母和数字、长度 2–64：{key!r}。"
            f"留空则按技能名自动生成（形如 {GENERATED_PREFIX}0123456789）"
        )
    return k


def looks_like_name(key: str | None) -> bool:
    """这个 key 是不是旧形态（直接拿名字当 key）。

    迁移期用来判断「这行还没刷过」：非 ASCII，或含 URL 敏感字符，或不符合 KEY_RE。
    """
    k = (key or "").strip()
    if not k:
        return False
    return not is_valid_key(k)


def SQL_DERIVE_KEY(name_expr: str) -> str:  # noqa: N802 —— 是 SQL 片段常量，不是函数名风格问题
    """与 `derive_key` **等价**的 SQL 表达式。

    `name_expr` 是一段产出技能名的 SQL（列或表达式）。
    `md5()` 与 `normalize(..., NFC)` 都是 PG 原生（normalize 需要 PG≥13，
    线上是 16）。实测与 Python 侧逐位相同 —— 改这里必须回头改 `derive_key`，
    两边漂了不会报错，只会让同一个技能分裂成两个。
    """
    return (
        f"('{GENERATED_PREFIX}' || substr(md5(normalize(btrim({name_expr}), NFC)),"
        f" 1, {_HASH_LEN}))"
    )
