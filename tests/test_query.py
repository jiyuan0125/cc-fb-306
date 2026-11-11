"""query.py 的单元测试：三种口径、半开区间边界、统计闭合性。"""

from __future__ import annotations

import pandas as pd
import pytest

import dim_builder as db
import query

CHANGES = [
    # M1: 补录 + 同时间戳两条（后者胜）+ 后续升级
    ("2026-09-10 09:00:00", "M1", "银卡", "上海", "13800001001"),
    ("2026-10-15 10:23:44", "M1", "金卡", "上海", "13800001001"),
    ("2026-10-15 10:23:44", "M1", "金卡", "北京", "13800001002"),
    ("2026-11-02 16:00:00", "M1", "白金", "北京", "13800001002"),
    # M2: 两条误报
    ("2026-09-15 14:00:00", "M2", "普通", "杭州", "13800003001"),
    ("2026-10-05 10:00:00", "M2", "普通", "杭州", "13800003001"),
    ("2026-10-25 18:00:00", "M2", "银卡", "杭州", "13800003001"),
    ("2026-11-01 12:00:00", "M2", "银卡", "杭州", "13800003001"),
    # M3: 单版本，首现晚于自己的部分事件
    ("2026-10-18 11:00:00", "M3", "金卡", "北京", "13800006001"),
]

EVENTS = [
    ("E1", "M1", "2026-10-15 10:23:43", "100.00"),  # 变更前一秒 -> 银卡上海
    ("E2", "M1", "2026-10-15 10:23:44", "200.50"),  # 变更当刻 -> 金卡北京（后者胜）
    ("E3", "M1", "2026-11-03 09:00:00", "300.00"),   # 白金
    ("E4", "M2", "2026-10-06 10:00:00", "50.00"),    # 误报后仍是普通
    ("E5", "M2", "2026-10-26 09:00:00", "70.25"),    # 银卡
    ("E6", "M3", "2026-09-22 10:11:00", "999.00"),   # 早于首资料 -> 维度留空
    ("E7", "M3", "2026-10-19 09:00:00", "1.00"),     # 金卡
]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dim")
    changes = pd.DataFrame(CHANGES, columns=["change_time", "member_id", "level", "city", "phone"])
    changes["ts"] = pd.to_datetime(changes["change_time"])
    dim, _ = db.build_dim(changes)
    text = db.format_dim(dim)
    db_path = tmp / "dim.db"
    db.write_sqlite(text, db_path)

    events_path = tmp / "events.csv"
    pd.DataFrame(EVENTS, columns=["event_id", "member_id", "event_time", "order_amount"]).to_csv(
        events_path, index=False
    )
    return db_path, events_path


def test_slice_at_half_open_boundary(built):
    db_path, _ = built
    before = query.slice_at(db_path, pd.Timestamp("2026-10-15 10:23:43"))
    at = query.slice_at(db_path, pd.Timestamp("2026-10-15 10:23:44"))
    m1_before = before[before.member_id == "M1"].iloc[0]
    m1_at = at[at.member_id == "M1"].iloc[0]
    assert (m1_before.level, m1_before.city) == ("银卡", "上海")
    assert (m1_at.level, m1_at.city) == ("金卡", "北京")


def test_slice_at_excludes_members_not_yet_born(built):
    db_path, _ = built
    early = query.slice_at(db_path, pd.Timestamp("2026-09-01 00:00:00"))
    assert early.empty


def test_slice_at_date_only_argument(built):
    db_path, _ = built
    # 仅给日期 -> 当天 00:00:00，M1 此时仍是银卡
    result = query.slice_at(db_path, query.parse_at("2026-10-15"))
    m1 = result[result.member_id == "M1"].iloc[0]
    assert m1.level == "银卡"


def test_current_versions(built):
    db_path, _ = built
    cur = query.current_versions(db_path).set_index("member_id")
    assert (cur.loc["M1", "level"], cur.loc["M1", "city"]) == ("白金", "北京")
    assert (cur.loc["M2", "level"], cur.loc["M2", "city"]) == ("银卡", "杭州")
    assert cur.loc["M3", "level"] == "金卡"
    assert len(cur) == 3


