import unicodedata

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _call(entries):
    return client.post("/api/v1/preflight", json={"entries": entries})


def _repair(entries, repair=True):
    return client.post(
        "/api/v1/preflight", json={"entries": entries, "repair": repair}
    )


def _plan(resp):
    return {e["index"]: e for e in resp["repair_plan"]["entries"]}


def _by_index(resp):
    return {e["index"]: e for e in resp["entries"]}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "archive-path-preflight"}


def test_windows_normalization_and_collision_group():
    # Å (U+00C5) 的 NFD = A(U+0041) + 组合环(U+030A)；NFC 后为 Å，casefold 为 å。
    nfd_ring = unicodedata.normalize("NFD", "Å")
    entries = [
        {"path": "Docs\\ReadMe.txt", "type": "file"},
        {"path": "docs/readme.TXT", "type": "file"},  # 大小写折叠后同键
        {"path": f"docs/caf{nfd_ring}.txt", "type": "file"},  # NFC 归一（信息提示）
        {"path": "docs/readme.txt.  ", "type": "file"},  # 段尾点空格裁剪后同键
        {"path": "docs/CAFÅ.TXT", "type": "file"},  # 与 #3 经 NFC+折叠后碰撞
        {"path": "docs/other.md", "type": "file"},
    ]
    resp = _call(entries).json()
    by = _by_index(resp)

    assert by[1]["canonical_path"] == "Docs/ReadMe.txt"
    assert by[1]["comparison_key"] == "docs/readme.txt"
    assert by[3]["canonical_path"] == "docs/cafÅ.txt"
    assert by[4]["canonical_path"] == "docs/readme.txt"
    # 组 1：#1/#2/#4
    for i in (1, 2, 4):
        assert by[i]["safe"] is True
        assert by[i]["collision_group"] == 1
        assert "COLLISION" in [iss["code"] for iss in by[i]["issues"]]
    # 组 2：#3/#5（NFC + 大小写折叠）
    assert by[3]["collision_group"] == 2
    assert by[5]["collision_group"] == 2
    assert "UNICODE_NORMALIZED" in [i["code"] for i in by[3]["issues"]]
    assert "SEGMENT_TRIMMED" in [i["code"] for i in by[4]["issues"]]
    assert by[6]["collision_group"] is None
    groups = resp["collision_groups"]
    assert len(groups) == 2
    assert groups[0]["group"] == 1 and len(groups[0]["members"]) == 3
    assert groups[1]["group"] == 2 and len(groups[1]["members"]) == 2
    assert resp["summary"]["collisions"] == 2


def test_hierarchy_conflict_file_ancestor():
    entries = [
        {"path": "a/b", "type": "file"},
        {"path": "a/b/c.txt", "type": "file"},  # 被 #1 的文件阻挡
        {"path": "a/b/sub", "type": "directory"},  # 同样被阻挡
        {"path": "a/b2/c.txt", "type": "file"},  # 不冲突（仅词法前缀）
    ]
    resp = _call(entries).json()
    by = _by_index(resp)
    assert by[2]["blocked_by_file_ancestors"] == [1]
    assert by[3]["blocked_by_file_ancestors"] == [1]
    assert by[1]["blocks_descendants"] == [2, 3]
    assert by[4]["blocked_by_file_ancestors"] == []
    assert resp["summary"]["hierarchy_conflicts"] == 2
    pairs = [
        (c["file_index"], c["descendant_index"])
        for c in resp["hierarchy_conflicts"]
    ]
    assert pairs == [(1, 2), (1, 3)]


