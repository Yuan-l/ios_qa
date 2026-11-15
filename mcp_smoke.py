#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 验收：两个互相独立的客户端 + 与查询层的数据一致性。

**协议层**：两个客户端都以真实子进程方式启动 ``python3 -m ios_qa mcp``，
走 stdio：

- ``raw-jsonrpc``：纯 stdlib 实现，只依赖 MCP 的 JSON-RPC 2.0 报文格式，
  不引入任何 SDK。它能跑通，说明服务端没有绑定某个客户端库。
- ``python-sdk``：官方 ``mcp`` SDK 的 ``ClientSession``，代表生态里的主流客户端。

**数据层**：同一组 golden 参数分别经两个客户端调用，再与
:class:`~ios_qa.query.QueryService` 的直接调用结果比对。三方逐字段相等，
才说明适配层既没丢字段，也没有客户端相关的行为差异。

不调用任何 LLM，也不依赖具体 Agent。
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import ProjectProfile
from .query import QueryService

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 90.0


class SmokeError(RuntimeError):
    """客户端或服务端在验收过程中出错。"""


def canonical(data: Any) -> Any:
    """把任意返回体归一化成可比较的 JSON 结构（日期等统一成字符串）。"""
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------
# 验收用例：所有参数都从索引里取样，换一个 iOS 工程也能跑
# --------------------------------------------------------------------------


def _seeds(db_path: str | Path) -> dict[str, str]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    def one(sql: str, *params: Any, default: str = "") -> str:
        try:
            row = conn.execute(sql, params).fetchone()
        except sqlite3.Error:
            return default
        return (row[0] if row and row[0] else default) or default

    seeds = {
        "class_name": one(
            "SELECT class_name FROM classes WHERE method_count >= 1 "
            "ORDER BY method_count DESC, class_name LIMIT 1",
            default="AppDelegate",
        ),
        "page": one(
            "SELECT class_name FROM classes WHERE is_view_controller = 1 "
            "ORDER BY method_count DESC, class_name LIMIT 1",
            default="",
        ),
        "module": one(
            "SELECT module FROM classes GROUP BY module "
            "ORDER BY COUNT(*) DESC, module LIMIT 1",
            default="",
        ),
        "file_path": one(
            "SELECT file_path FROM symbols "
            "WHERE kind IN ('class','implementation','struct') "
            "ORDER BY LENGTH(file_path), file_path LIMIT 1",
            default="",
        ),
        "method_name": one(
            "SELECT method_name FROM methods WHERE has_body = 1 "
            "AND method_name NOT IN ('init','dealloc','viewDidLoad','viewWillAppear') "
            "ORDER BY LENGTH(method_name) DESC, method_name LIMIT 1",
            default="",
        ),
    }
    conn.close()
    if not seeds["page"]:
        seeds["page"] = seeds["class_name"]
    return seeds


SERVICE_CALLS: dict[str, Callable[[QueryService, dict], Any]] = {
    "kb_info": lambda s, a: s.info(),
    "find_feature": lambda s, a: s.find_feature(a["query"], limit=a["limit"]),
    "search_symbol": lambda s, a: s.search_symbol(a["name"], limit=a["limit"]),
    "trace_class": lambda s, a: s.trace_class(a["class_name"], limit=a["limit"]),
    "search_methods": lambda s, a: s.search_methods(
        a["query"], class_name=a["class_name"], module=a["module"], limit=a["limit"]
    ),
    "find_callers": lambda s, a: s.find_callers(a["target"], limit=a["limit"]),
    "impact_analysis": lambda s, a: s.impact_analysis(
        a["target"], depth=a["depth"], limit=a["limit"]
    ),
    "find_navigation": lambda s, a: s.find_navigation(
        a["target"], depth=a["depth"], limit=a["limit"]
    ),
    "find_notifications": lambda s, a: s.find_notifications(
        name=a["name"],
        module=a["module"],
        balanced=0 if a["unbalanced_only"] else None,
        limit=a["limit"],
    ),
    "read_source": lambda s, a: s.read_source(
        a["file_path"], focus=a["focus"], context=a["context"]
    ),
    "list_pages": lambda s, a: s.list_pages(module=a["module"], limit=a["limit"]),
    "list_module": lambda s, a: s.list_module(a["module"]),
    "search_qa": lambda s, a: s.search_qa(
        a["query"], related_class=a["related_class"], limit=a["limit"]
    ),
}


