#!/usr/bin/env python3
"""dim_builder.py — 从会员资料变更流水构建 SCD2 拉链维度表。

输入: changes.csv（change_time, member_id, level, city, phone）
输出:
  1. SQLite 库 dim.db 的 member_dim 表
     列: member_id, level, city, phone, effective_from, effective_to, is_current
     时间统一 YYYY-MM-DD HH:MM:SS，当前版本 effective_to 留空（NULL）
  2. check.txt 自检报告（无重叠 / 无遗漏 / 当前标记唯一 / 误报未建新版本）

口径约定:
  - 版本区间为半开区间 [effective_from, effective_to)，相邻版本首尾相接
  - 同一会员同一 change_time 有多条流水时，以流水行序靠后者为该时刻最终状态
  - 排序后状态（level/city/phone 三元组）与上一生效版本完全一致的流水视为
    系统误报，不产生新版本
  - change_time 早于该会员在文件中首次出现位置的补录流水，按时间排序后自然
    成为更早的版本，不做特殊剔除
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

LOGGER = logging.getLogger("dim_builder")

STATE_COLS = ["level", "city", "phone"]
DIM_COLUMNS = [
    "member_id",
    "level",
    "city",
    "phone",
    "effective_from",
    "effective_to",
    "is_current",
]
TS_FMT = "%Y-%m-%d %H:%M:%S"
TABLE_NAME = "member_dim"


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #
def load_changes(path: str | Path) -> pd.DataFrame:
    """读取变更流水，字符串列保持原始文本，时间列解析为 Timestamp。"""
    df = pd.read_csv(path, dtype=str)
    missing = {"change_time", "member_id", *STATE_COLS} - set(df.columns)
    if missing:
        raise ValueError(f"变更流水缺少必需列: {sorted(missing)}")
    df["ts"] = pd.to_datetime(df["change_time"], errors="raise")
    return df


def build_dim(changes: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """把变更流水压缩成拉链版本。

    返回 (维度 DataFrame, 审计信息)。维度 DataFrame 的时间列为 datetime，
    由调用方负责格式化落库；审计信息记录同时间戳折叠行数与误报明细。
    """
    df = changes.copy()
    df["src_order"] = range(len(df))
    df = df.sort_values(["member_id", "ts", "src_order"], kind="mergesort")

    # 同一会员同一时间戳: 行序靠后者为最终状态，前面的折叠丢弃
    same_ts_collapsed = int(df.duplicated(subset=["member_id", "ts"]).sum())
    df = df.drop_duplicates(subset=["member_id", "ts"], keep="last")
    df = df.reset_index(drop=True)

    records: list[dict] = []
    false_positives: list[dict] = []
    for member_id, group in df.groupby("member_id", sort=False):
        current_state: tuple[str, ...] | None = None
        current_from: pd.Timestamp | None = None
        for row in group.itertuples(index=False):
            state = (row.level, row.city, row.phone)
            if state == current_state:
                # 与上一生效状态一模一样: 系统误报，不产生新版本
                false_positives.append(
                    {
                        "member_id": member_id,
                        "ts": row.ts,
                        "level": row.level,
                        "city": row.city,
                        "phone": row.phone,
                    }
                )
                continue
            if current_state is not None:
                records.append(
                    {
                        "member_id": member_id,
                        **dict(zip(STATE_COLS, current_state, strict=True)),
                        "effective_from": current_from,
                    }
                )
            current_state, current_from = state, row.ts
        if current_state is not None:
            records.append(
                {
                    "member_id": member_id,
                    **dict(zip(STATE_COLS, current_state, strict=True)),
                    "effective_from": current_from,
                }
            )

    dim = pd.DataFrame(records)
    # 同会员内下一版本的生效时间即本版本的失效时间；末版本为 NaT -> 当前版本
    dim["effective_to"] = dim.groupby("member_id", sort=False)["effective_from"].shift(-1)
    dim["is_current"] = dim["effective_to"].isna().astype(int)
    dim = dim.sort_values(["member_id", "effective_from"], kind="mergesort").reset_index(drop=True)

    audit = {
        "same_timestamp_collapsed": same_ts_collapsed,
        "false_positives": false_positives,
    }
    return dim, audit


def format_dim(dim: pd.DataFrame) -> pd.DataFrame:
    """把 datetime 列格式化为题面要求的文本形式，NaT 转 None（落库为 NULL）。"""
    out = dim.copy()
    out["effective_from"] = out["effective_from"].dt.strftime(TS_FMT)
    out["effective_to"] = out["effective_to"].dt.strftime(TS_FMT)
    # pd.NA / NaN 不能被 sqlite3 绑定，统一转 Python None
    out["effective_to"] = out["effective_to"].astype(object).where(out["effective_to"].notna(), None)
    return out[DIM_COLUMNS]


def write_sqlite(dim_text: pd.DataFrame, db_path: str | Path) -> None:
    """重建 SQLite 库并写入 member_dim（含查询索引）。"""
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"CREATE TABLE {TABLE_NAME} ("
            "member_id TEXT NOT NULL, "
            "level TEXT NOT NULL, "
            "city TEXT NOT NULL, "
            "phone TEXT NOT NULL, "
            "effective_from TEXT NOT NULL, "
            "effective_to TEXT, "
            "is_current INTEGER NOT NULL)"
        )
        conn.executemany(
            f"INSERT INTO {TABLE_NAME} "
            "(member_id, level, city, phone, effective_from, effective_to, is_current) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            dim_text.itertuples(index=False, name=None),
        )
        conn.execute(
            f"CREATE INDEX idx_{TABLE_NAME}_member_from "
            f"ON {TABLE_NAME}(member_id, effective_from)"
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #
@dataclass
class CheckResult:
    name: str
    passed: bool
    violations: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


def _recompute_expected_versions(changes: pd.DataFrame) -> set[tuple]:
    """独立于 build_dim 重算期望版本集合，供第四项检查交叉验证。"""
    df = changes.copy()
    df["src_order"] = range(len(df))
    df = df.sort_values(["member_id", "ts", "src_order"], kind="mergesort")
    df = df.drop_duplicates(subset=["member_id", "ts"], keep="last")

    expected: set[tuple] = set()
    for member_id, group in df.groupby("member_id", sort=False):
        last_state: tuple[str, ...] | None = None
        last_ts: pd.Timestamp | None = None
        for row in group.itertuples(index=False):
            state = (row.level, row.city, row.phone)
            if state == last_state:
                continue
            if last_state is not None:
                expected.add((member_id, *last_state, last_ts.strftime(TS_FMT)))
            last_state, last_ts = state, row.ts
        if last_state is not None:
            expected.add((member_id, *last_state, last_ts.strftime(TS_FMT)))
    return expected


def run_checks(changes: pd.DataFrame, dim_text: pd.DataFrame) -> list[CheckResult]:
    """对落库后的维度表执行四项检查。"""
    dim = dim_text.copy()
    dim["from_dt"] = pd.to_datetime(dim["effective_from"], errors="raise")
    dim["to_dt"] = pd.to_datetime(dim["effective_to"], errors="coerce")

    # 1. 时间段无重叠
    overlap = CheckResult("时间段无重叠", True)
    # 2. 时间段无遗漏（相邻版本首尾相接，无空洞）
    gap = CheckResult("时间段无遗漏", True)
    for member_id, group in dim.groupby("member_id", sort=False):
        rows = group.sort_values("from_dt").itertuples(index=False)
        prev = None
        for cur in rows:
            if prev is not None:
                if cur.from_dt < prev.to_dt:
                    overlap.passed = False
                    overlap.violations.append(
                        f"- {member_id}: [{prev.effective_from}, {prev.effective_to}) "
                        f"与 [{cur.effective_from}, {cur.effective_to or ''}) 重叠"
                    )
                if cur.from_dt != prev.to_dt:
                    gap.passed = False
                    gap.violations.append(
                        f"- {member_id}: 空洞 ({prev.effective_to}, {cur.effective_from})"
                    )
            prev = cur
    overlap.details.append(f"检查 {dim.member_id.nunique()} 名会员的相邻版本区间，违例 {len(overlap.violations)} 条")
    gap.details.append(f"检查相邻版本 effective_to 与下一版本 effective_from 是否相等，违例 {len(gap.violations)} 条")

    # 3. 当前标记唯一
    current = CheckResult("当前标记唯一", True)
    cur_counts = dim.groupby("member_id", sort=False)["is_current"].sum()
    bad_markers = cur_counts[cur_counts != 1].index.tolist()
    for member_id in bad_markers:
        current.passed = False
        current.violations.append(
            f"- {member_id}: is_current=1 的行数为 {int(cur_counts[member_id])}，应为 1"
        )
    bad_tail = dim[(dim.is_current == 1) & dim.effective_to.notna()]
    for row in bad_tail.itertuples(index=False):
        current.passed = False
        current.violations.append(
            f"- {row.member_id}: 当前版本 effective_to 未留空（{row.effective_to}）"
        )
    current.details.append(
        f"{len(cur_counts) - len(bad_markers)}/{len(cur_counts)} 名会员恰好拥有一条当前版本"
    )

    # 4. 误报未建新版本（独立重算 + 误报时间点反查）
    false_positive = CheckResult("误报未建新版本", True)
    expected = _recompute_expected_versions(changes)
    actual = {
        (r.member_id, r.level, r.city, r.phone, r.effective_from)
        for r in dim.itertuples(index=False)
    }
    if actual != expected:
        false_positive.passed = False
        only_actual = sorted(actual - expected)
        only_expected = sorted(expected - actual)
        if only_actual:
            false_positive.violations.append(f"- 库中多出 {len(only_actual)} 个非期望版本: {only_actual[:5]}")
        if only_expected:
            false_positive.violations.append(f"- 库中缺失 {len(only_expected)} 个期望版本: {only_expected[:5]}")
    false_positive.details.append(
        f"独立重算期望版本 {len(expected)} 个，member_dim 实际版本 {len(actual)} 个，集合一致: {actual == expected}"
    )

    effective_points = set(zip(dim.member_id, dim.effective_from, strict=True))
    df = changes.copy()
    df["src_order"] = range(len(df))
    df = df.sort_values(["member_id", "ts", "src_order"], kind="mergesort")
    df = df.drop_duplicates(subset=["member_id", "ts"], keep="last")
    skipped: list[tuple] = []
    for member_id, group in df.groupby("member_id", sort=False):
        last_state: tuple[str, ...] | None = None
        for row in group.itertuples(index=False):
            state = (row.level, row.city, row.phone)
            if state == last_state:
                skipped.append((member_id, row))
            last_state = state
    for member_id, row in skipped:
        point = (member_id, row.ts.strftime(TS_FMT))
        if point in effective_points:
            false_positive.passed = False
            false_positive.violations.append(
                f"- {member_id}: 误报流水时间 {point[1]} 被建成了新版本生效时间"
            )
    false_positive.details.append(f"识别误报流水 {len(skipped)} 条，其时间点均未成为版本生效时间")
    false_positive.details.extend(
        f"  误报明细: {m} {r.ts.strftime(TS_FMT)} 状态={r.level}/{r.city}/{r.phone} 与前序版本一致"
        for m, r in skipped
    )

    return [overlap, gap, current, false_positive]


def render_check_report(results: list[CheckResult], source: Path, dim_text: pd.DataFrame) -> str:
    now = datetime.now(timezone.utc).strftime(TS_FMT)
    lines = [
        "拉链表自检报告",
        f"生成时间: {now}",
        f"数据源: {source}",
        f"会员数: {dim_text.member_id.nunique()}    版本总数: {len(dim_text)}",
        "",
    ]
    for idx, result in enumerate(results, start=1):
        lines.append(f"[{idx}/4] {result.name}")
        lines.append(f"结论: {'通过' if result.passed else '未通过'}")
        lines.extend(result.details)
        if result.violations:
            lines.append("违例:")
            lines.extend(result.violations)
        lines.append("")
    overall = all(r.passed for r in results)
    lines.append(f"总体结论: {'全部通过' if overall else '存在违例，见上述条目'}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="从变更流水构建会员拉链维度表并自检")
    parser.add_argument("--changes", default="data/changes.csv", help="变更流水 CSV 路径")
    parser.add_argument("--db", default="dim.db", help="输出 SQLite 文件路径")
    parser.add_argument("--check", default="check.txt", help="自检报告输出路径")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    changes_path = Path(args.changes)
    if not changes_path.is_file():
        LOGGER.error("变更流水文件不存在: %s", changes_path)
        return 2

    try:
        changes = load_changes(changes_path)
        LOGGER.info("读取变更流水 %s 行，会员 %s 名", len(changes), changes.member_id.nunique())

        dim, audit = build_dim(changes)
        LOGGER.info(
            "同时间戳折叠 %s 行，识别误报 %s 条，生成版本 %s 个",
            audit["same_timestamp_collapsed"],
            len(audit["false_positives"]),
            len(dim),
        )

        dim_text = format_dim(dim)
        write_sqlite(dim_text, args.db)
        LOGGER.info("维度表已写入 %s 的 %s 表", args.db, TABLE_NAME)

        results = run_checks(changes, dim_text)
        report = render_check_report(results, changes_path, dim_text)
        Path(args.check).write_text(report, encoding="utf-8")
        for result in results:
            LOGGER.info("[%s] %s", "通过" if result.passed else "未通过", result.name)
        LOGGER.info("自检报告已写入 %s", args.check)
    except (OSError, ValueError, sqlite3.Error):
        LOGGER.exception("构建失败")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
