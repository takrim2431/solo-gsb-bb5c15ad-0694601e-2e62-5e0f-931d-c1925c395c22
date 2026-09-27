"""selection_rules：glob 匹配单测 + 预检 API 集成测试。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.selection import apply_selection_rules

client = TestClient(app)


def _post(payload):
    return client.post("/api/v1/preflight", json=payload)


def _decide(paths, rules):
    return apply_selection_rules([p.split("/") for p in paths], rules)


# ---------------------------------------------------------------------------
# glob 匹配单测
# ---------------------------------------------------------------------------


def test_glob_star_does_not_cross_slash():
    # 单段模式只匹配根级单段：* 不得跨 /（未命中条目默认 include，
    # 故用 exclude 规则直接观察命中情况）。
    d = _decide(["a/b", "a/x/b", "a.txt", "xa"], [("exclude", "a*")])
    assert [x.selected for x in d] == [True, True, False, True]
    # * 在中间段同样不跨段。
    d = _decide(["a/b/c", "a/x/c", "a/b/d/c"], [("exclude", "a/*/c")])
    assert [x.selected for x in d] == [False, False, True]
    d = _decide(["a/b", "a/b/c"], [("exclude", "a/*")])
    assert [x.selected for x in d] == [False, True]
    # * 可匹配 0 个字符。
    d = _decide(["a", "ax", "a/x"], [("exclude", "a*")])
    assert [x.selected for x in d] == [False, False, True]


def test_glob_question_mark_one_char_within_segment():
    d = _decide(["a1", "a12", "a/b", "a"], [("exclude", "a?")])
    assert [x.selected for x in d] == [False, True, True, True]
    d = _decide(["x/y", "x1y", "x/y1", "xy"], [("exclude", "x?y")])
    assert [x.selected for x in d] == [True, False, True, True]
    # ? 必须恰好匹配一个字符，不匹配 0 个也不多匹配。
    d = _decide(["xy", "xay", "xayz", "xayzw"], [("exclude", "x?y?")])
    assert [x.selected for x in d] == [True, True, False, True]


def test_glob_double_star_crosses_multiple_segments():
    d = _decide(
        ["tmp/a", "tmp/a/b", "a", "tmp", "xtmp/a"],
        [("exclude", "tmp/**")],
    )
    assert [x.selected for x in d] == [False, False, True, True, True]
    # 中间 ** 可匹配 0 段或多级。
    d = _decide(["a/b", "a/x/b", "a/x/y/b", "b"], [("exclude", "a/**/b")])
    assert [x.selected for x in d] == [False, False, False, True]
    # 仅 ** 匹配任意深度（含单段）。
    d = _decide(["a", "a/b", "a/b/c"], [("exclude", "**")])
    assert [x.selected for x in d] == [False, False, False]
    # 前缀段 + ** 不匹配只到该段的路径，也不匹配前缀为词法子串的路径。
    d = _decide(["tmp", "tmp12/x", "otmp/a"], [("exclude", "tmp/**")])
    assert [x.selected for x in d] == [True, True, True]
    # * 与 ** 组合：tmp* 先匹配 tmp12 段，** 再吞后续段。
    d = _decide(["tmp12/x", "tmp12/x/y"], [("exclude", "tmp*/**")])
    assert [x.selected for x in d] == [False, False]
    # ** 不匹配负数级：tmp/**/x 不匹配 tmp 本身。
    d = _decide(["tmp"], [("exclude", "tmp/**")])
    assert d[0].selected is True


def test_glob_case_sensitive():
    d = _decide(["Docs/x", "docs/x"], [("exclude", "Docs/*")])
    assert [x.selected for x in d] == [False, True]
    d = _decide(["Docs", "docs"], [("exclude", "Docs")])
    assert [x.selected for x in d] == [False, True]


def test_glob_matches_normalized_segments():
    # 匹配对象为规范化（NFC、段尾裁剪、分隔符统一）后的路径段。
    d = apply_selection_rules([["a", "b.txt"]], [("exclude", "a/b.txt")])
    assert d[0].selected is False
    assert d[0].matched_rule_index == 0
    # 未规范化的字符串段不会命中。
    d2 = apply_selection_rules([["a", "b.txt.  "]], [("exclude", "a/b.txt")])
    assert d2[0].selected is True
    assert d2[0].matched_rule_index is None


def test_last_matching_rule_wins_and_default_include():
    rules = [("exclude", "a/*"), ("include", "a/keep")]
    d = _decide(["a/keep", "a/drop", "b/x"], rules)
    assert d[0].selected is True and d[0].matched_rule_index == 1
    assert d[1].selected is False and d[1].matched_rule_index == 0
    # 未命中任何规则：默认 include，matched_rule_index 为 None。
    assert d[2].selected is True and d[2].matched_rule_index is None


def test_invalid_patterns_raise():
    from app.selection import validate_pattern

    for bad in ["", "/a", "a/", "a//b", "a/**b", "a/b**/c", "a/b*c**"]:
        with pytest.raises(ValueError):
            validate_pattern(bad)
    for good in ["a", "a/b", "**", "a/**", "**/b", "a/**/b", "*.txt", "a/?/c"]:
        validate_pattern(good)  # 不抛异常


# ---------------------------------------------------------------------------
# API 集成
# ---------------------------------------------------------------------------


def _rules(entries, rules, **extra):
    return _post({"entries": entries, "selection_rules": rules, **extra})


def test_no_selection_rules_keeps_response_compatible():
    entries = [
        {"path": "a.txt", "type": "file"},
        {"path": "a/b", "type": "file"},
    ]
    resp = _post({"entries": entries}).json()
    by = {e["index"]: e for e in resp["entries"]}
    assert all(e["selected"] is True for e in resp["entries"])
    assert all(e["matched_rule_index"] is None for e in resp["entries"])
    assert by[1]["link_target"] is None
    s = resp["summary"]
    assert s["selected"] == 2 and s["excluded"] == 0
    # 显式空列表等价于无规则。
    resp2 = _post({"entries": entries, "selection_rules": []}).json()
    assert all(e["selected"] for e in resp2["entries"])
    assert resp2["summary"]["selected"] == 2


def test_selection_fields_and_summary():
    entries = [
        {"path": "keep/a.txt", "type": "file"},
        {"path": "tmp/cache.bin", "type": "file"},
        {"path": "tmp/sub/x.log", "type": "file"},
        {"path": "keep/b.txt", "type": "file"},
    ]
    rules = [
        {"action": "exclude", "pattern": "tmp/**"},
        {"action": "exclude", "pattern": "*.log"},
    ]
    resp = _rules(entries, rules)
    assert resp.status_code == 200
    data = resp.json()
    by = {e["index"]: e for e in data["entries"]}
    assert by[1]["selected"] is True and by[1]["matched_rule_index"] is None
    assert by[2]["selected"] is False and by[2]["matched_rule_index"] == 0
    assert by[3]["selected"] is False and by[3]["matched_rule_index"] == 0
    assert by[4]["selected"] is True
    # 被排除条目仍返回规范化信息。
    assert by[2]["canonical_path"] == "tmp/cache.bin"
    assert by[2]["comparison_key"] == "tmp/cache.bin"
    s = data["summary"]
    assert s["total"] == 4 and s["selected"] == 2 and s["excluded"] == 2


def test_last_rule_wins_via_api():
    entries = [
        {"path": "docs/readme.txt", "type": "file"},
        {"path": "docs/secret.txt", "type": "file"},
    ]
    rules = [
        {"action": "exclude", "pattern": "docs/*"},
        {"action": "include", "pattern": "docs/readme.txt"},
    ]
    data = _rules(entries, rules).json()
    by = {e["index"]: e for e in data["entries"]}
    assert by[1]["selected"] is True and by[1]["matched_rule_index"] == 1
    assert by[2]["selected"] is False and by[2]["matched_rule_index"] == 0
    assert data["summary"]["selected"] == 1


def test_excluded_entries_skip_collision_and_hierarchy():
    # #1/#2/#3 规范化后同名碰撞（大小写差异）；#4 文件阻挡 #5 后代。
    # 排除 #2-#5 后既无碰撞也无层级冲突，下标保持原值。
    entries = [
        {"path": "keep/readme.txt", "type": "file"},
        {"path": "Docs/ReadMe.TXT", "type": "file"},
        {"path": "docs/readme.txt.  ", "type": "file"},
        {"path": "a/b", "type": "file"},
        {"path": "a/b/c.txt", "type": "file"},
    ]
    rules = [
        {"action": "exclude", "pattern": "Docs/ReadMe.TXT"},
        {"action": "exclude", "pattern": "docs/readme.txt"},
        {"action": "exclude", "pattern": "a/**"},
    ]
    data = _rules(entries, rules).json()
    by = {e["index"]: e for e in data["entries"]}
    assert [e["index"] for e in data["entries"]] == [1, 2, 3, 4, 5]
    assert by[1]["selected"] is True and by[1]["collision_group"] is None
    assert by[2]["selected"] is False and by[2]["matched_rule_index"] == 0
    assert by[2]["collision_group"] is None
    assert by[3]["selected"] is False and by[3]["matched_rule_index"] == 1
    assert by[3]["collision_group"] is None
    assert by[4]["selected"] is False and by[4]["blocks_descendants"] == []
    assert by[5]["selected"] is False and by[5]["blocked_by_file_ancestors"] == []
    # 被排除条目没有任何下游追加的问题。
    assert all(
        iss["code"] not in {"COLLISION", "BLOCKS_DESCENDANT", "BLOCKED_BY_FILE_ANCESTOR"}
        for iss in by[2]["issues"] + by[3]["issues"]
    )
    s = data["summary"]
    assert s["collisions"] == 0 and s["collision_entries"] == 0
    assert s["hierarchy_conflicts"] == 0
    assert data["collision_groups"] == []
    assert data["hierarchy_conflicts"] == []


def test_excluded_entries_still_normalize_and_match_trimmed_path():
    # 模式匹配规范化（段尾裁剪）后的路径。
    entries = [
        {"path": "a/b.txt.  ", "type": "file"},
        {"path": "a\\b.TXT", "type": "file"},  # 反斜杠统一为 /；匹配区分大小写
    ]
    rules = [{"action": "exclude", "pattern": "a/b.txt"}]
    data = _rules(entries, rules).json()
    by = {e["index"]: e for e in data["entries"]}
    assert by[1]["selected"] is False and by[1]["matched_rule_index"] == 0
    assert by[2]["selected"] is True  # b.TXT ≠ b.txt（区分大小写）


def test_excluded_symlink_skipped_from_analysis():
    entries = [
        {"path": "evil/lnk", "type": "symlink", "link_target": "../../etc/passwd"},
        {"path": "good/lnk", "type": "symlink", "link_target": "../x"},
        {"path": "good/x", "type": "file"},
    ]
    rules = [{"action": "exclude", "pattern": "evil/**"}]
    data = _rules(entries, rules).json()
    by = {e["index"]: e for e in data["entries"]}
    assert by[1]["selected"] is False
    # 被排除链接仍回显 link_target 与规范化信息。
    assert by[1]["link_target"] == "../../etc/passwd"
    assert by[1]["canonical_path"] == "evil/lnk"
    # 但不进入 symlink_analysis。
    assert [l["index"] for l in data["symlink_analysis"]["links"]] == [2]
    assert data["summary"]["symlinks"] == 1


def test_excluded_symlink_does_not_hijack_selected_descendant():
    # 排除 #1 链接后，#2 不再被劫持。
    entries = [
        {"path": "a", "type": "symlink", "link_target": "/outside"},
        {"path": "a/child.txt", "type": "file"},
    ]
    data = _rules(
        entries, [{"action": "exclude", "pattern": "a"}]
    ).json()
    by = {e["index"]: e for e in data["entries"]}
    assert by[2]["selected"] is True and by[2]["safe"] is True
    assert data["symlink_analysis"]["hijacked_entries"] == []
    assert data["summary"]["symlink_hijacked"] == 0

    # 对照：不排除时 #2 被劫持。
    data2 = _post({"entries": entries}).json()
    assert data2["summary"]["symlink_hijacked"] == 1


def test_excluded_files_skip_resource_budgets():
    entries = [
        # 被排除文件即便超限也不报错、不计入总量。
        {
            "path": "tmp/huge.bin",
            "type": "file",
            "compressed_bytes": 1,
            "uncompressed_bytes": 5000,
        },
        {
            "path": "ok.bin",
            "type": "file",
            "compressed_bytes": 10,
            "uncompressed_bytes": 100,
        },
    ]
    rules = [{"action": "exclude", "pattern": "tmp/**"}]
    payload = {
        "entries": entries,
        "selection_rules": rules,
        "resource_limits": {
            "max_file_uncompressed_bytes": 1000,
            "max_total_uncompressed_bytes": 1000,
            "max_compression_ratio": 100,
        },
    }
    data = _post(payload).json()
    by = {e["index"]: e for e in data["entries"]}
    assert by[1]["selected"] is False and by[1]["safe"] is True
    assert by[1]["issues"] == []
    s = data["summary"]
    assert s["declared_compressed_bytes"] == 10
    assert s["declared_uncompressed_bytes"] == 100
    assert s["resource_limits_exceeded"] is False


def test_excluded_file_need_not_declare_sizes():
    # 启用限制时，被排除文件无需提供 uncompressed_bytes（不返回 422）。
    entries = [
        {"path": "tmp/x.bin", "type": "file"},
        {"path": "ok.bin", "type": "file", "uncompressed_bytes": 10},
    ]
    payload = {
        "entries": entries,
        "selection_rules": [{"action": "exclude", "pattern": "tmp/**"}],
        "resource_limits": {"max_file_uncompressed_bytes": 100},
    }
    assert _post(payload).status_code == 200

    # 对照：选中文件缺少大小时仍 422。
    payload["selection_rules"] = []
    assert _post(payload).status_code == 422


def test_repair_plan_only_contains_selected_entries_with_original_indices():
    entries = [
        {"path": "docs/readme.txt", "type": "file"},
        {"path": "Docs/ReadMe.TXT", "type": "file"},
        {"path": "tmp/skip.txt", "type": "file"},
        {"path": "docs/other.md", "type": "file"},
    ]
    payload = {
        "entries": entries,
        "repair": True,
        "selection_rules": [{"action": "exclude", "pattern": "tmp/**"}],
    }
    data = _post(payload).json()
    plan = {e["index"]: e for e in data["repair_plan"]["entries"]}
    # 下标保持原请求下标；#3 不在计划中。
    assert sorted(plan) == [1, 2, 4]
    assert plan[1]["repaired_path"] == "docs/readme.txt"
    assert plan[2]["repaired_path"] == "Docs/ReadMe (1).TXT"
    assert plan[4]["repaired_path"] == "docs/other.md"
    # #3 仍在逐条目响应中，带规范化信息。
    by = {e["index"]: e for e in data["entries"]}
    assert by[3]["selected"] is False
    assert by[3]["canonical_path"] == "tmp/skip.txt"
    assert data["repair_plan"]["skipped_count"] == 0
    assert data["repair_plan"]["renamed_count"] == 1


def test_illegal_rules_return_422():
    base = {"entries": [{"path": "a", "type": "file"}]}
    bad_payloads = [
        {"selection_rules": [{"action": "drop", "pattern": "a"}]},
        {"selection_rules": [{"action": "include", "pattern": ""}]},
        {"selection_rules": [{"action": "include", "pattern": "/a"}]},
        {"selection_rules": [{"action": "include", "pattern": "a/"}]},
        {"selection_rules": [{"action": "include", "pattern": "a//b"}]},
        {"selection_rules": [{"action": "include", "pattern": "a/**b"}]},
        {"selection_rules": [{"action": "include", "pattern": "**x"}]},
        {"selection_rules": [{}]},
    ]
    for extra in bad_payloads:
        r = _post({**base, **extra})
        assert r.status_code == 422, extra


def test_selection_with_symlink_cycle_excluded():
    # #1 与 #2 构成循环；排除其中一个后循环消失。
    entries = [
        {"path": "a", "type": "symlink", "link_target": "b"},
        {"path": "b", "type": "symlink", "link_target": "a"},
        {"path": "c", "type": "file"},
    ]
    data = _rules(
        entries, [{"action": "exclude", "pattern": "b"}]
    ).json()
    assert [l["index"] for l in data["symlink_analysis"]["links"]] == [1]
    assert data["symlink_analysis"]["cycles"] == []
    assert data["summary"]["symlink_cycles"] == 0

    data2 = _post({"entries": entries}).json()
    assert len(data2["symlink_analysis"]["cycles"]) == 1
