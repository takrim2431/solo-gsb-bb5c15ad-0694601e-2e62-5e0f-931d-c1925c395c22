"""rollback-preview：前缀解包回退预演测试（备份清单 + 有序回退步骤）。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _post(payload):
    return client.post("/api/v1/rollback-preview", json=payload)


def _preview(existing, incoming, stop_after=None):
    if stop_after is None:
        stop_after = len(incoming)
    return _post(
        {"existing": existing, "incoming": incoming, "stop_after": stop_after}
    )


def _ops(resp):
    return [(s["operation"], s["source"], s["index"], s["canonical_path"]) for s in resp["rollback_steps"]]


# ---------------------------------------------------------------------------
# 基本：新建文件删除、覆盖备份与恢复、新建目录深→浅删除
# ---------------------------------------------------------------------------


def test_full_prefix_create_overwrite_and_directory_teardown():
    resp = _preview(
        existing=[
            {"path": "docs/readme.txt", "type": "file"},
            {"path": "assets/logo.png", "type": "file"},
        ],
        incoming=[
            {"path": "docs/readme.txt", "type": "file"},  # 1 OVERWRITE 现存
            {"path": "new/a/deep.txt", "type": "file"},  # 2 CREATE + 隐含 new/new/a
            {"path": "new/b", "type": "directory"},  # 3 CREATE 目录
            {"path": "new/b/x.txt", "type": "file"},  # 4 CREATE
            {"path": "docs", "type": "file"},  # 5 BLOCKED 类型冲突
            {"path": "../evil", "type": "file"},  # 6 BLOCKED 不安全
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["stop_after"] == 6
    assert [e["index"] for e in body["entries"]] == [1, 2, 3, 4, 5, 6]
    by = {e["index"]: e for e in body["entries"]}
    assert by[1]["status"] == "OVERWRITE"
    assert by[2]["status"] == "CREATE"
    assert by[3]["status"] == "CREATE"
    assert by[4]["status"] == "CREATE"
    assert by[5]["status"] == "BLOCKED"
    assert by[6]["status"] == "BLOCKED"

    # 备份清单：同键一次，指向现存清单下标。
    assert body["backup_files"] == [
        {
            "existing_index": 1,
            "canonical_path": "docs/readme.txt",
            "comparison_key": "docs/readme.txt",
            "first_overwrite_index": 1,
        }
    ]

    orders = [s["order"] for s in body["rollback_steps"]]
    assert orders == list(range(1, len(orders) + 1))
    assert _ops(body) == [
        ("DELETE_CREATED_FILE", "incoming", 4, "new/b/x.txt"),
        ("DELETE_CREATED_FILE", "incoming", 2, "new/a/deep.txt"),
        ("RESTORE_FILE", "existing", 1, "docs/readme.txt"),
        ("REMOVE_DIRECTORY", "implicit", None, "new/a"),
        ("REMOVE_DIRECTORY", "incoming", 3, "new/b"),
        ("REMOVE_DIRECTORY", "implicit", None, "new"),
    ]
    assert body["summary"] == {
        "total": 6,
        "create": 3,
        "overwrite": 1,
        "reuse_directory": 0,
        "blocked": 2,
        "backups": 1,
        "rollback_steps": 6,
        "delete_created_file": 2,
        "restore_file": 1,
        "remove_directory": 3,
    }


def test_empty_prefix_has_no_steps():
    resp = _preview(
        existing=[{"path": "a.txt", "type": "file"}],
        incoming=[{"path": "a.txt", "type": "file"}],
        stop_after=0,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == []
    assert body["backup_files"] == []
    assert body["rollback_steps"] == []
    assert body["summary"]["total"] == 0


def test_empty_incoming_allows_zero_stop():
    r = _post({"existing": [], "incoming": [], "stop_after": 0})
    assert r.status_code == 200
    assert r.json()["rollback_steps"] == []


# ---------------------------------------------------------------------------
# stop_after：只模拟前缀，其后条目不产生任何影响
# ---------------------------------------------------------------------------


def test_stop_after_excludes_tail_entries():
    resp = _preview(
        existing=[{"path": "keep.txt", "type": "file"}],
        incoming=[
            {"path": "a.txt", "type": "file"},  # 前缀内：CREATE
            {"path": "keep.txt", "type": "file"},  # 前缀外：覆盖不发生
            {"path": "b/c.txt", "type": "file"},  # 前缀外：新建不发生
        ],
        stop_after=1,
    )
    body = resp.json()
    assert [e["index"] for e in body["entries"]] == [1]
    assert body["backup_files"] == []
    assert _ops(body) == [("DELETE_CREATED_FILE", "incoming", 1, "a.txt")]
    assert body["summary"]["backups"] == 0


def test_stop_after_full_length_matches_full_prefix():
    resp = _preview(
        existing=[],
        incoming=[{"path": "x/y.txt", "type": "file"}],
        stop_after=1,
    )
    body = resp.json()
    assert body["entries"][0]["status"] == "CREATE"
    assert _ops(body) == [
        ("DELETE_CREATED_FILE", "incoming", 1, "x/y.txt"),
        ("REMOVE_DIRECTORY", "implicit", None, "x"),
    ]


def test_stop_after_out_of_range_rejected_422():
    r = _post(
        {
            "existing": [],
            "incoming": [{"path": "a", "type": "file"}],
            "stop_after": 2,
        }
    )
    assert r.status_code == 422
    r = _post({"existing": [], "incoming": [], "stop_after": 1})
    assert r.status_code == 422
    r = _post(
        {
            "existing": [],
            "incoming": [{"path": "a", "type": "file"}],
            "stop_after": -1,
        }
    )
    assert r.status_code == 422


def test_stop_after_required():
    r = _post(
        {"existing": [], "incoming": [{"path": "a", "type": "file"}]}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 备份语义：同键一次、重复覆盖以解包前状态为恢复目标
# ---------------------------------------------------------------------------


def test_repeated_overwrite_backed_up_and_restored_once():
    resp = _preview(
        existing=[{"path": "a.txt", "type": "file"}],
        incoming=[
            {"path": "a.txt", "type": "file"},  # 首次覆盖
            {"path": "a.txt", "type": "file"},  # 再次覆盖
            {"path": "A.TXT", "type": "file"},  # 大小写折叠后同键，第三次覆盖
        ],
    )
    body = resp.json()
    assert len(body["backup_files"]) == 1
    assert body["backup_files"][0]["first_overwrite_index"] == 1
    restore = [s for s in body["rollback_steps"] if s["operation"] == "RESTORE_FILE"]
    assert len(restore) == 1
    assert restore[0]["index"] == 1  # 现存清单下标
    assert body["summary"] == {
        "total": 3,
        "create": 0,
        "overwrite": 3,
        "reuse_directory": 0,
        "blocked": 0,
        "backups": 1,
        "rollback_steps": 1,
        "delete_created_file": 0,
        "restore_file": 1,
        "remove_directory": 0,
    }


def test_backup_uses_pre_unpack_existing_identity():
    # 覆盖会把树节点身份改写为最新 incoming；备份/恢复必须仍指向现存条目 #1
    # 与现存清单中的原始展示路径。
    resp = _preview(
        existing=[{"path": "Docs/ReadMe.TXT", "type": "file"}],
        incoming=[
            {"path": "docs/readme.txt", "type": "file"},
            {"path": "DOCS\\README.txt", "type": "file"},
        ],
    )
    body = resp.json()
    assert body["backup_files"] == [
        {
            "existing_index": 1,
            "canonical_path": "Docs/ReadMe.TXT",
            "comparison_key": "docs/readme.txt",
            "first_overwrite_index": 1,
        }
    ]
    restore = body["rollback_steps"][-1]
    assert restore["operation"] == "RESTORE_FILE"
    assert restore["canonical_path"] == "Docs/ReadMe.TXT"
    assert restore["index"] == 1


def test_overwrite_newly_created_file_is_deleted_not_restored():
    # 前缀先新建后又覆盖同键：属于“新建文件删除一次”，不产生备份/恢复。
    resp = _preview(
        existing=[],
        incoming=[
            {"path": "a.txt", "type": "file"},  # CREATE
            {"path": "a.txt", "type": "file"},  # OVERWRITE 前缀自身产物
        ],
    )
    body = resp.json()
    assert body["backup_files"] == []
    assert _ops(body) == [("DELETE_CREATED_FILE", "incoming", 1, "a.txt")]


def test_multiple_backups_ordered_by_first_overwrite_index():
    resp = _preview(
        existing=[
            {"path": "b.txt", "type": "file"},
            {"path": "a.txt", "type": "file"},
        ],
        incoming=[
            {"path": "a.txt", "type": "file"},  # 覆盖现存 #2
            {"path": "b.txt", "type": "file"},  # 覆盖现存 #1
        ],
    )
    body = resp.json()
    assert [b["first_overwrite_index"] for b in body["backup_files"]] == [1, 2]
    assert [b["existing_index"] for b in body["backup_files"]] == [2, 1]
    # 恢复按首次覆盖下标逆序：先 b.txt 后 a.txt。
    restores = [s for s in body["rollback_steps"] if s["operation"] == "RESTORE_FILE"]
    assert [s["canonical_path"] for s in restores] == ["b.txt", "a.txt"]


# ---------------------------------------------------------------------------
# 复用目录与被阻挡条目不产生回退步骤
# ---------------------------------------------------------------------------


def test_reuse_directories_produce_no_steps():
    resp = _preview(
        existing=[
            {"path": "docs", "type": "directory"},  # 显式现存目录
            {"path": "deep/n/f.txt", "type": "file"},  # deep、deep/n 为隐含目录
        ],
        incoming=[
            {"path": "docs", "type": "directory"},  # REUSE 显式
            {"path": "docs/sub", "type": "directory"},  # CREATE 新子目录
            {"path": "deep", "type": "directory"},  # REUSE 隐含
            {"path": "deep/n", "type": "directory"},  # REUSE 隐含
        ],
    )
    body = resp.json()
    by = {e["index"]: e for e in body["entries"]}
    assert by[1]["status"] == "REUSE_DIRECTORY"
    assert by[2]["status"] == "CREATE"
    assert by[3]["status"] == "REUSE_DIRECTORY"
    assert by[4]["status"] == "REUSE_DIRECTORY"
    # 仅新建的 docs/sub 需要回退；现存/隐含目录一律保留。
    assert _ops(body) == [
        ("REMOVE_DIRECTORY", "incoming", 2, "docs/sub"),
    ]


def test_blocked_entries_produce_no_steps():
    resp = _preview(
        existing=[
            {"path": "f", "type": "file"},
            {"path": "d", "type": "directory"},
        ],
        incoming=[
            {"path": "f/x.txt", "type": "file"},  # FILE_ANCESTOR 阻挡
            {"path": "d", "type": "file"},  # TYPE_CONFLICT 阻挡
            {"path": "f", "type": "directory"},  # TYPE_CONFLICT 阻挡
            {"path": "CON", "type": "file"},  # UNSAFE_PATH 阻挡
        ],
    )
    body = resp.json()
    assert all(e["status"] == "BLOCKED" for e in body["entries"])
    assert body["backup_files"] == []
    assert body["rollback_steps"] == []
    assert body["summary"]["blocked"] == 4


def test_type_conflict_on_existing_file_is_not_backed_up():
    # 现存文件 vs 解包目录：被阻挡，现存文件未被覆盖，无需备份。
    resp = _preview(
        existing=[{"path": "a", "type": "file"}],
        incoming=[{"path": "a", "type": "directory"}],
    )
    body = resp.json()
    assert body["entries"][0]["status"] == "BLOCKED"
    assert body["backup_files"] == []
    assert body["rollback_steps"] == []


# ---------------------------------------------------------------------------
# 新建目录（含隐含父目录）删除次序：深 → 浅、同深度按比较键升序
# ---------------------------------------------------------------------------


def test_directory_removal_deep_to_shallow_same_depth_key_sorted():
    resp = _preview(
        existing=[],
        incoming=[
            {"path": "a/b/c1/f.txt", "type": "file"},
            {"path": "a/b/c2/g.txt", "type": "file"},
            {"path": "top.txt", "type": "file"},
        ],
    )
    body = resp.json()
    removes = [
        s["canonical_path"]
        for s in body["rollback_steps"]
        if s["operation"] == "REMOVE_DIRECTORY"
    ]
    # 深度 3：a/b/c1、a/b/c2（同深度按键升序）；深度 2：a/b；深度 1：a
    assert removes == ["a/b/c1", "a/b/c2", "a/b", "a"]
    # 目录删除整体排在文件删除之后。
    op_seq = [s["operation"] for s in body["rollback_steps"]]
    assert op_seq.count("REMOVE_DIRECTORY") == 4
    assert max(i for i, op in enumerate(op_seq) if op == "DELETE_CREATED_FILE") < min(
        i for i, op in enumerate(op_seq) if op == "REMOVE_DIRECTORY"
    )


def test_explicit_created_directory_removed_with_incoming_reference():
    resp = _preview(
        existing=[],
        incoming=[
            {"path": "d", "type": "directory"},
            {"path": "d/e.txt", "type": "file"},
        ],
    )
    body = resp.json()
    removes = {(s["source"], s["index"], s["canonical_path"]) for s in body["rollback_steps"] if s["operation"] == "REMOVE_DIRECTORY"}
    assert removes == {("incoming", 1, "d")}


def test_empty_new_directory_is_removed():
    resp = _preview(
        existing=[],
        incoming=[{"path": "empty/dir", "type": "directory"}],
    )
    body = resp.json()
    assert _ops(body) == [
        ("REMOVE_DIRECTORY", "incoming", 1, "empty/dir"),
        ("REMOVE_DIRECTORY", "implicit", None, "empty"),
    ]


def test_new_files_under_existing_directory_leave_directory_intact():
    resp = _preview(
        existing=[{"path": "keep", "type": "directory"}],
        incoming=[{"path": "keep/new.txt", "type": "file"}],
    )
    body = resp.json()
    assert _ops(body) == [("DELETE_CREATED_FILE", "incoming", 1, "keep/new.txt")]


def test_file_phase_ordering_is_reverse_first_index():
    # 新建文件删除与恢复均按首次相关下标逆序；两类互不相同键，不交叉。
    resp = _preview(
        existing=[{"path": "o1.txt", "type": "file"}],
        incoming=[
            {"path": "o1.txt", "type": "file"},  # 1 覆盖
            {"path": "n1.txt", "type": "file"},  # 2 新建
            {"path": "n2.txt", "type": "file"},  # 3 新建
        ],
    )
    body = resp.json()
    assert [s["operation"] for s in body["rollback_steps"]] == [
        "DELETE_CREATED_FILE",
        "DELETE_CREATED_FILE",
        "RESTORE_FILE",
    ]
    assert [s["index"] for s in body["rollback_steps"]] == [3, 2, 1]


# ---------------------------------------------------------------------------
# 前缀内中间状态：后建文件阻挡等规则仍与 overlay 一致
# ---------------------------------------------------------------------------


def test_prefix_dry_run_follows_overlay_blocking_rules():
    resp = _preview(
        existing=[],
        incoming=[
            {"path": "a", "type": "file"},  # CREATE
            {"path": "a/x", "type": "file"},  # 被 #1 文件祖先阻挡
        ],
    )
    body = resp.json()
    by = {e["index"]: e for e in body["entries"]}
    assert by[2]["status"] == "BLOCKED"
    assert by[2]["blocked_by"] == {
        "source": "incoming",
        "index": 1,
        "canonical_path": "a",
    }
    assert _ops(body) == [("DELETE_CREATED_FILE", "incoming", 1, "a")]


# ---------------------------------------------------------------------------
# 现存清单校验与 Windows 规范化沿用 overlay
# ---------------------------------------------------------------------------


def test_existing_manifest_validation_reused_422():
    r = _post(
        {
            "existing": [{"path": "../escape", "type": "file"}],
            "incoming": [{"path": "a", "type": "file"}],
            "stop_after": 1,
        }
    )
    assert r.status_code == 422
    r = _post(
        {
            "existing": [
                {"path": "a.txt", "type": "file"},
                {"path": "A.TXT", "type": "file"},
            ],
            "incoming": [],
            "stop_after": 0,
        }
    )
    assert r.status_code == 422
    r = _post(
        {
            "existing": [
                {"path": "a/b", "type": "file"},
                {"path": "a/b/c.txt", "type": "file"},
            ],
            "incoming": [],
            "stop_after": 0,
        }
    )
    assert r.status_code == 422


def test_windows_normalization_reused():
    resp = _preview(
        existing=[{"path": "Docs\\ReadMe.txt", "type": "file"}],
        incoming=[{"path": "docs/readme.TXT", "type": "file"}],
    )
    body = resp.json()
    assert body["entries"][0]["status"] == "OVERWRITE"
    assert body["entries"][0]["comparison_key"] == "docs/readme.txt"
    assert body["backup_files"][0]["canonical_path"] == "Docs/ReadMe.txt"


def test_unsafe_incoming_marked_blocked_not_422():
    resp = _preview(
        existing=[],
        incoming=[{"path": "../evil.bin", "type": "file"}],
    )
    assert resp.status_code == 200
    entry = resp.json()["entries"][0]
    assert entry["status"] == "BLOCKED"
    assert entry["blocked_reason"] == "UNSAFE_PATH"
