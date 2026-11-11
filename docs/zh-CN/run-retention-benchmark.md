# run 保留的堆内存和序列化基线

测量日期 2026-11-11，Python 3.11.14，包版本 0.10.4；代码为
`2626349daf48ae3c38b1850d8ba6642c3c484234` 加本轮保留上限实现。三份原始报告明确记录
`working_tree_differs=true`，精确 SDK 源码 SHA-256 为
`c449255a5175f47b0cd50f052bb48726ea3ed35efe10d11f186410b1bea52e7d`，可独立于随后包含报告的提交核对被测代码。
复现命令：

```bash
uv run python -m benchmarks.run_retention --runs 24
uv run python -m benchmarks.run_retention --runs 16 --onchange
uv run python -m benchmarks.run_retention --runs 1 --steps 300 --output-chars 1024
```

[脚本](../../benchmarks/run_retention.py)使用离线合成 provider、legacy 上下文，不配置
`read_file`。每个根 run 执行四次工具，每次产生 32,768 个新建文本字符；DAG 模式
每个根 run 有一个子 agent。不保留网络请求、事件或批次日志；启用订阅时宿主只确认。
消费完全部事件、释放旧结果并 GC 后再采样。

## 连续 24 个根 run 后的堆内存

| 模式 | 上限 | 当前堆 MiB | 峰值堆 MiB | 最后 8 次波动字节 | 状态/checkpoint/子会话数 | 去重归属字节 |
| --- | --- | ---: | ---: | ---: | --- | ---: |
| tool | None | 7.030 | 8.577 | 2087727 | 24/24/0 | 5675302 |
| tool | 2 | 0.743 | 2.291 | 11904 | 2/2/0 | 485034 |
| tool | 0 | 0.171 | 1.864 | 12759 | 0/0/0 | 368 |
| dag | None | 8.175 | 9.201 | 2452655 | 24/24/24 | 6661564 |
| dag | 2 | 0.833 | 1.859 | 7749 | 2/2/2 | 571976 |
| dag | 0 | 0.166 | 1.337 | 7914 | 0/0/0 | 592 |

`None` 随历史增长。上限 2、0 在此负载中达到平台，仍有轻量 id 和测量开销增长。
16 个 run 的宿主订阅变体中，有限上限下最后八次波动最大为 13763 字节。
当前/峰值是 Python `tracemalloc` 堆采样，不是 RSS 或序列化体积。归属估算递归统计
保留对象图，跨三个缓存只计算一次共享引用；原始报告中的各缓存独立估算不可相加。

## 单次 300 轮工具交互，每次输出 1,024 字符

| 模式 | 记录 | UTF-8 字节 | JSON 值数量 |
| --- | --- | ---: | ---: |
| tool | RunState | 1498709 | 50156 |
| tool | checkpoint | 1505574 | 50204 |
| tool | run.finished | 2066509 | 63690 |
| dag | RunState | 2291976 | 76558 |
| dag | checkpoint | 2298942 | 76608 |
| dag | run.finished | 2292261 | 76571 |

序列化使用紧凑 UTF-8 JSON；值数量计入容器和标量，不计对象键。保留策略不缩小
已返回结果或 checkpoint。这是下一轮 trace 设计的基线，本轮没有改变 trace 结构和
`new_items` 语义。即使上限为 0，单次长 run 的堆峰值仍明显较高（见原始数据），
不承诺单次 run 内存恒定。路径长度可能使序列化体积略有变化。

原始数据：[24 个 run 堆采样](../../benchmarks/run_retention_report.json)、
[宿主订阅](../../benchmarks/run_retention_onchange_report.json)、
[300 轮工具的序列化和堆采样](../../benchmarks/run_retention_long_report.json)。