def test_unsafe_absolute_drive_and_traversal():
    entries = [
        {"path": "/etc/passwd", "type": "file"},
        {"path": "C:\\Windows\\system32", "type": "directory"},
        {"path": "../../evil.bin", "type": "file"},
        {"path": "safe/../inside.txt", "type": "file"},  # 根内 .. 合法
        {"path": "a/../../escape.txt", "type": "file"},  # 越过根
        {"path": "\\\\server\\share\\x", "type": "file"},  # UNC
    ]
    resp = _call(entries).json()
    by = _by_index(resp)
    assert by[1]["safe"] is False
    assert by[1]["issues"][0]["code"] == "ABSOLUTE_PATH"
    assert by[2]["safe"] is False
    assert by[2]["issues"][0]["code"] == "DRIVE_PATH"
    assert by[3]["safe"] is False
    assert by[4]["safe"] is True
    assert by[4]["canonical_path"] == "inside.txt"
    assert by[5]["safe"] is False
    assert by[6]["safe"] is False
    assert by[6]["issues"][0]["code"] == "UNC_PATH"
    assert resp["summary"]["unsafe"] == 5
    assert resp["summary"]["safe"] == 1


def test_reserved_names_and_invalid_chars():
    entries = [
        {"path": "dir/CON", "type": "file"},
        {"path": "LPT9.txt", "type": "file"},
        {"path": "COM0", "type": "file"},  # COM0 不是文档保留名
        {"path": "bad:name?.txt", "type": "file"},
        {"path": "nul/x", "type": "directory"},  # 中间段保留名也拦截
    ]
    resp = _call(entries).json()
    by = _by_index(resp)
    assert by[1]["safe"] is False
    assert by[2]["safe"] is False
    assert by[3]["safe"] is True
    assert by[4]["safe"] is False
    assert by[5]["safe"] is False
    codes_5 = {i["code"] for i in by[5]["issues"]}
    assert "RESERVED_NAME" in codes_5


def test_order_preserved_and_trimming_issues():
    entries = [
        {"path": "z z/last. .", "type": "file"},
        {"path": ".", "type": "directory"},  # 规范化为空
        {"path": "a//b/", "type": "file"},
    ]
    resp = _call(entries).json()
    assert [e["index"] for e in resp["entries"]] == [1, 2, 3]
    by = _by_index(resp)
    assert by[1]["canonical_path"] == "z z/last"
    codes_1 = {i["code"] for i in by[1]["issues"]}
    assert "SEGMENT_TRIMMED" in codes_1
    assert by[2]["safe"] is False
    assert any(i["code"] == "EMPTY_TARGET" for i in by[2]["issues"])
    assert by[3]["canonical_path"] == "a/b"
    assert any(i["code"] == "EMPTY_SEGMENT" for i in by[3]["issues"])


def test_empty_body_rejected():
    r = _call([])
    assert r.status_code == 422


def test_directory_ancestor_does_not_conflict():
    entries = [
        {"path": "a/b", "type": "directory"},
        {"path": "a/b/c", "type": "file"},
    ]
    resp = _call(entries).json()
    assert resp["summary"]["hierarchy_conflicts"] == 0


# ---------------------------------------------------------------------------
# repair=true 修复计划
# ---------------------------------------------------------------------------


def test_repair_default_and_false_keep_response_compatible():
    entries = [
        {"path": "a.txt", "type": "file"},
        {"path": "A.TXT", "type": "file"},
    ]
    assert "repair_plan" not in _call(entries).json()
    assert "repair_plan" not in _repair(entries, repair=False).json()
    # 显式传入其他未知行为不变：repair 省略时摘要照常给出碰撞。
    assert _call(entries).json()["summary"]["collisions"] == 1


