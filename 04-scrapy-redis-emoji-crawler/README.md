# Scrapy-Redis 表情包分布式爬虫

本项目使用 Scrapy 和 scrapy-redis 抓取表情包列表页中的图片。三个采集 Spider 采用不同的启动和翻页方式，但统一输出 `EmojiItem`，并共用图片解析、Redis 去重、图片下载、元数据补全和结果输出链路；另有一个 `image_retry` Spider 专门重放图片下载失败任务。

> 安全说明：本文不记录真实 Redis 密码。Redis 命令中的 `<REDIS_PASSWORD>` 需要替换为本机配置；真实连接串只写入被忽略的 `.env` 或节点环境变量。曾经写入源码的旧密码应视为已暴露，如果仍在使用，必须先在 Redis 管理端轮换，再生成新的运行证据。

## 一、项目结构

```text
scrapyWithRedis/
├── .env.example                     # 不含真实凭据的环境变量模板
├── .gitignore                       # 阻止本地 .env 被提交
├── scrapy.cfg
├── requirements.txt                 # 已验证的 Python 直接依赖版本
├── start.py                         # 统一启动入口
├── aggregate_outputs.py             # 聚合当前节点的 Worker 文件
├── export_redis_results.py          # 从 Redis 导出跨节点总结果
├── failure_tasks.py                 # 查看和原子重放失败任务
├── scripts/linux/
│   ├── init_env.sh                  # Ubuntu 初始化与虚拟环境安装
│   ├── enqueue_urls.sh              # 原子去重投放多个起始 URL
│   ├── start_workers.sh             # 默认启动两个独立 Worker
│   ├── stop_workers.sh              # 校验 PID 归属后停止 Worker
│   ├── observe_redis.sh             # 按真实类型查看 Redis 数量
│   ├── replay_page_failures.sh      # 页面失败批量重放
│   ├── replay_image_failures.sh     # 图片失败批量重放
│   ├── redis_admin.py               # Redis 投放和观测核心
│   └── crontab.example              # 最小定时重放示例
├── samples/
│   ├── sample-30-sanitized.jl       # 30 条成功 Item 脱敏样例
│   ├── failed-item-sanitized.jl     # 可控图片失败 Item 脱敏样例
│   └── artifact-validation.json     # 图片文件与 MD5 校验摘要
├── README.md
├── output/
│   ├── items-worker-a-1234.jl       # Worker/进程独立结果
│   └── all-items.jl                 # 可重复生成的聚合结果
├── images/                          # 默认图片目录，含 30 张可核验样例
├── tests/
│   ├── fixtures/emoji_listing.html   # 不依赖目标站的固定解析样本
│   ├── test_items.py
│   ├── test_parsers.py
│   ├── test_pipeline_flow.py
│   ├── test_pipelines.py
│   ├── test_spider_parsing.py
│   └── test_start.py
└── scrapyWithRedis/
    ├── items.py                     # EmojiItem 字段定义
    ├── parsers.py                   # 三个 Spider 共用的图片解析逻辑
    ├── pipelines.py                 # 校验、去重、下载、补全和输出
    ├── failure_store.py             # Redis 失败入队/重放原子操作
    ├── redis_start_queue.py         # 为普通 CrawlSpider 组合 Redis 起始队列消费能力
    ├── start_urls.py                # 起始 URL 校验、规范化和输入内去重
    ├── middlewares.py               # UA、日志与页面失败留存
    ├── settings.py
    └── spiders/
        ├── dmoz.py
        ├── myspider_redis.py
        ├── mycrawler_redis.py
        └── image_retry.py
```

## 二、当前数据流

```text
start_urls / Redis start_urls
        ↓
Spider 获取列表页和分页链接
        ↓
extract_emoji_items(response, logger)
        ↓
EmojiItem(img_url, title, source_url)
        ↓
RequiredFieldsPipeline
        ↓
RedisItemUrlDedupPipeline
        ↓
EmojiImagesPipeline
        ↓
MetadataPipeline
        ↓
ImageFailurePipeline
        ↓
JsonLinesPipeline
        ↓
scrapy_redis.pipelines.RedisPipeline
```

图片下载成功或失败后，Item 才会进入元数据补全和结果输出环节，因此 `OUTPUT_DIR/items-<worker_id>-<pid>.jl` 与 Redis 中不会出现尚未经过图片处理的半成品 Item。

## 三、三个采集 Spider

| Spider 名 | 基类 | 起始 URL 来源 | 翻页方式 |
|---|---|---|---|
| `dmoz` | `CrawlSpider` | 默认 `start_urls`、直接参数或 Linux Worker 使用的 Redis List | `Rule + LinkExtractor` |
| `myspider_redis` | `RedisSpider` | Redis List：`scrapyWithRedis:myspider_redis:start_urls` | `response.follow()` 跟进第一页和“下一页” |
| `mycrawler_redis` | `RedisCrawlSpider` | Redis List：`scrapyWithRedis:mycrawler_redis:start_urls` | `Rule + LinkExtractor` |

