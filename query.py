"""拉链维度表查询工具：三种口径 + 两项月度统计。

用法（结果以 CSV 打印到标准输出，带表头；统计写入 stats.txt）：
    python3 query.py --at "2026-10-15 12:00:00"   # 任意时点切片：该时刻每位会员的等级城市
    python3 query.py --event                       # 逐事件关联发生当时的版本，输出带维度的事实明细
    python3 query.py --current                     # 当前版本

每次运行都会基于 dim.db 重新生成 stats.txt：
    1. 月度等级迁移矩阵（每月月初等级 -> 月末等级的迁移计数）
    2. 月度订单金额分布（按当时等级 / 按当前等级两版口径）
"""

import argparse
import sqlite3
import sys

import pandas as pd

SNAPSHOT_SQL = """
SELECT member_id, level, city, phone
FROM member_dim
WHERE effective_from <= ? AND (effective_to IS NULL OR effective_to > ?)
ORDER BY member_id
"""

CURRENT_SQL = """
SELECT member_id, level, city, phone
FROM member_dim
WHERE is_current = 1
ORDER BY member_id
"""

ASOF_JOIN_SQL = """
SELECT e.event_id, e.member_id, e.event_time, e.order_amount,
       d.level, d.city, d.phone
FROM events e
LEFT JOIN member_dim d
  ON d.member_id = e.member_id
 AND d.effective_from <= e.event_time
 AND (d.effective_to IS NULL OR d.effective_to > e.event_time)
ORDER BY e.event_time, e.event_id
"""

UNKNOWN_LEVEL = "未知"  # 事件发生时会员尚无版本（早于首条变更）


def snapshot_at(con, ts):
    """任意时点切片：该时刻每位会员的等级城市。"""
    return pd.read_sql_query(SNAPSHOT_SQL, con, params=(ts, ts))


def current_versions(con):
    """当前版本。"""
    return pd.read_sql_query(CURRENT_SQL, con)


def event_join(con, events_path):
    """逐事件关联事件发生当时的版本，输出带维度的事实明细。"""
    events = pd.read_csv(events_path, dtype={"order_amount": float})
    events.to_sql("events", con, if_exists="replace", index=False)
    try:
        return pd.read_sql_query(ASOF_JOIN_SQL, con)
    finally:
        con.execute("DROP TABLE IF EXISTS events")


def _month_boundaries(con):
    """拉链覆盖范围内的月份列表，每月返回 (月份, 月初时刻, 月末时刻)。"""
    min_from = con.execute("SELECT MIN(effective_from) FROM member_dim").fetchone()[0]
    start = pd.Timestamp(min_from).to_period("M").start_time
    # 当前版本生效至今，月末不能超过“现在”这一概念边界，统一取自然月末最后一秒
    end = pd.Timestamp.now().to_period("M").end_time
    months = []
    for period in pd.period_range(start, end, freq="M"):
        months.append(
            (
                str(period),
                period.start_time.strftime("%Y-%m-%d %H:%M:%S"),
                period.end_time.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
    return months


def migration_matrix(con):
    """月度等级迁移矩阵：每月月初等级 -> 月末等级的迁移计数。

    仅统计月初与月末两个时点都已有版本的会员。
    返回列：month, level_from, level_to, cnt
    """
    frames = []
    for month, month_start, month_end in _month_boundaries(con):
        begin = snapshot_at(con, month_start).rename(columns={"level": "level_from"})
        end = snapshot_at(con, month_end)[["member_id", "level"]].rename(
            columns={"level": "level_to"}
        )
        pair = begin[["member_id", "level_from"]].merge(end, on="member_id")
        cnt = pair.groupby(["level_from", "level_to"]).size().reset_index(name="cnt")
        cnt.insert(0, "month", month)
        frames.append(cnt)
    return pd.concat(frames, ignore_index=True)


def amount_distribution(con, events_path):
    """月度订单金额分布，按当时等级与按当前等级两版口径。

    返回列：caliber, month, level, amount
    """
    joined = event_join(con, events_path)
    joined["month"] = joined["event_time"].str[:7]
    joined["level_at_event"] = joined["level"].fillna(UNKNOWN_LEVEL)

    cur = current_versions(con).rename(columns={"level": "level_current"})
    joined = joined.merge(
        cur[["member_id", "level_current"]], on="member_id", how="left"
    )

    frames = []
    for caliber, col in (("当时等级", "level_at_event"), ("当前等级", "level_current")):
        dist = (
            joined.groupby(["month", col])["order_amount"]
            .sum()
            .round(2)
            .reset_index()
            .rename(columns={col: "level", "order_amount": "amount"})
        )
        dist.insert(0, "caliber", caliber)
        frames.append(dist)
    return pd.concat(frames, ignore_index=True)


def write_stats(con, events_path, path):
    """把月度迁移矩阵与两版金额分布写入 stats.txt。"""
    matrix = migration_matrix(con)
    dist = amount_distribution(con, events_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 月度等级迁移矩阵（每行：月份、月初等级、月末等级、人数）\n")
        matrix.to_csv(f, index=False)
        f.write("\n# 月度订单金额分布（每行：口径、月份、等级、金额合计）\n")
        dist.to_csv(f, index=False)
    return matrix, dist


def main(argv=None):
    parser = argparse.ArgumentParser(description="拉链维度表查询与统计")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--at", metavar="DATETIME", help="任意时点切片，如 '2026-10-15 12:00:00'"
    )
    mode.add_argument("--event", action="store_true", help="逐事件关联发生当时的版本")
    mode.add_argument("--current", action="store_true", help="当前版本")
    parser.add_argument("--db", default="dim.db", help="SQLite 路径")
    parser.add_argument("--events", default="data/events.csv", help="行为事实 CSV 路径")
    parser.add_argument("--stats", default="stats.txt", help="统计输出路径")
    args = parser.parse_args(argv)

    with sqlite3.connect(args.db) as con:
        if args.at:
            result = snapshot_at(con, args.at)
        elif args.event:
            result = event_join(con, args.events)
        else:
            result = current_versions(con)
        write_stats(con, args.events, args.stats)

    try:
        result.to_csv(sys.stdout, index=False)
    except BrokenPipeError:
        # 下游（如 head）提前关闭管道时静默退出
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
