# ios_qa — iOS 静态解析 + 知识库（P1）

纯 Python 词法扫描，把 iOS 工程解析成 SQLite 结构化索引，提供：

- **结构化索引**：类/协议/扩展、方法体、依赖、调用边、页面入口、跳转、通知、API 端点
- **中文全文检索**：CJK 单字 + bigram 预分词，不依赖 jieba
- **查询 CLI**：8 个命令，输出带文件与行号
- **MCP server**：12 个工具，可直接接 Claude Code / 其他 MCP 客户端

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

# 模型无关的 golden 验收（14 个用例，不调用任何 LLM）
python3 -m ios_qa eval

# 查询
python3 -m ios_qa find "网络 请求"
python3 -m ios_qa trace TBRequestManager
python3 -m ios_qa methods "采样"
python3 -m ios_qa callers TBRequestManager
python3 -m ios_qa impact TBRequestManager --depth 2
python3 -m ios_qa read components/TBNetworkManager/Classess/Objc/TBRequestManager.h --focus shared
python3 -m ios_qa pages
python3 -m ios_qa module TDMonitorKit
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
| `trace <类>` | 继承链、方法、依赖/被依赖、API、通知配对、调用方/被调方、跳转、相关问答 |
| `methods [query]` | 方法体全文检索，支持 `--class` / `--module` |
| `callers <目标>` | `TBRequestManager` 或 `TBRequestManager.requestWithMethod` 的调用方 |
| `impact <目标>` | 反向 BFS 影响面，`--depth 1..4` |
| `read <file>` | 带行号读源码，`--focus` 定位首次命中 |
| `pages` | 列出 UIViewController 子类 |
| `module <名称>` | 模块概览 |
| `qa save/search/delete` | 历史问答知识库 |
| `mcp` | 启动 MCP stdio server |
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
| `trace_class` | 类数据流全量视图 |
| `search_methods` | 方法体全文检索 |
| `find_callers` | 调用方 |
| `impact_analysis` | 影响面分析 |
| `read_source` | 读源码（带行号，支持 focus） |
| `list_pages` | 页面列表 |
| `list_module` | 模块概览 |
| `search_qa` | 检索历史问答 |
| `save_qa` | 保存问答 |

所有工具返回 JSON 字符串，字段里统一有 `file_path`、`line_start/line_number`。

先构建索引再启动 MCP；索引不存在时工具会返回空结果，不会崩溃。

---

## 架构

```
ios_qa/
├── config.py          Profile：所有项目差异（目录、类前缀、HTTP 门面、VC 基类）
├── discover.py        源码发现、目录/文件过滤、sha256
├── source.py          注释/字符串屏蔽、括号配对、行号换算、URL 清洗
├── textnorm.py        CJK 单字+bigram、驼峰拆词、FTS 表达式
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

- 中文单字/bigram、驼峰拆词、FTS 表达式转义
- ObjC 头文件/实现：符号、方法、调用、通知、行号
- Swift：类型/属性/方法、调用、导航、通知、URL
- Storyboard 入口与 segue
- 增量构建幂等、文件修改/删除
- 中文查询、Q&A 往返
- MCP 工具集合与调用返回（12 个工具）
- `build --check`：通过 / 失败退出码 3 / 无用例跳过 / 不加参数不跑 eval
- 真实仓库 smoke：规模、`TBRequestManager.h:29`、`TBFinanceHTTP.m:74` 等已知行号
- `run_eval()`：14 个 golden 用例全绿，且每条都带 `last_reviewed`

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

## P1 验收（本仓库实测）

| 项目 | 结果 |
|---|---|
| 全量构建耗时 | 约 3~4s（361 文件 / 5.2 万行） |
| 重复构建 | `files_parsed=0`，计数不变 |
| 解析失败 | 0 |
| 索引规模 | 347 类 / 2217 方法 / 4035 调用边 / 1029 依赖边 |
| 关键词验证 | 中文 FTS 命中；头文件/实现/调用方行号与源码一致 |
| `eval` golden 用例 | **14/14 通过**（不依赖任何模型） |
| 测试 | 32 个 unittest 全部通过 |

手动抽查建议：

```bash
python3 -m ios_qa trace TBRequestManager
python3 -m ios_qa trace TDMonitor
python3 -m ios_qa find "性能 监控"
python3 -m ios_qa impact TDMonitor --depth 2
```

---

## 已知边界（P1 不做）

- 不做类型推断/编译级语义分析：跨文件调用关系以「类名 + 方法名」启发式为主，
  `confidence` 字段标注 `high/medium/low`；
- Swift 属性支持显式类型与 `= Foo()` 推断，复杂表达式不展开；
- 通知配对基于同名注册/移除，RAC / block 观察者标为 `-1`（无法判定）；
- 向量检索、相似代码、重复扫描（参考系统的 6 个工具）留到 P3；
- 不解析 Objective-C++ 模板细节与宏展开。

## 后续

**P2 验收不绑定模型或客户端**，分三层：

1. **协议层**：任意支持 stdio 的 MCP 客户端都能 `list_tools` / `call_tool`；
2. **数据层**：`python3 -m ios_qa eval` 跑 golden 用例（类名、文件、行号、接口路径），
   不调用 LLM，结果可复现；
3. **Agent 层（可选）**：如果要用 LLM 评估自然语言答案，接任意 Agent 即可，
   评测脚本只消费 MCP 返回的 JSON，不依赖具体厂商。换模型只需要换客户端配置。

- **P2**：补充更多 golden 用例（页面入口、通知配对、影响面），跑通任意 MCP 客户端接入；
- **P3**：向量层（`sqlite-vec` 或外部索引）+ 相似代码/重复扫描 + Agent Loom 接入。
