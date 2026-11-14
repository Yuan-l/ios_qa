#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目 Profile：所有项目差异都收敛到这份配置里。

设计原则：解析器只认 Profile，不认具体项目。换工程时改 JSON（或另写一份
``--profile``）即可，不需要动代码。
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_SKIP_DIR_NAMES = {
    ".git", ".build", ".swiftpm", "__pycache__", ".reports",
    "Pods", "build", "DerivedData", "Carthage", "vendor", "node_modules",
    "Frameworks", "Example", "Tests", "UITests",
}

DEFAULT_SKIP_PATH_PARTS = [
    "/SDWebImage/",
    "/Flutter.framework/",
    "/QWeather.framework/",
    "/.git/",
    "/.build/",
]

DEFAULT_INCLUDE_EXTS = {".h", ".m", ".mm", ".swift", ".storyboard", ".xib"}

DEFAULT_SKIP_FILE_GLOBS = [
    "*-Swift.h",
    "Pods-*.h",
    "*.generated.swift",
    "*.pb.swift",
    "R.generated.swift",
]

DEFAULT_OWN_CLASS_PATTERNS = [r"^(TB|LM|TD|My)[A-Za-z0-9_]*$"]

_TEST_DIR_RE = re.compile(r"(?:Tests|UITests|Specs|SnapshotTests)$")

DEFAULT_HTTP_MANAGERS = [
    "TBRequestManager",
    "TDNetworkFacade",
    "TDURLSessionNetworkClient",
    "TBHttpRequestManager",
    "AFHTTPSessionManager",
]

DEFAULT_API_MANAGER_PATTERNS = [
    r"^(TB|TD|LM|My)[A-Za-z0-9_]*(Manager|Service|API|Api|Client|Facade|Helper|Utils)$"
]

DEFAULT_VC_BASES = [
    "UIViewController",
    "UITableViewController",
    "UICollectionViewController",
    "UINavigationController",
    "UITabBarController",
    "UISplitViewController",
    "UIHostingController",
    "UIInputViewController",
    "UIPageViewController",
    "TBBaseViewController",
]

PROFILE_FILENAME = "project.json"
DB_RELATIVE_PATH = Path(".data") / "ios_qa.db"


@dataclass
class ProjectProfile:
    name: str
    root: Path
    source_dirs: list[str] = field(default_factory=lambda: ["."])
    skip_dir_names: set[str] = field(default_factory=lambda: set(DEFAULT_SKIP_DIR_NAMES))
    skip_path_parts: list[str] = field(default_factory=lambda: list(DEFAULT_SKIP_PATH_PARTS))
    include_exts: set[str] = field(default_factory=lambda: set(DEFAULT_INCLUDE_EXTS))
    skip_file_globs: list[str] = field(default_factory=lambda: list(DEFAULT_SKIP_FILE_GLOBS))
    own_class_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_OWN_CLASS_PATTERNS))
    http_managers: list[str] = field(default_factory=lambda: list(DEFAULT_HTTP_MANAGERS))
    api_manager_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_API_MANAGER_PATTERNS))
    view_controller_bases: list[str] = field(default_factory=lambda: list(DEFAULT_VC_BASES))
    min_method_body_lines: int = 0
    max_body_chars: int = 8000
    max_fts_body_chars: int = 4000
    db_path: Path | None = None

    # -- 编译后的匹配器（dataclass 之外惰性构建） -------------------------
    _own_class_res: list[re.Pattern] = field(default_factory=list, init=False, repr=False)
    _api_manager_res: list[re.Pattern] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        self._own_class_res = [re.compile(p) for p in self.own_class_patterns]
        self._api_manager_res = [re.compile(p) for p in self.api_manager_patterns]

    # ------------------------------------------------------------------
    # 载入
    # ------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        profile_path: str | Path | None = None,
        root: str | Path | None = None,
    ) -> "ProjectProfile":
        """载入 profile。

        优先级：显式 ``profile_path`` > ``root/ios_qa/project.json`` >
        包内默认 ``project.json``。
        """
        if root is not None:
            root_path = Path(root).expanduser().resolve()
        else:
            root_path = Path(__file__).resolve().parent.parent

        candidate: Path | None = None
        if profile_path:
            candidate = Path(profile_path).expanduser()
            if not candidate.is_absolute():
                candidate = root_path / candidate
        else:
            local = root_path / "ios_qa" / PROFILE_FILENAME
            if local.exists():
                candidate = local
            else:
                candidate = Path(__file__).resolve().with_name(PROFILE_FILENAME)

        data: dict = {}
        if candidate and candidate.exists():
            data = json.loads(candidate.read_text(encoding="utf-8"))

        db_path = data.get("db_path")
        if db_path:
            db_path = Path(db_path).expanduser()
            if not db_path.is_absolute():
                db_path = root_path / db_path
        else:
            db_path = root_path / "ios_qa" / DB_RELATIVE_PATH

        return cls(
            name=data.get("name") or root_path.name,
            root=root_path,
            source_dirs=list(data.get("source_dirs") or ["."]),
            skip_dir_names=set(data.get("skip_dir_names") or DEFAULT_SKIP_DIR_NAMES),
            skip_path_parts=list(data.get("skip_path_parts") or DEFAULT_SKIP_PATH_PARTS),
            include_exts=set(data.get("include_exts") or DEFAULT_INCLUDE_EXTS),
            skip_file_globs=list(data.get("skip_file_globs") or DEFAULT_SKIP_FILE_GLOBS),
            own_class_patterns=list(data.get("own_class_patterns") or DEFAULT_OWN_CLASS_PATTERNS),
            http_managers=list(data.get("http_managers") or DEFAULT_HTTP_MANAGERS),
            api_manager_patterns=list(data.get("api_manager_patterns") or DEFAULT_API_MANAGER_PATTERNS),
            view_controller_bases=list(data.get("view_controller_bases") or DEFAULT_VC_BASES),
            min_method_body_lines=int(data.get("min_method_body_lines", 0)),
            max_body_chars=int(data.get("max_body_chars", 8000)),
            max_fts_body_chars=int(data.get("max_fts_body_chars", 4000)),
            db_path=Path(db_path),
        )

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    def is_own_class(self, name: str) -> bool:
        if not name:
            return False
        return any(r.fullmatch(name) for r in self._own_class_res)

    def is_api_manager(self, name: str) -> bool:
        if not name:
            return False
        return any(r.fullmatch(name) for r in self._api_manager_res)

    def should_skip_dir(self, name: str) -> bool:
        if name in self.skip_dir_names:
            return True
        # 组件常见的 TDMonitorKitTests / MyCameraModuleUITests 等测试目录
        return bool(_TEST_DIR_RE.search(name))

    def should_skip_path(self, rel_path: str) -> bool:
        normalized = "/" + rel_path.replace("\\", "/").strip("/") + "/"
        return any(part in normalized for part in self.skip_path_parts)

    def should_skip_file(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pattern) for pattern in self.skip_file_globs)

    def module_for(self, rel_path: Path) -> str:
        parts = rel_path.parts
        if not parts:
            return "Unknown"
        if parts[0] == "components" and len(parts) > 1:
            return parts[1]
        if parts[0].startswith("TrueDepthDemo2"):
            return "App"
        return parts[0]

    @property
    def resolved_db_path(self) -> Path:
        assert self.db_path is not None
        return self.db_path
