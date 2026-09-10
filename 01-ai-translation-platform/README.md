# 通用 AI 翻译平台：逆向工程与可复现批处理

这是一个把接口逆向过程工程化的案例，也是一个可复现、可审计的 CSV/XLSX 批处理管线。项目把动态签名、鉴权、SSE 解析、有限重试、访问控制、断点恢复与原子导出拆成可测试组件，重点展示边界设计和证据口径，而不是包装一个现成在线服务。

当前真实接口验证状态为 **`degraded`**，不是可用服务：固定双向 20 条样本中仅 3 成功、17 `empty_result`，成功率 15%。这一状态可能随外部接口变化；公开代码和样例不能证明在线能力已经恢复。

## 逆向链路

一次完整链路按以下顺序组织：

1. 为 secret 请求计算动态签名；
2. 按需获取并缓存 token，遇到 401 鉴权失效时丢弃旧值且最多刷新一次；
3. 为 chat 请求计算动态签名，并对每次业务尝试只发送一个 POST；
4. 按事件解析 SSE 流，不把整段响应当作单个 JSON；
5. 由有限重试与访问控制决定是否等待、终止或有限切换显式启用的备用代理；
6. 批处理按 checkpoint 恢复，生成聚合摘要，并把结果原子写入新文件。

签名器只读取一次毫秒时钟，签名原文中的时间戳与最终请求参数中的时间戳来自同一个结果，避免二次取时造成参数漂移。JavaScript/Node 返回值使用 Base64 JSON 的 ASCII 安全信封，再由 Python 解码，以规避 Windows 下 ExecJS 子进程的非 ASCII 编码问题。

## 目录职责与证据边界

- `translation_platform/`：签名、协议、HTTP、token 生命周期、SSE、稳定性、批处理、checkpoint、摘要与存储等 Python 组件。
- `js/`：由 Node.js 执行的签名实现及 ASCII 安全返回信封。
- `scripts/`：历史只读审计、千行离线合成验证和低负载真实冒烟入口。
- `tests/`：组件测试、集成测试、公开证据与文档口径回归测试。
- `artifacts/public-evidence/`：可公开的人工合成小样例；只用于离线复核，不代表真实调用。
- `artifacts/validation/`：历史审计、离线合成和当前真实冒烟的公开安全聚合证据；不存放业务原文或完整响应。
- `artifacts/private/`：本地私有材料边界，不公开、不作为公开复现的输入，也不应被复制进发布目录。

历史只读审计、离线合成验证、当前真实冒烟是三类不同证据，不能把其中一类的数量或结论替代另一类。

## 安装与前置条件

要求 Python 3.9 兼容环境和 Node.js。Python 依赖包括 PyExecJS、requests、pandas 与 openpyxl：

