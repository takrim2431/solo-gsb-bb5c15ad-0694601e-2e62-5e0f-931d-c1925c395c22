"""manifest-audit：约定清单 vs 交付清单核对测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SHA1 = "a" * 64
SHA2 = "b" * 64
SHA3 = "c" * 64


def _post(payload):
    return client.post("/api/v1/manifest-audit", json=payload)


def _audit(agreed, delivered):
    r = _post({"agreed": agreed, "delivered": delivered})
    assert r.status_code == 200, r.text
    return r.json()


def _file(path, sha=SHA1):
    return {"path": path, "type": "file", "sha256": sha}


def _dir(path):
    return {"path": path, "type": "directory"}


def _keys(findings):
    return [f["comparison_key"] for f in findings]


def _blocked_keys(findings):
    return [f["blocked"]["comparison_key"] for f in findings]


# ---------------------------------------------------------------------------
# 基本核对：缺失 / 额外 / 一致 / 摘要不符 / 类型不符
# ---------------------------------------------------------------------------


def test_missing_extra_matched_and_sorting():
    body = _audit(
        agreed=[
            _file("b/missing.txt"),
            _file("keep.txt"),
            _dir("docs"),
        ],
        delivered=[
            _file("keep.txt"),
            _dir("docs"),
            _file("extra.bin"),
        ],
    )
    assert _keys(body["missing"]) == ["b/missing.txt"]
    assert _keys(body["extra"]) == ["extra.bin"]
    assert _keys(body["matched"]) == ["docs", "keep.txt"]
    assert body["type_mismatches"] == []
    assert body["digest_mismatches"] == []
    assert body["hierarchy_blocked"] == []
    assert body["ambiguous_keys"] == []
    assert body["summary"] == {
        "agreed_total": 3,
        "delivered_total": 3,
        "missing": 1,
        "extra": 1,
        "type_mismatches": 0,
        "digest_mismatches": 0,
        "matched": 2,
        "hierarchy_blocked": 0,
        "ambiguous_keys": 0,
        "ambiguous_entries": 0,
    }


def test_digest_mismatch_only_for_files_case_insensitive():
    # 大写十六进制与小写等价；不同值即摘要不符。
    body = _audit(
        agreed=[_file("a.txt", SHA1), _dir("d")],
        delivered=[_file("a.txt", SHA2), _dir("d")],
    )
    assert _keys(body["digest_mismatches"]) == ["a.txt"]
    mismatch = body["digest_mismatches"][0]
    assert mismatch["status"] == "DIGEST_MISMATCH"
    assert mismatch["agreed"]["sha256"] == SHA1
    assert mismatch["delivered"]["sha256"] == SHA2
    # 目录同键永远视为一致（不比较摘要）。
    assert _keys(body["matched"]) == ["d"]

    body = _audit(
        agreed=[_file("a.txt", SHA1)],
        delivered=[_file("a.txt", SHA1.upper())],
    )
    assert _keys(body["matched"]) == ["a.txt"]
    assert body["matched"][0]["delivered"]["sha256"] == SHA1


def test_type_mismatch():
    body = _audit(
        agreed=[_dir("a"), _file("b", SHA1)],
        delivered=[_file("a", SHA2), _dir("b")],
    )
    assert _keys(body["type_mismatches"]) == ["a", "b"]
    assert body["type_mismatches"][0]["agreed"]["type"] == "directory"
    assert body["type_mismatches"][0]["delivered"]["type"] == "file"
    # 类型不符时两侧摘要仍原样给出，供展示。
    assert body["type_mismatches"][0]["delivered"]["sha256"] == SHA2
    assert body["type_mismatches"][1]["agreed"]["sha256"] == SHA1
    assert body["matched"] == []


# ---------------------------------------------------------------------------
# Windows 规范化：两侧同键匹配，保留原下标与规范路径
# ---------------------------------------------------------------------------


def test_windows_normalization_pairs_across_sides():
    body = _audit(
        agreed=[_file("Docs\\ReadMe.TXT", SHA1)],
        delivered=[_file("docs/readme.txt", SHA1)],
    )
    assert _keys(body["matched"]) == ["docs/readme.txt"]
    pair = body["matched"][0]
    # 约定侧保留输入大小写用于展示，比较键折叠。
    assert pair["agreed"]["canonical_path"] == "Docs/ReadMe.TXT"
    assert pair["agreed"]["original_path"] == "Docs\\ReadMe.TXT"
    assert pair["agreed"]["comparison_key"] == "docs/readme.txt"
    assert pair["delivered"]["canonical_path"] == "docs/readme.txt"
    # 两侧条目清单保留下标（从 1 开始）。
    assert [e["index"] for e in body["agreed_entries"]] == [1]
    assert [e["index"] for e in body["delivered_entries"]] == [1]


# ---------------------------------------------------------------------------
# 层级阻挡：一侧文件占据另一侧条目的祖先路径
# ---------------------------------------------------------------------------


def test_delivered_file_blocks_agreed_descendants():
    body = _audit(
        agreed=[
            _file("a/b/c.txt"),
            _file("a/b/d.txt"),
        ],
        delivered=[
            _file("a", SHA2),
            _file("a/b", SHA3),
        ],
    )
    # 后代归为层级阻挡，而不是普通缺失。
    assert body["missing"] == []
    blocked = body["hierarchy_blocked"]
    assert _blocked_keys(blocked) == ["a/b/c.txt", "a/b/d.txt"]

    first = blocked[0]
    assert first["blocked"]["side"] == "agreed"
    assert first["blocked"]["index"] == 1
    # 阻挡文件由近及远（深度升序）：a/b 在前、a 在后。
    assert [
        (bl["side"], bl["index"], bl["canonical_path"])
        for bl in first["blockers"]
    ] == [
        ("delivered", 2, "a/b"),
        ("delivered", 1, "a"),
    ]
    # 阻挡文件自身按其自身键核对：约定侧无 a、a/b → 额外（而非消失在阻挡清单里）。
    assert _keys(body["extra"]) == ["a", "a/b"]
    assert body["summary"]["hierarchy_blocked"] == 2


def test_agreed_file_blocks_delivered_descendant():
    body = _audit(
        agreed=[_file("x/y", SHA1)],
        delivered=[_dir("x/y/z"), _file("x/y/z/deep.bin", SHA1)],
    )
    blocked = body["hierarchy_blocked"]
    assert _blocked_keys(blocked) == ["x/y/z", "x/y/z/deep.bin"]
    assert {b["blocked"]["side"] for b in blocked} == {"delivered"}
    # 目录后代也可被阻挡；阻挡文件本身为缺失。
    assert _keys(body["missing"]) == ["x/y"]
    assert all(
        bl["canonical_path"] == "x/y"
        for item in blocked
        for bl in item["blockers"]
    )


def test_directory_ancestor_is_not_blocking():
    # 一侧目录占据另一侧条目的祖先属于正常布局，不算阻挡。
    body = _audit(
        agreed=[_dir("a"), _file("a/f.txt", SHA1)],
        delivered=[_dir("a"), _file("a/f.txt", SHA1)],
    )
    assert body["hierarchy_blocked"] == []
    assert _keys(body["matched"]) == ["a", "a/f.txt"]


def test_same_side_file_ancestor_is_not_cross_side_blocking():
    # 阻挡只发生在两侧之间；单侧内部文件祖先不作为拒绝也不作为跨侧阻挡。
    body = _audit(
        agreed=[_file("a/b", SHA1), _file("a/b/c.txt", SHA1)],
        delivered=[_file("other.txt", SHA1)],
    )
    assert body["hierarchy_blocked"] == []
    assert _keys(body["missing"]) == ["a/b", "a/b/c.txt"]
    assert _keys(body["extra"]) == ["other.txt"]


# ---------------------------------------------------------------------------
# 单侧同键重复：整键歧义，不任意配对
# ---------------------------------------------------------------------------


def test_delivered_duplicate_key_is_ambiguous():
    body = _audit(
        agreed=[_file("dup/x", SHA1)],
        delivered=[
            _file("dup/x", SHA1),
            _file("dup/X", SHA2),  # 大小写折叠后同键
        ],
    )
    assert body["ambiguous_keys"][0]["comparison_key"] == "dup/x"
    amb = body["ambiguous_keys"][0]
    assert [m["index"] for m in amb["agreed"]] == [1]
    assert [m["index"] for m in amb["delivered"]] == [1, 2]
    # 歧义键不与约定侧任意配对：不进 matched/mismatch/missing/extra。
    for field in (
        "missing",
        "extra",
        "type_mismatches",
        "digest_mismatches",
        "matched",
        "hierarchy_blocked",
    ):
        assert body[field] == []
    assert body["summary"]["ambiguous_keys"] == 1
    assert body["summary"]["ambiguous_entries"] == 3


def test_agreed_duplicate_key_is_ambiguous():
    body = _audit(
        agreed=[_file("k", SHA1), _file("K ", SHA2)],  # 段尾空格裁剪后同键
        delivered=[_file("k", SHA1)],
    )
    amb = body["ambiguous_keys"][0]
    assert amb["comparison_key"] == "k"
    assert [m["index"] for m in amb["agreed"]] == [1, 2]
    assert [m["index"] for m in amb["delivered"]] == [1]


def test_both_sides_duplicate_same_key_is_one_ambiguous_key():
    body = _audit(
        agreed=[_file("k", SHA1), _file("k", SHA2)],
        delivered=[_file("k", SHA3), _file("k", SHA1)],
    )
    assert len(body["ambiguous_keys"]) == 1
    amb = body["ambiguous_keys"][0]
    assert len(amb["agreed"]) == 2
    assert len(amb["delivered"]) == 2
    assert body["summary"]["ambiguous_entries"] == 4


def test_ambiguous_files_still_block_opposite_descendants():
    # 歧义键自身不配对，但其文件仍是一侧文件，会阻挡另一侧后代。
    body = _audit(
        agreed=[_file("a/b/c.txt", SHA1)],
        delivered=[
            _file("a/b", SHA1),
            _file("a/b", SHA2),
        ],
    )
    assert len(body["ambiguous_keys"]) == 1
    blocked = body["hierarchy_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["blocked"]["comparison_key"] == "a/b/c.txt"
    # 两个同键文件都列为阻挡文件（按下标稳定排序）。
    assert [bl["index"] for bl in blocked[0]["blockers"]] == [1, 2]


def test_ambiguous_key_sorted_by_comparison_key():
    body = _audit(
        agreed=[
            _file("z/t", SHA1),
            _file("z/t", SHA2),
            _file("a/t", SHA1),
            _file("a/t", SHA2),
        ],
        delivered=[],
    )
    assert _keys(body["ambiguous_keys"]) == ["a/t", "z/t"]


# ---------------------------------------------------------------------------
# 条目回显与稳定排序
# ---------------------------------------------------------------------------


def test_entries_echo_keep_side_order():
    body = _audit(
        agreed=[_file("z.txt", SHA1), _dir("d")],
        delivered=[_file("a.txt", SHA1)],
    )
    assert [
        (e["side"], e["index"], e["canonical_path"])
        for e in body["agreed_entries"]
    ] == [
        ("agreed", 1, "z.txt"),
        ("agreed", 2, "d"),
    ]
    assert [
        (e["side"], e["index"]) for e in body["delivered_entries"]
    ] == [("delivered", 1)]
    # 文件条目摘要小写回显，目录为 null。
    assert body["agreed_entries"][0]["sha256"] == SHA1
    assert body["agreed_entries"][1]["sha256"] is None


def test_pair_lists_sorted_by_comparison_key():
    body = _audit(
        agreed=[
            _file("z.txt", SHA1),
            _file("M.txt", SHA1),
            _file("a.txt", SHA2),
        ],
        delivered=[
            _file("a.txt", SHA1),
            _file("m.txt", SHA2),
            _file("n.txt", SHA1),
        ],
    )
    # z 缺失、n 额外、a/m 摘要不符；全部按比较键升序。
    assert _keys(body["missing"]) == ["z.txt"]
    assert _keys(body["extra"]) == ["n.txt"]
    assert _keys(body["digest_mismatches"]) == ["a.txt", "m.txt"]


# ---------------------------------------------------------------------------
# 空清单
# ---------------------------------------------------------------------------


def test_empty_manifests():
    body = _audit([], [])
    for field in (
        "agreed_entries",
        "delivered_entries",
        "missing",
        "extra",
        "type_mismatches",
        "digest_mismatches",
        "matched",
        "hierarchy_blocked",
        "ambiguous_keys",
    ):
        assert body[field] == []
    assert body["summary"]["agreed_total"] == 0
    assert body["summary"]["delivered_total"] == 0


def test_fields_may_be_omitted():
    r = _post({})
    assert r.status_code == 200
    assert r.json()["summary"] == {
        "agreed_total": 0,
        "delivered_total": 0,
        "missing": 0,
        "extra": 0,
        "type_mismatches": 0,
        "digest_mismatches": 0,
        "matched": 0,
        "hierarchy_blocked": 0,
        "ambiguous_keys": 0,
        "ambiguous_entries": 0,
    }


# ---------------------------------------------------------------------------
# 422 校验
# ---------------------------------------------------------------------------


def test_unsafe_paths_rejected_422():
    for label, side in (
        ("约定清单", "agreed"),
        ("交付清单", "delivered"),
    ):
        payload = {"agreed": [], "delivered": []}
        payload[side] = [_file("../escape", SHA1)]
        r = _post(payload)
        assert r.status_code == 422
        assert label in r.text
        assert "TRAVERSAL_ESCAPE" in r.text

        payload = {"agreed": [], "delivered": []}
        payload[side] = [_file("C:\\Windows\\x", SHA1)]
        assert _post(payload).status_code == 422


def test_sha256_required_for_files_422():
    r = _post({"agreed": [{"path": "a", "type": "file"}], "delivered": []})
    assert r.status_code == 422
    assert "sha256" in r.text

    r = _post(
        {"agreed": [], "delivered": [{"path": "a", "type": "file", "sha256": "00"}]}
    )
    assert r.status_code == 422

    r = _post(
        {
            "agreed": [],
            "delivered": [
                {"path": "a", "type": "file", "sha256": "g" * 64}
            ],
        }
    )
    assert r.status_code == 422


def test_directory_must_not_carry_sha256_422():
    r = _post({"agreed": [_dir("d") | {"sha256": SHA1}], "delivered": []})
    assert r.status_code == 422


def test_unsupported_type_rejected_422():
    r = _post(
        {"agreed": [{"path": "l", "type": "symlink", "sha256": SHA1}], "delivered": []}
    )
    assert r.status_code == 422


def test_duplicate_keys_and_file_ancestors_are_not_rejected():
    # 与 overlay/mirror 不同：本接口不拒绝同键重复与单侧文件祖先，
    # 它们分别进入歧义与层级阻挡结论。
    r = _post(
        {
            "agreed": [_file("a/b", SHA1), _file("a/b", SHA2)],
            "delivered": [
                _file("a/b", SHA1),
                _file("a/b/c.txt", SHA1),
            ],
        }
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["ambiguous_keys"]) == 1
    assert body["summary"]["hierarchy_blocked"] == 1


# ---------------------------------------------------------------------------
# 既有接口保持不变
# ---------------------------------------------------------------------------


def test_other_endpoints_untouched():
    assert client.get("/health").json()["status"] == "ok"
    root = client.get("/").json()
    assert root["manifest_audit"] == "POST /api/v1/manifest-audit"
    r = client.post(
        "/api/v1/overlay-preview",
        json={
            "existing": [_file("a.txt", SHA1)],
            "incoming": [_file("a.txt", SHA1)],
        },
    )
    assert r.status_code == 200
    assert r.json()["entries"][0]["status"] == "OVERWRITE"