def test_repair_simple_collision_first_wins():
    entries = [
        {"path": "docs/readme.txt", "type": "file"},
        {"path": "Docs/ReadMe.TXT", "type": "file"},
        {"path": "docs/readme.txt.  ", "type": "file"},
        {"path": "docs/other.md", "type": "file"},
    ]
    resp = _repair(entries).json()
    plan = _plan(resp)
    assert plan[1]["repaired_path"] == "docs/readme.txt"
    assert plan[1]["reasons"] == []
    assert plan[2]["repaired_path"] == "Docs/ReadMe (1).TXT"
    assert plan[2]["reasons"] == ["COLLISION"]
    assert plan[3]["repaired_path"] == "docs/readme (2).txt"
    assert plan[3]["reasons"] == ["COLLISION"]
    # 无冲突条目的 repaired_path 等于 canonical_path。
    assert plan[4]["repaired_path"] == "docs/other.md"
    assert plan[4]["skipped"] is False
    rp = resp["repair_plan"]
    assert rp["renamed_count"] == 2
    assert rp["skipped_count"] == 0
    assert rp["collisions_remaining"] == 0
    assert rp["hierarchy_conflicts_remaining"] == 0
    assert rp["collisions_resolved"] == 1
    assert rp["hierarchy_conflicts_resolved"] == 0
    # 原诊断字段仍然保留。
    by = _by_index(resp)
    assert by[1]["collision_group"] == 1


def test_repair_hierarchy_conflict_renames_blocking_file():
    entries = [
        {"path": "a/b", "type": "file"},
        {"path": "a/b/c.txt", "type": "file"},
        {"path": "a/b/sub", "type": "directory"},
        {"path": "a/b2/c.txt", "type": "file"},
    ]
    resp = _repair(entries).json()
    plan = _plan(resp)
    assert plan[1]["repaired_path"] == "a/b (1)"
    assert plan[1]["reasons"] == ["BLOCKS_DESCENDANT"]
    assert plan[2]["repaired_path"] == "a/b/c.txt"
    assert plan[2]["reasons"] == []
    assert plan[3]["repaired_path"] == "a/b/sub"
    assert plan[4]["repaired_path"] == "a/b2/c.txt"
    rp = resp["repair_plan"]
    assert rp["renamed_count"] == 1
    assert rp["hierarchy_conflicts_resolved"] == 2
    assert rp["hierarchy_conflicts_remaining"] == 0


def test_repair_collision_keeper_must_not_block_descendants():
    # 先出现的是阻挡后代的文件，后出现的目录不阻挡 -> 目录胜出保留。
    entries = [
        {"path": "a/b", "type": "file"},
        {"path": "a/b", "type": "directory"},
        {"path": "a/b/c", "type": "file"},
    ]
    resp = _repair(entries).json()
    plan = _plan(resp)
    assert plan[1]["repaired_path"] == "a/b (1)"
    assert plan[1]["reasons"] == ["COLLISION", "BLOCKS_DESCENDANT"]
    assert plan[2]["repaired_path"] == "a/b"
    assert plan[2]["reasons"] == []
    assert plan[3]["repaired_path"] == "a/b/c"
    assert resp["repair_plan"]["collisions_remaining"] == 0
    assert resp["repair_plan"]["hierarchy_conflicts_remaining"] == 0


def test_repair_n_increments_past_name_and_hierarchy_clashes():
    # #1 文件 a 阻挡 #2/#3；#4 文件 a (1) 阻挡 #5。
    # #1 改名时 a (1) 已被保留者 #4 预占（且其为文件祖先），须跳到 a (2)；
    # #4 自身阻挡后代，改名为 "a (1) (1)"。
    entries = [
        {"path": "a", "type": "file"},
        {"path": "a/x", "type": "file"},
        {"path": "a/y", "type": "file"},
        {"path": "a (1)", "type": "file"},
        {"path": "a (1)/z", "type": "file"},
    ]
    resp = _repair(entries).json()
    plan = _plan(resp)
    assert plan[1]["repaired_path"] == "a (2)"
    assert plan[2]["repaired_path"] == "a/x"
    assert plan[3]["repaired_path"] == "a/y"
    assert plan[4]["repaired_path"] == "a (1) (1)"
    assert plan[5]["repaired_path"] == "a (1)/z"
    rp = resp["repair_plan"]
    assert rp["collisions_remaining"] == 0
    assert rp["hierarchy_conflicts_remaining"] == 0
    assert rp["hierarchy_conflicts_resolved"] == 3
    assert rp["renamed_count"] == 2


