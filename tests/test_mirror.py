"""mirror-preview：目录镜像同步预演测试（三阶段操作序列 + 两侧清单校验）。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _post(payload):
    return client.post("/api/v1/mirror-preview", json=payload)


def _mirror(existing, incoming):
    r = _post({"existing": existing, "incoming": incoming})
    assert r.status_code == 200, r.text
    return r.json()


def _ops(body):
    return [(s["order"], s["operation"], s["canonical_path"]) for s in body["steps"]]


# ---------------------------------------------------------------------------
# 基本三阶段排序
# ---------------------------------------------------------------------------


def test_three_phase_ordering_and_sorting():
    # 现存：a/keep.txt 保留；a/old.txt 多余（删除）；
    # a/sub 为现存目录但期望是文件（类型互换，先删目录）；
    # junk 为顶层多余节点（删除）。
    # 期望：b 为新目录；b/n.txt 为新文件；keep.txt 同键覆盖。
    body = _mirror(
        existing=[
            {"path": "a/keep.txt", "type": "file"},
            {"path": "a/old.txt", "type": "file"},
            {"path": "a/sub", "type": "directory"},
            {"path": "junk", "type": "file"},
        ],
        incoming=[
            {"path": "a/keep.txt", "type": "file"},  # 覆盖（#1）
            {"path": "b", "type": "directory"},  # 新建目录（#2）
            {"path": "b/n.txt", "type": "file"},  # 新建文件（#3）
        ],
    )
    assert _ops(body) == [
        # 删除：深 → 浅，同深度按比较键
        (1, "DELETE", "a/old.txt"),
        (2, "DELETE", "a/sub"),
        (3, "DELETE", "junk"),
        # 目录：浅 → 深
        (4, "KEEP_DIRECTORY", "a"),  # a 由现存文件推导为隐含目录，保留
        (5, "CREATE_DIRECTORY", "b"),
        # 文件：按 incoming 顺序
        (6, "OVERWRITE_FILE", "a/keep.txt"),
        (7, "CREATE_FILE", "b/n.txt"),
    ]
    assert body["summary"] == {
        "total": 7,
        "delete": 3,
        "keep_directory": 1,
        "create_directory": 1,
        "create_file": 1,
        "overwrite_file": 1,
    }


def test_delete_removes_descendants_before_ancestors():
    # 整棵多余目录树：删除顺序必须先子后父。
    body = _mirror(
        existing=[
            {"path": "x/y/z/f.txt", "type": "file"},
            {"path": "x/y/g.txt", "type": "file"},
            {"path": "keep.txt", "type": "file"},
        ],
        incoming=[{"path": "keep.txt", "type": "file"}],
    )
    deleted = [p for _, op, p in _ops(body) if op == "DELETE"]
    assert deleted == [
        "x/y/z/f.txt",  # 深度 4
        "x/y/g.txt",  # 深度 3
        "x/y/z",  # 深度 3（隐含目录，键序在 x/y/g.txt 之后）
        "x/y",  # 深度 2
        "x",  # 深度 1
    ]
    # 隐含目录删除步骤：source=implicit、index=None、node_type=directory。
    impl = [s for s in body["steps"] if s["canonical_path"] == "x"][0]
    assert impl["source"] == "implicit"
    assert impl["index"] is None
    assert impl["node_type"] == "directory"
    assert impl["comparison_key"] == "x"


def test_same_depth_sorted_by_comparison_key_case_insensitive():
    body = _mirror(
        existing=[
            {"path": "B.txt", "type": "file"},
            {"path": "a.txt", "type": "file"},
            {"path": "C", "type": "directory"},
        ],
        incoming=[],
    )
    deleted = [p for _, op, p in _ops(body) if op == "DELETE"]
    # 文件同深度按键序：a.txt < b.txt；目录 C 深度 1 最后。
    assert deleted == ["a.txt", "B.txt", "C"]


# ---------------------------------------------------------------------------
# 目录保留：现存目录与隐含目录
# ---------------------------------------------------------------------------


def test_existing_directories_marked_keep():
    body = _mirror(
        existing=[
            {"path": "docs", "type": "directory"},
            {"path": "docs/a.txt", "type": "file"},
        ],
        incoming=[
            {"path": "docs", "type": "directory"},
            {"path": "docs/a.txt", "type": "file"},
            {"path": "docs/b.txt", "type": "file"},
        ],
    )
    # 显式现存目录保留：沿用现存节点来源 existing#1。
    docs = next(s for s in body["steps"] if s["canonical_path"] == "docs")
    assert docs["operation"] == "KEEP_DIRECTORY"
    assert docs["source"] == "existing"
    assert docs["index"] == 1
    # incoming 显式目录本身不产生独立的 incoming 下标。
    file_steps = [s for s in body["steps"] if s["node_type"] == "file"]
    assert [s["operation"] for s in file_steps] == [
        "OVERWRITE_FILE",
        "CREATE_FILE",
    ]
    assert [s["index"] for s in file_steps] == [2, 3]


def test_implicit_existing_directory_kept_when_still_needed():
    # 现存深层文件使 a、a/b 成为隐含目录；期望仍需要它们 → 保留。
    body = _mirror(
        existing=[{"path": "a/b/c.txt", "type": "file"}],
        incoming=[
            {"path": "a/b/c.txt", "type": "file"},  # 覆盖
            {"path": "a/b/d.txt", "type": "file"},  # 新建
        ],
    )
    dir_steps = [
        (s["operation"], s["canonical_path"], s["source"], s["index"])
        for s in body["steps"]
        if s["node_type"] == "directory"
    ]
    assert dir_steps == [
        ("KEEP_DIRECTORY", "a", "implicit", None),
        ("KEEP_DIRECTORY", "a/b", "implicit", None),
    ]


def test_explicit_incoming_directory_creates_missing_parents():
    body = _mirror(
        existing=[],
        incoming=[
            {"path": "x/y/z", "type": "directory"},
            {"path": "x/y/z/f.txt", "type": "file"},
        ],
    )
    dirs = [
        (s["operation"], s["canonical_path"], s["source"], s["index"])
        for s in body["steps"]
        if s["node_type"] == "directory"
    ]
    assert dirs == [
        ("CREATE_DIRECTORY", "x", "implicit", None),
        ("CREATE_DIRECTORY", "x/y", "implicit", None),
        ("CREATE_DIRECTORY", "x/y/z", "incoming", 1),
    ]


def test_implicit_desired_directory_absorbed_by_existing_one():
    # 期望文件推导的隐含目录 x 在现存树里已有同名目录 → 保留而非新建。
    body = _mirror(
        existing=[{"path": "x", "type": "directory"}],
        incoming=[{"path": "x/f.txt", "type": "file"}],
    )
    dirs = [(s["operation"], s["source"]) for s in body["steps"]
            if s["node_type"] == "directory"]
    assert dirs == [("KEEP_DIRECTORY", "existing")]


# ---------------------------------------------------------------------------
# 类型互换（file ↔ directory）
# ---------------------------------------------------------------------------


def test_directory_to_file_swap():
    # 现存目录 a（含子文件）→ 期望文件 a：删除目录及其后代，再写文件。
    body = _mirror(
        existing=[
            {"path": "a", "type": "directory"},
            {"path": "a/child.txt", "type": "file"},
        ],
        incoming=[{"path": "a", "type": "file"}],
    )
    assert _ops(body) == [
        (1, "DELETE", "a/child.txt"),
        (2, "DELETE", "a"),
        (3, "CREATE_FILE", "a"),
    ]
    swap = body["steps"][1]
    assert swap["node_type"] == "directory"
    assert swap["source"] == "existing"
    assert swap["index"] == 1


def test_file_to_directory_swap():
    # 现存文件 a → 期望目录 a（含子文件）：删文件，建目录，写子文件。
    body = _mirror(
        existing=[{"path": "a", "type": "file"}],
        incoming=[
            {"path": "a", "type": "directory"},
            {"path": "a/child.txt", "type": "file"},
        ],
    )
    assert _ops(body) == [
        (1, "DELETE", "a"),
        (2, "CREATE_DIRECTORY", "a"),
        (3, "CREATE_FILE", "a/child.txt"),
    ]
    create_dir = body["steps"][1]
    # 目录是 incoming 显式条目 #1。
    assert create_dir["source"] == "incoming"
    assert create_dir["index"] == 1


def test_existing_file_under_desired_directory_no_false_delete():
    # a/b 为期望目录；现存同路径文件 a/b 的子文件 a/b/f 不可能出现
    # （现存清单校验拒绝文件祖先），此处验证普通并存不被误删。
    body = _mirror(
        existing=[
            {"path": "a/f1.txt", "type": "file"},
            {"path": "a/b", "type": "directory"},
            {"path": "a/b/f2.txt", "type": "file"},
        ],
        incoming=[
            {"path": "a/b/f2.txt", "type": "file"},
            {"path": "a/f1.txt", "type": "file"},
        ],
    )
    assert [s["operation"] for s in body["steps"] if s["operation"] == "DELETE"] == []
    keeps = [p for _, op, p in _ops(body) if op == "KEEP_DIRECTORY"]
    assert keeps == ["a", "a/b"]


# ---------------------------------------------------------------------------
# 空清单 / 全量同步
# ---------------------------------------------------------------------------


def test_empty_incoming_wipes_everything():
    body = _mirror(
        existing=[
            {"path": "a/b.txt", "type": "file"},
            {"path": "c", "type": "directory"},
        ],
        incoming=[],
    )
    assert [s["operation"] for s in body["steps"]] == [
        "DELETE",
        "DELETE",
        "DELETE",  # a（隐含目录）
    ]
    assert body["summary"] == {
        "total": 3,
        "delete": 3,
        "keep_directory": 0,
        "create_directory": 0,
        "create_file": 0,
        "overwrite_file": 0,
    }


def test_both_empty_yields_no_steps():
    body = _mirror(existing=[], incoming=[])
    assert body["steps"] == []
    assert body["summary"]["total"] == 0
    # 两字段均可省略。
    r = _post({})
    assert r.status_code == 200
    assert r.json()["steps"] == []


def test_empty_existing_creates_everything():
    body = _mirror(
        existing=[],
        incoming=[
            {"path": "f.txt", "type": "file"},
            {"path": "d/sub", "type": "directory"},
            {"path": "d/sub/g.txt", "type": "file"},
        ],
    )
    assert _ops(body) == [
        # 文件阶段严格按 incoming 顺序；目录阶段先于文件。
        (1, "CREATE_DIRECTORY", "d"),
        (2, "CREATE_DIRECTORY", "d/sub"),
        (3, "CREATE_FILE", "f.txt"),
        (4, "CREATE_FILE", "d/sub/g.txt"),
    ]


# ---------------------------------------------------------------------------
# Windows 规范化在两侧的复用
# ---------------------------------------------------------------------------


def test_windows_normalization_on_both_sides():
    body = _mirror(
        existing=[{"path": "Docs\\ReadMe.TXT", "type": "file"}],
        incoming=[{"path": "docs/readme.txt", "type": "file"}],
    )
    # 现存文件隐含的 docs 目录仍被期望使用 → 保留；文件同键 → 覆盖。
    assert [s["operation"] for s in body["steps"]] == [
        "KEEP_DIRECTORY",
        "OVERWRITE_FILE",
    ]
    step = body["steps"][1]
    # 展示路径沿用 incoming 原文大小写；比较键为小写折叠。
    assert step["canonical_path"] == "docs/readme.txt"
    assert step["comparison_key"] == "docs/readme.txt"
    assert step["index"] == 1


# ---------------------------------------------------------------------------
# 422 校验：两侧清单
# ---------------------------------------------------------------------------


def test_existing_violations_rejected_422():
    # 现存清单沿用 overlay-preview 校验。
    assert _mirror_status([{"path": "../escape", "type": "file"}], []) == 422
    assert _mirror_status(
        [
            {"path": "a.txt", "type": "file"},
            {"path": "A.TXT", "type": "file"},
        ],
        [],
    ) == 422  # 同键重复
    assert _mirror_status(
        [
            {"path": "a/b", "type": "file"},
            {"path": "a/b/c.txt", "type": "file"},
        ],
        [],
    ) == 422  # 文件祖先冲突


def test_incoming_unsafe_path_rejected_422():
    r = _post(
        {
            "existing": [],
            "incoming": [{"path": "../evil.bin", "type": "file"}],
        }
    )
    assert r.status_code == 422
    assert "期望清单" in r.text


def test_incoming_duplicate_keys_rejected_422():
    r = _post(
        {
            "existing": [],
            "incoming": [
                {"path": "docs/a.txt", "type": "file"},
                {"path": "Docs\\A.TXT", "type": "file"},
            ],
        }
    )
    assert r.status_code == 422
    # 目录与文件同键也算重复。
    r = _post(
        {
            "existing": [],
            "incoming": [
                {"path": "a", "type": "directory"},
                {"path": "a", "type": "file"},
            ],
        }
    )
    assert r.status_code == 422


def test_incoming_file_ancestor_rejected_422():
    r = _post(
        {
            "existing": [],
            "incoming": [
                {"path": "a/b", "type": "file"},
                {"path": "a/b/c.txt", "type": "file"},
            ],
        }
    )
    assert r.status_code == 422


def test_symlink_type_rejected_422():
    r = _post(
        {
            "existing": [{"path": "lnk", "type": "symlink"}],
            "incoming": [],
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


def _mirror_status(existing, incoming):
    return _post({"existing": existing, "incoming": incoming}).status_code


# ---------------------------------------------------------------------------
# 既有接口保持不变
# ---------------------------------------------------------------------------


def test_other_endpoints_untouched():
    r = client.post(
        "/api/v1/overlay-preview",
        json={
            "existing": [{"path": "a.txt", "type": "file"}],
            "incoming": [{"path": "a.txt", "type": "file"}],
        },
    )
    assert r.status_code == 200
    assert r.json()["entries"][0]["status"] == "OVERWRITE"
    assert client.get("/health").json()["status"] == "ok"
