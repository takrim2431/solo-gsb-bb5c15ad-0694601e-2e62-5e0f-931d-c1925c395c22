"""解压资源预算校验（resource_limits）的测试。"""

from fastapi.testclient import TestClient

from app.main import app
from app.resource import ratio_exceeded

client = TestClient(app)


def _call(entries, **extra):
    payload = {"entries": entries, **extra}
    return client.post("/api/v1/preflight", json=payload)


def _by_index(resp):
    return {e["index"]: e for e in resp["entries"]}


# ---------------------------------------------------------------------------
# 单文件解压大小上限
# ---------------------------------------------------------------------------


def test_file_size_limit_exceeded_marks_entry_unsafe():
    entries = [
        {"path": "big.bin", "type": "file", "uncompressed_bytes": 2_000},
        {"path": "small.bin", "type": "file", "uncompressed_bytes": 1_000},
    ]
    resp = _call(
        entries, resource_limits={"max_file_uncompressed_bytes": 1_000}
    ).json()
    by = _by_index(resp)
    assert by[1]["safe"] is False
    codes_1 = [i["code"] for i in by[1]["issues"]]
    assert "FILE_SIZE_LIMIT_EXCEEDED" in codes_1
    # 等于上限不超限。
    assert by[2]["safe"] is True
    assert by[2]["issues"] == []
    summary = resp["summary"]
    assert summary["resource_limits_exceeded"] is True
    assert summary["declared_uncompressed_bytes"] == 3_000
    assert summary["declared_compressed_bytes"] == 0


def test_file_size_limit_not_exceeded_when_all_within_budget():
    entries = [
        {"path": "a.bin", "type": "file", "uncompressed_bytes": 10},
        {"path": "b.bin", "type": "file", "uncompressed_bytes": 20},
    ]
    resp = _call(
        entries, resource_limits={"max_file_uncompressed_bytes": 20}
    ).json()
    assert all(e["safe"] for e in resp["entries"])
    assert resp["summary"]["resource_limits_exceeded"] is False
    assert resp["summary"]["declared_uncompressed_bytes"] == 30


# ---------------------------------------------------------------------------
# 整批解压总量上限
# ---------------------------------------------------------------------------


def test_total_limit_exceeded_is_batch_level_only():
    entries = [
        {"path": "a.bin", "type": "file", "uncompressed_bytes": 60},
        {"path": "b.bin", "type": "file", "uncompressed_bytes": 50},
    ]
    resp = _call(
        entries, resource_limits={"max_total_uncompressed_bytes": 100}
    ).json()
    # 总量超限是批次级状态，不归属单个条目。
    assert all(e["safe"] for e in resp["entries"])
    assert all(e["issues"] == [] for e in resp["entries"])
    summary = resp["summary"]
    assert summary["resource_limits_exceeded"] is True
    assert summary["declared_uncompressed_bytes"] == 110


def test_total_limit_boundary_equal_passes():
    entries = [
        {"path": "a.bin", "type": "file", "uncompressed_bytes": 60},
        {"path": "b.bin", "type": "file", "uncompressed_bytes": 40},
    ]
    resp = _call(
        entries, resource_limits={"max_total_uncompressed_bytes": 100}
    ).json()
    assert resp["summary"]["resource_limits_exceeded"] is False


# ---------------------------------------------------------------------------
# 最大压缩比（整数交叉乘法）
# ---------------------------------------------------------------------------


def test_compression_ratio_uses_integer_cross_multiplication():
    # 100/10 = 10，恰好等于上限 -> 通过；101/10 > 10 -> 超限。
    entries = [
        {
            "path": "exact.bin",
            "type": "file",
            "compressed_bytes": 10,
            "uncompressed_bytes": 100,
        },
        {
            "path": "over.bin",
            "type": "file",
            "compressed_bytes": 10,
            "uncompressed_bytes": 101,
        },
    ]
    resp = _call(entries, resource_limits={"max_compression_ratio": 10}).json()
    by = _by_index(resp)
    assert by[1]["safe"] is True
    assert by[2]["safe"] is False
    codes_2 = [i["code"] for i in by[2]["issues"]]
    assert "COMPRESSION_RATIO_EXCEEDED" in codes_2
    assert resp["summary"]["resource_limits_exceeded"] is True
    assert resp["summary"]["declared_compressed_bytes"] == 20
    assert resp["summary"]["declared_uncompressed_bytes"] == 201


