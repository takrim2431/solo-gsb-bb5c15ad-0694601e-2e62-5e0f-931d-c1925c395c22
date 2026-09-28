"""disk-peak-preview：磁盘暂存峰值预演测试（同盘备份/暂存/替换占用曲线）。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _post(payload):
    return client.post("/api/v1/disk-peak-preview", json=payload)


def _preview(existing, incoming, initial_available):
    return _post(
        {
            "existing": existing,
            "incoming": incoming,
            "initial_available_bytes": initial_available,
        }
    )


def _by_index(body):
    return {e["index"]: e for e in body["entries"]}


# ---------------------------------------------------------------------------
# 基本计空间：新建暂存、首次覆盖备份一次、替换释放旧占用
# ---------------------------------------------------------------------------


def test_create_and_first_overwrite_space_accounting():
    resp = _preview(
        existing=[{"path": "docs/readme.txt", "type": "file", "size_bytes": 100}],
        incoming=[
            {"path": "docs/readme.txt", "type": "file", "size_bytes": 300},
            {"path": "new/a.bin", "type": "file", "size_bytes": 50},
        ],
        initial_available=10000,
    )
    assert resp.status_code == 200
    body = resp.json()
    by = _by_index(body)
    # 条目 1：备份 +100 → 暂存 +300（峰值 400）→ 释放旧 100（提交 300）。
    assert by[1]["status"] == "OVERWRITE"
    assert by[1]["staging_peak_bytes"] == 400
    assert by[1]["committed_bytes"] == 300
    # 条目 2：新建暂存 +50（峰值 350），无旧文件释放，提交 350。
    assert by[2]["status"] == "CREATE"
    assert by[2]["staging_peak_bytes"] == 350
    assert by[2]["committed_bytes"] == 350
    assert body["summary"]["batch_peak_bytes"] == 400
    assert body["summary"]["committed_bytes"] == 350
    assert body["summary"]["first_exceed"] is None


def test_repeated_overwrite_backs_up_only_once():
    resp = _preview(
        existing=[{"path": "f.bin", "type": "file", "size_bytes": 100}],
        incoming=[
            {"path": "f.bin", "type": "file", "size_bytes": 300},  # 首次覆盖：备份 100
            {"path": "f.bin", "type": "file", "size_bytes": 20},   # 再次覆盖：不再备份
        ],
        initial_available=10000,
    )
    body = resp.json()
    by = _by_index(body)
    # 条目 1：备份 100、暂存 300 → 峰值 400，提交 300。
    assert by[1]["staging_peak_bytes"] == 400
    assert by[1]["committed_bytes"] == 300
    # 条目 2：无备份；暂存 20 → 300+20=320；释放旧 300 → 提交 20。
    assert by[2]["staging_peak_bytes"] == 320
    assert by[2]["committed_bytes"] == 20
    assert body["summary"]["batch_peak_bytes"] == 400
    assert body["summary"]["committed_bytes"] == 20


def test_created_then_overwritten_in_same_batch_is_not_backed_up():
    resp = _preview(
        existing=[],
        incoming=[
            {"path": "f.bin", "type": "file", "size_bytes": 100},  # 新建
            {"path": "f.bin", "type": "file", "size_bytes": 500},  # 覆盖本批新建
        ],
        initial_available=10000,
    )
    body = resp.json()
    by = _by_index(body)
    assert by[1]["staging_peak_bytes"] == 100
    assert by[1]["committed_bytes"] == 100
    # 无备份：暂存 500 → 600，释放旧 100 → 提交 500。
    assert by[2]["staging_peak_bytes"] == 600
    assert by[2]["committed_bytes"] == 500
    assert body["summary"]["batch_peak_bytes"] == 600
    assert body["summary"]["committed_bytes"] == 500
    assert body["summary"]["first_exceed"] is None


# ---------------------------------------------------------------------------
# 首次超额：阶段（BACKUP / STAGING）、条目下标，等于额度不算，超额继续预演
# ---------------------------------------------------------------------------


def test_first_exceed_during_backup_phase():
    resp = _preview(
        existing=[
            {"path": "a.bin", "type": "file", "size_bytes": 100},
            {"path": "big.bin", "type": "file", "size_bytes": 1000},
        ],
        incoming=[
            {"path": "a.bin", "type": "file", "size_bytes": 300},  # 峰值 400
            {"path": "big.bin", "type": "file", "size_bytes": 2000},  # 备份即 1300
        ],
        initial_available=1000,
    )
    body = resp.json()
    assert body["summary"]["first_exceed"] == {
        "phase": "BACKUP",
        "index": 2,
        "canonical_path": "big.bin",
    }
    # 即使超额，预演继续：整批峰值与提交占用照常给出。
    by = _by_index(body)
    assert by[2]["staging_peak_bytes"] == 3300
    assert by[2]["committed_bytes"] == 2300
    assert body["summary"]["batch_peak_bytes"] == 3300
    assert body["summary"]["committed_bytes"] == 2300


def test_first_exceed_during_staging_phase():
    resp = _preview(
        existing=[],
        incoming=[
            {"path": "f.bin", "type": "file", "size_bytes": 100},  # 新建
            {"path": "f.bin", "type": "file", "size_bytes": 500},  # 覆盖本批新建
        ],
        initial_available=100,
    )
    body = resp.json()
    by = _by_index(body)
    # 条目 1 暂存 100，恰好等于额度，不算超出。
    assert by[1]["staging_peak_bytes"] == 100
    # 条目 2 无备份，暂存 600 > 100 → STAGING 首次超额。
    assert body["summary"]["first_exceed"] == {
        "phase": "STAGING",
        "index": 2,
        "canonical_path": "f.bin",
    }


def test_equal_to_quota_is_not_exceeding():
    resp = _preview(
        existing=[{"path": "f.bin", "type": "file", "size_bytes": 100}],
        incoming=[
            {"path": "f.bin", "type": "file", "size_bytes": 0},  # 备份 100 = 额度
        ],
        initial_available=100,
    )
    body = resp.json()
    assert body["summary"]["first_exceed"] is None
    assert body["summary"]["batch_peak_bytes"] == 100
    # 暂存 0、释放旧 100：备份 100 + 新文件 0 − 旧 100 = 净新增 0。
    assert body["summary"]["committed_bytes"] == 0


def test_backup_exceed_takes_precedence_over_staging():
    # 备份 600 已超额（额度 500），即使同一暂存点也更高，首次超额阶段为 BACKUP。
    resp = _preview(
        existing=[{"path": "f.bin", "type": "file", "size_bytes": 600}],
        incoming=[{"path": "f.bin", "type": "file", "size_bytes": 5000}],
        initial_available=500,
    )
    body = resp.json()
    assert body["summary"]["first_exceed"]["phase"] == "BACKUP"
    assert body["summary"]["first_exceed"]["index"] == 1


# ---------------------------------------------------------------------------
# 目录与被阻挡条目不占空间；判定规则完全沿用 overlay-preview
# ---------------------------------------------------------------------------


def test_directories_and_blocked_entries_consume_no_space():
    resp = _preview(
        existing=[
            {"path": "docs", "type": "directory"},
            {"path": "block", "type": "file", "size_bytes": 10},
        ],
        incoming=[
            {"path": "docs", "type": "directory"},         # REUSE_DIRECTORY
            {"path": "newdir", "type": "directory"},      # CREATE 目录
            {"path": "newdir/a.txt", "type": "file", "size_bytes": 7},
            {"path": "block/x", "type": "file", "size_bytes": 9},  # FILE_ANCESTOR
            {"path": "docs", "type": "file", "size_bytes": 1},     # TYPE_CONFLICT
            {"path": "../evil", "type": "file", "size_bytes": 1},  # UNSAFE_PATH
        ],
        initial_available=10000,
    )
    body = resp.json()
    by = _by_index(body)
    assert by[1]["status"] == "REUSE_DIRECTORY"
    assert by[2]["status"] == "CREATE"
    assert by[4]["status"] == "BLOCKED"
    assert by[4]["blocked_reason"] == "FILE_ANCESTOR"
    assert by[5]["status"] == "BLOCKED"
    assert by[5]["blocked_reason"] == "TYPE_CONFLICT"
    assert by[6]["status"] == "BLOCKED"
    assert by[6]["blocked_reason"] == "UNSAFE_PATH"
    for index in (1, 2, 4, 5, 6):
        assert by[index]["staging_peak_bytes"] is None
        assert by[index]["committed_bytes"] is None
    # 仅条目 3 成功写入。
    assert by[3]["staging_peak_bytes"] == 7
    assert by[3]["committed_bytes"] == 7
    assert body["summary"]["batch_peak_bytes"] == 7
    assert body["summary"]["committed_bytes"] == 7
    assert body["summary"]["create"] == 2  # 一个目录 + 一个文件
    assert body["summary"]["reuse_directory"] == 1
    assert body["summary"]["blocked"] == 3


def test_windows_same_key_overwrite_backs_up_once():
    # 大小写/段尾变体经 Windows 比较键视为同键，覆盖前备份一次。
    resp = _preview(
        existing=[{"path": "Docs/ReadMe.TXT", "type": "file", "size_bytes": 100}],
        incoming=[{"path": "docs/readme.txt", "type": "file", "size_bytes": 200}],
        initial_available=10000,
    )
    body = resp.json()
    entry = body["entries"][0]
    assert entry["status"] == "OVERWRITE"
    assert entry["staging_peak_bytes"] == 300
    assert entry["committed_bytes"] == 200


def test_overwrite_smaller_file_reduces_committed_but_peak_kept():
    resp = _preview(
        existing=[{"path": "f.bin", "type": "file", "size_bytes": 1000}],
        incoming=[
            {"path": "f.bin", "type": "file", "size_bytes": 10},
            {"path": "g.bin", "type": "file", "size_bytes": 50},
        ],
        initial_available=10000,
    )
    body = resp.json()
    by = _by_index(body)
    # 备份 1000、暂存 10 → 峰值 1010；释放旧 1000 → 提交 10（净新增：备份 1000 + 新 10 − 旧 1000）。
    assert by[1]["staging_peak_bytes"] == 1010
    assert by[1]["committed_bytes"] == 10
    # 新文件 50：暂存 10+50=60、提交 60；整批峰值仍为条目 1 的 1010。
    assert by[2]["staging_peak_bytes"] == 60
    assert by[2]["committed_bytes"] == 60
    assert body["summary"]["batch_peak_bytes"] == 1010
    assert body["summary"]["committed_bytes"] == 60


# ---------------------------------------------------------------------------
# 请求校验（422）
# ---------------------------------------------------------------------------


def test_incoming_file_requires_size_bytes():
    resp = _preview(
        existing=[],
        incoming=[{"path": "f.bin", "type": "file"}],
        initial_available=100,
    )
    assert resp.status_code == 422


def test_unsafe_incoming_file_still_requires_size_bytes():
    # schema 层先校验大小：不安全路径的文件同样必须声明非负 size_bytes。
    resp = _preview(
        existing=[],
        incoming=[{"path": "../evil", "type": "file"}],
        initial_available=100,
    )
    assert resp.status_code == 422


def test_directory_must_not_carry_size_bytes():
    resp = _preview(
        existing=[],
        incoming=[{"path": "d", "type": "directory", "size_bytes": 1}],
        initial_available=100,
    )
    assert resp.status_code == 422


def test_negative_size_bytes_rejected():
    resp = _preview(
        existing=[],
        incoming=[{"path": "f.bin", "type": "file", "size_bytes": -1}],
        initial_available=100,
    )
    assert resp.status_code == 422


def test_existing_file_requires_size_bytes():
    resp = _preview(
        existing=[{"path": "f.bin", "type": "file"}],
        incoming=[{"path": "g.bin", "type": "file", "size_bytes": 1}],
        initial_available=100,
    )
    assert resp.status_code == 422


def test_existing_unsafe_path_rejected():
    resp = _preview(
        existing=[{"path": "../evil", "type": "file", "size_bytes": 1}],
        incoming=[{"path": "g.bin", "type": "file", "size_bytes": 1}],
        initial_available=100,
    )
    assert resp.status_code == 422


def test_existing_duplicate_key_rejected():
    resp = _preview(
        existing=[
            {"path": "a.txt", "type": "file", "size_bytes": 1},
            {"path": "A.TXT", "type": "file", "size_bytes": 2},
        ],
        incoming=[{"path": "g.bin", "type": "file", "size_bytes": 1}],
        initial_available=100,
    )
    assert resp.status_code == 422


def test_existing_file_ancestor_rejected():
    resp = _preview(
        existing=[
            {"path": "a/b", "type": "file", "size_bytes": 1},
            {"path": "a/b/c", "type": "file", "size_bytes": 2},
        ],
        incoming=[{"path": "g.bin", "type": "file", "size_bytes": 1}],
        initial_available=100,
    )
    assert resp.status_code == 422


def test_negative_initial_available_rejected():
    resp = _preview(
        existing=[],
        incoming=[{"path": "g.bin", "type": "file", "size_bytes": 1}],
        initial_available=-1,
    )
    assert resp.status_code == 422


def test_empty_incoming_rejected():
    resp = _preview(existing=[], incoming=[], initial_available=100)
    assert resp.status_code == 422
