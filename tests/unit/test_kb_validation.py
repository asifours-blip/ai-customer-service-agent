"""知识库上传校验（纯函数，离线）：各类反例都要指出具体文件与不合格项。"""

from __future__ import annotations

import pytest

from app.kb.validation import (
    MAX_FILE_BYTES,
    MAX_FILES,
    UploadedFile,
    UploadRejected,
    check_filename,
    source_hash,
    validate_documents,
)


def md(doc_id: str = "doc-new", category: str = "policies", body: str = "## 小节\n正文内容。\n") -> bytes:
    return (
        f"---\ndocument_id: {doc_id}\ndocument_name: 新文档\ncategory: {category}\n"
        f'policy_version: "2026.09"\n---\n\n# 标题\n\n{body}'
    ).encode()


def rejected(files: list[UploadedFile]) -> list[tuple[str, str]]:
    with pytest.raises(UploadRejected) as exc:
        validate_documents(files)
    return [(e.file, e.check) for e in exc.value.errors]


def test_valid_file_builds_path_from_category_and_hashes_content() -> None:
    [doc] = validate_documents([UploadedFile("policy-new.md", md())])
    assert doc.path == "policies/policy-new.md"
    assert doc.document_id == "doc-new"
    assert len(doc.content_hash) == 64
    # 来源哈希与上传顺序无关
    a = validate_documents([UploadedFile("a.md", md("doc-a")), UploadedFile("b.md", md("doc-b"))])
    b = validate_documents([UploadedFile("b.md", md("doc-b")), UploadedFile("a.md", md("doc-a"))])
    assert source_hash(a) == source_hash(b)


def test_bom_and_crlf_are_normalised() -> None:
    [doc] = validate_documents([UploadedFile("x.md", b"\xef\xbb\xbf" + md().replace(b"\n", b"\r\n"))])
    assert doc.content.startswith("---\n") and "\r" not in doc.content


@pytest.mark.parametrize("name", ["notes.txt", "policy.MD", "policy.md.exe", "readme"])
def test_rejects_non_markdown(name: str) -> None:
    assert rejected([UploadedFile(name, md())]) == [(name, "extension")]


@pytest.mark.parametrize(
    "name",
    ["../evil.md", "..\\evil.md", "sub/evil.md", "/etc/evil.md", "C:\\evil.md", "c:evil.md", "..md", "a/../b.md"],
)
def test_rejects_path_traversal(name: str) -> None:
    assert rejected([UploadedFile(name, md())]) == [(name, "path")]


def test_rejects_hidden_or_odd_filenames() -> None:
    assert check_filename(".hidden.md") is not None
    assert check_filename("空格 文件.md") is not None
    assert check_filename("") is not None


def test_rejects_oversize_file() -> None:
    big = md(body="## 小节\n" + "字" * (MAX_FILE_BYTES // 3 + 10))
    assert len(big) > MAX_FILE_BYTES
    assert rejected([UploadedFile("big.md", big)]) == [("big.md", "size")]


def test_rejects_empty_file() -> None:
    assert rejected([UploadedFile("empty.md", b"")]) == [("empty.md", "size")]


def test_rejects_non_utf8() -> None:
    gbk = md().decode().encode("gbk")
    assert rejected([UploadedFile("gbk.md", gbk)]) == [("gbk.md", "encoding")]


def test_rejects_missing_front_matter() -> None:
    assert rejected([UploadedFile("plain.md", "# 标题\n\n## 节\n正文".encode())]) == [("plain.md", "front_matter")]


def test_rejects_front_matter_missing_required_field() -> None:
    raw = b'---\ndocument_id: doc-x\ndocument_name: X\npolicy_version: "1"\n---\n\n## s\nbody\n'
    with pytest.raises(UploadRejected) as exc:
        validate_documents([UploadedFile("x.md", raw)])
    [err] = exc.value.errors
    assert (err.file, err.check) == ("x.md", "front_matter")
    assert "category" in err.message


@pytest.mark.parametrize("doc_id,category", [("Doc_Upper", "policies"), ("doc-x", "../etc"), ("doc x", "faq")])
def test_rejects_unsafe_front_matter_values(doc_id: str, category: str) -> None:
    assert rejected([UploadedFile("x.md", md(doc_id, category))]) == [("x.md", "front_matter")]


def test_rejects_empty_body() -> None:
    only_front_matter = b'---\ndocument_id: doc-x\ndocument_name: X\ncategory: faq\npolicy_version: "1"\n---\n\n  \n'
    assert rejected([UploadedFile("x.md", only_front_matter)]) == [("x.md", "content")]


def test_rejects_too_many_files() -> None:
    files = [UploadedFile(f"f{i}.md", md(f"doc-{i}")) for i in range(MAX_FILES + 1)]
    assert ("*", "count") in rejected(files)


def test_rejects_duplicate_document_id_and_filename() -> None:
    errors = rejected(
        [UploadedFile("a.md", md("doc-same")), UploadedFile("b.md", md("doc-same")), UploadedFile("A.md", md("doc-z"))]
    )
    assert ("b.md", "duplicate") in errors
    assert ("A.md", "duplicate") in errors


def test_reports_every_bad_file_not_just_the_first() -> None:
    errors = rejected(
        [UploadedFile("ok.md", md()), UploadedFile("bad.txt", md("doc-b")), UploadedFile("../x.md", md("doc-c"))]
    )
    assert errors == [("bad.txt", "extension"), ("../x.md", "path")]
