# ios_qa — iOS 静态解析 + 知识库（P1 / P2）

纯 Python 词法扫描，把 iOS 工程解析成 SQLite 结构化索引，提供：

- **结构化索引**：类/协议/扩展、方法体、依赖、调用边、页面入口、跳转、通知、API 端点
- **中文全文检索**：CJK 单字 + bigram 预分词，不依赖 jieba；支持业务别名扩展
- **查询 CLI**：功能检索、符号、调用、影响面、页面入口路径、通知配对
- **MCP server**：14 个工具，可直接接任意支持 stdio 的 MCP 客户端

查询层（`query.py`）不依赖 CLI / MCP，后续接 Agent Loom 只需换接入层。

---

## 快速开始

```bash
cd /path/to/TrueDepthDemo2

# 构建索引（首次全量约 3~7 秒；之后按文件 sha256 增量）
python3 -m ios_qa build

# 构建 + 自动跑 golden 自检（失败退出码 3）
python3 -m ios_qa build --check

# 自检
python3 -m ios_qa doctor

# 模型无关的 golden 验收（27 个用例，不调用任何 LLM）
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
```

索引默认落在 `ios_qa/.data/ios_qa.db`（已加入 `.gitignore`）。

### 常用参数

| 参数 | 说明 |
|---|---|
| `--root <dir>` | 工程根目录，默认包所在目录 |
| `--db <path>` | SQLite 路径 |
| `--profile <json>` | 自定义 project.json |
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
      [TrueDepthDemo2/AppDelegate.swift:58]
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

复制 `ios_qa/mcp.json.example` 到工程根目录并改名为 `.mcp.json`，
或合并进已有客户端配置（字段名以客户端为准）：

```json
{
  "mcpServers": {
    "ios_qa": {
      "command": "python3",
      "args": ["-m", "ios_qa", "mcp"],
      "cwd": "/absolute/path/to/TrueDepthDemo2"
    }
  }
}
```

提供的工具：

| 工具 | 说明 |
|---|---|
| `kb_info` | 索引健康信息 |
| `find_feature` | 功能/页面检索（中文） |
| `search_symbol` | 类名/方法名符号检索 |
| `trace_class` | 类数据流全量视图；同名类返回 `ambiguous`/`candidates`，可传 `module` 消歧 |
| `search_methods` | 方法体全文检索 |
| `find_callers` | 调用方 |
| `impact_analysis` | 影响面分析 |
| `read_source` | 读源码（带行号，支持 focus） |
| `list_pages` | 页面列表 |
| `find_navigation` | 入口 → 页面的跳转链（含低置信度实例化边） |
| `find_notifications` | 通知注册/移除查询，`unbalanced_only` 只看未配对 |
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
python3 -m ios_qa mcp-smoke                 # 两个客户端 + 13 个只读工具
python3 -m ios_qa mcp-smoke --client raw    # 只跑 stdlib 客户端
python3 -m ios_qa mcp-smoke --only trace_class
python3 -m ios_qa mcp-smoke --json          # CI 消费
```

退出码：`0` 通过 / `1` 协议或数据不一致 / `2` 索引不存在。

```text
MCP 验收：TrueDepthDemo2  (ios_qa/.data/ios_qa.db)

协议层
  ✅ raw-jsonrpc  14 个工具  服务端 ios_qa / 协议 2024-11-05
  ✅ python-sdk  14 个工具  服务端 ios_qa / 协议 2025-11-25
  工具集合一致: 是（14 个）

数据层（客户端返回 vs QueryService 直接调用）
  ✅ kb_info              与查询层逐字段一致
  ✅ find_feature         与查询层逐字段一致
  ...
  ⏭  save_qa              写路径只验协议

