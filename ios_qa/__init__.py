"""ios_qa — iOS 项目静态解析 + 知识库。

对外只有三类入口：
- 构建：``python3 -m ios_qa build``
- 查询：``python3 -m ios_qa ...`` / :class:`ios_qa.query.QueryService`
- 接入：``python3 -m ios_qa mcp``（MCP stdio server）

解析结果全部落在 SQLite，查询层不依赖 MCP / CLI，方便后续接 Agent Loom。
"""

__version__ = "0.1.0"
SCHEMA_VERSION = 3
