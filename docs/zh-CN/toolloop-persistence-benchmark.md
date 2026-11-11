# ToolLoop 持久化流 V2：传输与内存基准

测量日期 2026-11-11，Python 3.11.14。这是未发布源码实现的离线基准。
[原始报告](../../benchmarks/toolloop_persistence_report.json) 记录 Git 基线
`4ac6bcdce2b198868e2daad4ceb9f635a7d7e783`、工作区已修改标记、SDK 源码 SHA-256
`57058eea757a0c72cd3003a723cf891eae048ea49db483e2419b941ead412d4a` 和基准/示例脚本哈希。所有场景的源码哈希一致。

## 方法

普通根运行执行四次工具，每次返回 32,768 个文本字符；上下文窗口 8,192 token、输出预算
512 token，沿用 legacy 压缩策略，不提供 read_file。委派场景使用一个叶子 ToolAgent；
待审核场景不执行工具。脚本分别测 None/2/0 保留上限，下表无特殊说明均使用 0。
不在内存中保存请求、事件或历史批次，宿主 reducer 只保留有效上下文，不模拟完整长期数据库。
消费完事件并释放引用后，执行 GC 再采样。

字节数是所有 SDK 流事件的紧凑 UTF-8 JSON，包含展示事件和持久化批次。报告也记录
tracemalloc 下的观察者序列化耗时，该时间不代表生产延迟保证。内存是 Python 分配而非 RSS，
包含宿主当前投影、防重放 ID、SDK 缓存及序列化观察开销。SDK 对象图归因对共享引用只计一次。
普通场景中的 DAG 数据仅用于旧 API 对照，不代表增加了 DAG 持久化模式。

## 流式传输量

| 场景 | 普通模式字节 | 持久化模式字节 | 减少 |
| --- | ---: | ---: | ---: |
| 16 次独立运行 | 8,945,268 | 2,619,994 | 70.7% |
| 同会话 24 轮 | 13,444,404 | 4,031,250 | 70.0% |
| 16 次工具代理委派 | 4,697,942 | 2,638,022 | 43.8% |

最后一次独立运行的完整终态事件为 422,567 字节，持久化终态批次为
5,050 字节。每次 ACK 延迟 1 ms 后，总字节仍为 2,619,994，
与不延迟场景一致，就绪队列最高 2 个事件。
该工作负载不测试最大帧大小；另有慢 token 读取回归，在就绪队列 2 个事件/256 字节预算下
验证背压和正文不丢失。

## 内存和保留

| 场景 | 普通峰值 MiB | 持久化峰值 MiB | 普通 GC 后字节 | 持久化 GC 后字节 |
| --- | ---: | ---: | ---: | ---: |
| 独立运行 | 1.665 | 0.823 | 163,679 | 170,139 |
| 同会话 | 1.675 | 0.883 | 343,359 | 348,797 |
| 委派 | 1.255 | 0.876 | 178,398 | 178,731 |

保留上限 0 已能让两种模式释放普通运行历史。持久化主要降低 trace、结果和序列化的临时峰值，
不承诺每种场景的 GC 后内存都更少。同会话场景仍保留有效上下文。普通无限保留继续增长，不会
被新模式偷偷覆盖。

24 个待审核运行、普通保留上限 None 时，普通模式保留
24 个状态/24 个 checkpoint，GC 后
1,650,831 字节。持久化模式在审核批次 ACK 后保留
0 个状态/0 个 checkpoint，GC 后
267,901 字节。所有保留配置均释放已确认的审核重状态。
防重放 ID 仍然保留，因此不承诺进程总内存随时间绝对恒定。

## 短审核运行的传输取舍

普通审核事件流合计 194,756 字节，持久化合计 379,024 字节。
持久化审核批次包含可移植 V9 checkpoint 和冻结 plan；普通 RunResult 序列化排除 plan，
单独的普通事件流不能提供同等的跨进程恢复记录。宿主另外传输 checkpoint 的字节不在此基准内。
因此，短审核运行的 SDK 事件流可能更大，同时其 checkpoint 不含 trace，确认后的缓存更小。
本期保留 V9 完整状态和已有重复字段，不增加另一种瘦 checkpoint 协议。

## 复现

```bash
uv run python -m benchmarks.run_retention --runs 16 --steps 4 --output-chars 32768
uv run python -m benchmarks.run_retention --runs 16 --steps 4 --output-chars 32768 --persistent
uv run python -m benchmarks.run_retention --runs 24 --steps 4 --output-chars 32768 --same-conversation
uv run python -m benchmarks.run_retention --runs 24 --steps 4 --output-chars 32768 --same-conversation --persistent
uv run python -m benchmarks.run_retention --runs 16 --steps 4 --output-chars 32768 --delegate
uv run python -m benchmarks.run_retention --runs 16 --steps 4 --output-chars 32768 --delegate --persistent
uv run python -m benchmarks.run_retention --runs 24 --hold-reviews
uv run python -m benchmarks.run_retention --runs 24 --hold-reviews --persistent
uv run python -m benchmarks.run_retention --runs 16 --steps 4 --output-chars 32768 --persistent --ack-delay 0.001
```

契约见[持久化流](conversation-changes.md)，旧回调/trace 历史数据见
[此前保留基准](run-retention-benchmark.md)。缓存输入由实际请求前缀回归验证，本字节基准
不测服务端缓存命中率。不增加拆帧或固定帧上限。