三个 Spider 的 `parse_emoji_image()` 都委托给 `parsers.py` 中的：

```python
extract_emoji_items(response, self.logger)
```

公共解析器执行以下操作：

- 从 `//img[@class="ui image lazy"]` 提取 `data-original` 和 `alt`。
- 使用 `response.urljoin()` 将图片地址规范化为绝对 URL。
- 缺少 `img_url` 时记录包含 `source_url` 的 warning，并跳过该图片。
- 缺少 `title` 时保留 Item，同时记录 warning。
- 每张图片输出一个 `EmojiItem`，其中 `source_url` 是图片所在列表页。

`dmoz` 保持 `DmozSpider(CrawlSpider)` 的继承声明。直接运行时使用类变量或参数 URL；由 `start_workers.sh` 启动时进入 Redis 模式，从 `scrapyWithRedis:dmoz:start_urls` 分批领取任务。每次领取数量由 `DMOZ_REDIS_BATCH_SIZE` 控制，范围为 1～50；调度暂时空闲时最多等待 `DMOZ_REDIS_MAX_IDLE_TIME` 秒，期间有新任务会继续工作。

`dmoz` 和 `mycrawler_redis` 的 `Rule` 只提取分页栏中 `href` 包含 `page` 的链接，不再限制为前两个分页链接；分页栏之外的链接仍不会被该 Rule 提取。`myspider_redis` 则继续按“下一页”链接顺序翻页。

## 四、启动方式

### 1. 使用统一入口 `start.py`

直接运行时默认启动 `dmoz`：

```bash
python start.py
```

通过 `--spider` 选择 Spider：

```bash
python start.py --spider dmoz
python start.py --spider myspider_redis
python start.py --spider mycrawler_redis
python start.py --spider image_retry
```

也可以通过环境变量 `SPIDER_NAME` 选择；`--spider` 的优先级高于环境变量。

PowerShell：

```powershell
$env:SPIDER_NAME = "mycrawler_redis"
python start.py
```

`start.py` 不认识的参数会继续传给 Scrapy，例如：

```bash
python start.py --spider dmoz -a start_url=https://fabiaoqing.com/biaoqing -o output.json
```

`dmoz` 也可以重复使用 `--start-url` 传入多个 URL。`start.py` 会校验、规范化并按输入顺序去重：

```bash
python start.py --spider dmoz \
  --start-url 'https://fabiaoqing.com/biaoqing/lists/page/1.html' \
  --start-url 'https://fabiaoqing.com/biaoqing/lists/page/2.html'
```

`--start-url` 只适用于 `dmoz`，不能和 `-a redis_start=true` 同时使用；原有单 URL 的 `-a start_url=...` 仍然兼容。

### 2. 直接使用 Scrapy 命令

普通 Spider：

```bash
scrapy crawl dmoz
scrapy crawl dmoz -a start_url=https://fabiaoqing.com/biaoqing
```

Redis Spider 需要先投放起始 URL，再启动对应进程：

```bash
redis-cli -a <REDIS_PASSWORD> LPUSH scrapyWithRedis:myspider_redis:start_urls "https://fabiaoqing.com/biaoqing"
scrapy crawl myspider_redis
```

```bash
redis-cli -a <REDIS_PASSWORD> LPUSH scrapyWithRedis:mycrawler_redis:start_urls "https://fabiaoqing.com/biaoqing"
scrapy crawl mycrawler_redis
```

`domain` 参数是可选的。需要限制允许访问的域名时可以传入：

```bash
scrapy crawl myspider_redis -a domain=fabiaoqing.com
scrapy crawl mycrawler_redis -a domain=fabiaoqing.com
```

未传 `domain` 时，两个 Redis Spider 的 `allowed_domains` 是空列表，表示不启用域名限制；传入多个域名时使用逗号分隔。

## 五、统一 Item 字段

一个 `EmojiItem` 表示一张图片，不使用 Scrapy `ImagesPipeline` 默认的 `image_urls` 和 `images` 复数字段。

| 字段 | 填写者 | 含义 |
|---|---|---|
| `img_url` | 公共解析器 | 图片下载地址，必填 |
| `title` | 公共解析器 | 图片标题，可为空 |
| `source_url` | 公共解析器 | 图片所在列表页，必填 |
| `image_path` | `EmojiImagesPipeline` | 图片相对于 `IMAGES_STORE` 的保存路径 |
| `image_checksum` | `EmojiImagesPipeline` | Scrapy 返回的已下载文件校验值 |
| `download_status` | `EmojiImagesPipeline` | Scrapy 下载状态，失败时为 `failed` |
| `download_error` | `EmojiImagesPipeline` | 失败异常摘要；成功时为空字符串 |
| `crawled` | `MetadataPipeline` | UTC 时区的 ISO 8601 时间 |
| `spider` | `MetadataPipeline` | 产生该 Item 的 Spider 名称 |
| `worker_id` | `MetadataPipeline` | 环境变量 `WORKER_ID`，未配置时为空字符串 |

