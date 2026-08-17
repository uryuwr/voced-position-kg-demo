"""出站 TLS 校验配置 + UC MAC header 解析。

`settings.tls_verify()` 是重构新引入的**唯一**出站 verify 口径：三处出站客户端
（uc/client.py、bts/client.py、bts/auth.py）都读它，别再各自写死 `verify=False`
—— 留一处没改等于没改。所以这里既测取值规则，也扫源码确认没人绕过。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.uc.client import UCAuthError, parse_mac_header

REPO_ROOT = Path(__file__).resolve().parents[2]

OUTBOUND_CLIENTS = [
    REPO_ROOT / "backend" / "uc" / "client.py",
    REPO_ROOT / "backend" / "bts" / "client.py",
    REPO_ROOT / "backend" / "bts" / "auth.py",
]


class TestTlsVerify:
    def test_默认开启校验(self, settings_probe):
        """代码默认必须是安全的：关掉校验时 BTS 响应体里的 mac_key 是明文。

        注意本机 `backend/.env` 里写着 VERIFY_TLS=0（内测环境没有内网 CA）；
        settings_probe 会把 .env 隔离掉，这里测的是**干净镜像里的默认值**。
        """
        assert settings_probe().tls_verify() is True

    def test_可显式关闭(self, settings_probe):
        assert settings_probe(VERIFY_TLS="0").tls_verify() is False

    @pytest.mark.parametrize("v", ["1", "true", "yes", "on"])
    def test_多种真值写法(self, settings_probe, v):
        assert settings_probe(VERIFY_TLS=v).tls_verify() is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "off", ""])
    def test_多种假值写法(self, settings_probe, v):
        assert settings_probe(VERIFY_TLS=v).tls_verify() is False

    def test_内网根证书路径优先于布尔开关(self, settings_probe):
        s = settings_probe(TLS_CA_BUNDLE="/etc/ssl/certs/internal-ca.pem")
        assert s.tls_verify() == "/etc/ssl/certs/internal-ca.pem"

    def test_有证书路径时即使关了开关也仍校验(self, settings_probe):
        """配了 CA 就说明有办法验，别再被 VERIFY_TLS=0 降级成裸奔。"""
        assert settings_probe(VERIFY_TLS="0", TLS_CA_BUNDLE="/ca.pem").tls_verify() == "/ca.pem"

    def test_空白证书路径按没配处理(self, settings_probe):
        assert settings_probe(TLS_CA_BUNDLE="   ").tls_verify() is True

    @pytest.mark.parametrize("kw", [{}, {"VERIFY_TLS": "0"}, {"TLS_CA_BUNDLE": "/ca.pem"}])
    def test_返回值可直接喂给httpx(self, settings_probe, kw):
        assert isinstance(settings_probe(**kw).tls_verify(), (bool, str))


class TestNoHardcodedVerify:
    @pytest.mark.parametrize("src", OUTBOUND_CLIENTS, ids=lambda p: p.parent.name + "/" + p.name)
    def test_出站客户端不再写死verify_False(self, src):
        text = src.read_text(encoding="utf-8")
        hits = [
            m.group(0)
            for m in re.finditer(r"verify\s*=\s*(False|True)\b", text)
        ]
        assert not hits, f"{src.name} 里还有写死的 verify：{hits}"

    @pytest.mark.parametrize("src", OUTBOUND_CLIENTS, ids=lambda p: p.parent.name + "/" + p.name)
    def test_出站客户端都走统一口径(self, src):
        text = src.read_text(encoding="utf-8")
        assert "tls_verify()" in text, f"{src.name} 没读 settings.tls_verify()"


class TestParseMacHeader:
    HEADER = 'MAC id="tok-abc",nonce="123:xyz",mac="c2lnbg=="'

    def test_解析出三个字段(self):
        got = parse_mac_header(self.HEADER)
        assert got["id"] == "tok-abc"
        assert got["nonce"] == "123:xyz"
        assert got["mac"] == "c2lnbg=="

    def test_方案名大小写不敏感(self):
        assert parse_mac_header(self.HEADER.replace("MAC ", "mac "))["id"] == "tok-abc"

    def test_多余字段一并带出(self):
        got = parse_mac_header(self.HEADER + ',ext="x"')
        assert got["ext"] == "x"

    def test_字段顺序无关(self):
        h = 'MAC mac="m",id="i",nonce="n"'
        assert parse_mac_header(h) == {"mac": "m", "id": "i", "nonce": "n"}

    @pytest.mark.parametrize("bad", ["", None, "Bearer xxx", "MACid=\"x\"", "  ", "Basic dXNlcg=="])
    def test_不是MAC方案时报错(self, bad):
        with pytest.raises(UCAuthError, match="必须以 'MAC ' 开头"):
            parse_mac_header(bad)

    @pytest.mark.parametrize(
        "h,missing",
        [
            ('MAC id="i",nonce="n"', "mac"),
            ('MAC id="i",mac="m"', "nonce"),
            ('MAC nonce="n",mac="m"', "id"),
            ('MAC foo="bar"', "id"),
        ],
    )
    def test_缺字段时点名缺哪个(self, h, missing):
        with pytest.raises(UCAuthError) as e:
            parse_mac_header(h)
        assert missing in str(e.value)

    def test_空值字段算存在(self):
        """UC 会自己判空值无效；这里只负责结构解析，不越权替它判。"""
        assert parse_mac_header('MAC id="",nonce="",mac=""') == {"id": "", "nonce": "", "mac": ""}
