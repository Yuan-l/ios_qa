# ios_qa — iOS 静态解析 + 知识库（P1 / P2 / P3）

> **仓库边界**：这是**工具仓库**，只放代码、默认模板和测试。
> 被解析的 iOS 工程不在这里——它的配置、golden、同义词、索引都放在工程自己的
> `<工程根>/.ios_qa/` 下。同一份工具可以服务任意多个 iOS 工程，互不干扰。

纯 Python 词法扫描，把 iOS 工程解析成 SQLite 结构化索引，提供：

- **结构化索引**：类/协议/扩展、方法体、依赖、调用边、页面入口、跳转、通知、API 端点
- **中文全文检索**：CJK 单字 + bigram 预分词，不依赖 jieba；支持业务别名扩展
- **查询 CLI**：功能检索、符号、调用、影响面、页面入口路径、通知配对
- **重复/相似代码**：词法指纹（改局部变量名也能认出复制粘贴），精确 + 近似重复聚类
- **语义检索**：Qdrant 双通道（Qwen3-1024 稠密 + BM42 稀疏，RRF 融合）+ Cross-Encoder 精排，自然语言找方法体
- **MCP server**：19 个工具，可直接接任意支持 stdio 的 MCP 客户端

查询层（`query.py`）不依赖 CLI / MCP，后续接 Agent Loom 只需换接入层。

---

## 安装

```bash
# 只装构建/查询/MCP（除 MCP SDK 外全用标准库）
python3 -m pip install -e /path/to/ios_qa

# 需要语义检索时再装可选依赖（Qdrant 本地模式 + fastembed/ONNX）
python3 -m pip install -e "/path/to/ios_qa[vectors]"
```

装好后命令在**任何目录**都可用；也可以不安装，用
`PYTHONPATH=/path/to/ios_qa python3 -m ios_qa ...` 直接跑源码。

---

## 快速开始

```bash
cd /path/to/YourApp

# 1. 初始化工程配置（模板在 ios_qa 仓库 templates/）
mkdir -p .ios_qa
cp /path/to/ios_qa/templates/project.json     .ios_qa/project.json
cp /path/to/ios_qa/templates/synonyms.json    .ios_qa/synonyms.json
cp /path/to/ios_qa/templates/eval_cases.json  .ios_qa/eval_cases.json
# 然后按工程改 project.json 的 name / source_dirs / own_class_patterns

# 2. 构建索引（首次全量约 3~7 秒；之后按文件 sha256 增量）
python3 -m ios_qa build

# 构建 + 自动跑 golden 自检（失败退出码 3）
python3 -m ios_qa build --check

# 自检
python3 -m ios_qa doctor

# 模型无关的 golden 验收（不调用任何 LLM）
python3 -m ios_qa eval

# MCP 双客户端验收（协议层 + 数据层，失败退出码 1）
python3 -m ios_qa mcp-smoke

# 查询
python3 -m ios_qa find "网络 请求"
python3 -m ios_qa trace TBRequestManager
python3 -m ios_qa methods "采样"
python3 -m ios_qa callers TBRequestManager
python3 -m ios_qa impact TBRequestManager --depth 2
python3 -m ios_qa read components/TBNetworkManager/Classess/Objc/TBRequestManager.h --focus shared
python3 -m ios_qa pages
python3 -m ios_qa module TDMonitorKit
python3 -m ios_qa path TBFinanceDetailViewController
python3 -m ios_qa notifications --unbalanced
python3 -m ios_qa similar QWeatherBaseButton.initWithFrame:
python3 -m ios_qa duplicates --min-lines 8

# 语义层（可选：需要 qdrant-client + fastembed，首次会下载 Qwen3/BM42/reranker 模型）
python3 -m ios_qa vectors                     # 增量构建 Qdrant 向量索引
python3 -m ios_qa semantic "行情页面怎么加载数据"
python3 -m ios_qa similar TBAlphaView.setupLayout --mode semantic
```

工程内目录布局：

```text
YourApp/
├── .ios_qa/                     # 工程专属，全部由本工具读写
│   ├── project.json             #   profile：扫描范围、类前缀、跳过规则
│   ├── eval_cases.json          #   golden 用例（建议提交）
│   ├── synonyms.json            #   业务别名分组（建议提交）
│   ├── ios_qa.db                #   SQLite 索引（可随时重建，gitignore）
│   ├── embedding_cache.db       #   向量缓存（可随时重建，gitignore）
│   └── qdrant_storage/          #   Qdrant 本地持久化（可随时重建，gitignore）
├── YourApp/
└── components/
```

工程根目录的 `.gitignore` 建议加：

```gitignore
.ios_qa/*.db
.ios_qa/*.db-shm
.ios_qa/*.db-wal
.ios_qa/embedding_cache.db
.ios_qa/qdrant_storage/
```

### 多工程

每个工程一个 `.ios_qa/`，工具仓库不保存任何工程的配置或数据：

```bash
cd ~/work/AppA && python3 -m ios_qa build      # 用 ~/work/AppA/.ios_qa/
cd ~/work/AppB && python3 -m ios_qa build      # 用 ~/work/AppB/.ios_qa/

# 不在工程目录时用 --root 或环境变量指定
python3 -m ios_qa --root ~/work/AppA info
IOS_QA_ROOT=~/work/AppA python3 -m ios_qa build
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `--root <dir>` | 工程根目录；默认 `$IOS_QA_ROOT`，否则当前目录 |
| `--db <path>` | SQLite 路径，默认 `<root>/.ios_qa/ios_qa.db` |
| `--profile <json>` | 自定义 project.json，默认 `<root>/.ios_qa/project.json` |
| `--json` | 机器可读输出 |

`build --full` 忽略哈希全量重建；`build --quiet` 不打印进度。

---

## 命令一览

| 命令 | 用途 |
|---|---|
| `build` | 增量构建；单文件失败不中断，错误写入 `files.error`；`--check` 构建后跑 golden |
| `info` | 构建时间、分支、脏文件、表计数、健康警告 |
| `find <描述>` | 按中文/英文功能描述找类与页面（类名 + FTS + 方法体 + 入口融合排序） |
| `symbol <名称>` | 查类/方法签名、属性、文件行号 |
| `trace <类>` | 继承链、方法、依赖/被依赖、API、通知配对、调用方/被调方、跳转、相关问答；`--module` 消歧同名类 |
| `methods [query]` | 方法体全文检索，支持 `--class` / `--module` |
| `callers <目标>` | `TBRequestManager` 或 `TBRequestManager.requestWithMethod` 的调用方 |
| `impact <目标>` | 反向 BFS 影响面，`--depth 1..4` |
| `read <file>` | 带行号读源码，`--focus` 定位首次命中 |
| `pages` | 列出 UIViewController 子类 |
| `path <类>` | 从已知入口到该页面的跳转链；`--depth` / `--limit` / `--no-weak` |
| `notifications [名称]` | 通知注册/移除配对；`--module` / `--unbalanced` / `--unknown` |
| `similar <目标>` | 找相似方法；默认词法指纹（`--min-similarity` 0.3），`--mode semantic` 走 Qdrant |
| `duplicates` | 重复代码聚类；`--min-lines`（默认 6）/ `--min-similarity`（默认 0.8）/ `--class` |
| `semantic <查询>` | 自然语言语义检索方法体；`--module` / `--limit`（默认 5） |
| `similar-snippet <文件>` | 任意代码片段找语义相近的方法；`--min-similarity`（默认 0.90） |
| `vectors` | 构建 Qdrant 向量索引；`--full` 重建 collection，`--limit` 调试 |
| `module <名称>` | 模块概览 |
| `qa save/search/delete` | 历史问答知识库 |
| `mcp` | 启动 MCP stdio server |
| `mcp-smoke` | 双 MCP 客户端验收（协议层 + 数据层），`--client raw/sdk/both`、`--only` |
| `eval` | 跑 `eval_cases.json` 的 golden 用例，断言类/文件/行号/接口路径 |
| `doctor` | 索引自检 |

示例输出（`trace`）：

```
TBRequestManager  (class/objc/TBNetworkManager)
  文件: components/TBNetworkManager/Classess/Objc/TBRequestManager.m
  继承链: TBRequestManager -> NSObject
  方法: 4 个实现  扩展: 0
  ...
  TBFinanceHTTP.attemptGETAtIndex:... -> shared
    [components/TBFinanceKit/TBFinanceKit/Classes/Network/TBFinanceHTTP.m:74]