完整 Item 示例：

```json
{
  "img_url": "https://img.example.com/emoji.gif",
  "title": "示例表情包",
  "source_url": "https://fabiaoqing.com/biaoqing/lists/page/2.html",
  "image_path": "full/example.jpg",
  "image_checksum": "0123456789abcdef",
  "download_status": "downloaded",
  "download_error": "",
  "crawled": "2026-07-18T09:34:58.965488+00:00",
  "spider": "dmoz",
  "worker_id": "worker-a"
}
```

## 六、Pipeline 顺序与职责

`settings.py` 中的顺序为：

| 优先级 | Pipeline | 职责 |
|---:|---|---|
| 100 | `RequiredFieldsPipeline` | 兜底校验 `img_url`、`source_url`，缺失或空白时丢弃 Item |
| 200 | `RedisItemUrlDedupPipeline` | 使用 Redis `SADD` 按 `img_url` 原子去重 |
| 300 | `EmojiImagesPipeline` | 下载单张图片，并把结果展开到当前 Item |
| 400 | `MetadataPipeline` | 填写 `crawled`、`spider`、`worker_id` |
| 450 | `ImageFailurePipeline` | 将失败图片原子去重后写入 Redis 失败队列，Item 仍继续向后传递 |
| 500 | `JsonLinesPipeline` | 将完整 Item 追加写入 Worker/进程独立的 JSON Lines 文件 |
| 600 | `RedisPipeline` | 将完整 Item 写入 `scrapyWithRedis:<spider>:items` Redis List |

### 图片下载

`EmojiImagesPipeline` 继承 Scrapy 的 `ImagesPipeline`，直接从单图片 Item 的 `img_url` 创建一个请求，并使用 `source_url` 作为图片请求的 `Referer`。

- 下载成功：填写 `image_path`、`image_checksum`、`download_status`，并将 `download_error` 设为空字符串。
- 下载失败：不丢弃 Item；将路径和校验值设为空字符串、`download_status` 设为 `failed`，并记录异常摘要。

Scrapy 的 `FilesPipeline.media_failed()` 会把原始异常写入日志后抛出一个可能没有消息的 `FileException`。自定义 Pipeline 会在这个边界保存原始 Failure 摘要，确保真实连接失败等场景的 `download_error` 不为空。

默认图片目录为项目根目录下的 `images`，可通过环境变量覆盖：

```powershell
$env:IMAGES_STORE = "D:\crawler-data\emoji-images"
python start.py
```

在当前节点的 `.env` 中设置 Worker 标识：

```dotenv
WORKER_ID=worker-a
```

### Worker 输出隔离与聚合

`JsonLinesPipeline` 使用 Worker 标识和当前进程 PID 生成文件名：

```text
OUTPUT_DIR/items-<worker_id>-<pid>.jl
```

例如两个进程即使误用了相同 `WORKER_ID`，仍会分别写入：

```text
output/items-worker-a-1001.jl
output/items-worker-a-1002.jl
```

`worker_id` 中不适合文件名的字符会被替换为下划线；未配置时文件名使用 `worker`。PID 保证同时运行的进程不会打开同一个文件。

需要汇总当前节点的文件时运行：

```bash
python aggregate_outputs.py
```

默认读取 `OUTPUT_DIR` 下所有 `items-*.jl`，覆盖生成 `OUTPUT_DIR/all-items.jl`。也可以明确指定路径：

```bash
python aggregate_outputs.py --input-dir output --output output/all-items.jl
```

本地聚合器按文件名排序并校验每一行 JSON。它每次覆盖目标文件，也不会把 `all-items.jl` 或旧版 `output.jl` 当作输入，因此重复运行不会不断追加同一批聚合数据。它只能看到执行命令所在节点的文件。

所有节点的完整 Item 同时写入共享 Redis。需要跨节点总结果时，在任意能连接同一 Redis 的节点执行：

```bash
python export_redis_results.py
```

默认读取各 Spider 的 `scrapyWithRedis:<spider>:items`，分批扫描并覆盖生成 `OUTPUT_DIR/all-items-from-redis.jl`。导出开始时会固定每个列表的长度边界，不删除或弹出 Redis 数据；重复执行会重新生成文件。相同 `img_url` 同时存在失败证据和后来重试成功结果时，导出器优先保留成功结果，否则保留 `crawled` 较新的记录。可使用 `--output`、`--batch-size` 和可重复的 `--spider` 参数调整导出范围。

### 环境变量模板