通过 13/13 个工具（失败 0）；协议层通过
```

验收参数不写死类名：`build_cases()` 从索引里取样（方法最多的类、页面、模块、
源码文件、方法名），换一个工程可以直接跑。

---

## 架构

```
ios_qa/
├── config.py          Profile：所有项目差异（目录、类前缀、HTTP 门面、VC 基类）
├── discover.py        源码发现、目录/文件过滤、sha256
├── source.py          注释/字符串屏蔽、括号配对、行号换算、URL 清洗、UI 文案提取
├── textnorm.py        CJK 单字+bigram、驼峰拆词、FTS 表达式
├── synonyms.py        业务别名加载与查询扩展
├── synonyms.json      业务别名分组（行情/报价、崩溃/crash …）
├── model.py           SymbolRec / MethodRec / DepRec / CallRec ...
├── parsers/
│   ├── objc.py        .h/.m/.mm：interface/category/协议/方法体/通知/导航
│   ├── swift.py       class/struct/enum/protocol/extension/actor
│   └── ui.py          storyboard/xib：入口与 segue
├── db.py              DDL、增量写入、classes 聚合、FTS 重建
├── build.py           构建编排、git 元数据、build_runs
├── query.py           查询服务（纯 Python API）
├── cli.py             argparse 入口
├── mcp_server.py      FastMCP 适配层
├── mcp_smoke.py       MCP 双客户端验收（stdlib JSON-RPC + 官方 SDK）
└── tests/             unittest
```

### 数据表

文件级事实表（按 `file_path` 增量替换）：
`symbols`、`methods`、`deps`、`api_calls`、`notifications`、`calls`、`entrypoints`、`navigation`

聚合/辅助表：
`classes`（每次构建重建）、`files`、`metadata`、`build_runs`、`qa_history`

FTS5 虚表：
`classes_fts`、`methods_fts`、`qa_fts`（全量重建，数据量级下成本可接受）

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

---

## Profile（换工程只改 JSON）

`ios_qa/project.json` 示例字段：

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
python3 -m unittest discover -s ios_qa/tests -v
```

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
- MCP 工具集合与调用返回（14 个工具）
- 双客户端验收：stdlib JSON-RPC 客户端、官方 SDK 客户端、`save_qa` 写路径往返、
  `mcp-smoke` 退出码
- `build --check`：通过 / 失败退出码 3 / 无用例跳过 / 不加参数不跑 eval
- 真实仓库 smoke：规模、`TBRequestManager.h:29`、`TBFinanceHTTP.m:74`、
  入口链存在性（具体行号由 golden 负责，源码每次改动都会挪行）
- `run_eval()`：27 个 golden 用例全绿，且每条都带 `last_reviewed`

---

## Golden 用例（`eval`）

`eval_cases.json` 是对**当前真实仓库**的端到端断言：问什么 → 必须返回哪个类、
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

- 只读当前工程的 `ios_qa/eval_cases.json`；不存在时打印「跳过 eval 自检」，不影响构建；
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
| 全量构建耗时 | 约 3~4s（364 文件 / 5.1 万行） |
| 重复构建 | `files_parsed=0`，计数不变 |
| 解析失败 | 0 |
| 索引规模 | 352 类 / 2266 方法 / 4119 调用边 / 1102 依赖边 / 64 导航边 / 21 通知 |
| 关键词验证 | 中文 FTS 命中；头文件/实现/调用方行号与源码一致 |
| 入口路径 | `path TBFinanceDetailViewController`：AppDelegate --tab@58--> 行情首页 --push@838--> 详情页 |
| 通知配对 | 未配对 1 条：`CameraViewController.tbLanguageDidChange`（只注册不移除） |
| 业务别名 | `报价` 经同义词命中行情页 `TBFinanceHomeViewController`（靠界面文案 `@"行情"`）；`jank` → 卡顿 → `TDLongTaskMonitor` |
| 协议/block 调用 | `delegate?.captureOutput` → `CameraCaptureViewModelDelegate`；`onSelect` 4 处 block 调用 |
| 分类归属 | `search_symbol("BreadCrumb")` 命中 `UIViewController` 等 5 个宿主类 |
| 同名类消歧 | `trace TBTabbarViewController` 返回 2 个声明点，`--module` 切换后方法不串模块 |
| `eval` golden 用例 | **27/27 通过**（不依赖任何模型） |
| MCP 双客户端验收 | `raw-jsonrpc` 与 `python-sdk` 各 14 个工具；13 个只读工具与查询层逐字段一致 |
| UI 文案检索 | `find_feature("行情")` 命中 `TBFinanceHomeViewController`；注释里的中文不进类级检索 |
| 测试 | 68 个 unittest 全部通过 |

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
- 向量检索、相似代码、重复扫描（参考系统的 6 个工具）留到 P3；
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
- **P3**：向量层（`sqlite-vec` 或外部索引）+ 相似代码/重复扫描 + Agent Loom 接入。
