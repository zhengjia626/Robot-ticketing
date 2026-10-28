"""配置与业务规则加载（方案 9.8 节）

设计要点：
1. 服务配置 app.yaml 与业务规则 rules.json 分离，前者管运行时，后者管业务。
2. 规则文件支持热更新：mtime 变化即重载，无需重启服务。
3. 规则变更留痕：每次重载写入规则版本历史，供异常复盘回溯某一时刻生效的规则。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict

import yaml

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = BASE_DIR / "config"
APP_YAML = CONFIG_DIR / "app.yaml"
RULES_JSON = CONFIG_DIR / "rules.json"


def _deep_get(d: Dict[str, Any], path: str, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


class Settings:
    """服务配置（app.yaml）"""

    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def get(self, path: str, default=None):
        return _deep_get(self._data, path, default)

    @property
    def host(self) -> str:
        return self.get("service.host", "0.0.0.0")

    @property
    def port(self) -> int:
        return int(self.get("service.port", 8080))

    @property
    def ops_port(self) -> int:
        return int(self.get("service.ops_port", 8081))

    @property
    def long_poll_timeout(self) -> int:
        return int(self.get("display.long_poll_timeout_sec", 25))


class RuleStore:
    """业务规则文件（rules.json），带热更新与版本留痕"""

    def __init__(self, path: Path = RULES_JSON):
        self._path = path
        self._lock = threading.RLock()
        self._rules: Dict[str, Any] = {}
        self._mtime = 0.0
        self._history: list[dict] = []
        self.load(force=True)

    # ---------- 加载 ----------
    def load(self, force: bool = False) -> bool:
        """返回是否发生了重载"""
        with self._lock:
            try:
                mtime = os.path.getmtime(self._path)
            except OSError:
                return False
            if not force and mtime == self._mtime:
                return False
            with open(self._path, "r", encoding="utf-8") as f:
                new_rules = json.load(f)
            old_version = self._rules.get("version")
            self._rules = new_rules
            self._mtime = mtime
            self._history.append(
                {
                    "ts": time.time(),
                    "from_version": old_version,
                    "to_version": new_rules.get("version"),
                    "source": "file" if not force else "startup",
                }
            )
            # 历史只保留最近 100 条
            self._history = self._history[-100:]
            return True

    def reload_if_changed(self) -> bool:
        return self.load(force=False)

    def replace(self, new_rules: Dict[str, Any], operator: str = "api") -> str:
        """运维接口整体替换规则（POST /api/ops/rule）"""
        with self._lock:
            old_version = self._rules.get("version")
            self._rules = new_rules
            try:
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(new_rules, f, ensure_ascii=False, indent=2)
                self._mtime = os.path.getmtime(self._path)
            except OSError:
                pass
            self._history.append(
                {
                    "ts": time.time(),
                    "from_version": old_version,
                    "to_version": new_rules.get("version"),
                    "source": f"api:{operator}",
                }
            )
            return str(new_rules.get("version"))

    # ---------- 读取 ----------
    @property
    def data(self) -> Dict[str, Any]:
        with self._lock:
            return self._rules

    @property
    def version(self) -> str:
        return str(self._rules.get("version", "unknown"))

    @property
    def history(self) -> list[dict]:
        with self._lock:
            return list(self._history)

    def get(self, path: str, default=None):
        with self._lock:
            return _deep_get(self._rules, path, default)

    def timeout(self, key: str, default: float = 30.0) -> float:
        return float(self.get(f"timeouts.{key}", default))

    def prompt(self, key: str, **kwargs) -> str:
        tpl = self.get(f"prompts.{key}", key)
        try:
            return tpl.format(**kwargs)
        except (KeyError, IndexError):
            return tpl

    def page_for_state(self, state: str) -> str:
        return str(self.get(f"state_page_map.{state}", "P-0"))


def load_settings() -> Settings:
    with open(APP_YAML, "r", encoding="utf-8") as f:
        return Settings(yaml.safe_load(f) or {})


settings = load_settings()
rules = RuleStore()