项目根目录的 `.env.example` 列出了当前支持的运行参数，示例中不包含 Redis 密码。每个节点部署时复制一次，并在自己的 `.env` 中填写 Redis 密码、Worker 标识和运行参数；真实 `.env` 已被 `.gitignore` 忽略：

```bash
cp .env.example .env
```

PowerShell：

```powershell
Copy-Item .env.example .env
```

编辑 `.env` 后直接启动，不需要逐个向终端输入环境变量：

```bash
python start.py
```

`settings.py` 使用 `python-dotenv` 自动读取项目根目录的 `.env`。配置优先级为“系统环境变量 > `.env` > `settings.py` 默认值”，因此 Linux 服务、CI 或容器仍可使用系统环境变量临时覆盖节点配置。

## 七、去重机制

### 1. 请求级分布式去重

当前配置为：

```python
DUPEFILTER_CLASS = "scrapy_redis.dupefilter.RFPDupeFilter"
SCHEDULER_DUPEFILTER_KEY = "scrapyWithRedis:dupe:requests"
SCHEDULER = "scrapy_redis.scheduler.Scheduler"
SCHEDULER_PERSIST = True
```

所有 Spider 和 Worker 共用 Redis Set `scrapyWithRedis:dupe:requests`。请求指纹由请求方法、规范化 URL 和请求体生成，所以普通后续请求可以跨 Spider、跨 Worker 去重。

需要注意：Scrapy 和 scrapy-redis 默认给起始请求设置 `dont_filter=True`。因此 `start_urls` 和 Redis 起始队列里的任务仍会执行；它们产生的普通后续请求才进入全局请求去重。

共享 Key 还意味着：如果两个 Spider 准备调度同一个后续页面，最先执行 Redis `SADD` 的请求会保留，另一个 Spider 的请求会被过滤。

### 2. Item 图片 URL 分布式去重

```python
ITEM_URL_DUPE_KEY = "scrapyWithRedis:dupe:item_urls"
```

`RedisItemUrlDedupPipeline` 使用 Redis `SADD`，按照完整 `img_url` 在所有 Spider 和 Worker 之间去重。两个 Item 即使 `title` 或 `source_url` 不同，只要 `img_url` 相同，后到的 Item 就会被丢弃。

该标记发生在图片下载之前。图片失败重放使用 Redis Lua 脚本，在领取失败记录的同一原子操作中清理对应 `img_url`，使专用重试 Item 能重新进入图片 Pipeline；同一失败记录无法被重复领取。

### 3. 当前未实现内容去重

`image_checksum` 当前只用于记录下载结果，不参与去重。两个不同 URL 即使返回完全相同的文件，仍会分别作为两张图片处理。

## 八、Redis Key

| Key | 类型 | 作用 |
|---|---|---|
| `scrapyWithRedis:<spider>:start_urls` | List | Redis Spider 的起始任务队列 |
| `scrapyWithRedis:<spider>:dupe:start_urls` | Set | Linux 投放脚本使用的起始 URL 原子去重集合 |
| `scrapyWithRedis:<spider>:requests` | scrapy-redis 队列 | 对应 Spider 尚未处理的调度请求 |
| `scrapyWithRedis:dupe:requests` | Set | 所有 Spider 共用的请求指纹 |
| `scrapyWithRedis:dupe:item_urls` | Set | 所有 Spider 共用的图片 URL |
| `scrapyWithRedis:<spider>:items` | List | `RedisPipeline` 输出的完整 Item |
| `scrapyWithRedis:<spider>:failures:pages` | List | Scrapy 重试耗尽后的页面失败记录 |
| `scrapyWithRedis:<spider>:failures:pages:dupe` | Set | 对应 Spider 的页面失败去重指纹 |
| `scrapyWithRedis:<spider>:failures:images` | List | 图片下载失败记录，失败 Item 本身仍正常输出 |
| `scrapyWithRedis:failures:images:dupe` | Set | 所有 Spider 共用的图片失败 URL 去重集合 |
| `scrapyWithRedis:image_retry:start_urls` | List | `image_retry` 专用起始任务队列 |

常用观测命令：

```bash
redis-cli -a <REDIS_PASSWORD> LLEN scrapyWithRedis:myspider_redis:start_urls
redis-cli -a <REDIS_PASSWORD> LLEN scrapyWithRedis:mycrawler_redis:start_urls
redis-cli -a <REDIS_PASSWORD> SCARD scrapyWithRedis:dupe:requests
redis-cli -a <REDIS_PASSWORD> SCARD scrapyWithRedis:dupe:item_urls
redis-cli -a <REDIS_PASSWORD> LLEN scrapyWithRedis:dmoz:items
redis-cli -a <REDIS_PASSWORD> LRANGE scrapyWithRedis:dmoz:items 0 1
redis-cli -a <REDIS_PASSWORD> LLEN scrapyWithRedis:mycrawler_redis:failures:pages
redis-cli -a <REDIS_PASSWORD> LLEN scrapyWithRedis:mycrawler_redis:failures:images
```

