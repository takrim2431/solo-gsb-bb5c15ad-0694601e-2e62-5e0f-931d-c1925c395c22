"""relocation-preview：路径重定位预演测试（规则匹配、结果冲突与 422 校验）。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _post(payload):
    return client.post("/api/v1/relocation-preview", json=payload)


def _relocate(entries, rules=None):
    payload = {"entries": entries}
    if rules is not None:
        payload["rules"] = rules
    r = _post(payload)
    assert r.status_code == 200, r.text
    return r.json()


def _status(entries, rules=None):
    payload = {"entries": entries}
    if rules is not None:
        payload["rules"] = rules
    return _post(payload).status_code


# ---------------------------------------------------------------------------
# 基本重定位与规则匹配
# ---------------------------------------------------------------------------


def test_basic_relocation_rewrites_matched_prefix():
    body = _relocate(
        entries=[
            {"path": "old/docs/a.txt", "type": "file"},
            {"path": "old/docs", "type": "directory"},
            {"path": "keep/b.txt", "type": "file"},
        ],
        rules=[{"source_prefix": "old/docs", "target_prefix": "new/home"}],
    )
    assert [(e["index"], e["new_path"], e["matched_rule_index"]) for e in body["entries"]] == [
        (1, "new/home/a.txt", 0),
        (2, "new/home", 0),  # 路径恰好等于源前缀 → 新路径即目标前缀
        (3, "keep/b.txt", None),  # 未命中 → 保留原（规范化）路径
    ]
    assert all(e["usable"] for e in body["entries"])
    assert all(e["conflicts"] == [] for e in body["entries"])
    assert body["summary"] == {
        "total": 3,
        "usable": 3,
        "conflicted": 0,
        "duplicate_key_groups": 0,
        "duplicate_key_entries": 0,
        "file_ancestor_conflicts": 0,
    }


def test_longest_source_prefix_wins_regardless_of_rule_order():
    rules = [
        {"source_prefix": "a", "target_prefix": "x"},
        {"source_prefix": "a/b", "target_prefix": "y"},
    ]
    body = _relocate(
        entries=[{"path": "a/b/c.txt", "type": "file"}], rules=rules
    )
    assert body["entries"][0]["new_path"] == "y/c.txt"
    assert body["entries"][0]["matched_rule_index"] == 1
    # 最长优先与规则先后顺序无关：交换顺序结果不变。
    body = _relocate(
        entries=[{"path": "a/b/c.txt", "type": "file"}],
        rules=list(reversed(rules)),
    )
    assert body["entries"][0]["new_path"] == "y/c.txt"
    assert body["entries"][0]["matched_rule_index"] == 0


def test_same_length_prefix_last_rule_wins():
    body = _relocate(
        entries=[{"path": "a/f.txt", "type": "file"}],
        rules=[
            {"source_prefix": "a", "target_prefix": "x"},
            {"source_prefix": "a", "target_prefix": "y"},
        ],
    )
    assert body["entries"][0]["new_path"] == "y/f.txt"
    assert body["entries"][0]["matched_rule_index"] == 1


def test_prefix_matching_is_segment_wise_not_string_wise():
    # 源前缀 "ab" 不应命中路径段 "abc"。
    body = _relocate(
        entries=[{"path": "abc/f.txt", "type": "file"}],
        rules=[{"source_prefix": "ab", "target_prefix": "x"}],
    )
    assert body["entries"][0]["new_path"] == "abc/f.txt"
    assert body["entries"][0]["matched_rule_index"] is None


def test_rules_optional_and_empty_rules_keep_original_paths():
    entries = [{"path": "a/b.txt", "type": "file"}]
    for payload_rules in (None, []):
        body = _relocate(entries, rules=payload_rules)
        entry = body["entries"][0]
        assert entry["new_path"] == "a/b.txt"
        assert entry["matched_rule_index"] is None
        assert entry["usable"] is True


def test_windows_normalization_reused_for_entries_and_rules():
    # 条目与规则前缀都先做 Windows 规范化：分隔符统一、段尾点/空格裁剪、
    # 比较键大小写折叠；匹配基于规范化后的路径段。
    body = _relocate(
        entries=[
            {"path": "Src\\Sub\\ReadMe.TXT", "type": "file"},
            {"path": "src/sub ./note.txt", "type": "file"},
        ],
        rules=[{"source_prefix": "SRC\\sub", "target_prefix": "Dest\\Home"}],
    )
    first, second = body["entries"]
    assert first["new_path"] == "Dest/Home/ReadMe.TXT"
    assert first["matched_rule_index"] == 0
    # 第二条的 "sub " 段尾空格被裁剪为 "sub"，同样命中规则。
    assert second["new_path"] == "Dest/Home/note.txt"
    assert second["matched_rule_index"] == 0


def test_unmatched_entry_new_path_is_normalized_original():
    body = _relocate(
        entries=[{"path": "Keep\\Dir\\File.TXT", "type": "file"}],
        rules=[{"source_prefix": "other", "target_prefix": "x"}],
    )
    entry = body["entries"][0]
    assert entry["original_path"] == "Keep\\Dir\\File.TXT"
    assert entry["new_path"] == "Keep/Dir/File.TXT"
    assert entry["matched_rule_index"] is None


def test_entries_keep_input_order_and_one_based_index():
    body = _relocate(
        entries=[
            {"path": "c/3.txt", "type": "file"},
            {"path": "a/1.txt", "type": "file"},
            {"path": "b/2.txt", "type": "directory"},
        ]
    )
    assert [e["index"] for e in body["entries"]] == [1, 2, 3]
    assert [e["original_path"] for e in body["entries"]] == [
        "c/3.txt",
        "a/1.txt",
        "b/2.txt",
    ]
    assert [e["type"] for e in body["entries"]] == ["file", "file", "directory"]


# ---------------------------------------------------------------------------
# 结果冲突：同键重复
# ---------------------------------------------------------------------------


def test_duplicate_keys_in_result_marked_unusable():
    # 两条原本不同的路径被重定位到同一键。
    body = _relocate(
        entries=[
            {"path": "old/a.txt", "type": "file"},
            {"path": "new/a.txt", "type": "file"},
            {"path": "ok.txt", "type": "file"},
        ],
        rules=[{"source_prefix": "old", "target_prefix": "new"}],
    )
    first, second, third = body["entries"]
    assert first["new_path"] == second["new_path"] == "new/a.txt"
    assert first["usable"] is False
    assert second["usable"] is False
    assert first["conflicts"] == [
        {"type": "DUPLICATE_KEY", "related_indices": [2]}
    ]
    assert second["conflicts"] == [
        {"type": "DUPLICATE_KEY", "related_indices": [1]}
    ]
    assert third["usable"] is True
    assert body["summary"]["usable"] == 1
    assert body["summary"]["conflicted"] == 2
    assert body["summary"]["duplicate_key_groups"] == 1
    assert body["summary"]["duplicate_key_entries"] == 2


def test_duplicate_keys_case_insensitive_and_cross_type():
    # 大小写变体同键；文件与目录同键也算重复。
    body = _relocate(
        entries=[
            {"path": "Docs/ReadMe.txt", "type": "file"},
            {"path": "docs/readme.TXT", "type": "file"},
            {"path": "data", "type": "directory"},
            {"path": "DATA", "type": "file"},
        ]
    )
    assert [e["usable"] for e in body["entries"]] == [False, False, False, False]
    assert body["entries"][0]["conflicts"] == [
        {"type": "DUPLICATE_KEY", "related_indices": [2]}
    ]
    assert body["entries"][2]["conflicts"] == [
        {"type": "DUPLICATE_KEY", "related_indices": [4]}
    ]
    summary = body["summary"]
    assert summary["duplicate_key_groups"] == 2
    assert summary["duplicate_key_entries"] == 4
    assert summary["conflicted"] == 4


def test_relocation_can_resolve_preexisting_duplicates():
    # 输入本身同键，但一条被重定位走后结果不再冲突。
    body = _relocate(
        entries=[
            {"path": "a.txt", "type": "file"},
            {"path": "old/a.txt", "type": "file"},
        ],
        rules=[{"source_prefix": "old", "target_prefix": "archive"}],
    )
    assert [e["new_path"] for e in body["entries"]] == [
        "a.txt",
        "archive/a.txt",
    ]
    assert all(e["usable"] for e in body["entries"])
    assert body["summary"]["duplicate_key_groups"] == 0


# ---------------------------------------------------------------------------
# 结果冲突：文件占据祖先路径
# ---------------------------------------------------------------------------


def test_file_ancestor_conflict_marks_both_sides():
    # 重定位后文件 new/pkg 占据 new/pkg/inner.dat 的祖先路径。
    body = _relocate(
        entries=[
            {"path": "src/pkg", "type": "file"},
            {"path": "src/pkg/inner.dat", "type": "file"},
            {"path": "unrelated.txt", "type": "file"},
        ],
        rules=[{"source_prefix": "src", "target_prefix": "new"}],
    )
    file_entry, descendant, other = body["entries"]
    assert file_entry["new_path"] == "new/pkg"
    assert descendant["new_path"] == "new/pkg/inner.dat"
    assert file_entry["usable"] is False
    assert file_entry["conflicts"] == [
        {"type": "FILE_ANCESTOR", "related_indices": [2]}
    ]
    assert descendant["usable"] is False
    assert descendant["conflicts"] == [
        {"type": "BLOCKED_BY_FILE_ANCESTOR", "related_indices": [1]}
    ]
    assert other["usable"] is True
    assert body["summary"]["file_ancestor_conflicts"] == 1
    assert body["summary"]["conflicted"] == 2
    assert body["summary"]["usable"] == 1


def test_directory_ancestor_is_not_a_conflict():
    body = _relocate(
        entries=[
            {"path": "a/b", "type": "directory"},
            {"path": "a/b/c.txt", "type": "file"},
        ]
    )
    assert all(e["usable"] for e in body["entries"])
    assert body["summary"]["file_ancestor_conflicts"] == 0


def test_file_ancestor_created_only_by_relocation():
    # 输入布局合法；重定位把文件搬到另一条目的祖先路径上。
    body = _relocate(
        entries=[
            {"path": "lib", "type": "file"},
            {"path": "app/lib/config.json", "type": "file"},
        ],
        rules=[{"source_prefix": "lib", "target_prefix": "app/lib"}],
    )
    file_entry, descendant = body["entries"]
    assert file_entry["new_path"] == "app/lib"
    assert file_entry["conflicts"] == [
        {"type": "FILE_ANCESTOR", "related_indices": [2]}
    ]
    assert descendant["conflicts"] == [
        {"type": "BLOCKED_BY_FILE_ANCESTOR", "related_indices": [1]}
    ]
    assert body["summary"]["file_ancestor_conflicts"] == 1


def test_multiple_ancestors_and_combined_conflicts():
    # 条目同时涉及同键重复与文件祖先两类冲突；相关下标升序列出。
    body = _relocate(
        entries=[
            {"path": "a", "type": "file"},  # 占据 #3、#4 的祖先
            {"path": "a", "type": "file"},  # 与 #1 同键重复，也占据 #3、#4
            {"path": "a/b/x.txt", "type": "file"},
            {"path": "a/b/y.txt", "type": "file"},
        ]
    )
    first, second, third, fourth = body["entries"]
    assert first["conflicts"] == [
        {"type": "DUPLICATE_KEY", "related_indices": [2]},
        {"type": "FILE_ANCESTOR", "related_indices": [3, 4]},
    ]
    assert second["conflicts"] == [
        {"type": "DUPLICATE_KEY", "related_indices": [1]},
        {"type": "FILE_ANCESTOR", "related_indices": [3, 4]},
    ]
    assert third["conflicts"] == [
        {"type": "BLOCKED_BY_FILE_ANCESTOR", "related_indices": [1, 2]}
    ]
    assert fourth["conflicts"] == [
        {"type": "BLOCKED_BY_FILE_ANCESTOR", "related_indices": [1, 2]}
    ]
    summary = body["summary"]
    assert summary["usable"] == 0
    assert summary["conflicted"] == 4
    assert summary["duplicate_key_groups"] == 1
    assert summary["duplicate_key_entries"] == 2
    # 冲突对数：(#1,#3) (#1,#4) (#2,#3) (#2,#4)。
    assert summary["file_ancestor_conflicts"] == 4


# ---------------------------------------------------------------------------
# 422 校验
# ---------------------------------------------------------------------------


def test_unsafe_entry_path_rejected_422():
    assert _status([{"path": "../evil.bin", "type": "file"}]) == 422
    assert _status([{"path": "/abs/path", "type": "file"}]) == 422
    assert _status([{"path": "C:/win.ini", "type": "file"}]) == 422
    assert _status([{"path": "NUL", "type": "file"}]) == 422
    r = _post({"entries": [{"path": "../evil.bin", "type": "file"}]})
    assert r.status_code == 422
    assert "TRAVERSAL_ESCAPE" in r.text


def test_unsafe_rule_prefix_rejected_422():
    entries = [{"path": "a.txt", "type": "file"}]
    # 源前缀不安全
    r = _post(
        {
            "entries": entries,
            "rules": [{"source_prefix": "../escape", "target_prefix": "x"}],
        }
    )
    assert r.status_code == 422
    assert "源前缀" in r.text
    # 目标前缀不安全
    r = _post(
        {
            "entries": entries,
            "rules": [{"source_prefix": "x", "target_prefix": "NUL"}],
        }
    )
    assert r.status_code == 422
    assert "目标前缀" in r.text
    # 空前缀（规范化后为空）同样不安全
    assert _status(entries, [{"source_prefix": "", "target_prefix": "x"}]) == 422
    assert _status(entries, [{"source_prefix": "x", "target_prefix": "."}]) == 422


def test_invalid_entry_type_and_shape_rejected_422():
    assert _status([{"path": "lnk", "type": "symlink"}]) == 422
    assert _status([]) == 422  # 至少 1 条
    assert _status([{"path": "a.txt"}]) == 422  # 缺少 type
    # 规则缺少字段
    assert _status([{"path": "a.txt", "type": "file"}], [{"source_prefix": "a"}]) == 422


# ---------------------------------------------------------------------------
# 既有接口保持不变
# ---------------------------------------------------------------------------


def test_other_endpoints_untouched():
    r = client.post(
        "/api/v1/preflight",
        json={"entries": [{"path": "a.txt", "type": "file"}]},
    )
    assert r.status_code == 200
    assert r.json()["summary"]["total"] == 1
    assert client.get("/health").json()["status"] == "ok"
