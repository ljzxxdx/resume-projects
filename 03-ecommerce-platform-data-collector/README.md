# 电商平台商品、评论与直播数据采集

> 当前状态：`🟢 核心功能与离线测试已完成`
> 代码范围：DrissionPage 与 Selenium 均已实现商品/评论、直播采集流程。
> 验证边界：双引擎商品/评论均已完成低负载真实运行；双引擎直播均已完成真实运行，Selenium 已验证3个不同直播间。DrissionPage旧运行暴露的 `liveId` 解析缺陷已在两个引擎中修复并通过回归测试，但修复后尚未再次进行DrissionPage真实直播复跑。项目结论限于小规模简历项目验收，不代表生产环境长期稳定性。

本项目只执行公开页面的只读采集，不自动登录、不绕过验证码或风控，也不执行点赞、关注、加购、下单等账号操作。请遵守网站规则并控制采集规模。

## 1. 目录职责

```text
main.py                         # 唯一命令行入口
taobao_collector/
├── cli.py                      # 参数解析、引擎分派、摘要落盘
├── config.py                   # .env -> AppConfig
├── models.py                   # 商品、评论、直播间、运行摘要模型
├── selectors.py                # 双引擎共用选择器与结构约束
├── parsers.py                  # 数值纯函数解析
├── identity.py                 # URL规范化与唯一键
├── exporters.py                # JSONL/CSV/XLSX原子导出与跨运行去重
├── quality.py                  # 数据质量统计
├── redaction.py                # 可公开样例脱敏
├── observability.py            # 日志、阻塞识别、截图、人工登录等待
└── collectors/                 # 两种引擎的独立采集与会话实现
tests/fixtures/                 # 合成、脱敏HTML样本
scripts/refresh_taobao_login.py # 交互式刷新专用Profile登录态
requirements.txt               # 运行依赖
requirements-dev.txt           # 离线测试依赖
.env.example                   # 不含凭证的配置模板
```

公开仓库只保留维护后的实现、测试和合成样本；真实采集结果、浏览器Profile、日志和本机配置均不提交。

## 2. 安装与配置