```

示例输出（`path`）：

```
TBFinanceDetailViewController  导航  (TBFinanceKit)

  路径 1  (入口 AppDelegate / 2 跳)
    AppDelegate --tab--> TBFinanceHomeViewController
      [TrueDepthDemo2/AppDelegate.swift:59]
    TBFinanceHomeViewController --push--> TBFinanceDetailViewController
      [components/TBFinanceKit/TBFinanceKit/Classes/UI/TBFinanceHomeViewController.m:838]
```

`path` 只做图上的连通性回溯，不猜类型：强边来自 `navigation`
（push/present/root/tab/segue），弱边来自 `deps` 的 `instantiate`
（未能识别的容器写法），同一对类已有强边时丢弃弱边。同一条逻辑链只返回
一条：`rootViewController` 的创建与赋值会被解析成两条同 (from, to, kind)
的边，行号不同但对「怎么到这个页面」没有区别，`incoming` 里仍能看到全部边。
没有连通路径时返回 `incoming` 直接来源，并说明未连通原因。

---

## MCP 接入

MCP 是协议，不绑定客户端：OpenCode、Cursor、Claude Code、自研 Agent Loom，
只要支持 stdio MCP 就能接。索引与查询层不感知模型。

复制 `mcp.json.example` 到工程根目录并改名为 `.mcp.json`，
或合并进已有客户端配置（字段名以客户端为准）：

```json
{
  "mcpServers": {
    "ios_qa": {
      "command": "python3",
      "args": ["-m", "ios_qa", "mcp"],
      "cwd": "/absolute/path/to/YourApp",
      "env": {
        "IOS_QA_ROOT": "/absolute/path/to/YourApp"
      }
    }
  }
}
```

`cwd` 必须指向**目标 iOS 工程**（`cwd` 里包含 `.ios_qa/`），不要指向工具仓库；
`IOS_QA_ROOT` 是同一件事的显式写法，客户端不保证启动目录时更稳妥。
接多个工程就配多个 server 条目，各自指向不同工程。

提供的工具：

| 工具 | 说明 |
|---|---|
| `kb_info` | 索引健康信息 |
| `find_feature` | 功能/页面检索（中文，纯词法） |
| `hybrid_find_feature` | FTS + 向量混合找功能，按类合并；默认 Cross-Encoder 精排 top-8 |
| `search_symbol` | 类名/方法名符号检索 |
| `trace_class` | 类数据流全量视图；同名类返回 `ambiguous`/`candidates`，可传 `module` 消歧 |
| `search_methods` | 方法体全文检索；方法名精确/包含命中优先于「方法体里提到」 |
| `find_callers` | 调用方 |
| `impact_analysis` | 影响面分析 |
| `read_source` | 读源码（带行号，支持 focus） |
| `list_pages` | 页面列表 |
| `find_navigation` | 入口 → 页面的跳转链（含低置信度实例化边） |
| `find_notifications` | 通知注册/移除查询，`unbalanced_only` 只看未配对 |
| `find_similar_code` | 相似方法；`mode=lexical`（默认，词法指纹）/ `mode=semantic`（Qdrant 近邻） |
| `find_duplicates` | 重复代码聚类，`min_lines` / `min_similarity` / `class_name` |
| `vector_search` | 自然语言语义搜索方法体（dense + BM42，RRF 融合），可传 `module` |
| `search_similar_snippet` | 任意代码片段找语义相近的方法，返回 `source_code` 与复用建议 |
| `list_module` | 模块概览 |
| `search_qa` | 检索历史问答 |
| `save_qa` | 保存问答 |

所有工具返回 JSON 字符串，字段里统一有 `file_path`、`line_start/line_number`。

先构建索引再启动 MCP；索引不存在时工具会返回空结果，不会崩溃。

### MCP 验收（`mcp-smoke`）

「任意 MCP 客户端拿到同一份数据」不能只靠手工点一次，所以用两个互相独立的
客户端各跑一遍，再和查询层对齐：

**协议层**：两个客户端都以真实子进程方式启动 `python3 -m ios_qa mcp`，走 stdio。

| 客户端 | 实现 | 证明什么 |
|---|---|---|
| `raw-jsonrpc` | 纯 stdlib，手写 JSON-RPC 2.0 报文（换行分隔） | 服务端不绑定任何 SDK，协议本身就走得通 |
| `python-sdk` | 官方 `mcp` SDK 的 `ClientSession` | 生态里的主流客户端能直接接入 |

**数据层**：同一组参数分别经两个客户端调用，再与 `QueryService` 的直接调用
结果逐字段比对，三方相等才算通过。`save_qa` 是写路径，只验工具存在与入参
schema，不在真实索引里造数据。

```bash
python3 -m ios_qa mcp-smoke                 # 两个客户端 + 18 个只读工具
python3 -m ios_qa mcp-smoke --client raw    # 只跑 stdlib 客户端
python3 -m ios_qa mcp-smoke --only trace_class
python3 -m ios_qa mcp-smoke --json          # CI 消费
```

退出码：`0` 通过 / `1` 协议或数据不一致 / `2` 索引不存在。

```text
MCP 验收：TrueDepthDemo2  (.ios_qa/ios_qa.db)