```console
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

确认 Node 和 ExecJS 能找到同一个可用的 Node.js 运行时：

```console
node --version
python -c "import execjs; print(execjs.get().name)"
```

第二条命令应打印包含 `Node.js` 的运行时名称。这里只检查本地运行环境，不发送网络请求。

## 命令与输入输出

查看统一入口及子命令帮助：

```console
python main.py --help
python main.py translate --help
python main.py batch --help
```

单条参数校验示例：

```console
python main.py translate --text "人工合成示例文本" --from-lang zh-CHS --to-lang en
```

批量参数校验示例使用公开合成 CSV；它不会创建输出文件：

```console
python main.py batch --input artifacts/public-evidence/sample_input.csv --columns source_text --output validated-output.csv --from-lang zh-CHS --to-lang en --workers 1 --min-interval 1 --max-retries 3 --checkpoint synthetic-checkpoint.jsonl
```

`translate` / `batch` 当前只完成参数校验，并提供后续装配所需的组件化能力；它们未装配为公开的一键真实翻译或批量执行入口。命令成功退出不等于发生过翻译、网络请求或文件导出。

历史只读审计命令如下。`PRIVATE_TRANSLATION_LOG`、`PRIVATE_WRITE_LOG` 和 `PRIVATE_WORKBOOK` 是必须由使用者在本机替换的占位符；原始材料不随项目公开：

```console
python scripts/audit_history.py --translation-log PRIVATE_TRANSLATION_LOG --write-log PRIVATE_WRITE_LOG --workbook PRIVATE_WORKBOOK --evidence-output artifacts/validation/historical_evidence.json
```

运行 1000 行、每行两列的确定性离线 mock 验证：

```console
python -m scripts.validate_synthetic_batch --rows 1000 --evidence-path artifacts/validation/synthetic_batch_evidence.json
```

真实冒烟依赖不会公开的私有配置。先做只读结构检查；`PRIVATE_CONFIG` 和 `PRIVATE_CHECKPOINT` 同样只是本地占位符：

```console
python scripts/validate_live_smoke.py --config PRIVATE_CONFIG --check-config
```

在明确理解低负载边界后，真实冒烟与相同输入的缓存复跑命令分别为：

```console
python scripts/validate_live_smoke.py --config PRIVATE_CONFIG --checkpoint PRIVATE_CHECKPOINT --evidence artifacts/validation/live_smoke_evidence.json
python scripts/validate_live_smoke.py --config PRIVATE_CONFIG --checkpoint PRIVATE_CHECKPOINT --evidence artifacts/validation/live_smoke_replay_evidence.json
```

真实冒烟代码固定关闭代理。第二条命令只有在沿用同一成功/失败 checkpoint 和同一固定输入集时，才是可比较的缓存复跑。

静态编译检查与全量测试：

```console
python -m compileall -q main.py translation_platform scripts tests
python -m unittest discover -s tests -v
```

## 批处理能力

组件层支持：

- 读取 CSV 或 XLSX，并按列名选择一列或多列；
- 记录每个选中单元格的原始行列位置，将结果回填到原位置；
- 按语言方向和规范化文本去重，一个唯一任务可复用于多个位置；
- 以 JSONL checkpoint 保存成功、失败和待处理状态，中断后只处理未完成任务；
- 默认复用失败 checkpoint，只有显式指定 `--retry-failures` 才重试已有失败；
- 将结果原子写入不同于源文件的新 CSV/XLSX，不覆盖输入；
- 输出成功、失败、缓存命中、重试和延迟等聚合摘要。

这些是生产组件及离线验证覆盖的能力；不要据此声称 `main.py batch` 已执行整条管线。当前 CLI 的 batch 子命令只校验输入、输出、列、并发、间隔、重试、checkpoint 和代理开关等参数。

## 稳定性与访问控制边界

- 默认单线程，CLI 最多允许 2 个 worker；请求间隔不得低于配置值。
- 只对连接失败、超时以及可恢复的 500/502/503/504 做有限重试；重试次数有硬上限。
- 429 优先读取 `Retry-After`，返回停止或延后建议，不立即无限重试。
- 401 只触发一次 token 刷新；刷新后仍失败就终止本次操作。
- 代理默认关闭。只有显式启用备用策略时，401/403/429 才能在有限预算内轮换；其他 4xx 不轮换。
- 真实冒烟始终关闭代理，不用代理切换掩盖当前接口状态。

## 三类证据

### 1. 历史只读审计

口径来自 `artifacts/validation/historical_evidence.json`，审计脚本只读原始材料，公开的是聚合结果：

- translation log：1080 个物理行，1057 个成功记录、13 个失败记录、1028 个唯一输入、5 个无法解析行。物理行包含空白轮次分隔行，行数不等于唯一翻译数，也不能用几个分类简单相加替代原始口径。
- write log：1060 个物理行，1058 个成功记录、1028 个唯一输入、1 个重复记录、1 个无法解析行。
- 工作簿对账：1058 个可核验写入记录，其中位置和值均匹配 1027，不一致 31，无法定位 0。31 个不一致不能写成成功匹配。

这组证据说明历史材料中存在超过千条可审计记录，不证明当前外部接口仍稳定。

### 2. 离线合成

`artifacts/validation/synthetic_batch_evidence.json` 记录确定性 mock 验证：1000 行、2000 个输入单元格、250 个唯一任务、1750 个去重命中。首次 mock 业务操作为 251 次（包含 1 次受控重试）；相同输入第二轮为 0 次业务操作，250 个唯一任务全部由 checkpoint 命中。

这是无网络的 mock，不是 1000 次真实接口调用，也不是 251 次真实接口调用。它验证批处理、去重、有限重试、中断恢复、原位置回填、摘要和原子输出，不验证在线服务质量。

### 3. 当前真实

固定输入集为两个方向各 10 条，共 20 条。当前聚合结果是 3 成功、17 `empty_result`、成功率 15%，状态为 `degraded`。

- 恢复轮：18 个请求、2 个缓存命中、0 次重试，代理关闭。
- 同输入复跑：20 个缓存命中，0 个请求、0 个尝试、0 次重试，代理关闭。

当前结果没有达到可用性结论，也不能从历史数量或缓存复跑推导在线成功率。缓存复跑证明的是幂等与断点复用，不是新的真实响应。

## 已知限制

- 公开代码库不含真实配置、端点参数私值或访问凭据，因此不能直接复现实网请求。
- 当前真实响应大量为空，20 条固定样本中有 17 条 `empty_result`。
- CLI 尚未把通用组件装配成面向用户的一键真实翻译或批量执行流程。
- 签名规则、鉴权行为和返回格式由外部接口控制，实测状态会随其变化；应重新运行低负载冒烟并更新聚合证据，不能沿用旧结论。

## 安全边界

原始 Excel、完整日志、真实响应、私有 checkpoint、真实配置、代理地址、Cookie、token、凭据、`legacy/` 和开发计划均不公开。公开样例只使用人工编写或显式脱敏的数据，公开证据只保留复核结论所需的聚合字段。任何发布动作都应再次检查文件范围，确保私有材料没有越界。