def test_compression_ratio_zero_compressed_positive_uncompressed_exceeds():
    entries = [
        {
            "path": "bomb.bin",
            "type": "file",
            "compressed_bytes": 0,
            "uncompressed_bytes": 1,
        },
    ]
    resp = _call(entries, resource_limits={"max_compression_ratio": 100}).json()
    by = _by_index(resp)
    assert by[1]["safe"] is False
    assert "COMPRESSION_RATIO_EXCEEDED" in [
        i["code"] for i in by[1]["issues"]
    ]
    assert resp["summary"]["resource_limits_exceeded"] is True


def test_compression_ratio_both_zero_passes():
    entries = [
        {
            "path": "empty.bin",
            "type": "file",
            "compressed_bytes": 0,
            "uncompressed_bytes": 0,
        },
    ]
    resp = _call(entries, resource_limits={"max_compression_ratio": 100}).json()
    by = _by_index(resp)
    assert by[1]["safe"] is True
    assert by[1]["issues"] == []
    assert resp["summary"]["resource_limits_exceeded"] is False


def test_ratio_exceeded_unit():
    # 交叉乘法：uncompressed > max_ratio * compressed。
    assert ratio_exceeded(100, 10, 10) is False
    assert ratio_exceeded(101, 10, 10) is True
    assert ratio_exceeded(0, 0, 10) is False
    assert ratio_exceeded(1, 0, 10) is True
    assert ratio_exceeded(0, 5, 10) is False


# ---------------------------------------------------------------------------
# 组合限制与摘要
# ---------------------------------------------------------------------------


def test_combined_limits_and_summary_totals():
    entries = [
        {
            "path": "a.bin",
            "type": "file",
            "compressed_bytes": 10,
            "uncompressed_bytes": 50,
        },
        {
            "path": "b.bin",
            "type": "file",
            "compressed_bytes": 10,
            "uncompressed_bytes": 5000,
        },
        {"path": "dir", "type": "directory"},
    ]
    resp = _call(
        entries,
        resource_limits={
            "max_file_uncompressed_bytes": 10_000,
            "max_total_uncompressed_bytes": 4_000,
            "max_compression_ratio": 100,
        },
    ).json()
    by = _by_index(resp)
    # #1 未超限；#2 压缩比超限（5000 > 100*10）；整批总量 5050 > 4000。
    assert by[1]["safe"] is True
    assert by[2]["safe"] is False
    assert "COMPRESSION_RATIO_EXCEEDED" in [i["code"] for i in by[2]["issues"]]
    summary = resp["summary"]
    assert summary["declared_compressed_bytes"] == 20
    assert summary["declared_uncompressed_bytes"] == 5050
    assert summary["resource_limits_exceeded"] is True
    # 目录不参与大小声明，照常安全。
    assert by[3]["safe"] is True


def test_resource_limits_exceeded_file_drops_out_of_collision_group():
    entries = [
        {"path": "a.txt", "type": "file", "uncompressed_bytes": 5},
        {"path": "A.TXT", "type": "file", "uncompressed_bytes": 9_999},
    ]
    resp = _call(
        entries, resource_limits={"max_file_uncompressed_bytes": 100}
    ).json()
    by = _by_index(resp)
    # 超限文件标记为不安全，退出碰撞分组。
    assert by[1]["safe"] is True
    assert by[1]["collision_group"] is None
    assert by[2]["safe"] is False
    assert by[2]["collision_group"] is None
    assert resp["summary"]["collisions"] == 0


def test_repair_skips_resource_limited_files():
    entries = [
        {"path": "a.txt", "type": "file", "uncompressed_bytes": 5},
        {"path": "A.TXT", "type": "file", "uncompressed_bytes": 9_999},
    ]
    resp = _call(
        entries,
        repair=True,
        resource_limits={"max_file_uncompressed_bytes": 100},
    ).json()
    plan = {e["index"]: e for e in resp["repair_plan"]["entries"]}
    assert plan[1]["skipped"] is False
    assert plan[1]["repaired_path"] == "a.txt"
    assert plan[2]["skipped"] is True
    assert plan[2]["reasons"] == ["UNSAFE"]
    assert resp["repair_plan"]["collisions_remaining"] == 0