协议层
  ✅ raw-jsonrpc  19 个工具  服务端 ios_qa / 协议 2024-11-05
  ✅ python-sdk  19 个工具  服务端 ios_qa / 协议 2025-11-25
  工具集合一致: 是（19 个）

数据层（客户端返回 vs QueryService 直接调用）
  ✅ kb_info              与查询层逐字段一致
  ✅ find_feature         与查询层逐字段一致
  ✅ find_similar_code    与查询层逐字段一致
  ✅ find_duplicates      与查询层逐字段一致
  ...
  ⏭  save_qa              写路径只验协议

通过 18/18 个工具（失败 0）；协议层通过
```

验收参数不写死类名：`build_cases()` 从索引里取样（方法最多的类、页面、模块、
源码文件、方法名），换一个工程可以直接跑。

---

## 架构

```
ios_qa/                包（pip 安装的模块）
├── config.py          Profile：所有项目差异（目录、类前缀、HTTP 门面、VC 基类）
├── discover.py        源码发现、目录/文件过滤、sha256
├── source.py          注释/字符串屏蔽、括号配对、行号换算、URL 清洗、UI 文案提取
├── textnorm.py        CJK 单字+bigram、驼峰拆词、FTS 表达式
├── synonyms.py        业务别名加载与查询扩展
├── model.py           SymbolRec / MethodRec / DepRec / CallRec ...
├── parsers/
│   ├── objc.py        .h/.m/.mm：interface/category/协议/方法体/通知/导航
│   ├── swift.py       class/struct/enum/protocol/extension/actor
│   └── ui.py          storyboard/xib：入口与 segue
├── fingerprint.py     代码指纹：token 归一化、MinHash、LSH 分带、Jaccard
├── embedding.py       Qwen3-1024 / BM42 provider + embedding_cache.db 缓存
├── vector_store.py    Qdrant 本地模式：collection methods、dense/sparse/RRF
├── vector_build.py    向量索引构建：筛选、point_id、增量 file_hashes
├── db.py              DDL、增量写入、classes 聚合、指纹重建、FTS 重建
├── build.py           构建编排、git 元数据、build_runs
├── query.py           查询服务（纯 Python API）
├── cli.py             argparse 入口
├── mcp_server.py      FastMCP 适配层
├── mcp_smoke.py       MCP 双客户端验收（stdlib JSON-RPC + 官方 SDK）
└── tests/             unittest

templates/             工程侧配置模板（复制到 <工程>/.ios_qa/）
├── project.json
├── synonyms.json
└── eval_cases.json