建议使用 Python 3.9 或兼容版本：

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt  # 开发测试依赖
Copy-Item .env.example .env
```

常用配置：

- `TAOBAO_BROWSER_PATH`：本机Chrome路径；留空时由引擎查找。
- `TAOBAO_CHROME_MAJOR_VERSION`：可选的Chrome主版本；当自动下载的Driver与本机Chrome不一致时填写，例如 `151`。
- `TAOBAO_USER_DATA_DIR`：专用浏览器Profile目录，建议固定为项目内目录，如 `artifacts/manual-chrome-profile`。
- `TAOBAO_HEADLESS=false`：首次登录和真实验收建议使用可见窗口。
- `TAOBAO_DEFAULT_WAIT`、`TAOBAO_POLL_INTERVAL`：有界显式等待与轮询间隔。
- `TAOBAO_MAX_RETRIES`、`TAOBAO_RETRY_BASE_DELAY`：有限重试和线性退避。
- 商品、评论、翻页和直播停顿区间及最大滚动次数均在 `.env.example` 中列出。

`.env` 和浏览器Profile含本机路径或登录凭证，已由 `.gitignore` 排除；不得提交、复制给他人或制作公开样例。

### Selenium驱动管理

Selenium分支使用 `undetected-chromedriver>=3.5.5,<4.0` 创建
WebDriver。项目不写死ChromeDriver路径；该依赖会根据本机Chrome选择、下载并修补驱动，
因此全新环境首次启动可能需要联网。当前版本的默认缓存目录可用以下命令确认：

```powershell
python -c "from undetected_chromedriver.patcher import Patcher; print(Patcher.data_path)"
```

若启动失败，先检查Chrome是否可正常打开、网络是否允许下载，以及上述缓存目录是否可写；
程序会以 `SeleniumDriverSetupError` 保留底层异常链。Selenium专属模块按引擎延迟加载，
其依赖故障不会阻止DrissionPage入口加载。

若缓存Driver的主版本高于或低于本机Chrome，在 `.env` 中设置
`TAOBAO_CHROME_MAJOR_VERSION`；项目会把该值作为 `version_main` 传给
`undetected-chromedriver`，让它下载对应主版本的Driver。

`TAOBAO_HEADLESS=true` 会传入Chromium的新无头参数，但当前项目没有完成该组合的真实验收；
首次登录、登录态刷新和真实采集应保持 `false`。

## 3. 登录准备与Profile持久化

首次运行或登录态失效时执行：

```powershell
python scripts\refresh_taobao_login.py
```

在Chrome窗口内手动登录。离开登录页后脚本会正常关闭浏览器；Cookie等凭证由Chrome写入 `TAOBAO_USER_DATA_DIR`，后续两种引擎复用的是该磁盘Profile中的登录数据，不是同一个浏览器进程。

注意：

- 同一Profile不能被两个Chrome进程同时占用；商品、直播及两种引擎必须串行运行。
- 当前CLI每条命令只接收一个关键词，并会新建、最终关闭自己拥有的浏览器；不支持多个关键词或两种引擎共享同一浏览器会话。
- 出现登录页时，程序最多等待 `TAOBAO_LOGIN_WAIT_TIMEOUT` 秒供人工处理。
- 出现滑块、验证码、安全验证或访问受限时立即停止；程序只保留截图和摘要，不自动处理验证。
- 不要为微小测试频繁启动/关闭浏览器。先运行离线测试；恢复真实验收后每次只执行一条低负载命令，人工确认账号状态并拉开运行间隔。

## 4. 运行命令

查看帮助不会启动浏览器：

```powershell
python main.py --help
python main.py products --help
python main.py live --help
```

商品与评论：

```powershell
python main.py products --engine drission --keyword 德化瓷 --product-limit 5 --comment-limit 10 --timeout 30 --output-dir artifacts\runs\drission
python main.py products --engine selenium --keyword 德化瓷 --product-limit 5 --comment-limit 10 --timeout 30 --output-dir artifacts\runs\selenium
```

默认启用“销量至少30、评论总数至少20”业务筛选；评论不足时不保留商品。可调整阈值或关闭：

```powershell
python main.py products --engine drission --keyword 德化瓷 --min-sales 50 --min-comments 30
python main.py products --engine drission --keyword 德化瓷 --no-quality-filter
```

直播命令已完成小规模真实验证：

```powershell
python main.py live --engine drission --keyword 德化瓷 --live-limit 3 --timeout 30 --output-dir artifacts\runs\drission-live
python main.py live --engine selenium --keyword 德化瓷 --live-limit 3 --timeout 30 --output-dir artifacts\runs\selenium-live
```

直播页面结构和站点风控可能变化，运行时仍应采用低负载参数并人工确认账号状态。

## 5. 两种引擎的实现差异

两种引擎共用配置、选择器、解析、模型、唯一键、导出、质量和可观测性模块，但浏览器控制代码彼此独立：

| 方面 | DrissionPage | Selenium |
| --- | --- | --- |
| 会话对象 | `Chromium` / Tab对象 | `WebDriver` / window handle |
| 驱动创建 | DrissionPage管理Chromium连接 | `undetected-chromedriver`选择、下载并缓存驱动 |
| 等待 | 元素等待加有界轮询 | `WebDriverWait` 与 expected conditions |
| 标签管理 | 按Tab对象和标签ID跟踪、关闭 | 记录句柄、切换并在 `finally` 恢复 |
| 操作方式 | DrissionPage元素API | WebElement、ActionChains、JavaScript容器滚动 |
| 小规模验收状态 | 商品、评论、直播均已运行 | 商品、评论、直播均已运行 |

这不是用一层通用浏览器包装器模拟两个引擎；公共业务规则复用，页面控制细节分别实现和测试。

## 6. 数据字段与输出

- 商品：追踪字段、来源URL/ID、名称、价格、销量、采集时间、状态和错误信息。
- 评论：追踪字段、商品ID、用户名、规格、正文、采集时间和状态。
- 直播间：追踪字段、直播间ID、账号名、简介、观看数、粉丝数、商品数、采集时间和状态。
- 运行摘要：参数、开始/结束时间、三类记录数、新增/成功/重复/缺失/失败计数、截图索引和错误信息。

每次运行写入 `<output-dir>/<run_id>/`：

- `products.*`、`comments.*` 或 `live_rooms.*`：JSONL、CSV、XLSX三种格式。
- `run_summary.json`：运行状态与汇总计数。
- `export_stats.json`：新增、成功、重复、缺失、失败统计。
- `data_quality.json`：记录数、唯一数、重复数、必填缺失和异常状态。
- `redacted_sample.json`：受限公开样例；评论正文默认不公开，只有人工批准唯一键才可进入。
- `screenshot_index.json`：本次运行证据索引。

跨运行去重索引位于输出根目录。相同业务记录再次写入同一输出根目录时不重复追加；不要用不同输出根目录测试跨运行幂等。

## 7. 等待、重试与错误处理

- 页面状态使用显式等待，不用无界循环；DrissionPage轮询均受 `WaitPolicy.timeout` 截止时间约束。
- 默认最多3次重试，延迟为10、20、30秒；登录、验证码、风控和键盘中断不重试。
- Selenium区分元素超时、选择器无效、窗口丢失、驱动错误和未分类错误，并在重试耗尽后保留具体上下文。
- Selenium驱动创建失败会提示检查Chrome版本、网络和缓存权限；创建后的初始化失败会立即关闭已创建的Driver，并保留原始异常。
- 两引擎在正常、异常和中断路径清理自身创建的详情标签与会话；调用方注入的会话不由采集器关闭。
- 离线HTML契约测试能报告缺失选择器的名称、业务含义、表达式和数量约束。

## 8. 离线验证

以下命令不访问淘宝、不启动浏览器，也不读取历史Excel：

```powershell
python -m compileall -q main.py taobao_collector scripts tests
python -m unittest discover -s tests -v
```

HTML样本位于 `tests/fixtures/`，均使用 `example.test` 域名和合成内容；离线测试不依赖真实账号、浏览器Profile或历史Excel。

## 9. 小规模验证结果

| 引擎 / 关键词 | 商品 | 唯一评论 | 结果 |
| --- | ---: | ---: | --- |
| DrissionPage / 德化瓷 | 5 | 49 | 达到本轮数量目标 |
| DrissionPage / 苗族银饰 | 5 | 4 | 评论数量未达标 |
| Selenium / 德化瓷 | 0 | 0 | 出现滑块验证后停止 |
| Selenium / 苗族银饰 | 5 | 15 | 2026-08-15复测成功；原始评论17条、重复2条 |
| Selenium / 陶瓷杯垫 | 5 | 48 | 2026-08-15低负载复测成功；原始评论50条、重复2条 |

2026-08-15 DrissionPage直播流程成功返回3条记录，但旧版 `liveId` 解析缺陷使其仅生成1个唯一键；该缺陷已在双引擎中修复并通过回归测试。Selenium修正直播入口与 `liveId` 后，以“女装”完成3个不同直播间的真实采集，新增3条、重复0条、缺失0条、失败0条。真实重复运行的 `new_count=0` 尚未单独执行，幂等行为由离线测试覆盖。

本机Chrome主版本绑定、登录态、Selenium商品搜索与直播采集均已完成低负载成功运行。DrissionPage修复后的直播解析仍可在账号与站点状态合适时补做一次真实复跑。现有CLI不支持跨命令复用同一浏览器会话。