### 失败查看与重放

查看指定 Spider 的页面或图片失败记录：

```bash
python failure_tasks.py show page --spider mycrawler_redis
python failure_tasks.py show image --spider mycrawler_redis
```

输出中的 `failure_id` 用于重放。页面任务默认进入 `mycrawler_redis` 起始队列；该 Spider 的 `parse_start_url()` 会直接解析被重放的列表页，同时其 Rule 继续发现分页链接：

```bash
python failure_tasks.py replay page --spider mycrawler_redis --failure-id <FAILURE_ID>
```

`--target-spider` 仅用于已经实现“直接解析重放 URL”能力的其他 Redis Spider；当前经过验证的默认接收端是 `mycrawler_redis`。图片失败任务进入 `image_retry` 专用队列：

```bash
python failure_tasks.py replay image --spider mycrawler_redis --failure-id <IMG_URL>
python start.py --spider image_retry
```

需要重放某个 Spider 当前失败队列的全部记录时使用 `--all`：

```bash
python failure_tasks.py replay page --spider mycrawler_redis --all
python failure_tasks.py replay image --spider mycrawler_redis --all
```

也可以限制本次最多处理的快照记录数，先进行小批量试跑：

```bash
python failure_tasks.py replay page --spider mycrawler_redis --all --limit 100
python failure_tasks.py replay image --spider mycrawler_redis --all --limit 100
```

`--failure-id` 与 `--all` 必须二选一，`--limit` 只能和 `--all` 一起使用。批量重放只读取一次命令启动时的队列快照；执行期间新产生的失败记录留在队列中，等待下次处理。命令结束会输出 `scanned`、`queued`、`already_claimed` 和 `invalid` 统计，无效记录会保留在失败队列中，不会静默删除。

入队和重放都由 Redis Lua 脚本完成。页面失败按请求指纹、图片失败按 `img_url` 去重；重放会从失败列表领取精确的原始记录，同一记录重复执行只会有一次成功入队。若重试再次失败，会形成一条新的失败证据；若成功，跨节点 Redis 导出器会优先选择成功结果。

由于 `SCHEDULER_PERSIST=True`，调度队列和请求指纹不会在 Spider 正常关闭时自动清除。需要从头验证请求去重时，应先停止相关 Spider，再按测试范围清理相应 Key；不要在 Worker 运行期间清理共享集合。

### 调度持久化真实验证

2026-07-20 使用独立的 `scrapyWithRedis:verification:<timestamp>` 命名空间、本地可控分页服务器和真实 `mycrawler_redis` Worker 完成停止与重启验证，验证 key 在结束后已清理：

- 第一轮 Worker 停止前，Redis 调度队列类型为 ZSET，待处理请求数为 3；停止后仍为 3。
- 起始队列在重启前为 0，证明第二轮不是重新投放起始 URL。
- 请求去重 Set 在停止前后均为 6，证明 Worker 停止没有清理去重状态。
- 第二轮 Worker 重启后从持久化调度队列新增抓取 6 个页面，请求去重数从 6 增长到 16。
- 两轮 HTTP 请求记录中不存在重复页面，证明已处理的普通请求没有因重启再次抓取。

验证过程中还修复了 Redis Spider 默认域名配置：空的 `domain` 参数必须生成空列表，不能保存为空的 `filter` 对象，否则 `OffsiteMiddleware` 会过滤所有后续请求。

统一命名后，旧的 `myspider:start_urls`、`mycrawler:start_urls`、`<spider>:requests`、`<spider>:items`、`scrapyWithRedis:requests` 和 `scrapyWithRedis:item_urls` 不再由当前配置读取。项目不会自动迁移或删除旧数据；如仍有待处理任务，应在 Worker 停止后人工确认数据类型和数量，再迁移到对应的新 Key。

## 九、Downloader Middleware

当前启用了 `ScrapywithredisDownloaderMiddleware`：

- 每个请求从内置 UA 列表中随机选择一个 `User-Agent`。
- 请求发出前记录 URL 和 UA。
- 响应返回后记录 URL；非 200 响应使用 warning 日志。
- 中间件优先级为 543，Scrapy 的 `RetryMiddleware` 为 550。`process_response()` 按优先级逆序执行，因此可重试响应先由 Scrapy 返回新请求；`process_exception()` 按优先级正序执行，因此本项目显式比较请求的 `retry_times` 与 `RETRY_TIMES`/`max_retry_times`，只在异常重试耗尽后写入失败队列。最终记录包含 URL、状态码或异常类型、Spider、Worker、UTC 时间和尝试次数。
- `EmojiImagesPipeline` 创建的下载请求带有 `is_image_request` 标记，不会误写入页面失败队列；图片失败由 `ImageFailurePipeline` 单独留存。
- Redis 暂时不可用时会记录 error 日志，响应或失败 Item 的正常处理链不会因此被中断。