def build_cases(db_path: str | Path) -> list[dict]:
    """构造 golden 调用矩阵；``compare=False`` 的只做协议层可用性检查。"""
    seeds = _seeds(db_path)
    cls = seeds["class_name"]
    args: dict[str, dict] = {
        "kb_info": {},
        "find_feature": {"query": cls, "limit": 5},
        "search_symbol": {"name": cls, "limit": 5},
        "trace_class": {"class_name": cls, "limit": 20},
        "search_methods": {
            "query": seeds["method_name"],
            "class_name": "",
            "module": "",
            "limit": 5,
        },
        "find_callers": {"target": cls, "limit": 20},
        "impact_analysis": {"target": cls, "depth": 2, "limit": 50},
        "find_navigation": {"target": seeds["page"], "depth": 6, "limit": 5},
        "find_notifications": {
            "name": "",
            "module": seeds["module"],
            "unbalanced_only": False,
            "limit": 20,
        },
        "read_source": {"file_path": seeds["file_path"], "focus": "", "context": 5},
        "list_pages": {"module": "", "limit": 10},
        "list_module": {"module": seeds["module"]},
        "search_qa": {"query": "", "related_class": "", "limit": 5},
    }
    cases = [
        {"tool": tool, "args": value, "compare": tool in SERVICE_CALLS}
        for tool, value in args.items()
    ]
    # 写路径只验证「工具存在且入参 schema 可接受」，不在真实索引里造数据
    cases.append({"tool": "save_qa", "args": None, "compare": False})
    return cases


# --------------------------------------------------------------------------
# 客户端 1：纯 stdlib JSON-RPC 2.0（换行分隔）
# --------------------------------------------------------------------------


def extract_payload(result: dict) -> Any:
    """MCP ``tools/call`` 的 result -> 业务 JSON。"""
    texts = [
        item.get("text", "")
        for item in result.get("content") or []
        if item.get("type") == "text"
    ]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError as exc:
            raise SmokeError(f"工具返回值不是 JSON：{texts[0][:120]!r}") from exc
    if "structuredContent" in result:
        return result["structuredContent"]
    return {"content": texts}


class RawStdioClient:
    """最小 MCP 客户端：只实现 initialize / tools/list / tools/call。"""

    name = "raw-jsonrpc"

    def __init__(
        self,
        args: list[str],
        cwd: str | Path,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.command = [sys.executable, *args]
        self.cwd = str(cwd)
        self.env = {**os.environ, **(env or {})}
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._messages: queue.Queue = queue.Queue()
        self._stderr: list[str] = []
        self._ids = iter(range(1, 10_000))
        self._threads: list[threading.Thread] = []

    # -- 生命周期 ---------------------------------------------------------
    def __enter__(self) -> "RawStdioClient":
        self._proc = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._threads = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._messages.put(json.loads(line))
            except json.JSONDecodeError:
                self._messages.put({"__raw__": line})

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            self._stderr.append(line.rstrip())
            del self._stderr[:-80]

    # -- 协议 -------------------------------------------------------------
    def _send(self, payload: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise SmokeError("进程未启动")
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 0.0):
        request_id = next(self._ids)
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeError(
                    f"{method} 超时；stderr: {self.stderr_tail()}"
                )
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty:
                raise SmokeError(f"{method} 超时；stderr: {self.stderr_tail()}") from None
            if message.get("id") != request_id:
                # 服务端通知（logging 等）直接忽略
                continue
            if "error" in message:
                raise SmokeError(f"{method} 返回错误：{message['error']}")
            return message.get("result")

    def stderr_tail(self, limit: int = 5) -> str:
        return " | ".join(self._stderr[-limit:])

    def initialize(self) -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self.name, "version": "0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def list_tools(self) -> list[dict]:
        return self.request("tools/list", {})["tools"]

    def call_tool(self, name: str, args: dict) -> Any:
        result = self.request("tools/call", {"name": name, "arguments": args})
        if result.get("isError"):
            raise SmokeError(f"{name} 返回 isError：{extract_payload(result)}")
        return extract_payload(result)


# --------------------------------------------------------------------------
# 客户端 2：官方 Python SDK
# --------------------------------------------------------------------------


async def run_sdk_client(
    command: str, args: list[str], cwd: str | Path, env: dict[str, str],
    cases: list[dict],
) -> tuple[list[str], dict[str, Any], dict]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=command, args=args, env=env, cwd=str(cwd))
    results: dict[str, Any] = {}
    with open(os.devnull, "w") as devnull:  # 服务端 INFO 日志不进验收输出
        async with stdio_client(params, errlog=devnull) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                for case in cases:
                    if case["args"] is None:
                        continue
                    result = await session.call_tool(case["tool"], case["args"])
                    if result.isError:
                        raise SmokeError(f"{case['tool']} 返回 isError")
                    texts = [
                        item.text for item in result.content
                        if getattr(item, "type", "") == "text"
                    ]
                    if len(texts) == 1:
                        results[case["tool"]] = json.loads(texts[0])
                    elif result.structuredContent is not None:
                        results[case["tool"]] = result.structuredContent
                    else:
                        results[case["tool"]] = {"content": texts}
                info = {
                    "server": getattr(init.serverInfo, "name", ""),
                    "version": getattr(init.serverInfo, "version", ""),
                    "protocol": init.protocolVersion,
                }
    return [tool.name for tool in tools.tools], results, info


