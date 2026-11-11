"""命令行端到端测试：构建 -> 三种查询 -> 统计，含真实样例数据冒烟。"""

from __future__ import annotations

import csv
import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd, cwd):
    return subprocess.run(
        [sys.executable, "-I", *cmd],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def parse_csv(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_end_to_end_with_fixture_data(tmp_path):
    # 用仓库自带样例数据跑完整链路
    result = run(["dim_builder.py", "--changes", str(ROOT / "data/changes.csv"),
                  "--db", str(tmp_path / "dim.db"),
                  "--check", str(tmp_path / "check.txt")], cwd=ROOT)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "dim.db").is_file()
    check_text = (tmp_path / "check.txt").read_text(encoding="utf-8")
    assert "总体结论: 全部通过" in check_text

    # --at
    result = run(["query.py", "--db", str(tmp_path / "dim.db"),
                  "--at", "2026-10-15 10:23:44"], cwd=ROOT)
    assert result.returncode == 0, result.stderr
    rows = parse_csv(result.stdout)
    assert rows and set(rows[0]) == {"member_id", "level", "city"}

    # --current
    result = run(["query.py", "--db", str(tmp_path / "dim.db"), "--current"], cwd=ROOT)
    rows = parse_csv(result.stdout)
    assert set(rows[0]) == {"member_id", "level", "city", "phone", "effective_from"}
    assert len({r["member_id"] for r in rows}) == len(rows)  # 每会员仅一条

    # --event
    result = run(["query.py", "--db", str(tmp_path / "dim.db"),
                  "--events", str(ROOT / "data/events.csv"), "--event"], cwd=ROOT)
    rows = parse_csv(result.stdout)
    assert len(rows) == 1506
    assert set(rows[0]) == {"event_id", "member_id", "event_time", "order_amount", "level", "city", "phone"}

    # --stats
    result = run(["query.py", "--db", str(tmp_path / "dim.db"),
                  "--events", str(ROOT / "data/events.csv"),
                  "--stats", "--stats-out", str(tmp_path / "stats.txt")], cwd=ROOT)
    assert result.returncode == 0, result.stderr
    stats = (tmp_path / "stats.txt").read_text(encoding="utf-8")
    assert "月度等级迁移矩阵" in stats and "月度订单金额分布" in stats


def test_cli_mutual_exclusion_and_missing_db(tmp_path):
    # 四种模式互斥
    result = run(["query.py", "--current", "--at", "2026-10-01"], cwd=ROOT)
    assert result.returncode != 0
    # 维度库不存在时明确报错而非静默
    result = run(["query.py", "--db", str(tmp_path / "nope.db"), "--current"], cwd=ROOT)
    assert result.returncode == 1
    assert "维度库不存在" in result.stderr