# ---------------------------------------------------------------------------
# 422 校验
# ---------------------------------------------------------------------------


def test_directory_with_sizes_rejected_422():
    r = _call([{"path": "d", "type": "directory", "uncompressed_bytes": 1}])
    assert r.status_code == 422
    r = _call([{"path": "d", "type": "directory", "compressed_bytes": 1}])
    assert r.status_code == 422


def test_symlink_with_sizes_rejected_422():
    r = _call(
        [
            {
                "path": "lnk",
                "type": "symlink",
                "link_target": "x",
                "uncompressed_bytes": 1,
            }
        ]
    )
    assert r.status_code == 422


def test_negative_sizes_rejected_422():
    r = _call([{"path": "f", "type": "file", "uncompressed_bytes": -1}])
    assert r.status_code == 422
    r = _call([{"path": "f", "type": "file", "compressed_bytes": -5}])
    assert r.status_code == 422


def test_missing_uncompressed_bytes_with_size_limits_422():
    entries = [{"path": "f", "type": "file", "compressed_bytes": 1}]
    r = _call(entries, resource_limits={"max_file_uncompressed_bytes": 10})
    assert r.status_code == 422
    r = _call(entries, resource_limits={"max_total_uncompressed_bytes": 10})
    assert r.status_code == 422


def test_missing_sizes_with_ratio_limit_422():
    # 缺 compressed_bytes。
    r = _call(
        [{"path": "f", "type": "file", "uncompressed_bytes": 1}],
        resource_limits={"max_compression_ratio": 10},
    )
    assert r.status_code == 422
    # 缺 uncompressed_bytes。
    r = _call(
        [{"path": "f", "type": "file", "compressed_bytes": 1}],
        resource_limits={"max_compression_ratio": 10},
    )
    assert r.status_code == 422
    # 两者都缺。
    r = _call(
        [{"path": "f", "type": "file"}],
        resource_limits={"max_compression_ratio": 10},
    )
    assert r.status_code == 422


def test_ratio_limit_must_be_at_least_one():
    r = _call(
        [
            {
                "path": "f",
                "type": "file",
                "compressed_bytes": 1,
                "uncompressed_bytes": 1,
            }
        ],
        resource_limits={"max_compression_ratio": 0},
    )
    assert r.status_code == 422


def test_directories_and_symlinks_need_no_sizes_when_limits_enabled():
    entries = [
        {"path": "dir", "type": "directory"},
        {"path": "lnk", "type": "symlink", "link_target": "dir"},
        {"path": "f", "type": "file", "uncompressed_bytes": 1},
    ]
    r = _call(entries, resource_limits={"max_file_uncompressed_bytes": 10})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 兼容性
# ---------------------------------------------------------------------------


def test_no_resource_limits_keeps_response_compatible():
    entries = [
        {"path": "a.txt", "type": "file"},
        {"path": "A.TXT", "type": "file"},
    ]
    resp = _call(entries).json()
    assert "repair_plan" not in resp
    summary = resp["summary"]
    assert summary["collisions"] == 1
    # 新增摘要字段以零值出现，不影响既有字段。
    assert summary["declared_compressed_bytes"] == 0
    assert summary["declared_uncompressed_bytes"] == 0
    assert summary["resource_limits_exceeded"] is False


def test_sizes_declared_without_limits_are_summed_but_not_checked():
    entries = [
        {
            "path": "a.bin",
            "type": "file",
            "compressed_bytes": 3,
            "uncompressed_bytes": 10**9,
        },
        {"path": "b.bin", "type": "file", "uncompressed_bytes": 7},
    ]
    resp = _call(entries).json()
    by = _by_index(resp)
    assert by[1]["safe"] is True
    assert by[1]["issues"] == []
    summary = resp["summary"]
    assert summary["declared_compressed_bytes"] == 3
    assert summary["declared_uncompressed_bytes"] == 10**9 + 7
    assert summary["resource_limits_exceeded"] is False


def test_empty_resource_limits_object_is_noop():
    entries = [{"path": "f", "type": "file", "uncompressed_bytes": 10**9}]
    resp = _call(entries, resource_limits={}).json()
    assert resp["entries"][0]["safe"] is True
    assert resp["summary"]["resource_limits_exceeded"] is False
    assert resp["summary"]["declared_uncompressed_bytes"] == 10**9