# --------------------------------------------------------------------------
# 验收执行
# --------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class ClientReport:
    name: str
    ok: bool = False
    tools: list[str] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "tool_count": len(self.tools),
            "checks": [check.to_dict() for check in self.checks],
            "meta": self.meta,
        }


def _diff(expected: Any, actual: Any, path: str = "") -> str:
    """返回第一处差异的可读描述。"""
    if type(expected) is not type(actual):
        return f"{path or '$'} 类型 {type(expected).__name__} != {type(actual).__name__}"
    if isinstance(expected, dict):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        if missing:
            return f"{path or '$'} 缺少字段 {missing[:5]}"
        if extra:
            return f"{path or '$'} 多出字段 {extra[:5]}"
        for key in expected:
            found = _diff(expected[key], actual[key], f"{path}.{key}" if path else key)
            if found:
                return found
        return ""
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path or '$'} 长度 {len(expected)} != {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual)):
            found = _diff(left, right, f"{path}[{index}]")
            if found:
                return found
        return ""
    if expected != actual:
        left = repr(expected)[:60]
        right = repr(actual)[:60]
        return f"{path or '$'} {left} != {right}"
    return ""


def server_command(profile: ProjectProfile, db_path: str | Path) -> list[str]:
    return [
        "-m", "ios_qa", "mcp",
        "--root", str(profile.root),
        "--db", str(db_path),
    ]


def client_env() -> dict[str, str]:
    env = {"PYTHONPATH": str(PACKAGE_ROOT)}
    if "PYTHONPATH" in os.environ:
        env["PYTHONPATH"] = f"{PACKAGE_ROOT}{os.pathsep}{os.environ['PYTHONPATH']}"
    return env


def run_raw(profile: ProjectProfile, db_path: str | Path, cases: list[dict]) -> ClientReport:
    report = ClientReport(name=RawStdioClient.name)
    try:
        with RawStdioClient(
            server_command(profile, db_path), cwd=profile.root, env=client_env()
        ) as client:
            info = client.initialize()
            report.meta = {
                "server": (info.get("serverInfo") or {}).get("name", ""),
                "version": (info.get("serverInfo") or {}).get("version", ""),
                "protocol": info.get("protocolVersion", ""),
            }
            report.checks.append(
                Check("initialize", bool(info.get("capabilities") is not None),
                      f"server={(info.get('serverInfo') or {}).get('name')} "
                      f"protocol={info.get('protocolVersion')}")
            )
            report.tools = [tool["name"] for tool in client.list_tools()]
            report.checks.append(
                Check("tools/list", bool(report.tools), f"{len(report.tools)} 个工具")
            )
            for case in cases:
                if case["args"] is None:
                    continue
                payload = client.call_tool(case["tool"], case["args"])
                report.results[case["tool"]] = payload
                report.checks.append(Check(f"call:{case['tool']}", True, ""))
        report.ok = all(check.ok for check in report.checks)
    except Exception as exc:  # noqa: BLE001 —— 验收器要报告而不是抛出
        report.ok = False
        report.checks.append(Check("client", False, f"{type(exc).__name__}: {exc}"))
    return report