## 十、关键 Settings

| 配置 | 当前值/来源 | 作用 |
|---|---|---|
| `REDIS_URL` | 环境变量，默认 `redis://127.0.0.1:6379/0` | Redis 连接地址，默认值不含凭据 |
| `LOG_LEVEL` | 环境变量，默认 `INFO` | 日志级别 |
| `CONCURRENT_REQUESTS` | 环境变量，默认 `16` | Scrapy 全局并发请求数 |
| `DMOZ_REDIS_BATCH_SIZE` | 环境变量，默认 `min(CONCURRENT_REQUESTS, 50)` | `dmoz` Redis 模式每次领取的起始 URL 数，范围 1～50 |
| `DMOZ_REDIS_MAX_IDLE_TIME` | 环境变量，默认 `30` | `dmoz` Redis 模式连续空闲多少秒后关闭，`0` 表示立即关闭 |
| `DOWNLOAD_DELAY` | 环境变量，默认 `1` | 下载间隔，支持小数 |
| `OUTPUT_DIR` | 环境变量，默认 `output` | JSON Lines 结果目录 |
| `DUPEFILTER_CLASS` | `RFPDupeFilter` | Redis 请求去重 |
| `REDIS_START_URLS_KEY` | `scrapyWithRedis:%(name)s:start_urls` | Redis 起始队列模板 |
| `START_URL_DUPE_KEY` | `scrapyWithRedis:%(spider)s:dupe:start_urls` | 起始 URL 投放去重集合模板 |
| `SCHEDULER_QUEUE_KEY` | `scrapyWithRedis:%(spider)s:requests` | 按 Spider 隔离调度队列 |
| `SCHEDULER_DUPEFILTER_KEY` | `scrapyWithRedis:dupe:requests` | 跨 Spider 共享请求指纹 |
| `SCHEDULER` | scrapy-redis Scheduler | Redis 请求调度 |
| `SCHEDULER_PERSIST` | `True` | Spider 关闭后保留调度状态 |
| `IMAGES_STORE` | 环境变量，默认 `images` | 图片保存根目录 |
| `WORKER_ID` | 环境变量，默认空字符串 | 当前 Worker 标识 |
| `ITEM_URL_DUPE_KEY` | `scrapyWithRedis:dupe:item_urls` | 跨 Spider 图片 URL 去重 |
| `REDIS_ITEMS_KEY` | `scrapyWithRedis:%(spider)s:items` | 按 Spider 隔离结果列表 |
| `PAGE_FAILURE_QUEUE_KEY` | `scrapyWithRedis:%(spider)s:failures:pages` | 页面失败队列模板 |
| `PAGE_FAILURE_DUPE_KEY` | `scrapyWithRedis:%(spider)s:failures:pages:dupe` | 页面失败去重集合模板 |
| `IMAGE_FAILURE_QUEUE_KEY` | `scrapyWithRedis:%(spider)s:failures:images` | 图片失败队列模板 |
| `IMAGE_FAILURE_DUPE_KEY` | `scrapyWithRedis:failures:images:dupe` | 全局图片失败去重集合 |
| `IMAGE_RETRY_QUEUE_KEY` | `scrapyWithRedis:image_retry:start_urls` | 图片专用重试入口 |
| `FEED_EXPORT_ENCODING` | `utf-8` | Scrapy Feed 导出编码 |

## 十一、Ubuntu 多 Worker 运行

当前 Linux 脚本面向 Ubuntu 26.04 / Debian 系发行版，全部使用 `set -euo pipefail`。依赖基线已分别在 Windows/Python 3.9.9 和 Ubuntu 26.04/Python 3.14.4 环境验证；Linux 实测中 `pip check` 返回 `No broken requirements found.`：

| 组件 | 固定版本 |
|---|---:|
| Python | `>=3.9`；实测 `3.9.9`、`3.14.4` |
| Scrapy | `2.13.4` |
| scrapy-redis | `0.9.1` |
| redis Python 客户端 | `7.0.1` |
| Pillow | `11.3.0` |
| python-dotenv | `1.2.1` |

### 1. 初始化节点

进入项目根目录，创建当前节点自己的 `.env`，真实文件不会被 Git 跟踪：

```bash
cp .env.example .env
nano .env
bash scripts/linux/init_env.sh
```

初始化脚本通过 `apt` 安装 Python、venv、编译工具、Pillow/lxml 系统依赖、`redis-tools` 和 `flock`，随后创建 `.venv` 并安装 `requirements.txt`，最后建立 `images/`、`output/`、`logs/` 和 `run/`。

脚本不会把 `.env` `source` 到 Bash；Python 入口通过 `python-dotenv` 读取它，所以带特殊字符的 Redis 密码不会被 Shell 二次展开。系统环境变量仍可覆盖 `.env`。

