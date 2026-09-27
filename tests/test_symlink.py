"""符号链接风险分析测试：目标越界、链接链循环、路径劫持与 repair 跳过。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _call(entries, **kw):
    return client.post("/api/v1/preflight", json={"entries": entries, **kw})


def _by_index(resp):
    return {e["index"]: e for e in resp["entries"]}


def _links(resp):
    return {link["index"]: link for link in resp["symlink_analysis"]["links"]}


def _plan(resp):
    return {e["index"]: e for e in resp["repair_plan"]["entries"]}


# ---------------------------------------------------------------------------
# 目标解析与越界
# ---------------------------------------------------------------------------


def test_symlink_target_escape_relative_to_link_dir():
    entries = [
        # 链接所在目录为 a/b：三级 .. 越过根 -> 越界
        {"path": "a/b/lnk1", "type": "symlink", "link_target": "../../../etc/passwd"},
        # 两级 .. 恰好回到根内 -> 合法
        {"path": "a/b/lnk2", "type": "symlink", "link_target": "../../etc/passwd"},
        {"path": "lnk3", "type": "symlink", "link_target": "/etc/passwd"},  # 绝对路径
        {"path": "lnk4", "type": "symlink", "link_target": "C:/Windows"},  # 盘符
        {"path": "lnk5", "type": "symlink", "link_target": "\\\\server\\share"},  # UNC
        {"path": "lnk6", "type": "symlink", "link_target": "../out.txt"},  # 顶层 .. 越界
        {"path": "lnk7", "type": "symlink", "link_target": "docs/ok.txt"},  # 安全
    ]
    resp = _call(entries).json()
    links = _links(resp)
    by = _by_index(resp)

    for i in (1, 3, 4, 5, 6):
        assert links[i]["risk_codes"] == ["LINK_TARGET_ESCAPE"]
        assert links[i]["resolved_target"] is None
        assert links[i]["final_path"] is None
        assert by[i]["safe"] is False
        assert "LINK_TARGET_ESCAPE" in [iss["code"] for iss in by[i]["issues"]]

    # 根内 .. 合法：相对 a/b 解析为 etc/passwd
    assert links[2]["risk_codes"] == []
    assert links[2]["resolved_target"] == "etc/passwd"
    assert links[2]["final_path"] == "etc/passwd"
    assert by[2]["safe"] is True

    assert links[7]["risk_codes"] == []
    assert links[7]["resolved_target"] == "docs/ok.txt"
    # 解析链：自身 + 终点（非链接，index 为 null）
    assert [s["index"] for s in links[7]["chain"]] == [7, None]
    assert links[7]["chain"][0]["path"] == "lnk7"
    assert links[7]["chain"][-1]["path"] == "docs/ok.txt"
    assert by[7]["link_target"] == "docs/ok.txt"
    assert by[7]["type"] == "symlink"
    assert resp["summary"]["symlinks"] == 7


def test_symlink_target_windows_normalization_applies():
    # 目标同样按 Windows 规则规范化：\ 视为分隔符、段尾点空格裁剪、NFC。
    entries = [
        {"path": "dir/lnk", "type": "symlink", "link_target": "sub\\..\\file.txt "},
    ]
    resp = _call(entries).json()
    link = _links(resp)[1]
    # sub/.. 抵消，段尾空格裁剪 -> dir/file.txt
    assert link["resolved_target"] == "dir/file.txt"
    assert link["risk_codes"] == []


def test_symlink_invalid_target():
    entries = [
        {"path": "lnk1", "type": "symlink", "link_target": "CON"},  # 保留名
        {"path": "lnk2", "type": "symlink", "link_target": "bad:name"},  # 非法字符
        {"path": "lnk3", "type": "symlink", "link_target": ""},  # 空目标
        {"path": "lnk4", "type": "symlink", "link_target": "..."},  # 裁剪后为空段
        {"path": "dir/lnk5", "type": "symlink", "link_target": "ok.txt"},
    ]
    resp = _call(entries).json()
    links = _links(resp)
    by = _by_index(resp)
    for i in (1, 2, 3, 4):
        assert links[i]["risk_codes"] == ["LINK_TARGET_INVALID"]
        assert by[i]["safe"] is False
    assert links[5]["risk_codes"] == []
    assert links[5]["resolved_target"] == "dir/ok.txt"


def test_symlink_target_resolving_to_root_is_valid():
    # 非空目标解析到目标根本身合法（指向根目录），与链接所在目录深度无关。
    entries = [
        {"path": "lnk1", "type": "symlink", "link_target": "."},
        {"path": "a/lnk2", "type": "symlink", "link_target": ".."},
    ]
    resp = _call(entries).json()
    links = _links(resp)
    for i in (1, 2):
        assert links[i]["risk_codes"] == []
        assert links[i]["resolved_target"] == ""
        assert links[i]["final_path"] == ""
        assert links[i]["safe"] is True


def test_symlink_validation():
    # symlink 缺少 link_target -> 422
    assert _call([{"path": "a", "type": "symlink"}]).status_code == 422
    # file/directory 携带 link_target -> 422
    assert (
        _call([{"path": "a", "type": "file", "link_target": "x"}]).status_code
        == 422
    )
    assert (
        _call(
            [{"path": "a", "type": "directory", "link_target": "x"}]
        ).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# 链接链追踪与循环
# ---------------------------------------------------------------------------


def test_symlink_chain_resolution_across_batch():
    entries = [
        {"path": "a/lnk1", "type": "symlink", "link_target": "sub/lnk2"},
        {"path": "a/sub/lnk2", "type": "symlink", "link_target": "../data/file.txt"},
        {"path": "a/data/file.txt", "type": "file"},
    ]
    resp = _call(entries).json()
    links = _links(resp)

    # lnk1 -> a/sub/lnk2（符号链接）-> a/data/file.txt
    assert links[1]["resolved_target"] == "a/sub/lnk2"
    assert links[1]["final_path"] == "a/data/file.txt"
    assert [s["index"] for s in links[1]["chain"]] == [1, 2, None]
    assert [s["path"] for s in links[1]["chain"]] == [
        "a/lnk1",
        "a/sub/lnk2",
        "a/data/file.txt",
    ]
    assert links[1]["risk_codes"] == []
    assert links[1]["safe"] is True

    assert links[2]["resolved_target"] == "a/data/file.txt"
    assert links[2]["final_path"] == "a/data/file.txt"
    assert [s["index"] for s in links[2]["chain"]] == [2, None]
    assert resp["summary"]["symlink_cycles"] == 0


def test_symlink_chain_matching_is_casefolded():
    entries = [
        {"path": "lnk", "type": "symlink", "link_target": "docs"},
        {"path": "Docs", "type": "symlink", "link_target": "final.txt"},
        {"path": "final.txt", "type": "file"},
    ]
    links = _links(_call(entries).json())
    assert [s["index"] for s in links[1]["chain"]] == [1, 2, None]
    assert links[1]["final_path"] == "final.txt"


def test_symlink_chain_through_intermediate_ancestor():
    # 目标路径的中间祖先段是符号链接时也要跟随。
    entries = [
        {"path": "a", "type": "symlink", "link_target": "real"},
        {"path": "lnk", "type": "symlink", "link_target": "a/deep/x.txt"},
        {"path": "real/deep/x.txt", "type": "file"},
    ]
    links = _links(_call(entries).json())
    assert links[2]["resolved_target"] == "a/deep/x.txt"
    assert links[2]["final_path"] == "real/deep/x.txt"
    assert [s["index"] for s in links[2]["chain"]] == [2, 1, None]


def test_symlink_cycle_detection():
    entries = [
        {"path": "x", "type": "symlink", "link_target": "y"},
        {"path": "y", "type": "symlink", "link_target": "x"},
        {"path": "entry", "type": "symlink", "link_target": "x"},  # 链入循环
        {"path": "self", "type": "symlink", "link_target": "self"},  # 自循环
        {"path": "ok", "type": "symlink", "link_target": "z.txt"},
    ]
    resp = _call(entries).json()
    links = _links(resp)
    by = _by_index(resp)

    for i in (1, 2, 3, 4):
        assert links[i]["risk_codes"] == ["LINK_CYCLE"]
        assert links[i]["final_path"] is None
        assert by[i]["safe"] is False
        assert "LINK_CYCLE" in [iss["code"] for iss in by[i]["issues"]]
    assert links[5]["risk_codes"] == []

    # 循环组去重：x<->y 只报告一次（从最小下标开始），自循环单独一组。
    assert resp["symlink_analysis"]["cycles"] == [
        {"members": [1, 2]},
        {"members": [4]},
    ]
    assert resp["summary"]["symlink_cycles"] == 2


def test_symlink_transitive_escape_along_chain():
    entries = [
        {"path": "good", "type": "symlink", "link_target": "mid"},
        {"path": "mid", "type": "symlink", "link_target": "../../evil"},
    ]
    resp = _call(entries).json()
    links = _links(resp)
    assert links[2]["risk_codes"] == ["LINK_TARGET_ESCAPE"]
    # 越界沿链传播：good 自身目标在根内，但解析到底会越界。
    assert links[1]["risk_codes"] == ["LINK_TARGET_ESCAPE"]
    assert links[1]["resolved_target"] == "mid"
    assert links[1]["final_path"] is None
    assert [s["index"] for s in links[1]["chain"]] == [1, 2]


# ---------------------------------------------------------------------------
# 路径劫持
# ---------------------------------------------------------------------------


def test_symlink_ancestor_hijack_file_and_directory():
    entries = [
        {"path": "a", "type": "symlink", "link_target": "elsewhere"},
        {"path": "a/x.txt", "type": "file"},
        {"path": "a/sub", "type": "directory"},
        {"path": "a/sub/deep.txt", "type": "file"},  # 祖先 a 被占据（间接也算）
        {"path": "b/y.txt", "type": "file"},  # 不受影响
    ]
    resp = _call(entries).json()
    by = _by_index(resp)
    for i in (2, 3, 4):
        assert by[i]["safe"] is False
        issues = [iss for iss in by[i]["issues"] if iss["code"] == "SYMLINK_ANCESTOR"]
        assert len(issues) == 1
        assert issues[0]["related_index"] == 1
    assert by[5]["safe"] is True

    assert resp["symlink_analysis"]["hijacked_entries"] == [
        {"entry_index": 2, "canonical_path": "a/x.txt", "symlink_ancestors": [1]},
        {"entry_index": 3, "canonical_path": "a/sub", "symlink_ancestors": [1]},
        {
            "entry_index": 4,
            "canonical_path": "a/sub/deep.txt",
            "symlink_ancestors": [1],
        },
    ]
    assert resp["summary"]["symlink_hijacked"] == 3
    # 劫持判定与链接自身是否安全无关（目标在根内同样报告）。
    assert _links(resp)[1]["risk_codes"] == []


def test_symlink_hijack_casefolded_and_multiple_ancestors():
    entries = [
        {"path": "Dir", "type": "symlink", "link_target": "t1"},
        {"path": "dir/sub", "type": "symlink", "link_target": "t2"},  # 大小写折叠同键
        {"path": "DIR/SUB/f.txt", "type": "file"},  # 两级链接祖先
    ]
    resp = _call(entries).json()
    by = _by_index(resp)
    issues = [i for i in by[3]["issues"] if i["code"] == "SYMLINK_ANCESTOR"]
    assert [i["related_index"] for i in issues] == [1, 2]
    hij = resp["symlink_analysis"]["hijacked_entries"]
    assert hij == [
        {
            "entry_index": 3,
            "canonical_path": "DIR/SUB/f.txt",
            "symlink_ancestors": [1, 2],
        }
    ]


def test_hijacked_entries_excluded_from_collisions_and_hierarchy():
    entries = [
        {"path": "a", "type": "symlink", "link_target": "t"},
        {"path": "a/x.txt", "type": "file"},
        {"path": "A/X.TXT", "type": "file"},  # 与 #2 本可碰撞，但都被劫持
        {"path": "a/x.txt/y", "type": "file"},  # 本可被文件祖先阻挡，但已被劫持
    ]
    resp = _call(entries).json()
    assert resp["collision_groups"] == []
    assert resp["hierarchy_conflicts"] == []
    assert resp["summary"]["collisions"] == 0
    assert resp["summary"]["hierarchy_conflicts"] == 0
    assert resp["summary"]["unsafe"] == 3


def test_symlink_not_hijacked_and_excluded_from_collision_groups():
    entries = [
        {"path": "same", "type": "symlink", "link_target": "t"},
        {"path": "SAME", "type": "file"},  # 与链接同键：符号链接不参与碰撞分组
        {"path": "same/child", "type": "symlink", "link_target": "u"},  # 链接不被劫持
    ]
    resp = _call(entries).json()
    by = _by_index(resp)
    assert resp["collision_groups"] == []
    assert by[1]["collision_group"] is None
    assert by[2]["collision_group"] is None
    assert resp["symlink_analysis"]["hijacked_entries"] == []
    # 链接自身位于另一链接之下时不报 SYMLINK_ANCESTOR（仅文件/目录报告劫持）。
    assert "SYMLINK_ANCESTOR" not in [i["code"] for i in by[3]["issues"]]


def test_symlink_under_file_ancestor_is_hierarchy_conflict():
    entries = [
        {"path": "a", "type": "file"},
        {"path": "a/b", "type": "symlink", "link_target": "t"},
    ]
    resp = _call(entries).json()
    by = _by_index(resp)
    assert by[2]["blocked_by_file_ancestors"] == [1]
    assert by[1]["blocks_descendants"] == [2]
    assert resp["summary"]["hierarchy_conflicts"] == 1


# ---------------------------------------------------------------------------
# repair=true：符号链接与被劫持条目跳过
# ---------------------------------------------------------------------------


def test_repair_skips_symlinks_and_hijacked_entries():
    entries = [
        {"path": "a", "type": "symlink", "link_target": "elsewhere"},
        {"path": "a/x.txt", "type": "file"},  # 被劫持 -> skipped
        {"path": "b", "type": "symlink", "link_target": "../esc"},  # 越界链接
        {"path": "doc.txt", "type": "file"},
        {"path": "DOC.TXT", "type": "file"},  # 与 #4 碰撞 -> 改名
    ]
    resp = _call(entries, repair=True).json()
    plan = _plan(resp)

    for i in (1, 3):
        assert plan[i]["skipped"] is True
        assert plan[i]["repaired_path"] is None
        assert plan[i]["reasons"] == ["SYMLINK"]
    assert plan[2]["skipped"] is True
    assert plan[2]["repaired_path"] is None
    assert plan[2]["reasons"] == ["UNSAFE"]

    assert plan[4]["repaired_path"] == "doc.txt"
    assert plan[4]["skipped"] is False
    assert plan[5]["repaired_path"] == "DOC (1).TXT"
    assert plan[5]["reasons"] == ["COLLISION"]

    rp = resp["repair_plan"]
    assert rp["skipped_count"] == 3
    assert rp["renamed_count"] == 1
    assert rp["collisions_remaining"] == 0
    assert rp["hierarchy_conflicts_remaining"] == 0


def test_repair_symlink_does_not_block_renames_or_recheck():
    # 链接与文件同键：链接不参与碰撞分组，文件不改名；链接恒 skipped。
    entries = [
        {"path": "same", "type": "symlink", "link_target": "t"},
        {"path": "SAME", "type": "file"},
        {"path": "a", "type": "file"},
        {"path": "a/b", "type": "symlink", "link_target": "u"},  # 文件祖先阻挡链接
    ]
    resp = _call(entries, repair=True).json()
    plan = _plan(resp)
    assert plan[1]["skipped"] is True
    assert plan[1]["reasons"] == ["SYMLINK"]
    assert plan[2]["repaired_path"] == "SAME"
    assert plan[2]["reasons"] == []
    assert plan[3]["repaired_path"] == "a (1)"  # 阻挡链接后代，照常改名
    assert plan[3]["reasons"] == ["BLOCKS_DESCENDANT"]
    assert plan[4]["skipped"] is True
    rp = resp["repair_plan"]
    assert rp["collisions_remaining"] == 0
    assert rp["hierarchy_conflicts_remaining"] == 0
    assert rp["skipped_count"] == 2


# ---------------------------------------------------------------------------
# 响应结构与兼容性
# ---------------------------------------------------------------------------


def test_symlink_analysis_links_follow_input_order():
    entries = [
        {"path": "f1.txt", "type": "file"},
        {"path": "l1", "type": "symlink", "link_target": "f1.txt"},
        {"path": "d", "type": "directory"},
        {"path": "l2", "type": "symlink", "link_target": "l1"},
    ]
    resp = _call(entries).json()
    assert [link["index"] for link in resp["symlink_analysis"]["links"]] == [2, 4]
    links = _links(resp)
    assert links[4]["final_path"] == "f1.txt"
    assert [s["index"] for s in links[4]["chain"]] == [4, 2, None]


def test_no_symlinks_returns_empty_analysis_and_stays_compatible():
    resp = _call([{"path": "a.txt", "type": "file"}]).json()
    assert resp["symlink_analysis"] == {
        "links": [],
        "cycles": [],
        "hijacked_entries": [],
    }
    assert resp["summary"]["symlinks"] == 0
    assert resp["summary"]["symlink_cycles"] == 0
    assert resp["summary"]["symlink_hijacked"] == 0
    assert "repair_plan" not in resp
