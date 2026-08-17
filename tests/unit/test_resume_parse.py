"""backend/api/resume_parse.py —— 简历文件 → 文本。

上传是外部输入面，四类拒绝（类型 / 空 / 超限 / 抽不出字）都要给可读原因而不是 500。
PDF/DOCX 分支依赖三方包，这里只测不依赖包的路径 + 分支路由，避免把
「本机装没装 pypdf」混进单测结论里。
"""
from __future__ import annotations

import pytest

from backend.api import resume_parse
from backend.api.resume_parse import (
    MAX_BYTES,
    SAMPLE_RESUME,
    ResumeParseError,
    parse_resume_bytes,
)


class TestAccept:
    def test_txt直接解码(self):
        assert parse_resume_bytes("我的简历.txt", "配料准备\n搅拌操作".encode()) == "配料准备\n搅拌操作"

    def test_md也走纯文本(self):
        assert parse_resume_bytes("r.md", b"# hi") == "# hi"

    def test_扩展名大小写不敏感(self):
        assert parse_resume_bytes("R.TXT", b"ok") == "ok"

    def test_首尾空白被去掉(self):
        assert parse_resume_bytes("r.txt", b"  \n hi \n ") == "hi"

    def test_非UTF8字节忽略而不是报错(self):
        assert "abc" in parse_resume_bytes("r.txt", b"abc\xff\xfe def")


class TestReject:
    @pytest.mark.parametrize("name", ["r.doc", "r.jpg", "r", "", "r.pdf.exe", "r.txt.zip"])
    def test_不支持的扩展名(self, name):
        with pytest.raises(ResumeParseError, match="不支持的文件类型"):
            parse_resume_bytes(name, b"x")

    def test_文件名为空时报错信息可读(self):
        with pytest.raises(ResumeParseError, match="无文件名"):
            parse_resume_bytes("", b"x")

    @pytest.mark.parametrize("data", [b"", None])
    def test_内容为空(self, data):
        with pytest.raises(ResumeParseError, match="文件内容为空"):
            parse_resume_bytes("r.txt", data)

    def test_超过二十兆(self):
        with pytest.raises(ResumeParseError, match="文件过大"):
            parse_resume_bytes("r.txt", b"x" * (MAX_BYTES + 1))

    def test_上限本身放行(self):
        assert parse_resume_bytes("r.txt", b"x" * MAX_BYTES) == "x" * MAX_BYTES

    def test_抽不出文字时提示改用粘贴(self):
        with pytest.raises(ResumeParseError, match="扫描件"):
            parse_resume_bytes("r.txt", b"   \n\t  ")

    def test_错误类型是ValueError的子类便于路由转四百(self):
        assert issubclass(ResumeParseError, ValueError)


class TestDispatch:
    def test_pdf走pdf分支(self, monkeypatch):
        monkeypatch.setattr(resume_parse, "_from_pdf", lambda d: "PDF内容")
        assert parse_resume_bytes("r.pdf", b"%PDF-1.4") == "PDF内容"

    def test_docx走docx分支(self, monkeypatch):
        monkeypatch.setattr(resume_parse, "_from_docx", lambda d: "DOCX内容")
        assert parse_resume_bytes("r.docx", b"PK\x03\x04") == "DOCX内容"

    def test_pdf抽不出字时给可读原因而不是抛底层异常(self, monkeypatch):
        monkeypatch.setattr(resume_parse, "_from_pdf", lambda d: "")
        with pytest.raises(ResumeParseError, match="未能从文件中提取到文字"):
            parse_resume_bytes("r.pdf", b"%PDF-1.4")

    def test_底层解析器抛异常时被吞成空串(self):
        """三方包缺失或文件损坏都不该冒泡成 500 —— 两个 _from_* 都是 try/except 兜底。"""
        assert resume_parse._from_pdf(b"not a pdf") == ""
        assert resume_parse._from_docx(b"not a docx") == ""


class TestSampleResume:
    def test_范例用库内真实技能名(self):
        for skill in ("配料准备", "支模准备", "搅拌操作", "泵送操作", "设备维保"):
            assert skill in SAMPLE_RESUME

    def test_范例能过解析(self):
        assert parse_resume_bytes("sample.txt", SAMPLE_RESUME.encode()) == SAMPLE_RESUME.strip()