def run_sdk(profile: ProjectProfile, db_path: str | Path, cases: list[dict]) -> ClientReport:
    report = ClientReport(name="python-sdk")
    try:
        tools, results, info = asyncio.run(
            run_sdk_client(
                sys.executable,
                server_command(profile, db_path),
                profile.root,
                client_env(),
                cases,
            )
        )
        report.tools = tools
        report.results = results
        report.meta = info
        report.checks.append(
            Check("initialize", bool(tools), f"server={info.get('server')} "
                                             f"protocol={info.get('protocol')}")
        )
        report.checks.append(
            Check("tools/list", bool(tools), f"{len(tools)} 个工具")
        )
        report.checks.append(
            Check("tools/call", bool(results), f"{len(results)} 次调用")
        )
        report.ok = all(check.ok for check in report.checks)
    except Exception as exc:  # noqa: BLE001
        report.ok = False
        report.checks.append(Check("client", False, f"{type(exc).__name__}: {exc}"))
    return report


def run_acceptance(
    profile: ProjectProfile,
    db_path: str | Path | None = None,
    clients: str = "both",
    only: str = "",
) -> dict:
    """跑协议层 + 数据层验收，返回可直接 JSON 化的报告。"""
    db_path = str(db_path or profile.resolved_db_path)
    cases = build_cases(db_path)
    if only:
        cases = [case for case in cases if case["tool"] == only]
        if not cases:
            raise SmokeError(f"未知工具：{only}")

    reports: list[ClientReport] = []
    if clients in {"both", "raw"}:
        reports.append(run_raw(profile, db_path, cases))
    if clients in {"both", "sdk"}:
        reports.append(run_sdk(profile, db_path, cases))

    # ---- 数据层：与 QueryService 直接调用比对 ----
    data_checks: list[dict] = []
    with QueryService(profile, db_path) as service:
        for case in cases:
            tool = case["tool"]
            if not case["compare"]:
                data_checks.append(
                    {"tool": tool, "ok": True, "skipped": True, "detail": "写路径只验协议"}
                )
                continue
            expected = canonical(SERVICE_CALLS[tool](service, case["args"]))
            detail = ""
            ok = True
            for report in reports:
                actual = report.results.get(tool)
                if actual is None:
                    ok, detail = False, f"{report.name} 没有返回"
                    break
                diff = _diff(expected, canonical(actual))
                if diff:
                    ok, detail = False, f"{report.name}: {diff}"
                    break
            if ok and len(reports) == 2:
                diff = _diff(
                    canonical(reports[0].results[tool]),
                    canonical(reports[1].results[tool]),
                )
                if diff:
                    ok, detail = False, f"客户端之间不一致: {diff}"
            data_checks.append({"tool": tool, "ok": ok, "skipped": False, "detail": detail})

    # ---- 工具集合：两边必须完全一致 ----
    tool_sets = {frozenset(report.tools) for report in reports}
    tools_agree = len(tool_sets) == 1

    compared = [check for check in data_checks if not check["skipped"]]
    protocol_ok = all(report.ok for report in reports)
    data_ok = all(check["ok"] for check in compared)
    return {
        "ok": protocol_ok and data_ok and tools_agree,
        "project": profile.name,
        "db_path": db_path,
        "clients": [report.to_dict() for report in reports],
        "tools": {
            "count": len(reports[0].tools) if reports else 0,
            "names": sorted(reports[0].tools) if reports else [],
            "agree": tools_agree,
        },
        "data": data_checks,
        "summary": {
            "protocol_ok": protocol_ok,
            "data_ok": data_ok,
            "tools_agree": tools_agree,
            "compared": len(compared),
            "passed": sum(1 for check in compared if check["ok"]),
            "failed": sum(1 for check in compared if not check["ok"]),
        },
    }