### 2. 原子投放多个分页 URL

```bash
scripts/linux/enqueue_urls.sh \
  'https://fabiaoqing.com/biaoqing/lists/page/1.html' \
  'https://fabiaoqing.com/biaoqing/lists/page/2.html'
```

脚本会规范化 URL、去除同一批输入中的重复项，并通过一个 Redis Lua 脚本原子执行 `SADD + RPUSH`。重复运行不会再次投放已存在于 `scrapyWithRedis:<spider>:dupe:start_urls` 的 URL。输出包含输入数、新增数、重复数以及投放前后的起始队列长度。

需要投放给其他 Redis Spider 时临时覆盖：

```bash
SPIDER_NAME=myspider_redis scripts/linux/enqueue_urls.sh 'https://example.com/page/1'
```

投放脚本支持 `dmoz`、`myspider_redis` 和 `mycrawler_redis`。其中 `dmoz` 只有通过 Linux Worker 进入 Redis 模式时才会消费该队列；`image_retry` 使用独立的图片失败重放入口，因此会被投放脚本拒绝。

### 3. 启动和停止两个 Worker

默认启动两个 `mycrawler_redis` Worker：

```bash
scripts/linux/start_workers.sh
cat run/mycrawler_redis-*.pid
tail -n 30 logs/mycrawler_redis-1.log
tail -n 30 logs/mycrawler_redis-2.log
```

每个 Worker 使用独立 `WORKER_ID`、PID 文件和日志文件。启动脚本使用 `flock` 串行化启停操作；重复执行时，仍存活的 Worker 槽位会被跳过，只补齐缺失槽位。

选择 `dmoz` 时，启动脚本会自动传入 `-a redis_start=true`，两个 Worker 共同消费 `scrapyWithRedis:dmoz:start_urls`，不会使用类变量中的默认 URL：

```bash
SPIDER_NAME=dmoz scripts/linux/start_workers.sh
```

可通过当前 Shell 环境临时调整：

```bash
WORKER_COUNT=3 WORKER_ID_PREFIX=ubuntu-node-a scripts/linux/start_workers.sh
```

停止 Worker：

```bash
scripts/linux/stop_workers.sh
```

停止脚本会检查 `/proc/<pid>/cwd` 和命令行确实属于当前项目后才发送 `SIGTERM`，默认等待 20 秒；只有仍未退出时才发送 `SIGKILL`，避免陈旧 PID 文件误杀无关进程。图片批次尚在收尾时可延长优雅关闭窗口，例如 `STOP_TIMEOUT=120 scripts/linux/stop_workers.sh`。实测中20秒会强制终止仍在下载图片的 Pipeline，而120秒能正常输出 Pipeline 统计和 `Spider closed (shutdown)`。

### 4. Redis 观测

```bash
scripts/linux/observe_redis.sh mycrawler_redis
```

输出起始队列、起始 URL 去重、调度队列、请求去重、Item URL 去重、结果、页面失败和图片失败。工具先执行 `TYPE`，再根据真实类型选择 `LLEN`、`SCARD` 或 `ZCARD`，不会对 ZSET 错用 `LLEN`。

### 5. 失败重放与 crontab

默认每次最多重放 100 条：

```bash
scripts/linux/replay_page_failures.sh mycrawler_redis 100
scripts/linux/replay_image_failures.sh mycrawler_redis 100
```

两个入口都使用 `flock -n`。如果上一次定时重放尚未结束，新进程会直接跳过，不会形成失控的重复任务。最小定时示例位于 `scripts/linux/crontab.example`。可以先生成适合当前节点的临时副本，并用 Ubuntu 26.04 的 `crontab -n` 只检查语法：

```bash
sed "s#/opt/scrapyWithRedis#$HOME/projects/scrapyWithRedis#g" \
  scripts/linux/crontab.example \
  > /tmp/scrapyWithRedis.crontab
crontab -n /tmp/scrapyWithRedis.crontab
```

`-n` 是 dry run，不安装定时任务；不同 cron 实现的语法检查选项可能不同，应以本机 `crontab -h` 为准。需要实际启用时，通过 `crontab -e` 将两条任务合并进已有配置，不要直接运行 `crontab <file>` 覆盖整份用户 crontab。

所有 Redis 连接参数均由 `.env` 或系统环境变量提供；脚本、PID 和日志中不保存连接密码。

## 十二、测试与检查

列出当前可用 Spider：

```bash
python -m scrapy list
```

运行全部单元测试：

```bash
python -m unittest discover -s tests -v
```

运行语法编译和全部 Spider 检查：

```bash
python -m compileall -q \
  scrapyWithRedis tests start.py failure_tasks.py \
  aggregate_outputs.py export_redis_results.py

scrapy check dmoz image_retry mycrawler_redis myspider_redis
```

