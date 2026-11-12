"""从会员资料变更流水构建缓慢变化维（拉链表），写入 SQLite 并输出自检报告。

用法：
    python3 dim_builder.py                     # 默认读 data/changes.csv，写 dim.db 与 check.txt
    python3 dim_builder.py --changes x.csv --db y.db --check z.txt

拉链表 member_dim 列：member_id, level, city, phone,
    effective_from, effective_to, is_current
时间格式 YYYY-MM-DD HH:MM:SS，当前版本 effective_to 留空（NULL）。

流水处理规则：
1. 同一会员同一秒的多条变更（change_time 完全相同）：按文件行序（即上游落库
   顺序）取最后一条，早先的记录从未实际生效，不产生版本。
2. 与上一版本 level/city/phone 完全一致的变更属于系统误报，不产生新版本。
3. change_time 早于该会员首条记录的属于补录，按其时间插入历史序列，
   自然把拉链向前延伸，不丢弃。
"""

import argparse
import io
import sqlite3
import sys

import pandas as pd

DIM_COLUMNS = [
    "member_id",
    "level",
    "city",
    "phone",
    "effective_from",
    "effective_to",
    "is_current",
]
ATTR_COLUMNS = ["level", "city", "phone"]

DDL = """
CREATE TABLE member_dim (
    member_id      TEXT NOT NULL,
    level          TEXT NOT NULL,
    city           TEXT NOT NULL,
    phone          TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    is_current     INTEGER NOT NULL
)
"""


def load_changes_from_string(text):
    """从 CSV 文本构造变更流水（供测试复用）。"""
    df = pd.read_csv(io.StringIO(text), dtype=str)
    df["_file_order"] = range(len(df))
    return df


def load_changes(path):
    """读取变更流水，附加 _file_order 列记录落库顺序。"""
    with open(path, encoding="utf-8") as f:
        return load_changes_from_string(f.read())


def build_dim(changes):
    """把变更流水整理成拉链维度表（DataFrame，列同 member_dim）。"""
    df = changes.sort_values(["member_id", "change_time", "_file_order"])

    # 规则 1：同一会员同一秒多次变更，落库顺序靠后的覆盖靠前的
    df = df.drop_duplicates(["member_id", "change_time"], keep="last")

    # 规则 2：与上一版本资料完全一致的误报不产生新版本
    prev = df.groupby("member_id")[ATTR_COLUMNS].shift()
    is_false_alarm = (df[ATTR_COLUMNS] == prev).all(axis=1)
    df = df[~is_false_alarm]

    # 拉链：下一版本的生效时间即本版本的失效时间
    df = df.rename(columns={"change_time": "effective_from"})
    df["effective_to"] = df.groupby("member_id")["effective_from"].shift(-1)
    df["is_current"] = df["effective_to"].isna().astype(int)

    return (
        df[DIM_COLUMNS]
        .sort_values(["member_id", "effective_from"])
        .reset_index(drop=True)
    )


def write_db(dim, db_path):
    """把拉链表写入 SQLite（整表重建）。"""
    with sqlite3.connect(db_path) as con:
        con.execute("DROP TABLE IF EXISTS member_dim")
        con.execute(DDL)
        dim.to_sql("member_dim", con, if_exists="append", index=False)


def run_checks(dim):
    """四项自检，返回 [(检查名, 是否通过, 违例明细 DataFrame)]。"""
    results = []
    df = dim.sort_values(["member_id", "effective_from"]).reset_index(drop=True)
    nxt = df.shift(-1)
    has_next = nxt["member_id"] == df["member_id"]
    # 当前版本 effective_to 为空，比较时用哨兵值代替，避免 None 参与字符串比较
    to_cmp = df["effective_to"].fillna("9999-12-31 23:59:59")

    # 1. 时间段无重叠：下一版本生效时间不得早于本版本失效时间，且失效不得早于等于生效
    overlap = df[has_next & (nxt["effective_from"] < to_cmp)]
    inverted = df[df["effective_to"].notna() & (to_cmp <= df["effective_from"])]
    violations = pd.concat([overlap, inverted])
    results.append(("时间段无重叠", violations.empty, violations))

    # 2. 时间段无遗漏：本版本失效时间必须等于下一版本生效时间
    gaps = df[has_next & (nxt["effective_from"] != df["effective_to"])]
    results.append(("时间段无遗漏", gaps.empty, gaps))

    # 3. 当前标记唯一：每会员恰好一个 is_current=1，且与 effective_to 留空互为充要
    cur_cnt = df.groupby("member_id")["is_current"].sum()
    bad_cnt = pd.DataFrame(
        {
            "member_id": cur_cnt[cur_cnt != 1].index,
            "is_current总数": cur_cnt[cur_cnt != 1].values,
        }
    )
    bad_flag = df[(df["is_current"] == 1) != (df["effective_to"].isna())]
    violations = pd.concat([bad_cnt, bad_flag])
    results.append(("当前标记唯一", violations.empty, violations))

    # 4. 误报未建新版本：相邻版本的 level/city/phone 不得完全一致
    prev = df.groupby("member_id")[ATTR_COLUMNS].shift()
    repeated = df[(df[ATTR_COLUMNS] == prev).all(axis=1)]
    results.append(("误报未建新版本", repeated.empty, repeated))

    return results


def write_check_report(results, path):
    """把自检结论写入 check.txt，违例列出会员号与时间段。"""
    lines = ["拉链维度表自检报告", ""]
    for name, ok, violations in results:
        lines.append(
            f"【{name}】结论：{'通过' if ok else f'存在 {len(violations)} 处违例'}"
        )
        if not ok:
            for _, row in violations.iterrows():
                member = row.get("member_id", "")
                span = f"{row.get('effective_from', '')} ~ {row.get('effective_to', '') or '当前'}"
                extra = row.get("is_current总数", "")
                suffix = f"（is_current总数={extra}）" if extra != "" else ""
                lines.append(f"  违例：member_id={member} 时间段={span}{suffix}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main(argv=None):
    parser = argparse.ArgumentParser(description="构建会员资料拉链维度表并自检")
    parser.add_argument(
        "--changes", default="data/changes.csv", help="变更流水 CSV 路径"
    )
    parser.add_argument("--db", default="dim.db", help="输出 SQLite 路径")
    parser.add_argument("--check", default="check.txt", help="自检报告输出路径")
    args = parser.parse_args(argv)

    changes = load_changes(args.changes)
    dim = build_dim(changes)
    write_db(dim, args.db)

    results = run_checks(dim)
    write_check_report(results, args.check)

    print(
        f"拉链表已写入 {args.db} 的 member_dim 表，共 {len(dim)} 行，"
        f"覆盖 {dim['member_id'].nunique()} 名会员"
    )
    all_ok = True
    for name, ok, violations in results:
        print(f"  {name}: {'通过' if ok else f'存在 {len(violations)} 处违例'}")
        all_ok = all_ok and ok
    print(f"自检报告已写入 {args.check}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
