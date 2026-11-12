# 会员资料历史回溯：拉链维度表

从会员资料变更流水构建缓慢变化维（拉链表），支持任意历史时点的等级/城市回溯。

## 数据

仓库 data 目录下有两份数据（行序即上游落库顺序）：

- `data/changes.csv`：会员资料变更流水（change_time, member_id, level, city, phone）
- `data/events.csv`：行为事实（event_id, member_id, event_time, order_amount）

## 构建拉链表

```bash
python3 dim_builder.py            # 读 data/changes.csv，写 dim.db 与 check.txt
```

输出：

- `dim.db` 的 `member_dim` 表：member_id, level, city, phone, effective_from,
  effective_to, is_current。时间格式 `YYYY-MM-DD HH:MM:SS`，当前版本
  effective_to 留空、is_current=1。
- `check.txt`：四项自检结论（时间段无重叠、无遗漏、当前标记唯一、误报未建新版本），
  有违例会列出会员号与时间段。

流水中的特殊情况按如下规则处理：

1. **同一秒多次变更**：按文件行序（落库顺序）取最后一条，早先记录不产生版本。
2. **系统误报**（与上一版本 level/city/phone 完全一致）：不产生新版本。
3. **补录**（change_time 早于该会员首条记录）：按其时间插入历史序列，拉链向前延伸。

## 查询

```bash
python3 query.py --at "2026-10-15 12:00:00"   # 任意时点切片：该时刻每位会员的等级城市
python3 query.py --event                       # 逐事件关联发生当时的版本（带维度的事实明细）
python3 query.py --current                     # 当前版本
```

结果以 CSV 打印到标准输出（带表头）。事件早于会员首个版本时，维度列留空。

每次运行 query.py 都会重新生成 `stats.txt`：

1. **月度等级迁移矩阵**：每行是月份、月初等级、月末等级、人数
   （仅统计月初与月末两个时点都已有版本的会员）。
2. **月度订单金额分布**：每行是口径（当时等级/当前等级）、月份、等级、金额合计；
   事件发生时尚无版本的计入"未知"等级。

## 测试

```bash
python3 -m pytest test_dim.py -q
```

覆盖三类流水特殊情况、四项自检、三种查询口径与两项统计，并对仓库样例数据
做端到端构建验证。

## 环境

Python 3 + pandas + SQLite（标准库 sqlite3），无其他依赖，不需要 Docker。
脚本从仓库根目录运行，数据用相对路径 data/。
