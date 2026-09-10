# 公开人工合成样例

本目录是离线 mock 合成证据，不代表当前接口实测或真实调用次数。所有文本均为人工编写的“合成会议”“示例城市”等示例内容，不来自真实业务、历史运行或在线服务。

`sample_input.csv` 含三条输入，其中两条是重复文本；去重后形成两个唯一任务。`sample_output.csv` 展示每条输入的确定性结果：重复项复用同一结果，两个唯一任务由确定性假翻译器（fixture）离线生成。

`run_summary.json` 是这次离线 fixture 演示的固定字段摘要：3 个输入单元、2 个唯一文本、2 个成功结果、0 个失败结果、0 个缓存命中和 0 次重试。摘要不记录原始响应、账户信息或运行环境位置。

摘要中的 cache_hits 包含未进入处理器的成功与失败 checkpoint；失败 checkpoint 属于负缓存。批前去重后复用的重复输入不算 checkpoint 缓存命中，因此始终满足 `request_count + cache_hits == unique_texts`。

successes、failures、error_counts 包含缓存的最终结果；retries、average_latency_ms、p95_latency_ms 只统计本轮 executed keys。全缓存时 retries、average_latency_ms、p95_latency_ms 均为 0。