def test_repair_candidate_under_future_file_skips_n():
    # 候选 "a (1)" 自身无冲突，但其目录下存在保留者文件 #4，
    # 若文件候选落到 a (1) 会阻挡 #4 -> 跳到 a (2)。
    entries = [
        {"path": "a", "type": "file"},
        {"path": "a/x", "type": "file"},
        {"path": "a/y", "type": "file"},
        {"path": "a (1)/q", "type": "file"},
    ]
    resp = _repair(entries).json()
    plan = _plan(resp)
    assert plan[1]["repaired_path"] == "a (2)"
    assert plan[4]["repaired_path"] == "a (1)/q"
    assert resp["repair_plan"]["hierarchy_conflicts_remaining"] == 0


def test_repair_extension_insertion_boundaries():
    entries = [
        {"path": "Makefile", "type": "file"},  # 无扩展名
        {"path": "makefile", "type": "file"},
        {"path": ".gitignore", "type": "file"},  # 点文件整体为词干
        {"path": ".GITIGNORE", "type": "file"},
        {"path": "tar.tar.gz", "type": "file"},  # 只在最后一个扩展名前插入
        {"path": "TAR.TAR.GZ", "type": "file"},
    ]
    resp = _repair(entries).json()
    plan = _plan(resp)
    assert plan[2]["repaired_path"] == "makefile (1)"
    assert plan[4]["repaired_path"] == ".GITIGNORE (1)"
    assert plan[6]["repaired_path"] == "TAR.TAR (1).GZ"


def test_repair_unsafe_entries_are_skipped():
    entries = [
        {"path": "/etc/passwd", "type": "file"},
        {"path": "CON", "type": "file"},
        {"path": "safe.txt", "type": "file"},
        {"path": "../escape", "type": "file"},
        {"path": "SAFE.TXT", "type": "file"},  # 仅与安全条目碰撞
    ]
    resp = _repair(entries).json()
    plan = _plan(resp)
    for i in (1, 2, 4):
        assert plan[i]["skipped"] is True
        assert plan[i]["repaired_path"] is None
        assert plan[i]["reasons"] == ["UNSAFE"]
    assert plan[3]["repaired_path"] == "safe.txt"
    assert plan[3]["reasons"] == []
    assert plan[5]["repaired_path"] == "SAFE (1).TXT"
    assert plan[5]["reasons"] == ["COLLISION"]
    rp = resp["repair_plan"]
    assert rp["skipped_count"] == 3
    assert rp["renamed_count"] == 1
    assert rp["collisions_remaining"] == 0
    assert rp["hierarchy_conflicts_remaining"] == 0


def test_repair_plan_order_and_no_conflict_passthrough():
    entries = [
        {"path": "z/last.txt", "type": "file"},
        {"path": "a/dir", "type": "directory"},
        {"path": "a/dir/child.bin", "type": "file"},
    ]
    resp = _repair(entries).json()
    plan_entries = resp["repair_plan"]["entries"]
    assert [e["index"] for e in plan_entries] == [1, 2, 3]
    assert [e["repaired_path"] for e in plan_entries] == [
        "z/last.txt",
        "a/dir",
        "a/dir/child.bin",
    ]
    assert all(e["reasons"] == [] for e in plan_entries)
    assert resp["repair_plan"]["renamed_count"] == 0


def test_repair_nfc_and_trim_collision_uses_canonical_display():
    nfd_ring = unicodedata.normalize("NFD", "Å")
    entries = [
        {"path": f"docs/caf{nfd_ring}.txt", "type": "file"},
        {"path": "docs/CAFÅ.TXT", "type": "file"},
    ]
    resp = _repair(entries).json()
    plan = _plan(resp)
    assert plan[1]["repaired_path"] == "docs/cafÅ.txt"
    assert plan[2]["repaired_path"] == "docs/CAFÅ (1).TXT"