阶段 4 验证时共运行 97 个测试。测试数会随功能演进增加，应以命令当次输出的 `Ran ... tests` 和 `OK` 为准。

现有测试覆盖：

- `EmojiItem` 统一字段。
- 公共解析器的正常、缺 URL 和缺标题分支。
- 固定 HTML fixture，以及分页区全部链接和分页区外干扰链接。
- 三个 Spider 复用公共解析器。
- `dmoz` 多 `--start-url` 校验、规范化、去重及旧参数兼容。
- `dmoz` Redis 模式的批量领取、起始页解析、空闲续取和延迟关闭。
- 自定义图片 Pipeline 的成功和失败结果映射。
- 必填字段校验、Redis 原子 Item 去重和 Pipeline 顺序。
- `IMAGES_STORE` 环境变量覆盖。
- 元数据补全和失败 Item 输出。
- Worker/PID 输出文件隔离和可重复聚合。
- Redis 跨节点结果导出、成功结果优先和可重复覆盖。
- 页面/图片失败原子去重、失败 Item 保留和 Redis 异常分支。
- 页面/图片失败查看、原子重放及重复重放幂等性。
- `image_retry` 专用 Spider 和重放列表页解析。
- `start.py` 的默认 Spider、参数优先级和 Scrapy 参数透传。

## 十三、阶段 4 运行证据

Linux 验证脚本把原始证据保存在当前节点的 `evidence/phase4-<时间>/`。该目录可能包含完整日志、公开 URL 和节点名称，已加入 `.gitignore`，不应直接提交。经过检查、可随项目交付的30条成功样例、1条失败样例和校验摘要位于 `samples/`。

本轮实际验证结果：

- Ubuntu 26.04、Python 3.14.4 下97个测试通过，4个 Spider 的 `scrapy check` 通过。
- 两个 `mycrawler_redis` Worker 均产生独立日志和真实请求。
- Redis 调度队列在运行中为10，停止后仍为10；未投放新起始 URL 重启后变为9，并继续产生请求和结果。
- 从 Redis 导出342条唯一成功 Item；342个 `image_path` 均存在，文件 MD5 与 `image_checksum` 全部一致。
- 仓库随附的 `sample-30-sanitized.jl` 与 `images/` 来自同一批公开样例：30条成功 Item 均能找到对应图片，文件 MD5 与 `image_checksum` 全部一致；`artifact-validation.json` 保存该公开样例的数量和校验摘要。
- 可控图片连接失败经过3次尝试后产生1条图片失败记录；`download_status="failed"`、路径和校验值为空、`download_error` 保留 `Connection refused`，页面失败队列保持为空。
- 同一失败记录第一次重放成功，第二次被拒绝；Redis 中2条历史尝试经聚合后只输出1条最新唯一结果。
- `redis-before-start.txt`、`redis-during-run.txt`、`redis-after-stop.txt` 和 `redis-after-restart.txt` 分别保存四阶段 Redis 类型与数量。
- 2026-07-23 20:23:07 至 20:53:11，两个 `mycrawler_redis` Worker 连续运行超过30分钟；7次采样中两个进程始终存活，Redis 结果数由421增至3370。运行期间出现的1次真实图片失败被留存在失败 Item 和图片失败队列中，没有导致 Worker 退出。
- 使用 `STOP_TIMEOUT=120` 后，两个 Worker 均在 `SIGTERM` 下完成收尾，分别输出1672和1620条本地结果并记录 `Spider closed (shutdown)`；停止后的 Redis 结果数为3634，调度 ZSET 中保留13个请求供后续断点续爬。
- 停止后原 Worker PID、PID 文件和 `flock` 锁均不存在，未发现残留或僵尸进程。原始稳定性证据包括 `stability-30min.txt`、`stability-stop-workers.txt`、`stability-processes-after-stop.txt`、`stability-log-check.txt` 和 `stability-image-failure-record.txt`。

本地 Worker 文件只反映当前节点；跨节点总结果应使用 `export_redis_results.py` 从共享 Redis 导出。被 `SIGKILL` 的进程可能来不及刷新本地 JL 缓冲，因此正式证据应采用优雅停止后的输出或 Redis 导出结果。

## 十四、当前已知边界

- Redis 起始请求使用 `dont_filter=True`，起始队列本身不是请求去重集合。
- `dmoz` Redis 模式采用与 scrapy-redis `RedisMixin` 相同的原子批量弹出语义；如果进程恰好在任务已弹出但尚未提交给持久化 Scheduler 的极小窗口内崩溃，该条起始任务可能丢失。本阶段未引入 inflight 租约队列。
- `image_checksum` 尚未用于相同图片内容去重。
- 本地聚合器只处理当前节点的 `items-*.jl`；跨节点总结果应从共享 Redis 导出。
- 每个节点都要从 `.env.example` 复制自己的 `.env` 并填写唯一的 `WORKER_ID`；实际 `.env` 不应提交到版本库。
