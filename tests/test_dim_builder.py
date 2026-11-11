"""dim_builder 的单元测试：同时间戳折叠、误报、补录、自检与落库。"""

from __future__ import annotations

import sqlite3

import pandas as pd

import dim_builder as db


def make_changes(rows):
    """rows: (change_time, member_id, level, city, phone) 元组列表，行序即落库顺序。"""
    frame = pd.DataFrame(rows, columns=["change_time", "member_id", "level", "city", "phone"])
    frame["ts"] = pd.to_datetime(frame["change_time"])
    return frame


def test_same_timestamp_later_row_wins():
    changes = make_changes(
        [
            ("2026-10-15 10:23:44", "M1", "金卡", "上海", "13800001001"),
            ("2026-10-15 10:23:44", "M1", "金卡", "北京", "13800001002"),
            ("2026-09-10 09:00:00", "M1", "银卡", "上海", "13800001001"),
        ]
    )
    dim, audit = db.build_dim(changes)
    assert audit["same_timestamp_collapsed"] == 1
    member = dim[dim.member_id == "M1"].sort_values("effective_from")
    # 同时间戳折叠为一条，加上 9 月补录版本，共 2 个版本
    assert len(member) == 2
    current = member.iloc[1]
    # 同时间戳取流水行序靠后者: 北京 + 新手机号
    assert (current.level, current.city, current.phone) == ("金卡", "北京", "13800001002")
    assert current.effective_from == pd.Timestamp("2026-10-15 10:23:44")
    assert pd.isna(current.effective_to)
    # 半开区间首尾相接
    assert member.iloc[0].effective_to == current.effective_from


def test_false_positive_does_not_create_version():
    changes = make_changes(
        [
            ("2026-09-15 14:00:00", "M1", "普通", "杭州", "13800003001"),
            ("2026-10-05 10:00:00", "M1", "普通", "杭州", "13800003001"),
            ("2026-10-25 18:00:00", "M1", "银卡", "杭州", "13800003001"),
            ("2026-11-01 12:00:00", "M1", "银卡", "杭州", "13800003001"),
        ]
    )
    dim, audit = db.build_dim(changes)
    assert len(audit["false_positives"]) == 2
    member = dim[dim.member_id == "M1"].sort_values("effective_from")
    assert len(member) == 2
    assert member.iloc[0].effective_from == pd.Timestamp("2026-09-15 14:00:00")
    assert member.iloc[0].effective_to == pd.Timestamp("2026-10-25 18:00:00")
    assert member.iloc[1].effective_from == pd.Timestamp("2026-10-25 18:00:00")


def test_phone_only_change_creates_version():
    changes = make_changes(
        [
            ("2026-10-01 09:00:00", "M1", "金卡", "北京", "13800000001"),
            ("2026-10-02 09:00:00", "M1", "金卡", "北京", "13800000002"),
        ]
    )
    dim, _ = db.build_dim(changes)
    assert len(dim[dim.member_id == "M1"]) == 2


def test_backfill_becomes_earlier_version():
    # 文件中先出现 10 月流水，9 月流水是补录；构建后 9 月版本应排在最前
    changes = make_changes(
        [
            ("2026-10-01 10:00:00", "M1", "普通", "成都", "13800004001"),
            ("2026-09-05 08:00:00", "M1", "银卡", "成都", "13800004001"),
        ]
    )
    dim, _ = db.build_dim(changes)
    member = dim[dim.member_id == "M1"].sort_values("effective_from")
    assert member.iloc[0].effective_from == pd.Timestamp("2026-09-05 08:00:00")
    assert member.iloc[0].level == "银卡"
    assert member.iloc[0].effective_to == pd.Timestamp("2026-10-01 10:00:00")


def test_same_timestamp_revert_to_prior_state_is_noop():
    # 同时间戳两条: 先改后又改回前序状态，净效果为无变化，不应产生新版本
    changes = make_changes(
        [
            ("2026-10-01 09:00:00", "M1", "普通", "北京", "13800000001"),
            ("2026-10-02 09:00:00", "M1", "金卡", "北京", "13800000001"),
            ("2026-10-02 09:00:00", "M1", "普通", "北京", "13800000001"),
        ]
    )
    dim, _ = db.build_dim(changes)
    member = dim[dim.member_id == "M1"].sort_values("effective_from")
    assert len(member) == 1
    assert member.iloc[0].level == "普通"