def test_repair_all_collision_members_block_descendants():
    # 两个同键文件都阻挡各自后代 -> 无保留者，全部改名且互不碰撞。
    entries = [
        {"path": "a/b", "type": "file"},
        {"path": "a/b", "type": "file"},
        {"path": "a/b/c", "type": "file"},
        {"path": "a/b/d", "type": "file"},
    ]
    resp = _repair(entries).json()
    plan = _plan(resp)
    assert plan[1]["repaired_path"] == "a/b (1)"
    assert plan[2]["repaired_path"] == "a/b (2)"
    assert plan[3]["repaired_path"] == "a/b/c"
    assert plan[4]["repaired_path"] == "a/b/d"
    rp = resp["repair_plan"]
    assert rp["collisions_remaining"] == 0
    assert rp["hierarchy_conflicts_remaining"] == 0


def test_repair_directory_ancestor_is_not_a_clash():
    # 回归：候选位于已占用目录之下（段前缀但非文件祖先）合法，n 应从 1 开始。
    # #1 文件 p 与 #2 目录 p 碰撞 -> 目录胜出，文件改名 p (1)；
    # #1 同时阻挡 #3，故带两个原因。
    entries = [
        {"path": "p", "type": "file"},
        {"path": "p", "type": "directory"},
        {"path": "p/q", "type": "file"},
    ]
    resp = _repair(entries).json()
    plan = _plan(resp)
    assert plan[1]["repaired_path"] == "p (1)"
    assert plan[1]["reasons"] == ["COLLISION", "BLOCKS_DESCENDANT"]
    assert plan[2]["repaired_path"] == "p"
    assert plan[3]["repaired_path"] == "p/q"
    assert resp["repair_plan"]["hierarchy_conflicts_remaining"] == 0


def test_repair_nested_sibling_prefix_not_ancestor():
    # 回归：'p' 与 'p2' 仅共享首字符而非路径段前缀；目录 p 存在时，
    # 文件 p2/x 完全合法，无冲突条目不改名。
    entries = [
        {"path": "p", "type": "directory"},
        {"path": "p2/x", "type": "file"},
        {"path": "other", "type": "file"},
    ]
    resp = _repair(entries).json()
    plan = _plan(resp)
    assert plan[1]["repaired_path"] == "p"
    assert plan[2]["repaired_path"] == "p2/x"
    assert plan[3]["repaired_path"] == "other"
    assert resp["repair_plan"]["renamed_count"] == 0
    assert resp["repair_plan"]["hierarchy_conflicts_remaining"] == 0


def test_clash_prefix_predicate_unit():
    # 直接覆盖 _has_hierarchy_clash 的四个方向，防止段前缀判定回归。
    from app.repair import _Claim, _has_hierarchy_clash

    file_claim = _Claim(("p",), is_file=True)
    dir_claim = _Claim(("p",), is_file=False)

    # 候选位于已占用路径之下：文件祖先阻挡，目录祖先不阻挡。
    assert _has_hierarchy_clash(("p", "x"), True, file_claim) is True
    assert _has_hierarchy_clash(("p", "x"), False, file_claim) is True
    assert _has_hierarchy_clash(("p", "x"), True, dir_claim) is False
    # 仅首字符相同的兄弟段不算祖先。
    assert _has_hierarchy_clash(("p2", "x"), True, dir_claim) is False
    assert _has_hierarchy_clash(("p2", "x"), True, file_claim) is False
    # 候选文件是已占用路径的祖先 -> 冲突；候选目录则不冲突。
    assert _has_hierarchy_clash(("p",), True, _Claim(("p", "x"), False)) is True
    assert _has_hierarchy_clash(("p",), False, _Claim(("p", "x"), False)) is False
    # 同名即碰撞，与类型无关。
    assert _has_hierarchy_clash(("p",), False, dir_claim) is True


