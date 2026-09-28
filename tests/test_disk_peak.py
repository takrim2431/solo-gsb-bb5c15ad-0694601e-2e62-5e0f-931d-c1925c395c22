"""disk-peak-preview：磁盘暂存峰值预演测试（备份/暂存记账、整批峰值、首次超额）。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _post(payload):
    return client.post("/api/v1/disk-peak-preview", json=payload)


def _preview(existing, incoming, available=10**18):
    return _post(
        {
            "existing": existing,
            "incoming": incoming,
            "initial_available_bytes": available,
        }
    )


def _disk_fields(body):
    return {
        e["index"]: (
            e["status"],
            e["backup_bytes"],
            e["staging_peak_bytes"],
            e["committed_delta_bytes"],
        )
        for e in body["entries"]
    }


# ---------------------------------------------------------------------------
# 记账基础：新建、首次覆盖现存文件、重复覆盖
# ---------------------------------------------------------------------------


def test_create_file_accounts_full_size_only_on_staging():
    resp = _preview(
        existing=[],
        incoming=[{"path": "new/a.txt", "type": "file", "size_bytes": 50}],
    )
    assert resp.status_code == 200
    body = resp.json()
    e = body["entries"][0]
    assert e["status"] == "CREATE"
    assert e["size_bytes"] == 50
    assert e["backup_bytes"] == 0
    assert e["staging_peak_bytes"] == 50
    assert e["committed_delta_bytes"] == 50
    assert body["batch_peak_bytes"] == 50
    assert body["first_exceedance"] is None
    assert body["summary"]["committed_total_bytes"] == 50


def test_first_overwrite_backups_then_stages_then_releases():
    # 现存文件 10，新文件 100：备份 +10 -> 暂存 +100 -> 释放旧 10。
    resp = _preview(
        existing=[{"path": "a.txt", "type": "file", "size_bytes": 10}],
        incoming=[{"path": "a.txt", "type": "file", "size_bytes": 100}],
    )
    body = resp.json()
    e = body["entries"][0]
    assert e["status"] == "OVERWRITE"
    assert e["backup_bytes"] == 10
    assert e["staging_peak_bytes"] == 110
    assert e["committed_delta_bytes"] == 100
    # 瞬时峰值 110（备份 + 暂存并存），提交后只留新文件 100。
    assert body["batch_peak_bytes"] == 110
    assert body["summary"]["committed_total_bytes"] == 100


def test_repeated_overwrite_does_not_backup_again_and_can_shrink():
    resp = _preview(
        existing=[{"path": "a.txt", "type": "file", "size_bytes": 10}],
        incoming=[
            {"path": "a.txt", "type": "file", "size_bytes": 100},  # 首次覆盖
            {"path": "a.txt", "type": "file", "size_bytes": 40},  # 再覆盖，缩容
            {"path": "A.TXT", "type": "file", "size_bytes": 70},  # Windows 同键，扩容
        ],
    )
    body = resp.json()
    assert _disk_fields(body) == {
        1: ("OVERWRITE", 10, 110, 100),
        2: ("OVERWRITE", 0, 40, -60),
        3: ("OVERWRITE", 0, 70, 30),
    }
    # 水位：备份 10 -> 110 -> 提交 100 -> 暂存 140 -> 提交 40 -> 暂存 110 -> 提交 70。
    assert body["batch_peak_bytes"] == 140
    assert body["summary"]["committed_total_bytes"] == 70


def test_create_then_overwrite_same_key_is_not_backed_up():
    # 前缀中新建后又覆盖：不备份（回退只需删除新建文件），净增按差值计算。
    resp = _preview(
        existing=[],
        incoming=[
            {"path": "a.txt", "type": "file", "size_bytes": 50},
            {"path": "a.txt", "type": "file", "size_bytes": 80},
            {"path": "A.TXT", "type": "file", "size_bytes": 30},
        ],
    )
    body = resp.json()
    assert _disk_fields(body) == {
        1: ("CREATE", 0, 50, 50),
        2: ("OVERWRITE", 0, 80, 30),
        3: ("OVERWRITE", 0, 30, -50),
    }
    assert body["batch_peak_bytes"] == 130
    assert body["summary"]["committed_total_bytes"] == 30


def test_zero_sized_files_never_exceed():
    resp = _preview(
        existing=[{"path": "a", "type": "file", "size_bytes": 0}],
        incoming=[
            {"path": "a", "type": "file", "size_bytes": 0},
            {"path": "b", "type": "file", "size_bytes": 0},
        ],
        available=0,
    )
    body = resp.json()
    assert body["batch_peak_bytes"] == 0
    assert body["first_exceedance"] is None
    assert body["summary"]["committed_total_bytes"] == 0


# ---------------------------------------------------------------------------
# 目录与被阻挡条目不占空间
# ---------------------------------------------------------------------------


def test_directories_and_blocked_entries_occupy_nothing():
    resp = _preview(
        existing=[
            {"path": "f", "type": "file", "size_bytes": 100},
            {"path": "d", "type": "directory"},
        ],
        incoming=[
            {"path": "new/dir", "type": "directory"},  # CREATE 目录
            {"path": "d", "type": "directory"},  # REUSE_DIRECTORY
            {"path": "d/sub", "type": "directory"},  # CREATE 子目录
            {"path": "f/x.txt", "type": "file", "size_bytes": 9999},  # FILE_ANCESTOR
            {"path": "f", "type": "directory"},  # TYPE_CONFLICT
            {"path": "d", "type": "file", "size_bytes": 9999},  # TYPE_CONFLICT
            {"path": "../evil", "type": "file", "size_bytes": 9999},  # UNSAFE_PATH
            {"path": "ok.txt", "type": "file", "size_bytes": 5},
        ],
    )
    body = resp.json()
    by = _disk_fields(body)
    assert by[1] == ("CREATE", 0, 0, 0)
    assert by[2] == ("REUSE_DIRECTORY", 0, 0, 0)
    assert by[3] == ("CREATE", 0, 0, 0)
    for i in (4, 5, 6, 7):
        assert by[i][0] == "BLOCKED"
        assert by[i][1:] == (0, 0, 0)
    assert by[8] == ("CREATE", 0, 5, 5)
    # 被阻挡条目的声明大小绝不参与记账。
    assert body["batch_peak_bytes"] == 5
    assert body["summary"]["committed_total_bytes"] == 5
    for e in body["entries"]:
        if e["type"] == "directory":
            assert e["size_bytes"] is None


def test_implicit_parent_directories_occupy_nothing():
    resp = _preview(
        existing=[{"path": "keep/deep/f.txt", "type": "file", "size_bytes": 1}],
        incoming=[{"path": "brand/new/a/b/c.bin", "type": "file", "size_bytes": 7}],
    )
    body = resp.json()
    assert body["batch_peak_bytes"] == 7
    assert body["summary"]["committed_total_bytes"] == 7


# ---------------------------------------------------------------------------
# 整批峰值：备份与暂存阶段共同计入高水位，提交顺序影响峰值
# ---------------------------------------------------------------------------


def test_batch_peak_spans_backup_and_staging_across_entries():
    # entry1：新建 60 -> 水位 60，提交 60
    # entry2：首次覆盖现存 30 -> 备份 90 -> 暂存 +40 = 130 -> 释放 30 -> 提交 100
    # entry3：新建 50 -> 暂存 150 -> 提交 150
    resp = _preview(
        existing=[{"path": "old", "type": "file", "size_bytes": 30}],
        incoming=[
            {"path": "n1", "type": "file", "size_bytes": 60},
            {"path": "old", "type": "file", "size_bytes": 40},
            {"path": "n2", "type": "file", "size_bytes": 50},
        ],
    )
    body = resp.json()
    assert body["batch_peak_bytes"] == 150
    assert body["summary"]["committed_total_bytes"] == 150


def test_shrinking_overwrite_lowers_commit_level_for_later_staging():
    resp = _preview(
        existing=[{"path": "big", "type": "file", "size_bytes": 1000}],
        incoming=[
            {"path": "big", "type": "file", "size_bytes": 10},  # 峰值 1010，提交 10
            {"path": "x", "type": "file", "size_bytes": 100},  # 暂存 110
        ],
        available=10**9,
    )
    body = resp.json()
    assert body["batch_peak_bytes"] == 1010
    assert body["summary"]["committed_total_bytes"] == 110


# ---------------------------------------------------------------------------
# 首次超额：严格大于、等于不算、记录阶段（BACKUP/STAGING）与下标，继续预演
# ---------------------------------------------------------------------------


def test_first_exceedance_during_staging():
    resp = _preview(
        existing=[{"path": "a", "type": "file", "size_bytes": 10}],
        incoming=[
            {"path": "a", "type": "file", "size_bytes": 100},  # 峰值 110，提交 100
            {"path": "a", "type": "file", "size_bytes": 40},  # 暂存 140 首次超额
        ],
        available=120,
    )
    body = resp.json()
    assert body["first_exceedance"] == {
        "index": 2,
        "phase": "STAGING",
        "level_bytes": 140,
    }
    # 即使超额也继续预演：第二条提交后水位 40，汇总照常给出。
    assert body["summary"]["committed_total_bytes"] == 40


def test_first_exceedance_during_backup():
    # 备份一个 1000 字节的现存文件本身就超出额度（尚未暂存任何新文件）。
    resp = _preview(
        existing=[{"path": "a", "type": "file", "size_bytes": 1000}],
        incoming=[{"path": "a", "type": "file", "size_bytes": 10}],
        available=100,
    )
    body = resp.json()
    assert body["first_exceedance"] == {
        "index": 1,
        "phase": "BACKUP",
        "level_bytes": 1000,
    }
    # 备份后继续暂存，峰值继续抬升到 1010。
    assert body["batch_peak_bytes"] == 1010


def test_level_equal_to_quota_is_not_exceedance():
    resp = _preview(
        existing=[],
        incoming=[{"path": "a", "type": "file", "size_bytes": 100}],
        available=100,
    )
    body = resp.json()
    assert body["batch_peak_bytes"] == 100
    assert body["first_exceedance"] is None


def test_first_exceedance_picks_earliest_phase_only():
    # 备份阶段已超额时不再报告同条目的暂存阶段，也不报告后续条目。
    resp = _preview(
        existing=[{"path": "a", "type": "file", "size_bytes": 80}],
        incoming=[
            {"path": "a", "type": "file", "size_bytes": 80},  # 备份 80 = 额度，不超额；暂存 160 超额
            {"path": "b", "type": "file", "size_bytes": 999},
        ],
        available=80,
    )
    body = resp.json()
    assert body["first_exceedance"] == {
        "index": 1,
        "phase": "STAGING",
        "level_bytes": 160,
    }


def test_no_exceedance_when_peak_within_quota():
    resp = _preview(
        existing=[{"path": "a", "type": "file", "size_bytes": 40}],
        incoming=[
            {"path": "a", "type": "file", "size_bytes": 60},  # 峰值 100
            {"path": "b", "type": "file", "size_bytes": 1},  # 暂存 101? 提交后 a 为 60 -> 61
        ],
        available=100,
    )
    body = resp.json()
    # entry1 提交水位 60；entry2 暂存 61，未超 100。
    assert body["batch_peak_bytes"] == 100
    assert body["first_exceedance"] is None
    assert body["summary"]["committed_total_bytes"] == 61


# ---------------------------------------------------------------------------
# overlay 判定与 Windows 同键语义沿用
# ---------------------------------------------------------------------------


def test_overlay_decisions_and_windows_key_collation_reused():
    resp = _preview(
        existing=[{"path": "Docs\\ReadMe.TXT", "type": "file", "size_bytes": 12}],
        incoming=[
            {"path": "docs/readme.txt", "type": "file", "size_bytes": 34},
            {"path": "docs", "type": "file", "size_bytes": 5},
            {"path": "a", "type": "file", "size_bytes": 1},
            {"path": "a/x", "type": "file", "size_bytes": 1},
        ],
    )
    body = resp.json()
    by = {e["index"]: e for e in body["entries"]}
    assert by[1]["status"] == "OVERWRITE"
    assert by[1]["comparison_key"] == "docs/readme.txt"
    assert by[1]["backup_bytes"] == 12  # 现存原始大小只备份一次
    assert by[2]["status"] == "BLOCKED"
    assert by[2]["blocked_reason"] == "TYPE_CONFLICT"
    assert by[2]["staging_peak_bytes"] == 0
    assert by[3]["status"] == "CREATE"
    assert by[4]["status"] == "BLOCKED"
    assert by[4]["blocked_reason"] == "FILE_ANCESTOR"
    assert by[4]["blocked_by"] == {
        "source": "incoming",
        "index": 3,
        "canonical_path": "a",
    }


def test_existing_manifest_validation_reused_422():
    base_incoming = [{"path": "a", "type": "file", "size_bytes": 1}]
    for existing in (
        [{"path": "../escape", "type": "file", "size_bytes": 1}],  # 不安全路径
        [  # 同键重复
            {"path": "a", "type": "file", "size_bytes": 1},
            {"path": "A", "type": "file", "size_bytes": 2},
        ],
        [  # 文件占据祖先路径
            {"path": "a/b", "type": "file", "size_bytes": 1},
            {"path": "a/b/c", "type": "file", "size_bytes": 1},
        ],
    ):
        r = _post(
            {
                "existing": existing,
                "incoming": base_incoming,
                "initial_available_bytes": 0,
            }
        )
        assert r.status_code == 422


def test_unsafe_incoming_marked_blocked_not_422():
    r = _preview(
        existing=[],
        incoming=[{"path": "../evil.bin", "type": "file", "size_bytes": 1}],
    )
    assert r.status_code == 200
    e = r.json()["entries"][0]
    assert e["status"] == "BLOCKED"
    assert e["blocked_reason"] == "UNSAFE_PATH"
    assert e["staging_peak_bytes"] == 0


# ---------------------------------------------------------------------------
# 请求校验（422）：大小声明与初始可用字节
# ---------------------------------------------------------------------------


def test_size_declaration_validation_422():
    cases = [
        # file 缺少 size_bytes
        {"existing": [], "incoming": [{"path": "a", "type": "file"}], "initial_available_bytes": 0},
        # incoming directory 携带大小
        {"existing": [], "incoming": [{"path": "d", "type": "directory", "size_bytes": 1}], "initial_available_bytes": 0},
        # 负大小
        {"existing": [], "incoming": [{"path": "a", "type": "file", "size_bytes": -1}], "initial_available_bytes": 0},
        # existing file 缺少大小
        {"existing": [{"path": "a", "type": "file"}], "incoming": [{"path": "a", "type": "file", "size_bytes": 1}], "initial_available_bytes": 0},
        # existing directory 携带大小
        {"existing": [{"path": "d", "type": "directory", "size_bytes": 5}], "incoming": [{"path": "a", "type": "file", "size_bytes": 1}], "initial_available_bytes": 0},
        # 负可用字节
        {"existing": [], "incoming": [{"path": "a", "type": "file", "size_bytes": 1}], "initial_available_bytes": -1},
        # 缺少 initial_available_bytes
        {"existing": [], "incoming": [{"path": "a", "type": "file", "size_bytes": 1}]},
        # incoming 为空
        {"existing": [], "incoming": [], "initial_available_bytes": 0},
    ]
    for payload in cases:
        r = _post(payload)
        assert r.status_code == 422, payload


def test_response_summary_counts_and_echo():
    resp = _preview(
        existing=[{"path": "o", "type": "file", "size_bytes": 10}],
        incoming=[
            {"path": "o", "type": "file", "size_bytes": 10},
            {"path": "n", "type": "file", "size_bytes": 5},
            {"path": "d", "type": "directory"},
            {"path": "../x", "type": "file", "size_bytes": 1},
        ],
        available=1000,
    )
    body = resp.json()
    assert body["initial_available_bytes"] == 1000
    assert body["summary"] == {
        "total": 4,
        "create": 2,
        "overwrite": 1,
        "reuse_directory": 0,
        "blocked": 1,
        "committed_total_bytes": 15,
    }
    assert [e["index"] for e in body["entries"]] == [1, 2, 3, 4]
