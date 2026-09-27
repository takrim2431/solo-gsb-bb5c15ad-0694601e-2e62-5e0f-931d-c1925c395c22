"""overlay-preview：目标目录覆盖预演测试（现存清单校验 + 按序干跑）。"""

import unicodedata

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _post(payload):
    return client.post("/api/v1/overlay-preview", json=payload)


def _preview(existing, incoming):
    return _post({"existing": existing, "incoming": incoming})


def _by_index(resp):
    return {e["index"]: e for e in resp["entries"]}


# ---------------------------------------------------------------------------
# 基本判定：CREATE / OVERWRITE / REUSE_DIRECTORY / BLOCKED
# ---------------------------------------------------------------------------


def test_create_overwrite_reuse_and_summary():
    resp = _preview(
        existing=[
            {"path": "docs/readme.txt", "type": "file"},
            {"path": "images", "type": "directory"},
        ],
        incoming=[
            {"path": "docs/readme.txt", "type": "file"},  # 覆盖现存文件
            {"path": "images", "type": "directory"},  # 复用现存目录
            {"path": "images/logo.png", "type": "file"},  # 新建
            {"path": "notes.txt", "type": "file"},  # 新建
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["target"] == "windows"
    by = _by_index(body)
    assert [e["index"] for e in body["entries"]] == [1, 2, 3, 4]
    assert by[1]["status"] == "OVERWRITE"
    assert by[1]["blocked_reason"] is None
    assert by[1]["blocked_by"] is None
    assert by[2]["status"] == "REUSE_DIRECTORY"
    assert by[3]["status"] == "CREATE"
    assert by[4]["status"] == "CREATE"
    assert body["summary"] == {
        "total": 4,
        "create": 2,
        "overwrite": 1,
        "reuse_directory": 1,
        "blocked": 0,
    }


def test_type_conflict_with_existing_entries():
    resp = _preview(
        existing=[
            {"path": "docs", "type": "directory"},
            {"path": "data.bin", "type": "file"},
        ],
        incoming=[
            {"path": "docs", "type": "file"},  # 文件 vs 现存目录
            {"path": "data.bin", "type": "directory"},  # 目录 vs 现存文件
            {"path": "docs/a.txt", "type": "file"},  # docs 仍是目录，可新建
        ],
    )
    by = _by_index(resp.json())
    assert by[1]["status"] == "BLOCKED"
    assert by[1]["blocked_reason"] == "TYPE_CONFLICT"
    assert by[1]["blocked_by"] == {
        "source": "existing",
        "index": 1,
        "canonical_path": "docs",
    }
    assert by[2]["status"] == "BLOCKED"
    assert by[2]["blocked_reason"] == "TYPE_CONFLICT"
    assert by[2]["blocked_by"]["source"] == "existing"
    assert by[2]["blocked_by"]["index"] == 2
    # 被阻挡条目不进入虚拟目录树：docs 仍是目录，子路径可正常新建。
    assert by[3]["status"] == "CREATE"


def test_file_ancestor_blocking_from_existing():
    resp = _preview(
        existing=[{"path": "a/b", "type": "file"}],
        incoming=[
            {"path": "a/b/c.txt", "type": "file"},  # 祖先 a/b 是文件
            {"path": "a/b/sub", "type": "directory"},  # 目录同样被阻挡
        ],
    )
    body = resp.json()
    by = _by_index(body)
    for i in (1, 2):
        assert by[i]["status"] == "BLOCKED"
        assert by[i]["blocked_reason"] == "FILE_ANCESTOR"
        assert by[i]["blocked_by"] == {
            "source": "existing",
            "index": 1,
            "canonical_path": "a/b",
        }
    assert body["summary"]["blocked"] == 2


def test_implicit_parent_directories():
    # 现存深层文件使 a、a/b 成为隐含目录。
    resp = _preview(
        existing=[{"path": "a/b/c.txt", "type": "file"}],
        incoming=[
            {"path": "a", "type": "directory"},  # 复用隐含目录
            {"path": "a/b", "type": "file"},  # 文件 vs 隐含目录
            {"path": "a/b/d.txt", "type": "file"},  # 隐含目录下新建
        ],
    )
    by = _by_index(resp.json())
    assert by[1]["status"] == "REUSE_DIRECTORY"
    assert by[2]["status"] == "BLOCKED"
    assert by[2]["blocked_reason"] == "TYPE_CONFLICT"
    assert by[2]["blocked_by"] == {
        "source": "implicit",
        "index": None,
        "canonical_path": "a/b",
    }
    assert by[3]["status"] == "CREATE"


def test_successful_incoming_entries_enter_tree_immediately():
    resp = _preview(
        existing=[],
        incoming=[
            {"path": "a", "type": "file"},  # CREATE
            {"path": "a/x.txt", "type": "file"},  # 被 #1 文件祖先阻挡
            {"path": "a", "type": "file"},  # 覆盖 #1
            {"path": "b/c.txt", "type": "file"},  # CREATE（b 为隐含目录）
            {"path": "b", "type": "directory"},  # 复用隐含目录 b
            {"path": "b", "type": "file"},  # 文件 vs 目录（来源已变为 #5）
        ],
    )
    body = resp.json()
    by = _by_index(body)
    assert by[1]["status"] == "CREATE"
    assert by[2]["status"] == "BLOCKED"
    assert by[2]["blocked_reason"] == "FILE_ANCESTOR"
    assert by[2]["blocked_by"] == {
        "source": "incoming",
        "index": 1,
        "canonical_path": "a",
    }
    assert by[3]["status"] == "OVERWRITE"
    assert by[4]["status"] == "CREATE"
    assert by[5]["status"] == "REUSE_DIRECTORY"
    assert by[6]["status"] == "BLOCKED"
    assert by[6]["blocked_reason"] == "TYPE_CONFLICT"
    # 复用成功后节点来源更新为解包条目 #5。
    assert by[6]["blocked_by"] == {
        "source": "incoming",
        "index": 5,
        "canonical_path": "b",
    }
    assert body["summary"] == {
        "total": 6,
        "create": 2,
        "overwrite": 1,
        "reuse_directory": 1,
        "blocked": 2,
    }


def test_overwrite_updates_blocker_source_for_later_entries():
    resp = _preview(
        existing=[{"path": "a", "type": "file"}],
        incoming=[
            {"path": "a", "type": "file"},  # 覆盖现存文件
            {"path": "a/x", "type": "file"},  # 被 #1（覆盖后）阻挡
        ],
    )
    by = _by_index(resp.json())
    assert by[1]["status"] == "OVERWRITE"
    assert by[2]["status"] == "BLOCKED"
    assert by[2]["blocked_by"]["source"] == "incoming"
    assert by[2]["blocked_by"]["index"] == 1


# ---------------------------------------------------------------------------
# 不安全的新条目仅标为 BLOCKED
# ---------------------------------------------------------------------------


def test_unsafe_incoming_only_marked_blocked():
    resp = _preview(
        existing=[],
        incoming=[
            {"path": "../evil.bin", "type": "file"},  # 越界
            {"path": "/abs/path", "type": "file"},  # 绝对路径
            {"path": "CON", "type": "file"},  # 保留设备名
            {"path": "evil.bin", "type": "file"},  # 与 #1 规范路径同键，但 #1 未入树
        ],
    )
    body = resp.json()
    by = _by_index(body)
    for i in (1, 2, 3):
        assert by[i]["status"] == "BLOCKED"
        assert by[i]["blocked_reason"] == "UNSAFE_PATH"
        assert by[i]["blocked_by"] is None
        assert by[i]["safe"] is False
    assert "TRAVERSAL_ESCAPE" in {i["code"] for i in by[1]["issues"]}
    assert "ABSOLUTE_PATH" in {i["code"] for i in by[2]["issues"]}
    assert "RESERVED_NAME" in {i["code"] for i in by[3]["issues"]}
    # 不安全条目不进入虚拟目录树：#4 正常新建。
    assert by[4]["status"] == "CREATE"
    assert by[4]["safe"] is True
    assert body["summary"]["blocked"] == 3
    assert body["summary"]["create"] == 1


def test_safe_but_blocked_entry_keeps_safe_true():
    # 路径自身安全但被现存文件祖先阻挡：safe 仍为 true。
    resp = _preview(
        existing=[{"path": "a", "type": "file"}],
        incoming=[{"path": "a/b.txt", "type": "file"}],
    )
    entry = resp.json()["entries"][0]
    assert entry["safe"] is True
    assert entry["status"] == "BLOCKED"
    assert entry["blocked_reason"] == "FILE_ANCESTOR"


# ---------------------------------------------------------------------------
# 现存清单校验（422）
# ---------------------------------------------------------------------------


def test_existing_unsafe_path_rejected_422():
    r = _preview(
        existing=[{"path": "../escape", "type": "file"}],
        incoming=[{"path": "a.txt", "type": "file"}],
    )
    assert r.status_code == 422
    r = _preview(
        existing=[{"path": "ok.txt", "type": "file"}, {"path": "C:/win", "type": "file"}],
        incoming=[{"path": "a.txt", "type": "file"}],
    )
    assert r.status_code == 422


def test_existing_duplicate_keys_rejected_422():
    r = _preview(
        existing=[
            {"path": "docs/readme.txt", "type": "file"},
            {"path": "Docs\\ReadMe.TXT", "type": "file"},  # 大小写/分隔符变体同键
        ],
        incoming=[{"path": "a.txt", "type": "file"}],
    )
    assert r.status_code == 422
    # 目录与文件同键也算重复。
    r = _preview(
        existing=[
            {"path": "a", "type": "directory"},
            {"path": "a", "type": "file"},
        ],
        incoming=[{"path": "a.txt", "type": "file"}],
    )
    assert r.status_code == 422


def test_existing_file_ancestor_rejected_422():
    r = _preview(
        existing=[
            {"path": "a/b", "type": "file"},
            {"path": "a/b/c.txt", "type": "file"},
        ],
        incoming=[{"path": "a.txt", "type": "file"}],
    )
    assert r.status_code == 422
    # 目录占据祖先路径是正常布局，不拒绝。
    r = _preview(
        existing=[
            {"path": "a/b", "type": "directory"},
            {"path": "a/b/c.txt", "type": "file"},
        ],
        incoming=[{"path": "a.txt", "type": "file"}],
    )
    assert r.status_code == 200


def test_existing_symlink_type_rejected_422():
    r = _post(
        {
            "existing": [{"path": "lnk", "type": "symlink"}],
            "incoming": [{"path": "a.txt", "type": "file"}],
        }
    )
    assert r.status_code == 422
    r = _post(
        {
            "existing": [],
            "incoming": [{"path": "lnk", "type": "symlink"}],
        }
    )
    assert r.status_code == 422


def test_incoming_required_and_nonempty():
    assert _post({"incoming": []}).status_code == 422
    assert _post({"existing": []}).status_code == 422


# ---------------------------------------------------------------------------
# Windows 路径规范化复用
# ---------------------------------------------------------------------------


def test_windows_normalization_reused_on_both_sides():
    nfd_ring = unicodedata.normalize("NFD", "Å")
    resp = _preview(
        existing=[
            {"path": "Docs\\ReadMe.txt", "type": "file"},
            {"path": "docs/cafÅ.txt", "type": "file"},
        ],
        incoming=[
            {"path": "docs/readme.TXT", "type": "file"},  # 大小写折叠后同键
            {"path": f"docs/caf{nfd_ring}.txt", "type": "file"},  # NFC 归一后同键
            {"path": "docs/new.txt.  ", "type": "file"},  # 段尾裁剪后新建
        ],
    )
    by = _by_index(resp.json())
    assert by[1]["status"] == "OVERWRITE"
    assert by[1]["canonical_path"] == "docs/readme.TXT"
    assert by[1]["comparison_key"] == "docs/readme.txt"
    assert by[2]["status"] == "OVERWRITE"
    assert by[2]["canonical_path"] == "docs/cafÅ.txt"
    assert "UNICODE_NORMALIZED" in {i["code"] for i in by[2]["issues"]}
    assert by[3]["status"] == "CREATE"
    assert by[3]["canonical_path"] == "docs/new.txt"
    assert "SEGMENT_TRIMMED" in {i["code"] for i in by[3]["issues"]}


def test_empty_existing_means_all_create():
    resp = _preview(
        existing=[],
        incoming=[
            {"path": "x/y/z.txt", "type": "file"},
            {"path": "x/y", "type": "directory"},
        ],
    )
    by = _by_index(resp.json())
    assert by[1]["status"] == "CREATE"
    assert by[2]["status"] == "REUSE_DIRECTORY"  # x/y 已被 #1 推导为隐含目录
    # existing 字段可整体省略。
    r = _post({"incoming": [{"path": "a.txt", "type": "file"}]})
    assert r.status_code == 200
    assert r.json()["entries"][0]["status"] == "CREATE"


# ---------------------------------------------------------------------------
# 原预检接口行为保持不变
# ---------------------------------------------------------------------------


def test_preflight_endpoint_untouched():
    r = client.post(
        "/api/v1/preflight",
        json={
            "entries": [
                {"path": "Docs\\ReadMe.txt", "type": "file"},
                {"path": "docs/readme.txt", "type": "file"},
            ]
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["collisions"] == 1
    assert "repair_plan" not in body