pyproject.toml         可 pip install -e 的打包配置
mcp.json.example       MCP 客户端配置示例
```

### 数据表

文件级事实表（按 `file_path` 增量替换）：
`symbols`、`methods`、`deps`、`api_calls`、`notifications`、`calls`、`entrypoints`、`navigation`

聚合/辅助表：
`classes`（每次构建重建）、`code_fingerprints`（按方法体增量补算）、`files`、
`metadata`、`build_runs`、`qa_history`

FTS5 虚表：
`classes_fts`、`methods_fts`、`qa_fts`（全量重建，数据量级下成本可接受）

语义索引（与数据库同在 `<工程>/.ios_qa/` 下，均 gitignored）：
`qdrant_storage/`（collection `methods`，dense 1024 维 + sparse BM42）、
`embedding_cache.db`（`cache` 稠密 / `sparse_cache` 稀疏）、
`qdrant_storage/build_meta.json`、`qdrant_storage/file_hashes.json`

### 中文检索

FTS5 的 `unicode61` 会把连续中文当成一个 token，`MATCH '首页'` 对
`首页数据加载` 是 0 命中。因此在写入前预分词：

- 中文：单字 + bigram（`首` `页` `首页` `页数` `数据`）
- 标识符：驼峰/下划线/数字边界拆词并小写（`TDURLSessionNetworkClient`
  → `td url session network client`）
- 查询侧使用同一套分词，token 逐个加引号，避免 `and/or/not` 被当操作符

### 业务同义词

类名是英文（`TBFinanceQuote`、`TDANRFeature`），提问是中文口语（报价、卡顿），
中间的映射写在 `synonyms.json`，按「组」维护，一组内的词互相扩展：

```json
[["行情", "报价", "quote"], ["崩溃", "crash"], ["卡顿", "掉帧", "jank"]]
```

- 每组最多扩展 `MAX_EXPANSIONS=8` 个词，避免一个词炸成几十路查询；
- 只有**主查询最高分 < 55** 时才启用扩展。像「首页」这种字面就命中很强的查询，
  扩展成 `home` 会灌进一堆 `QWeatherHome*` 视图，反而变差；
- 扩展路径用与主查询相同的四路打分（类名 / FTS / 方法体 / 入口），整体减 2 分，
  并在结果里标 `matched_on=*_synonym`、`synonym="报价→行情"`；
- 返回体带 `synonyms` 字段，调用方能看出这次检索用了哪些别名。

`find_feature` 的打分基线：类名精确 100 / 包含 60~80、FTS AND 50、FTS OR 44
（部分命中不能和 AND 同分）、方法级 AND 35 / OR 30、入口 70、LIKE 兜底 30；
VC +4、App 入口 +3。

### 扩展 / 分类归属

- Swift `extension` 与 ObjC category 的方法统一归到**宿主类**
  （`class_name` = 宿主），来源保留在 `symbols.extension_of` / `category_name`；
- `trace` 的 `extensions` 字段带 `category_name` / `module` / `language` / 文件行号，
  CLI 输出新增「扩展/分类」小节；
- `search_symbol("BreadCrumb")` 能反查 `UIViewController` 并返回 `category` 信息，
  `find_feature` 的 `search_text` 也包含分类名（搜 `BreadCrumb` 能命中宿主类）；
- 多行类声明（`final class Foo: Bar,` 换行后 `Baz {`）也能识别——这是
  `TBInfrastructureDemoViewController` 之前整个文件没进索引的原因。

### 同名类消歧

仓库里有三处同名声明：`Item`（App 的 enum + TDMonitorKit 的 struct）、
`LocalUserDefaultPropertyWrapper`（MyCameraModule 两个目录）、
`TBTabbarViewController`（MyCameraModule + TBCustomHomePage）。聚合表按类名
建行，但 `classes.sites_json` 会记录每个**声明点**：

- 声明点 = (模块, 目录, 文件名主干)，头文件与实现文件算同一个点；
  category / extension 属于宿主类，不算第二个声明点；
- `is_ambiguous=1` 表示有多个声明点，`sites_json` 带模块、类型、文件行号
  与该方法数；
- `trace` 默认只看方法最多的声明点，返回 `ambiguous` / `selected_module` /
  `candidates` / `scoped_method_count`；`trace <类> --module <模块>` 切换后，
  属于本类自己的行（方法、依赖、通知、跳出的跳转、扩展）只取该模块；
- 反向的「谁依赖我 / 谁调用我 / 谁跳进来」按类名匹配，不同模块的同名类在
  `deps` / `calls` / `navigation` 里本来分不开，所以宁多勿漏，不做模块过滤；
- 传了不存在的模块名会回退到主声明点，不返回空结果。

```
$ python3 -m ios_qa trace TBTabbarViewController --limit 5
TBTabbarViewController  (class/swift/TBCustomHomePage)
  方法: 9 个实现  扩展: 0（当前声明点 6 个）
  ⚠ 同名类出现在 2 个声明点，当前只看 TBCustomHomePage
    * TBCustomHomePage     class      6 方法  [.../TBTabbarViewController.swift:5]
      MyCameraModule       class      3 方法  [.../ViewController/TBTabbarViewController.swift:11]
```

### 界面文案（UI 字面量）

类名是英文、但用户只知道界面上的中文时（`self.title = @"行情"`、按钮文案、
错误提示），只索引类名/方法签名就会漏。解析层给每个类记录主声明点范围内的
**含中文字符串字面量**（`symbols.literals_json`），聚合时取前 12 条写进
`classes.search_text`：

- 只收字符串，不收注释文字（注释走 `doc_comment`，本来就在检索里）；
- 掩码后提取，`@"..."` 与 Swift `"""..."""` 都支持，含转义与格式符；
- 长度上限 120 字符、每类前 12 条，避免长文案把 FTS 索引撑爆；
- 只取**主声明点**的文件，同名类不会把另一个模块的界面文字带进来。

效果：`报价`（同义词 → 行情）现在由行情页自身的 `@"行情"` 命中，而不是
靠类名里带 `Quote` 的模型类；`find_feature("行情")` 的第一名是
`TBFinanceHomeViewController`。

### 调用解析：协议 / block

- ObjC 实现解析时会读**同目录 `.h`** 的 `@property`：
  `id<FooDelegate> delegate` + `[self.delegate foo]` →
  `callee_class=FooDelegate`、`kind=delegate`、`confidence=low`；
- block 属性（`void (^onSelect)(NSInteger)`）的调用 `self.onSelect(index)`
  → `kind=block`，能被 `find_callers("onSelect")` 找到；
- Swift 按类名合并**同文件主类 + extension** 的属性，`delegate?.foo()` /
  `onSelect?(x)` 同样记录；
- 头文件变更时，同目录 `.m` / `.mm` 会一起重解析，避免增量索引留下旧调用边。

### tab 容器页面

之前的导航只认 `pushViewController` / `present` / `rootViewController`，
tab 里创建的页面只能靠 `instantiate` 弱边兜底。现在识别：

```swift
tabBar.viewControllers?.append(Self.navigationController(with: financeVC))
viewControllers = [cameraNav, weatherNav]
setViewControllers([a, b], animated: false)
```

```objc
[self setViewControllers:@[nav1, nav2] animated:NO];
self.viewControllers = @[nav1, nav2];
```

`UINavigationController` 及其子类（`TBNavigationController` 等）会自动解包到
内层页面，边类型记为 `tab`（强边）。

### 重复 / 相似代码（P3-a）

重复代码判断的是「结构是不是一样」，不是「语义像不像」，所以这里不用向量，
用纯 stdlib 的词法指纹，结果可复现、不依赖模型：

1. **归一化 token**：去注释；字符串统一成 `str`（含 ObjC `@"..."`）、数字统一
   成 `num`；标识符按驼峰/下划线拆成小写段并丢掉单字母（循环变量改名不影响）。
2. **精确重复**：归一化 token 序列的 sha1（`code_fingerprints.norm_hash`）。
3. **近似重复**：token 4-gram + MinHash（16 个排列）签名，8 个带各 2 行做
   LSH 出候选；候选再用 shingle 集合算**精确 Jaccard** 复核——报告出来的
   `similarity` 是真实集合相似度，不是 MinHash 估计值。
4. **过滤**：token 少于 24 的方法不做指纹（`super.init()` 这类样板全仓库一样）；
   两个方法 token 数差超过 1.3 倍直接淘汰。

```bash
# 精确复制（相似度 1.0）：三个 Base 控件的 initWithFrame: 是同一段代码
python3 -m ios_qa similar QWeatherBaseButton.initWithFrame:
# 近似/精确重复聚类（默认 ≥6 行、≥0.8 结构相同）
python3 -m ios_qa duplicates --min-lines 8
```

```
$ python3 -m ios_qa duplicates --min-lines 10 --limit 1
重复代码: 11 组（精确 4 / 近似 7）  指纹 939 个方法  复核 55 对

1. 近似复制  x12  相似度 0.80  模块 MyCameraModule
   ImageEditorViewController.applyAnimeTemplate  (10 行)
     components/MyCameraModule/ViewController/ImageEditorViewController.swift:380
   ...
```

- 指纹按 `(file_path, line_start, method_name)` 与 `methods` 对齐，body 没变
  就不重算（增量构建通常 `fingerprints=0`）；
- 一组重复用并查集聚成连通分量，`kind=exact` 要求全组哈希相同，
  否则 `near` 且 `similarity` 取组内最低的复核值；跨模块的组会标
  `cross_module=true`；
- 实测本工程：1461 个指纹 / 约 0.76 MB blob，全量计算约 1.6s；
  `duplicates` 查询约 0.1s。

已知边界：改名、改注释、换字面量都还能命中；**换一种实现思路**（结构不同）
不会命中，这是词法指纹的固有边界。语义相近（不要求结构一致）由 P3-b 的
向量层负责。

### 语义检索（Qdrant + Qwen3 / BM42 + Cross-Encoder）

两套索引用途不同：

| 问法 | 走哪层 | 原因 |
|---|---|---|
| 「TBRequestManager 在哪」 | SQLite FTS5 | 类名/方法名是精确词，FTS 更快更准 |
| 「行情页面怎么加载数据」 | Qdrant 向量 | 提问里没有符号名，需要语义相似 |
| 「这个改动会影响谁」 | SQLite 调用边 | 图结构查询，向量帮不上忙 |

与参考文档（`extracted/Ios-qa MCP Server完整技术分析.md`、
`iOS向量库 && 飞书loom.md`）逐条对齐：

| 文档要求 | 本实现 |
|---|---|
| 稠密模型 `BAAI/bge-small-zh-v1.5`，512 维 | 默认改为 `Qwen/Qwen3-Embedding-0.6B-Q`（1024 维，INT8）；BGE-512 保留为 profile 用于对照，见下文「模型选型」 |
| 稀疏 BM42（`Qdrant/bm42-all-minilm-l6-v2-attentions`） | 一致，与稠密向量同写一个 collection |
| collection `methods`，dense 余弦 | 一致 |
| Hybrid = dense + sparse，Qdrant 原生 RRF（k=60） | 一致，`Prefetch` + `Fusion.RRF` |
| `hybrid_find_feature`：FTS + 向量，按类去重 | 已实现（Tool 14）；FTS 分归一到 0-1，向量用 cosine × 0.8 |
| Cross-Encoder 精排 | 新增：`BAAI/bge-reranker-v2-m3-int8` 对规则召回的候选重排 top-K |
| 索引文本 `search_document: 类:{class} 方法:{method}\n{body[:2000]}` | Qwen profile 改为裸文本 `类:{class} 方法:{method}\n{body}`；BGE profile 保留原前缀 |
| 查询文本 `search_query: {query}`；片段 `search_document: {code}` | Qwen profile 用 `Instruct: {task}\nQuery: {query}`，片段裸代码；BGE profile 保留原前缀 |
| `point_id = MD5(file_path:method_name)[:16]` | 一致，upsert 天然幂等 |
| payload：class/method/module/language/file/line/body_preview | 一致 |
| 跳过 <3 行或 <80 字符的方法 | 一致 |
| `embedding_cache.db`：key=`MD5("{model}:{text}")`，float32 BLOB | 一致，稠密 `cache` + 稀疏 `sparse_cache` |
| `build_meta.json` / `file_hashes.json` 增量 | 一致 |
| `vector_search` / `find_similar_code` / `search_similar_snippet` / `scan_duplicates` | 前三个已实现；`scan_duplicates` 的能力由 P3-a `find_duplicates` 覆盖（词法指纹，可复现） |
| Qdrant server | 用 `QdrantClient(path=...)` 本地持久化，API 语义一致；换 server 只改构造参数 |

文档内部有一处冲突：总览写 `jina-embeddings-v2-base-code`（768 维），
MCP 技术分析写 BGE-512。我们没有在这两个里二选一，而是把模型/维度/文本模板
做成 `EmbeddingProfile`，用 `IOS_QA_EMBED_PROFILE` 切换：

| profile | 模型 | 维度 | 用途 |
|---|---|---|---|
| `qwen3-0.6b`（默认） | `Qwen/Qwen3-Embedding-0.6B-Q`（INT8） | 1024 | 中文 + 代码 + 100+ 语言，语义召回余量大 |
| `bge-small-zh` | `BAAI/bge-small-zh-v1.5` | 512 | 参考实现同款；重建 3 秒，适合快速对照 |
| `jina-code` | `jinaai/jina-embeddings-v2-base-code` | 768 | 只覆盖英文 + 30 种编程语言，中文 query 属分布外，仅作对照 |

为什么默认 Qwen3：

- `jina-code` 的模型卡明确写的是 English + 30 programming languages，不含中文；
  而且已被 `jina-code-embeddings-0.5b/1.5b` 取代，不适合作为中文问答的默认模型。
- `BAAI/bge-m3`（1024 维、100+ 语言、中文 RAG 强）在 `fastembed 0.9.1` 里
  **没有 dense 实现**，要用得自己接 ONNX，暂不采用。
- `Qwen3-Embedding-0.6B` 覆盖 100+ 语言并包含代码语料，query 用
  `Instruct: {task}\nQuery: {query}`、document 裸文本；`fastembed 0.9.1` 直接支持。
  INT8 版约 1.1 GB，CPU 上比 fp32 快一个数量级（fp32 跑第一个 256 batch
  超过 40 分钟，不可用）。

实测（同一套混合检索 + Cross-Encoder，7 条真实问题）：Qwen3 与 BGE 都是
7/7 命中 top-3；BGE 在网络重试、城市搜索、日志上传上排得更高，Qwen3 的
优势是语义召回余量（问题里没有代码字面量时更稳）。默认选 Qwen3，
`IOS_QA_EMBED_PROFILE=bge-small-zh` 可以随时切回快速重建。

维度记录在 `build_meta.json` 里：换 profile 后 `_vector_status` 会发现
model/dim 不一致并提示重新运行 `vectors`，不会拿旧维度的索引硬查。

安装依赖（首次运行会下载约 1.1 GB 的 INT8 embedding 模型；精排另需
约 0.6 GB 的 reranker）：

```bash
python3 -m pip install qdrant-client fastembed

# huggingface.co 直连证书失败时走镜像
export HF_ENDPOINT=https://hf-mirror.com
python3 -m ios_qa vectors
```

注意别用 `HF_HUB_OFFLINE=1`：`fastembed` 的缓存检查会去找旧目录名
（`fastembed_cache/Qwen3-Embedding-0.6B-Q`），即使模型已缓存也会报
`Could not load model ... from any source`。模型已缓存时同样保留上面的
镜像变量即可离线复用缓存。

构建与查询：

```bash
python3 -m ios_qa vectors              # 增量：按 file_hashes.json 只重算变更文件
python3 -m ios_qa vectors --full       # 重建 collection

python3 -m ios_qa semantic "相机拍照后怎么保存图片"
python3 -m ios_qa similar MainCameraEditViewController.saveImageToTempDirectory --mode semantic
python3 -m ios_qa similar-snippet SomeSnippet.m --min-similarity 0.9
```

```text
$ python3 -m ios_qa semantic "相机拍照后怎么保存图片"
语义检索「相机拍照后怎么保存图片」  rrf  索引 1522 个方法
1. 0.833  TBSuperResolutionViewController.saveTapped  [TBImageEditorKit]
   components/TBImageEditorKit/TBImageEditorKit/Classes/UI/TBSuperResolutionViewController.swift:289-310
   guard let image = resultImage else { return } let loading = TBEditorHUD.show(in: view, text: "正在写入相册…")
2. 0.667  MainCameraEditViewController.saveImageToTempDirectory  [MyCameraModule]
   components/MyCameraModule/Classes/EditModule/MainCameraEditViewController.swift:77-92
   guard let data = image.jpegData(compressionQuality: 0.8) else { return nil } ...
3. 0.533  ImageEditorViewController.image  [MyCameraModule]
   components/MyCameraModule/ViewController/ImageEditorViewController.swift:259-265
   if let error = error { print("保存失败: ...") } else { print("图片保存成功") }
```

真实问答建议直接用混合检索：规则负责召回，Cross-Encoder 决定顺序。

```bash
python3 -m ios_qa hybrid "相机拍照后怎么保存图片"
python3 -m ios_qa hybrid "网络请求失败怎么重试" --no-rerank   # 只看规则排序
```

实现要点：

- `query.py` 只依赖 `embedding.py` / `vector_store.py` 的接口，模型可换；
  没装依赖或没建索引时返回 `available=false` + `hint`，不抛异常；
- `find_similar_code` 保留 `mode=lexical`（默认，golden 依赖），语义模式用源方法
  的稠密向量取近邻，并附参考文档的复用建议阈值（≥0.95 高度重复 / ≥0.90
  结构相近 / 其余部分相似）+ 短方法、骨架方法警告；
- `search_similar_snippet` 把任意代码当文档向量化，返回时从源码读取
  `source_code`，默认只保留 score ≥ 0.90；
- Qdrant 本地模式有目录锁：同一个 `qdrant_storage` 不能并发开两个 client，
  `QueryService.close()` 会按顺序关闭 store / 缓存 / SQLite。

实测本工程：1946 个有方法体的方法 → 1522 个可向量化。Qwen3-INT8 全量
`vectors` 约 78 分钟（CPU，`EMBED_BATCH=32`，模型已缓存；这是首次建库的
一次性成本），之后增量索引只重算变更文件，2~4 秒；同一进程内 embedding /
reranker 只加载一次，命中缓存的语义查询 <1 秒。只想快速重建时用
`IOS_QA_EMBED_PROFILE=bge-small-zh`，全量约 3 秒。

已知限制：Qwen3-0.6B 是通用多语言模型，不是代码专用模型；中文 query →
ObjC/Swift 方法体的排序靠「FTS 召回 + Cross-Encoder」兜底，类名/方法名
明确时仍应优先用 `find_feature` / `search_methods`。

---

## Profile（换工程只改 JSON）

配置放在**工程内**的 `.ios_qa/project.json`（模板：`templates/project.json`）。示例字段：

| 字段 | 说明 |
|---|---|
| `source_dirs` | 只扫描这些目录，默认 `Pods/` 不碰 |
| `skip_dir_names` | 精确跳过的目录名 |
| `skip_path_parts` | 路径片段黑名单（vendored 三方） |
| `skip_file_globs` | 生成文件（`*-Swift.h` 等） |
| `include_exts` | 解析的文件后缀 |
| `own_class_patterns` | 自有类前缀，用于依赖边过滤 |
| `http_managers` | 网络门面类名 |
| `api_manager_patterns` | API 管理器识别正则 |
| `view_controller_bases` | VC 基类，用于 `is_view_controller` |
| `max_body_chars` | 方法体入库上限 |
| `max_fts_body_chars` | 方法体进入 FTS 的上限 |

测试目录额外规则：目录名以 `Tests` / `UITests` / `Specs` / `SnapshotTests`
结尾时自动跳过（`TDMonitorKitTests` 这类组件测试也会排除）。

---

## 增量构建

1. `discover()` 计算每个文件 sha256；
2. `files` 表中 hash 相同且 `status='ok'` 的文件跳过；
3. 变更/新增/上次失败的文件按 `file_path` 删除旧行后重新解析写入；
4. 已从磁盘删除的文件同步删除索引行；
5. `classes` 与全部 FTS 从事实表重建；
6. 记录 `build_runs`。

同一份源码重复构建时 `files_parsed=0`，表计数不变。

---

## 测试

```bash
# 在任意 iOS 工程根目录跑：真实仓库冒烟测试会自动使用当前工程的 .ios_qa/project.json
python3 -m unittest discover -s /path/to/ios_qa/ios_qa/tests -t /path/to/ios_qa -v
```

当前目录没有 `.ios_qa/project.json` 时，`RealRepoSmokeTest` 会整类跳过，
其余 90+ 个用例照常运行（它们用临时目录造小型 ObjC/Swift 工程）。

覆盖：

- 中文单字/bigram、驼峰拆词、FTS 表达式转义、业务别名扩展
- ObjC 头文件/实现：符号、方法、调用、通知、行号
- Swift：类型/属性/方法、调用、导航、通知、URL
- Storyboard 入口与 segue
- 增量构建幂等、文件修改/删除
- 中文查询、Q&A 往返
- 页面入口链：强边（push/present/root/tab）+ 弱边（instantiate）、未连通时的空结果
- tab 容器创建页面（`viewControllers` 赋值 / append / setViewControllers）
- 通知配对：未配对检测、按名称反查
- 同义词扩展：报价→行情（保留 Quote 字面命中）、jank→卡顿
- 协议/block 调用：`delegate?.foo()`、`self.onSelect(x)`，含头文件属性与同文件 extension
- 扩展/分类归属：`search_symbol("BreadCrumb")`、`trace` 的 `extension_category`
- 同名类消歧：声明点识别、`trace --module` 收敛、未知模块回退
- UI 文案：字面量提取（跳过注释/纯英文/超长）、类级检索命中
- 多行类声明、头文件变更触发同目录实现重解析
- 代码指纹：token 归一化（含 ObjC `@"..."`）、短方法过滤、精确/近似重复聚类
- 向量层：文本前缀、缓存读写、Qdrant 构建/增量/删除、dense/sparse/RRF 查询
- MCP 工具集合与调用返回（19 个工具）
- 双客户端验收：stdlib JSON-RPC 客户端、官方 SDK 客户端、`save_qa` 写路径往返、
  `mcp-smoke` 退出码
- `build --check`：通过 / 失败退出码 3 / 无用例跳过 / 不加参数不跑 eval
- 真实仓库 smoke：规模、`TBRequestManager.h:29`、`TBFinanceHTTP.m:74`、
  入口链存在性（具体行号由 golden 负责，源码每次改动都会挪行）
- `run_eval()`：38 个 golden 用例全绿，且每条都带 `last_reviewed`

---

## Golden 用例（`eval`）

`eval_cases.json`（工程内 `.ios_qa/eval_cases.json`）是对**当前真实工程**的端到端断言：问什么 → 必须返回哪个类、
哪个文件、第几行。它不调用任何 LLM，任何 MCP 客户端拿到的都是同一份数据。

```bash
python3 -m ios_qa build
python3 -m ios_qa eval                              # 全量
python3 -m ios_qa eval --only symbol-method-line    # 单条
python3 -m ios_qa eval --json                       # CI 消费
```

### `build --check`

构建完成后自动跑一遍 golden，把「体检」变成构建的一部分：

```bash
python3 -m ios_qa build --check
```

- 只读当前工程的 `.ios_qa/eval_cases.json`；不存在时打印「跳过 eval 自检」，不影响构建；
- 输出只列失败用例，通过时只打一行汇总；
- 退出码：`0` 正常 / `1` 有文件解析失败 / `3` golden 有失败；
  解析失败优先（`1`），避免两种问题混在一起；
- `--json` 时结果放在返回对象的 `check` 字段，方便 CI 直接判断。

每条用例的字段：

| 字段 | 说明 |
|---|---|
| `id` | 唯一标识，`--only` 用 |
| `type` | 查询方法，需在 `eval.py::_call` 中实现 |
| `args` | 调用参数 |
| `checks` | 断言列表（类名 / 行号 / 接口路径 / 计数 …） |
| `reason` | **为什么要有这条用例**，写清它防的是哪类回归 |
| `last_reviewed` | 上次人工确认的日期（`YYYY-MM-DD`），仅用于审计 |

`checks[].kind` 支持的断言：

| kind | 断言内容 |
|---|---|
| `class_in_results` / `min_results` | 结果类名包含 / 至少 N 条（results/pages/callers） |
| `superclass_equals` | 首条结果的父类 |
| `method_at_line` | 方法名 + 文件后缀 + 行号（`method_name` / `file_suffix` / `line`） |
| `trace_found` | `trace` 找到目标 |
| `file_line_present` | 某个列表字段里存在 `file_suffix` + `line` |
| `api_url_contains` | API 端点的 `url_pattern` 含子串 |
| `read_focus_matched` / `read_content_contains` | `read` 的 focus 命中 / 正文含子串 |
| `field_gte` / `warnings_empty` | 任意数值字段阈值 / `info` 无警告 |
| `level_class_present` | 影响面某一层包含指定类 |
| `navigation_reachable` | 有从已知入口连通的路径 |
| `navigation_edge` | 路径中出现指定边（`from` / `to` / `edge_kind` / `line` / `file_suffix`） |
| `notification_contains` | 结果含指定 `class` + `name` 的通知 |
| `summary_gte` | `summary` 某字段 ≥ 期望值 |
| `synonyms_applied` | 本次检索应用了指定别名对（`from` / `to`） |
| `extension_category` | `trace` 的扩展列表含指定 category（可加 `file_suffix`） |
| `ambiguous` | `trace` 判定为同名类，且候选声明点不少于 `min` 个 |
| `candidate_modules` | `candidates` 覆盖 `values` 里的全部模块 |
| `all_methods_module` | `methods` 全部来自指定模块 |
| `similar_found` | `similar` 找到了目标方法的指纹 |
| `similar_top` / `similar_contains` | 相似结果首条 / 任意一条是 `value`，且 `similarity` ≥ `min_similarity` |
| `duplicate_group` | 某一重复组覆盖 `members` 全部类.方法，且 `similarity` ≥ `min_similarity` |
| `vector_available` | 语义索引可用（未构建时整条用例按 `requires` 跳过） |
| `vector_top` / `vector_contains` | 首条 / 任意一条结果是 `value`，且 `score` ≥ `min_score` |

### 维护策略

- 执行：事件驱动，每次 `build` 后 / 改解析器后 / CI 中跑，秒级成本；
- 内容：只在「契约变化」时改——解析逻辑、Profile、代码真实重构、新增高风险场景；
- **不按时间自动过期**，`last_reviewed` 只记录人工复核时间；
- **禁止用当前输出自动覆盖 golden**，否则 `eval` 永远全绿、失去意义。

失败处理：

```
eval 失败
  ├─ 源码该位置没变  → 解析器回归，修代码
  └─ 源码确实重构了  → 人工确认后更新用例，并刷新 last_reviewed
```

### 与参考文档的对应关系

参考方案没有 `eval`、没有 `eval --suggest`、也没有 golden 自动化验收。
本节的 `eval` 是本项目的扩展，用于替代文档里的**手工验证**部分：

| 参考文档机制 | 本文档对应 |
|---|---|
| `get_db_info()` 新鲜度 / 分支一致性 / 方法数为 0 三项检查 | `info()` 的 `warnings`（含 `methods==0`），`doctor` 命令兜底 |
| 构建后用 SQL 数表 + 手工调 MCP 工具验证 | `eval` 自动跑同样的断言；`build --check` 可并入构建流程 |
| Langfuse 低分 trace 入 dataset、复刻验收清单 | 可选 Agent 层，不阻塞 P1/P2 |

`info()` 的警告和文档一致：**只提示、不阻塞**。

---

## 验收（本仓库实测）

| 项目 | 结果 |
|---|---|
| 全量构建耗时 | 约 6s（377 文件 / 5.2 万行，含代码指纹） |
| 重复构建 | `files_parsed=0`，计数不变 |
| 解析失败 | 0 |
| 索引规模 | 353 类 / 2279 方法（1946 有方法体）/ 4153 调用边 / 1111 依赖边 / 64 导航边 / 21 通知 |
| 代码指纹 | 1460 个方法（约 0.76 MB blob），全量 1.6s；`duplicates` 约 0.1s |
| 重复代码 | 21 组（精确 13 / 近似 8），含 5 个 Category 的 `swizzleMethod:withMethod:` |
| 关键词验证 | 中文 FTS 命中；头文件/实现/调用方行号与源码一致 |
| 入口路径 | `path TBFinanceDetailViewController`：AppDelegate --tab@60--> 行情首页 --push@838--> 详情页 |
| 通知配对 | 未配对 1 条：`CameraViewController.tbLanguageDidChange`（只注册不移除） |
| 业务别名 | `报价` 经同义词命中行情页 `TBFinanceHomeViewController`（靠界面文案 `@"行情"`）；`jank` → 卡顿 → `TDLongTaskMonitor` |
| 协议/block 调用 | `delegate?.captureOutput` → `CameraCaptureViewModelDelegate`；`onSelect` 4 处 block 调用 |
| 分类归属 | `search_symbol("BreadCrumb")` 命中 `UIViewController` 等 5 个宿主类 |
| 同名类消歧 | `trace TBTabbarViewController` 返回 2 个声明点，`--module` 切换后方法不串模块 |
| 向量索引 | 1522 个方法（≥3 行且 ≥80 字符）；Qwen3 全量约 78 分钟（一次性），增量约 2~4s（大批文件移动时约 1 分钟） |
| `eval` golden 用例 | **38/38 通过**（含 2 条语义 + 7 条真实问答；无向量时 29 通过 + 9 跳过） |
| MCP 双客户端验收 | `raw-jsonrpc` 与 `python-sdk` 各 19 个工具；18 个只读工具与查询层逐字段一致 |
| UI 文案检索 | `find_feature("行情")` 命中 `TBFinanceHomeViewController`；注释里的中文不进类级检索 |
| 测试 | 99 个 unittest 全部通过（真模型不参与单测，用假 embedder 验链路） |

### 真实 MCP 会话回放

单测和 golden 只能证明“数据和协议一致”，不能证明“能回答业务问题”。所以用官方
MCP SDK 真实拉起 `python3 -m ios_qa mcp` 子进程，按开发者口吻连问 7 个问题，
并做多工具串联（`find_feature` → `trace_class` → `search_methods` → `read_source`）。

| 问题 | 结果 |
|---|---|
| 改 `TBRequestManager` 影响谁？ | ✅ 3 层共 8 个类，带证据链 `TBFinanceHTTP... -> TBRequestManager.get` → `TBFinanceService` → `TBFinanceHomeViewController.loadQuotesForCategory:` |
| 启动后怎么进入行情详情页？ | ✅ `AppDelegate --tab--> TBFinanceHomeViewController`(AppDelegate.swift:59) `--push--> TBFinanceDetailViewController`(.m:838) |
| 有哪些重复代码？ | ✅ 21 组（精确 13 / 近似 8），能列出 `ImageEditorViewController` 的近似方法簇 |
| 行情页数据从哪来？ | ⚠️ 结构化链路通，但 `trace_class` 的 `callers` 里混入 `NSCalendar` 等系统类，需要在结果里筛 `own_class` |
| 相机拍照后怎么保存图片？ | ✅ `hybrid_find_feature` #1 `MainCameraEditViewController.saveImageToTempDirectory` |
| 行情页面怎么加载数据？ | ✅ #1 `TBFinanceHomeViewController.loadQuotesForCategory:` |
| 网络请求失败怎么重试？ | ✅ `TBFinanceHTTP.attemptGETAtIndex:...` 进 top-3；#1 是 `TBNetworkFacadeSmokeViewController.requestFailure`（演示页） |
| 怎么监控主线程卡顿？ | ✅ #1 `TDHangWatchdog.loop` |
| 崩溃日志怎么上报？ | ✅ #1 `TBCrashReporter`（handleException / writeReportFile） |
| 天气页面怎么搜索城市？ | ✅ `QWeatherSearchViewController.searchRemoteWithKeyword:` 进 top-3；#1 是首页定位数据 |
| 日志怎么分批上传？ | ✅ `TBLogUploader` #2；#1 是 `MMAPFileManagerObjc.moveLogsToUploading:`（文件搬运） |

结论：**结构化链路（impact / navigation / duplicates / read_source）可以直接
用于问答；自然语言检索现在走 `hybrid_find_feature`（FTS + 向量 + Cross-Encoder），
7 条真实问题全部命中 top-3**。剩下的问题是「问题里完全没有代码字面量」时
仍需要同义词/查询改写，以及精排偶尔会把演示页排在实现前（网络重试）。

手动抽查建议：

```bash
python3 -m ios_qa trace TBRequestManager
python3 -m ios_qa trace TDMonitor
python3 -m ios_qa find "性能 监控"
python3 -m ios_qa impact TDMonitor --depth 2
python3 -m ios_qa path TBFinanceDetailViewController
python3 -m ios_qa notifications --unbalanced
```

---

## 已知边界（P1 不做）

- 不做类型推断/编译级语义分析：跨文件调用关系以「类名 + 方法名」启发式为主，
  `confidence` 字段标注 `high/medium/low`；
- Swift 属性支持显式类型与 `= Foo()` 推断，复杂表达式不展开；
- 通知配对基于同名注册/移除，RAC / block 观察者标为 `-1`（无法判定）；
- 页面入口覆盖 storyboard、segue、push/present/root 与常见 tab 容器写法；
  自定义工厂函数里间接返回页面、跨文件的 tab 组装仍可能只落到 `instantiate` 弱边；
- 协议/block 调用只解析同文件属性（ObjC 额外读同目录 `.h`），跨文件 delegate
  属性仍是启发式，`confidence=low`；
- 同名类在 `classes` 里仍是一行（`method_count` 是合并值），
  `trace --module` 只收敛查询结果，不改写聚合表；
- 重复/相似代码默认用**词法指纹**判定结构一致（改名能命中，换实现思路不命中）；
  语义近邻需要先 `vectors`；默认 Qwen3-INT8 全量建库约 78 分钟（一次性），
  快速对照可切 `bge-small-zh`；
- `vector_search` 的 `score` 是 RRF 融合分（约 0.2~0.9）；`hybrid_find_feature`
  的 `score` 是 Cross-Encoder 概率，`rule_score` / `vector_score` 保留规则分；
  需要纯余弦看 `find_similar_code --mode semantic`；
- **自然语言检索质量**：`hybrid_find_feature` 在 7 条真实问题上 7/7 命中 top-3，
  但精排偶尔把演示页排在实现前（「网络失败重试」#1 是 Smoke VC，#3 才是
  `TBFinanceHTTP`）；候选召回仍依赖问题里的字面量，完全同义改写会漏。
  命中的类/方法名明确时优先用 `find_feature` / `search_methods` / `trace_class`；
- **同义词覆盖不足**：代码里没有“离屏渲染”“失败重试”这类字面量时，
  纯关键词会命中相近词的类（相机 Metal 渲染）。需要往 `.ios_qa/synonyms.json`
  补业务组；P1 计划把同义词写进索引文本，而不是只做查询侧扩展；
- Cross-Encoder 首次使用要下载约 0.6 GB 模型，候选 20~45 个时单次精排约数秒；
  不需要时传 `rerank=False` 只走规则排序；
- 不解析 Objective-C++ 模板细节与宏展开。

## 后续

**P2 验收不绑定模型或客户端**，分三层：

1. **协议层**：任意支持 stdio 的 MCP 客户端都能 `list_tools` / `call_tool`；
2. **数据层**：`python3 -m ios_qa eval` 跑 golden 用例（类名、文件、行号、接口路径），
   不调用 LLM，结果可复现；
3. **Agent 层（可选）**：如果要用 LLM 评估自然语言答案，接任意 Agent 即可，
   评测脚本只消费 MCP 返回的 JSON，不依赖具体厂商。换模型只需要换客户端配置。

- **P2-a（已完成）**：补查询面 —— 页面入口路径（`path` / `find_navigation`）、
  通知专项查询（`notifications` / `find_notifications`）、业务同义词
  （`synonyms.json`），golden 扩到 20 条；
- **P2-b（已完成）**：解析质量 —— Swift extension / ObjC category 归属、block / 协议
  方法调用、tab 容器创建页面的导航边、多行类声明；golden 扩到 25 条；
- **P2-c（已完成）**：双 MCP 客户端接入验收（`mcp-smoke`：stdlib JSON-RPC +
  官方 SDK，协议层 + 数据层三方比对）；
- **P2-d（已完成）**：同名类消歧 —— 识别多个声明点、`trace --module` 收敛；
  界面文案进类级检索（`symbols.literals_json`）；`path` 同链去重；golden 扩到
  27 条；
- **P3-a（已完成）**：相似代码 / 重复扫描 —— `code_fingerprints` 表、MinHash + LSH
  出候选、shingle 集合精确复核；`similar` / `duplicates` / `find_similar_code` /
  `find_duplicates`；golden 扩到 29 条；
- **P3-b（已完成）**：向量语义检索 —— Qdrant `methods` collection（Qwen3-1024 +
  BM42，RRF k=60）、`embedding_cache.db`、`file_hashes.json` 增量；
  `vectors` / `semantic` / `similar-snippet`、`vector_search` /
  `search_similar_snippet`、`find_similar_code(mode=semantic)`；
- **P0（检索质量，已完成）**：模型 profile（默认 Qwen3-INT8，BGE/jina 可切）；
  `hybrid_find_feature`（FTS + cosine×0.8，按类合并）；Cross-Encoder
  `bge-reranker-v2-m3-int8` 精排；7 条真实问答 golden（top-3 断言）；golden 扩到 38 条；
- **下一步**：索引期同义词标签、注释入 embedding、Agent Loom 接入（MCP 已是
  稳定协议层，不绑定模型）。
