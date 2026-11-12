"""拉链构建与查询的单元测试。

覆盖题面要求的三类流水特殊情况：
1. 同一会员同一秒多次变更（落库顺序靠后的生效）
2. 与当时资料完全一致的系统误报（不产生新版本）
3. 早于会员首次出现时间的补录（拉链向前延伸）
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import dim_builder
import query

REPO_ROOT = Path(__file__).parent

# 手工构造的流水，一个会员覆盖一种情况：
#   M001 正常两次变更
#   M002 同一秒两次变更，后落库的生效
#   M003 误报（与上一版本完全一致）
#   M004 补录（早于首条记录，且资料不同）
#   M005 补录且与首条记录资料一致（合并，不产生新版本）
CHANGES_CSV = """change_time,member_id,level,city,phone
2026-10-01 08:00:00,M001,普通,北京,13000000001
2026-10-10 08:00:00,M001,金卡,上海,13000000002
2026-10-01 08:00:00,M002,普通,北京,13000000003
2026-10-05 08:00:00,M002,银卡,广州,13000000004
2026-10-05 08:00:00,M002,银卡,深圳,13000000005
2026-10-01 08:00:00,M003,普通,北京,13000000006
2026-10-06 08:00:00,M003,普通,北京,13000000006
2026-10-01 08:00:00,M004,金卡,北京,13000000007
2026-09-15 08:00:00,M004,普通,杭州,13000000008
2026-10-01 08:00:00,M005,金卡,北京,13000000009
2026-09-15 08:00:00,M005,金卡,北京,13000000009
"""

EVENTS_CSV = """event_id,member_id,event_time,order_amount
E1,M001,2026-10-05 08:00:00,100.00
E2,M001,2026-10-15 08:00:00,200.00
E3,M004,2026-09-20 08:00:00,50.00
E4,M002,2026-09-01 08:00:00,30.00
"""


@pytest.fixture()
def dim():
    changes = dim_builder.load_changes_from_string(CHANGES_CSV)
    return dim_builder.build_dim(changes)


@pytest.fixture()
def db(tmp_path, dim):
    db_path = tmp_path / "dim.db"
    dim_builder.write_db(dim, str(db_path))
    events_path = tmp_path / "events.csv"
    events_path.write_text(EVENTS_CSV, encoding="utf-8")
    return str(db_path), str(events_path)


def rows(dim, member_id):
    return dim[dim.member_id == member_id].reset_index(drop=True)


def test_normal_two_versions(dim):
    r = rows(dim, "M001")
    assert len(r) == 2
    assert r.loc[0, "effective_to"] == "2026-10-10 08:00:00"
    assert r.loc[0, "is_current"] == 0
    assert pd.isna(r.loc[1, "effective_to"]) and r.loc[1, "is_current"] == 1


def test_same_second_change_last_file_order_wins(dim):
    r = rows(dim, "M002")
    assert len(r) == 2
    # 同一秒的两次变更只保留落库靠后的（深圳），不产生零长度版本
    assert r.loc[1, "city"] == "深圳"
    assert r.loc[1, "phone"] == "13000000005"
    assert r.loc[0, "effective_to"] == "2026-10-05 08:00:00"


def test_false_alarm_no_new_version(dim):
    r = rows(dim, "M003")
    assert len(r) == 1
    assert r.loc[0, "is_current"] == 1


def test_backfill_extends_history(dim):
    r = rows(dim, "M004")
    assert len(r) == 2
    assert r.loc[0, "effective_from"] == "2026-09-15 08:00:00"
    assert r.loc[0, "level"] == "普通"
    assert r.loc[0, "effective_to"] == "2026-10-01 08:00:00"


def test_backfill_identical_to_first_record_merges(dim):
    r = rows(dim, "M005")
    assert len(r) == 1
    assert r.loc[0, "effective_from"] == "2026-09-15 08:00:00"


def test_checks_all_pass(dim):
    for name, ok, violations in dim_builder.run_checks(dim):
        assert ok, f"{name} 存在违例:\n{violations}"


def test_check_detects_overlap(dim):
    bad = dim.copy()
    # M001 两行：第一行失效时间推迟到下一版本生效之后（重叠+遗漏），
    # 第二行是当前版本却被填上失效时间（当前标记违例）
    bad.loc[bad.member_id == "M001", "effective_to"] = [
        "2026-10-20 08:00:00",
        "2026-11-01 08:00:00",
    ]
    results = {name: (ok, v) for name, ok, v in dim_builder.run_checks(bad)}
    assert not results["时间段无重叠"][0]
    assert not results["时间段无遗漏"][0]
    assert not results["当前标记唯一"][0]


def test_snapshot_at(db):
    db_path, _ = db
    with sqlite3.connect(db_path) as con:
        snap = query.snapshot_at(con, "2026-10-05 08:00:00")
    got = dict(zip(snap.member_id, snap.city))
    # M002 同秒变更在边界时刻生效的是落库靠后的版本
    assert got["M002"] == "深圳"
    assert got["M001"] == "北京"
    # M004 补录后 10-05 已是金卡版本
    assert got["M004"] == "北京"


def test_snapshot_before_first_version_excludes_member(db):
    db_path, _ = db
    with sqlite3.connect(db_path) as con:
        snap = query.snapshot_at(con, "2026-09-01 00:00:00")
    assert snap.empty


def test_current_versions(db):
    db_path, _ = db
    with sqlite3.connect(db_path) as con:
        cur = query.current_versions(con)
    assert len(cur) == 5
    got = dict(zip(cur.member_id, cur.level))
    assert got["M001"] == "金卡"
    assert got["M003"] == "普通"


def test_event_join(db):
    db_path, events_path = db
    with sqlite3.connect(db_path) as con:
        joined = query.event_join(con, events_path)
    got = {r.event_id: r for r in joined.itertuples()}
    assert got["E1"].level == "普通"  # 变更前
    assert got["E2"].level == "金卡"  # 变更后
    assert got["E3"].city == "杭州"  # 补录版本覆盖的时间段
    assert pd.isna(got["E4"].level)  # 事件早于会员首个版本，维度留空


def test_stats_outputs(db, tmp_path):
    db_path, events_path = db
    stats_path = tmp_path / "stats.txt"
    with sqlite3.connect(db_path) as con:
        matrix, dist = query.write_stats(con, events_path, str(stats_path))
    assert {"month", "level_from", "level_to", "cnt"} == set(matrix.columns)
    assert {"caliber", "month", "level", "amount"} == set(dist.columns)
    assert set(dist.caliber) == {"当时等级", "当前等级"}
    # 两版口径的月度总额必须一致（同一批事件）
    total = dist.groupby(["caliber", "month"]).amount.sum()
    assert total.loc["当时等级"].equals(total.loc["当前等级"])
    text = stats_path.read_text(encoding="utf-8")
    assert "月度等级迁移矩阵" in text and "月度订单金额分布" in text


def test_real_data_build_and_checks(tmp_path):
    """对仓库样例数据端到端构建，四项自检必须全部通过。"""
    db_path = tmp_path / "dim.db"
    check_path = tmp_path / "check.txt"
    rc = dim_builder.main(
        [
            "--changes",
            str(REPO_ROOT / "data" / "changes.csv"),
            "--db",
            str(db_path),
            "--check",
            str(check_path),
        ]
    )
    assert rc == 0
    report = check_path.read_text(encoding="utf-8")
    assert report.count("结论：通过") == 4
    with sqlite3.connect(db_path) as con:
        n = con.execute("SELECT COUNT(*) FROM member_dim").fetchone()[0]
    assert n > 0


def test_query_cli_runs(tmp_path):
    """端到端：构建后三种口径的 CLI 都能正常输出 CSV。"""
    db_path = tmp_path / "dim.db"
    dim_builder.main(
        [
            "--changes",
            str(REPO_ROOT / "data" / "changes.csv"),
            "--db",
            str(db_path),
            "--check",
            str(tmp_path / "check.txt"),
        ]
    )
    base = [
        sys.executable,
        str(REPO_ROOT / "query.py"),
        "--db",
        str(db_path),
        "--events",
        str(REPO_ROOT / "data" / "events.csv"),
        "--stats",
        str(tmp_path / "stats.txt"),
    ]
    for mode in (["--at", "2026-10-15 12:00:00"], ["--event"], ["--current"]):
        proc = subprocess.run(base + mode, capture_output=True, text=True, check=True)
        assert proc.stdout.splitlines()[0].startswith(
            ("member_id,level,city,phone", "event_id,member_id")
        )