def test_chain_intervals_are_half_open_and_current_marked():
    changes = make_changes(
        [
            ("2026-09-01 00:00:00", "M1", "普通", "北京", "p1"),
            ("2026-10-01 00:00:00", "M1", "银卡", "北京", "p1"),
            ("2026-11-01 00:00:00", "M1", "金卡", "上海", "p2"),
        ]
    )
    dim, _ = db.build_dim(changes)
    member = dim[dim.member_id == "M1"].sort_values("effective_from")
    assert list(member.is_current) == [0, 0, 1]
    assert pd.isna(member.iloc[-1].effective_to)
    assert member.iloc[0].effective_to == member.iloc[1].effective_from
    assert member.iloc[1].effective_to == member.iloc[2].effective_from


def test_format_and_sqlite_roundtrip(tmp_path):
    changes = make_changes(
        [
            ("2026-10-01 09:00:00", "M1", "金卡", "北京", "13800000001"),
            ("2026-11-01 09:00:00", "M1", "白金", "北京", "13800000001"),
        ]
    )
    dim, _ = db.build_dim(changes)
    text = db.format_dim(dim)
    assert list(text.columns) == db.DIM_COLUMNS
    assert text.iloc[0]["effective_from"] == "2026-10-01 09:00:00"
    assert text.iloc[0]["effective_to"] == "2026-11-01 09:00:00"
    assert text.iloc[1]["effective_to"] is None
    assert text.iloc[1]["is_current"] == 1

    db_path = tmp_path / "dim.db"
    db.write_sqlite(text, db_path)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT member_id, effective_from, effective_to, is_current FROM {db.TABLE_NAME} ORDER BY effective_from"
        ).fetchall()
    assert rows == [
        ("M1", "2026-10-01 09:00:00", "2026-11-01 09:00:00", 0),
        ("M1", "2026-11-01 09:00:00", None, 1),
    ]


def test_run_checks_all_pass_on_clean_chain():
    changes = make_changes(
        [
            ("2026-09-01 00:00:00", "M1", "普通", "北京", "p1"),
            ("2026-10-01 00:00:00", "M1", "普通", "北京", "p1"),  # 误报
            ("2026-10-05 00:00:00", "M1", "银卡", "北京", "p1"),
            ("2026-09-02 00:00:00", "M2", "钻石", "上海", "p2"),
        ]
    )
    dim, _ = db.build_dim(changes)
    results = db.run_checks(changes, db.format_dim(dim))
    assert [r.name for r in results] == ["时间段无重叠", "时间段无遗漏", "当前标记唯一", "误报未建新版本"]
    assert all(r.passed for r in results)


def test_run_checks_detects_overlap_and_gap():
    changes = make_changes(
        [
            ("2026-09-01 00:00:00", "M1", "普通", "北京", "p1"),
            ("2026-10-01 00:00:00", "M1", "银卡", "北京", "p1"),
        ]
    )
    dim, _ = db.build_dim(changes)
    text = db.format_dim(dim)
    # 人为制造重叠: 把第一行的 effective_to 推后
    text.loc[0, "effective_to"] = "2026-10-02 00:00:00"
    results = db.run_checks(changes, text)
    overlap, gap, current, fp = results
    assert not overlap.passed and overlap.violations
    assert not gap.passed and gap.violations
    assert current.passed and fp.passed


def test_run_checks_detects_duplicate_current_and_false_positive_version():
    changes = make_changes(
        [
            ("2026-09-01 00:00:00", "M1", "普通", "北京", "p1"),
            ("2026-09-02 00:00:00", "M1", "普通", "北京", "p1"),  # 误报
        ]
    )
    dim, _ = db.build_dim(changes)
    text = db.format_dim(dim)
    # 人为把误报时间点加成一个多余的当前版本（原版本保留其 is_current=1）
    extra = pd.DataFrame(
        [
            {
                "member_id": "M1",
                "level": "普通",
                "city": "北京",
                "phone": "p1",
                "effective_from": "2026-09-02 00:00:00",
                "effective_to": None,
                "is_current": 1,
            }
        ]
    )
    tampered = pd.concat([text, extra], ignore_index=True)
    results = db.run_checks(changes, tampered)
    _overlap, _gap, current, fp = results
    assert not current.passed  # 出现两条 is_current=1
    assert not fp.passed  # 误报被建成了新版本
