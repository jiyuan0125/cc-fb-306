#!/usr/bin/env python3
"""query.py — 拉链维度表的三种口径查询与两个统计。

查询口径（结果以带表头 CSV 打印到标准输出）:
  --at "YYYY-MM-DD HH:MM:SS"   任意时点切片: 该时刻每位会员的等级与城市
  --event                      逐事件关联事件发生当时的版本（带维度事实明细）
  --current                    取每位会员的当前版本

统计（写入 stats.txt，并同步打印到标准输出）:
  --stats                      1) 月度等级迁移矩阵（月初等级 -> 月末等级计数）
                               2) 月度订单金额分布（按当时等级 / 按当前等级两版）

维度区间为半开区间 [effective_from, effective_to)，事件时刻等于某版本生效时间
时命中该版本；事件早于会员首个版本生效时间时维度字段留空。
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

import pandas as pd

LOGGER = logging.getLogger("query")

TS_FMT = "%Y-%m-%d %H:%M:%S"
TABLE_NAME = "member_dim"
STATE_COLS = ["level", "city", "phone"]
CALIBER_AS_OF = "按当时等级"
CALIBER_CURRENT = "按当前等级"


# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #
def load_dim(db_path: str | Path) -> pd.DataFrame:
    """从 SQLite 读取 member_dim 并解析时间列。"""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"维度库不存在: {db_path}（请先运行 dim_builder.py）")
    with sqlite3.connect(db_path) as conn:
        dim = pd.read_sql_query(
            f"SELECT member_id, level, city, phone, effective_from, effective_to, is_current "
            f"FROM {TABLE_NAME}",
            conn,
            dtype={
                "member_id": str,
                "level": str,
                "city": str,
                "phone": str,
                "effective_from": str,
                "effective_to": str,
            },
        )
    dim["is_current"] = dim["is_current"].astype(int)
    dim["from_dt"] = pd.to_datetime(dim["effective_from"])
    dim["to_dt"] = pd.to_datetime(dim["effective_to"])
    return dim


def load_events(events_path: str | Path) -> pd.DataFrame:
    events = pd.read_csv(
        events_path,
        dtype={"event_id": str, "member_id": str, "event_time": str, "order_amount": str},
    )
    missing = {"event_id", "member_id", "event_time", "order_amount"} - set(events.columns)
    if missing:
        raise ValueError(f"事件流水缺少必需列: {sorted(missing)}")
    events["ts"] = pd.to_datetime(events["event_time"])
    events["amount"] = pd.to_numeric(events["order_amount"])
    return events


def parse_at(text: str) -> pd.Timestamp:
    for fmt in (TS_FMT, "%Y-%m-%d"):
        try:
            return pd.Timestamp(pd.to_datetime(text, format=fmt))
        except (ValueError, TypeError):
            # 多格式尝试: 当前格式不匹配则继续，全部失败后在循环外显式报错
            continue
    raise argparse.ArgumentTypeError(f"无法解析时点: {text!r}，请使用 YYYY-MM-DD HH:MM:SS 或 YYYY-MM-DD")


def _to_csv(df: pd.DataFrame) -> str:
    return df.to_csv(index=False, lineterminator="\n")


# --------------------------------------------------------------------------- #
# 三种口径
# --------------------------------------------------------------------------- #
def slice_at(db_path: str | Path, at_ts: pd.Timestamp) -> pd.DataFrame:
    """任意时点切片: 半开区间 [effective_from, effective_to) 内的版本。"""
    at_text = at_ts.strftime(TS_FMT)
    # 文本时间等宽定长，字典序与时间序一致，可直接做字符串比较
    sql = (
        f"SELECT member_id, level, city FROM {TABLE_NAME} "
        "WHERE effective_from <= ? "
        "AND (effective_to IS NULL OR effective_to > ?) "
        "ORDER BY member_id"
    )
    with sqlite3.connect(db_path) as conn:
        return pd.read_sql_query(sql, conn, params=(at_text, at_text))


def current_versions(db_path: str | Path) -> pd.DataFrame:
    sql = (
        f"SELECT member_id, level, city, phone, effective_from FROM {TABLE_NAME} "
        "WHERE is_current = 1 ORDER BY member_id"
    )
    with sqlite3.connect(db_path) as conn:
        return pd.read_sql_query(sql, conn)


def join_events(dim: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """逐事件关联事件发生当时生效的版本；无匹配版本时维度字段留空。"""
    events = events.copy()
    events["src_order"] = range(len(events))
    ev_sorted = events.sort_values(["member_id", "ts"], kind="mergesort")
    dim_sorted = dim.sort_values(["member_id", "from_dt"], kind="mergesort")

    # pandas 3 的 merge_asof 要求 on 键全局有序，故按会员逐组做回溯关联
    pieces = []
    for member_id, ev_group in ev_sorted.groupby("member_id", sort=False):
        dim_group = dim_sorted[dim_sorted["member_id"] == member_id]
        if dim_group.empty:
            merged = ev_group.copy()
            for col in STATE_COLS:
                merged[col] = pd.NA
        else:
            merged = pd.merge_asof(
                ev_group,
                dim_group[["from_dt", *STATE_COLS]],
                left_on="ts",
                right_on="from_dt",
                direction="backward",
            )
        pieces.append(merged)
    merged = pd.concat(pieces, ignore_index=True)
    merged = merged.sort_values("src_order", kind="mergesort").reset_index(drop=True)
    merged[STATE_COLS] = merged[STATE_COLS].fillna("")
    return merged[
        ["event_id", "member_id", "event_time", "order_amount", "level", "city", "phone"]
    ]


# --------------------------------------------------------------------------- #
# 两个统计
# --------------------------------------------------------------------------- #
def _month_starts(dim: pd.DataFrame) -> list[pd.Timestamp]:
    """维度表覆盖的月份序列（从最早生效时间所在月到最晚生效时间所在月）。"""
    first = dim["from_dt"].min().to_period("M").to_timestamp()
    last = dim["from_dt"].max().to_period("M").to_timestamp()
    starts: list[pd.Timestamp] = []
    cur = first
    while cur <= last:
        starts.append(cur)
        cur = (cur.to_period("M") + 1).to_timestamp()
    return starts


def _level_at_points(dim: pd.DataFrame, members: list[str], points: list[pd.Timestamp]) -> pd.DataFrame:
    """对 (会员 × 时点) 笛卡尔点集，回查每个时点生效的等级。"""
    pts = pd.MultiIndex.from_product(
        [members, points], names=["member_id", "ts"]
    ).to_frame(index=False)
    pts = pts.sort_values(["member_id", "ts"], kind="mergesort")
    dim_sorted = dim.sort_values(["member_id", "from_dt"], kind="mergesort")

    pieces = []
    for member_id, pts_group in pts.groupby("member_id", sort=False):
        dim_group = dim_sorted[dim_sorted["member_id"] == member_id]
        pieces.append(
            pd.merge_asof(
                pts_group,
                dim_group[["from_dt", "level"]],
                left_on="ts",
                right_on="from_dt",
                direction="backward",
            )
        )
    out = pd.concat(pieces, ignore_index=True)
    return out[["member_id", "ts", "level"]]


def migration_matrix(dim: pd.DataFrame) -> pd.DataFrame:
    """月度等级迁移矩阵: 每月月初等级与月末（次月月初）等级之间的迁移计数。

    月初尚无任何版本的会员不计入当月；月末时点取次月 1 日 00:00:00，
    当前版本 effective_to 为空，仍可被该时点命中。
    """
    starts = _month_starts(dim)
    members = sorted(dim["member_id"].unique())

    start_levels = _level_at_points(dim, members, starts)
    end_points = [(p.to_period("M") + 1).to_timestamp() for p in starts]
    end_levels = _level_at_points(dim, members, end_points)

    rows = []
    for start, end_point in zip(starts, end_points, strict=True):
        month = start.strftime("%Y-%m")
        sl = start_levels[start_levels["ts"] == start].set_index("member_id")["level"]
        el = end_levels[end_levels["ts"] == end_point].set_index("member_id")["level"]
        # 月初尚无版本的会员不计入当月；能进入 pair 的会员月末必有生效版本
        pair = pd.DataFrame({"start": sl, "end": el}).dropna(subset=["start"])
        counts = pair.groupby(["start", "end"], dropna=False).size().reset_index(name="人数")
        counts.insert(0, "月份", month)
        rows.append(counts)
    matrix = pd.concat(rows, ignore_index=True)
    matrix = matrix.rename(columns={"start": "月初等级", "end": "月末等级"})
    return matrix.sort_values(["月份", "月初等级", "月末等级"], kind="mergesort").reset_index(drop=True)


def amount_distribution(dim: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """按当时等级、按当前等级两版的月度订单金额合计。"""
    events = events.copy()
    events["月份"] = events["ts"].dt.to_period("M").astype(str)

    # 口径一: 事件发生当时的等级（无匹配版本时等级为空）
    asof_level = join_events(dim, events)["level"].replace("", pd.NA)

    # 口径二: 当前等级（每会员唯一当前版本）
    current = dim[dim["is_current"] == 1].set_index("member_id")["level"]
    current_level = events["member_id"].map(current)

    def _agg(level: pd.Series, caliber: str) -> pd.DataFrame:
        frame = pd.DataFrame(
            {"月份": events["月份"], "等级": level.fillna(""), "amount": events["amount"]}
        )
        out = frame.groupby(["月份", "等级"], dropna=False, sort=True)["amount"].sum().reset_index()
        out["金额合计"] = out["amount"].round(2).map(lambda v: f"{v:.2f}")
        out.insert(1, "口径", caliber)
        return out[["月份", "口径", "等级", "金额合计"]]

    dist = pd.concat(
        [_agg(asof_level, CALIBER_AS_OF), _agg(current_level, CALIBER_CURRENT)],
        ignore_index=True,
    )
    return dist.sort_values(["月份", "口径", "等级"], kind="mergesort").reset_index(drop=True)


def render_stats(matrix: pd.DataFrame, distribution: pd.DataFrame) -> str:
    return (
        "月度等级迁移矩阵（月初 -> 月末，每行: 月份,月初等级,月末等级,人数）\n"
        f"{_to_csv(matrix)}"
        "月度订单金额分布（每行: 月份,口径,等级,金额合计；等级为空表示事件时点无匹配版本）\n"
        f"{_to_csv(distribution)}"
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="会员拉链维度表查询与统计")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--at", type=parse_at, metavar="DATETIME", help="任意时点切片，格式 YYYY-MM-DD[ HH:MM:SS]")
    mode.add_argument("--event", action="store_true", help="逐事件关联当时版本，输出带维度事实明细")
    mode.add_argument("--current", action="store_true", help="输出每位会员的当前版本")
    mode.add_argument("--stats", action="store_true", help="输出月度迁移矩阵与两版金额分布到 stats.txt")
    parser.add_argument("--db", default="dim.db", help="SQLite 维度库路径")
    parser.add_argument("--events", default="data/events.csv", help="事件流水 CSV 路径（--event/--stats 使用）")
    parser.add_argument("--stats-out", default="stats.txt", help="统计结果输出路径（--stats 使用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        if args.at is not None:
            result = slice_at(args.db, args.at)
            sys.stdout.write(_to_csv(result))
            return 0

        dim = load_dim(args.db)

        if args.current:
            sys.stdout.write(_to_csv(current_versions(args.db)))
            return 0

        events_path = Path(args.events)
        if not events_path.is_file():
            LOGGER.error("事件流水文件不存在: %s", events_path)
            return 2
        events = load_events(events_path)

        if args.event:
            sys.stdout.write(_to_csv(join_events(dim, events)))
            return 0

        if args.stats:
            matrix = migration_matrix(dim)
            distribution = amount_distribution(dim, events)
            report = render_stats(matrix, distribution)
            Path(args.stats_out).write_text(report, encoding="utf-8")
            sys.stdout.write(report)
            LOGGER.info("统计结果已写入 %s", args.stats_out)
            return 0
    except (OSError, ValueError, sqlite3.Error):
        LOGGER.exception("查询失败")
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
