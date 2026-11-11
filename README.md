# 会员资料历史回溯：SCD2 拉链维度表

从会员资料变更流水构建缓慢变化维（SCD2）拉链表，存入 SQLite，并提供三种历史
查询口径与两个月度统计。环境要求：Python 3.10+、pandas（标准库自带 sqlite3），
无需 Docker。

## 数据

| 文件 | 字段 | 说明 |
|---|---|---|
| `data/changes.csv` | change_time, member_id, level, city, phone | 会员资料变更流水，行序即上游落库顺序 |
| `data/events.csv` | event_id, member_id, event_time, order_amount | 行为事实（订单事件） |

流水包含四类边界情况，构建时均有明确处理：

1. **同会员同日多次变更**：按 change_time 排序，逐次产生版本。
2. **相邻两条 change_time 完全相同**：以流水行序靠后者为该时刻最终状态，前面的
   折叠丢弃，只产生一个版本（样例 M30001）。
3. **与当时资料完全一致的误报**：level/city/phone 三元组与上一生效版本相同的
   流水不产生新版本（样例 M30003 的两条误报）。
4. **补录（change_time 早于该会员在文件中首次出现的位置）**：按时间排序后自然
   成为更早的版本，不做特殊剔除。

## 拉链模型

- 版本区间为半开区间 `[effective_from, effective_to)`，相邻版本首尾相接
- 当前版本 `is_current=1`、`effective_to` 留空（SQLite 中为 NULL）
- 时间统一 `YYYY-MM-DD HH:MM:SS` 文本格式（定长等宽，字典序与时间序一致）
- 事件时刻恰好等于某版本生效时间时，命中该版本

## 使用方法

脚本从仓库根目录运行，默认路径可用参数覆盖。

### 1. 构建拉链表并自检

```bash
python3 dim_builder.py
# 可选参数: --changes data/changes.csv --db dim.db --check check.txt
```

产物：

- `dim.db`：SQLite 库，表 `member_dim`，列
  `member_id, level, city, phone, effective_from, effective_to, is_current`
  （另建 `member_id + effective_from` 查询索引）
- `check.txt`：自检报告，逐项给出四项检查结论，有违例时列出会员号与时间段
  1. 时间段无重叠
  2. 时间段无遗漏（相邻版本首尾相接，无空洞）
  3. 当前标记唯一（每会员恰好一条 is_current=1，且其 effective_to 为空）
  4. 误报未建新版本（独立重算期望版本集合与库内集合比对，并反查每条误报流水的
     时间点未成为任何版本的生效时间）

### 2. 三种查询口径

查询结果以带表头的 CSV 打印到标准输出。

```bash
# 任意时点切片：该时刻每位会员的等级与城市（时点之后才出现的会员不输出）
python3 query.py --at "2026-10-15 10:23:44"
python3 query.py --at "2026-10-15"          # 只给日期等价于当天 00:00:00

# 逐事件关联事件发生当时的版本，输出带维度的事实明细
python3 query.py --event

# 取每位会员的当前版本
python3 query.py --current
```

输出列：

| 口径 | 输出列 |
|---|---|
| `--at` | member_id, level, city |
| `--event` | event_id, member_id, event_time, order_amount, level, city, phone |
| `--current` | member_id, level, city, phone, effective_from |

`--event` 保持事件流水的原始行序；事件早于该会员首个版本生效时间时（样例 27
条，含 M30006/M30007/M30008），level/city/phone 留空，事实字段保留。

### 3. 两个月度统计

```bash
python3 query.py --stats
# 可选: --stats-out stats.txt
```

结果写入 `stats.txt` 并同步打印到标准输出，包含两段 CSV：

**月度等级迁移矩阵**——每月月初与月末（次月 1 日 00:00:00）等级状态之间的迁移
计数，每行 `月份,月初等级,月末等级,人数`。月初尚无任何版本的会员不计入当月；
样例数据 2026-09-01 无在册会员，故矩阵从 2026-10 开始。

**月度订单金额分布**——每行 `月份,口径,等级,金额合计`，两个口径各出一版：

- `按当时等级`：按事件发生当时生效版本的等级归集；事件时点无匹配版本时等级为空
- `按当前等级`：按会员当前版本的等级归集历史订单

两版各自的金额合计相等，均等于事件流水订单总额；金额保留两位小数。

## 测试

```bash
python3 -m pytest tests/ -v
```

20 个用例覆盖：同时间戳折叠（后者胜、含改回前序状态的净无变化）、误报不建版本、
仅手机号变更建版本、补录成为最早版本、半开区间边界、当前标记、SQLite 落库回读、
四项自检的通过与违例检出、三种查询口径、事件无匹配留空、迁移矩阵与金额分布的
闭合性，以及命令行端到端冒烟。

## 文件清单

```
dim_builder.py        构建拉链表 + 四项自检
query.py              三种口径查询 + 迁移矩阵 + 两版金额分布
tests/                pytest 测试（20 个用例）
data/                 样例数据（changes.csv 625 行、events.csv 1506 行）
dim.db/check.txt/stats.txt  运行产物（已在 .gitignore 忽略）
```