def test_join_events_keeps_row_order_and_blanks_early_events(built):
    db_path, events_path = built
    dim = query.load_dim(db_path)
    events = query.load_events(events_path)
    out = query.join_events(dim, events)
    # 原始事件行序
    assert out.event_id.tolist() == ["E1", "E2", "E3", "E4", "E5", "E6", "E7"]
    by_id = out.set_index("event_id")
    assert (by_id.loc["E1", "level"], by_id.loc["E1", "city"]) == ("银卡", "上海")
    assert (by_id.loc["E2", "level"], by_id.loc["E2", "city"], by_id.loc["E2", "phone"]) == (
        "金卡",
        "北京",
        "13800001002",
    )
    assert by_id.loc["E3", "level"] == "白金"
    assert by_id.loc["E4", "level"] == "普通"  # 误报未建版本，仍是普通
    assert by_id.loc["E5", "level"] == "银卡"
    # 早于首资料: 维度字段全部留空，事实字段保留
    assert (by_id.loc["E6", "level"], by_id.loc["E6", "city"], by_id.loc["E6", "phone"]) == ("", "", "")
    assert by_id.loc["E6", "order_amount"] == "999.00"
    assert by_id.loc["E7", "level"] == "金卡"


def test_migration_matrix(built):
    db_path, _ = built
    dim = query.load_dim(db_path)
    mat = query.migration_matrix(dim)
    # M1: 10-01 银卡, 11-01 金卡；M2: 10-01 普通, 11-01 银卡；M3 10-01 无版本不计入
    oct_ = mat[mat["月份"] == "2026-10"].set_index(["月初等级", "月末等级"])["人数"].to_dict()
    assert oct_ == {("银卡", "金卡"): 1, ("普通", "银卡"): 1}
    # 11 月: 全员在册；M1 月初金卡、11-02 升白金，M2 银卡不变，M3 金卡不变
    nov = mat[mat["月份"] == "2026-11"].set_index(["月初等级", "月末等级"])["人数"].to_dict()
    assert nov == {("金卡", "白金"): 1, ("金卡", "金卡"): 1, ("银卡", "银卡"): 1}
    # 9 月: 9-01 无任何会员有版本，不出矩阵
    assert "2026-09" not in mat["月份"].unique()


def test_amount_distribution_calibers_and_totals(built):
    db_path, events_path = built
    dim = query.load_dim(db_path)
    events = query.load_events(events_path)
    dist = query.amount_distribution(dim, events)
    total = events["amount"].sum()
    for caliber in (query.CALIBER_AS_OF, query.CALIBER_CURRENT):
        s = pd.to_numeric(dist[dist["口径"] == caliber]["金额合计"]).sum()
        assert abs(s - total) < 0.01
    # E6（9 月）无匹配版本 -> 按当时等级口径出现空等级行，金额 999.00
    blank = dist[(dist["口径"] == query.CALIBER_AS_OF) & (dist["等级"] == "")]
    assert len(blank) == 1
    assert pd.to_numeric(blank["金额合计"]).iloc[0] == pytest.approx(999.00, abs=0.001)
    # 按当前等级: M3 当前金卡，E6 的 999.00 归入 9 月金卡；E7 的 1.00 归入 10 月金卡
    cur_gold = dist[(dist["口径"] == query.CALIBER_CURRENT) & (dist["等级"] == "金卡")].set_index("月份")
    assert pd.to_numeric(cur_gold.loc["2026-09", "金额合计"]) == pytest.approx(999.00, abs=0.001)
    assert pd.to_numeric(cur_gold.loc["2026-10", "金额合计"]) == pytest.approx(1.00, abs=0.001)
    # 金额保留两位小数文本
    assert all("." in v and len(v.split(".")[1]) == 2 for v in dist["金额合计"])


def test_render_stats_has_two_sections(built):
    db_path, events_path = built
    dim = query.load_dim(db_path)
    events = query.load_events(events_path)
    report = query.render_stats(query.migration_matrix(dim), query.amount_distribution(dim, events))
    assert "月度等级迁移矩阵" in report
    assert "月度订单金额分布" in report
    # 两个 CSV 表头各出现一次（标题描述行不计）
    lines = report.splitlines()
    assert sum(1 for line in lines if line == "月份,月初等级,月末等级,人数") == 1
    assert sum(1 for line in lines if line == "月份,口径,等级,金额合计") == 1
