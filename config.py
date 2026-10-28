"""配置层。

只做一件事：把 AstrBot 塞进来的原始 dict 转成带默认值的强类型对象。
改配置不需要重启（AstrBot 保存配置时会刷新 self.config，这里每次判断都重新读）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 「她自己的名字」里允许的分隔符：空格 / 中英文逗号 / 顿号 / 竖线 / 分号 / 斜杠
_NAME_SPLIT = re.compile(r"[\s,，、|｜;；/／]+")


@dataclass
class WakeConfig:
    """插件运行时配置。所有字段都有默认值，坏配置也不会炸。"""

    enabled: bool = True
    whitelist_groups: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    self_name: str = "小鲸鱼"
    auto_keywords: bool = True
    window_seconds: int = 120
    debounce_seconds: float = 2.5
    debounce_outside: float = 0.0
    reply_probability: float = 0.8
    llm_provider: str = ""
    judge_timeout: float = 15.0
    history_count: int = 8
    enable_image: bool = True
    image_caption_provider: str = ""
    prompt_keyword: str = ""
    prompt_window: str = ""
    fail_open: bool = True
    debug_log: bool = False

    # ---------- 构造 ----------

    @classmethod
    def from_raw(cls, raw: dict | None) -> "WakeConfig":
        raw = raw or {}
        cfg = cls()
        cfg.enabled = _as_bool(raw.get("enabled"), True)
        cfg.whitelist_groups = _as_str_list(raw.get("whitelist_groups"))
        cfg.keywords = _as_str_list(raw.get("keywords"))
        cfg.self_name = str(raw.get("self_name") or "").strip()
        cfg.auto_keywords = _as_bool(raw.get("auto_keywords"), True)
        cfg.window_seconds = _as_int(raw.get("window_seconds"), 120, low=0, high=3600)
        cfg.debounce_seconds = _as_float(
            raw.get("debounce_seconds"), 2.5, low=0.0, high=15.0
        )
        cfg.debounce_outside = _as_float(
            raw.get("debounce_outside"), 0.0, low=0.0, high=15.0
        )
        cfg.reply_probability = _as_float(
            raw.get("reply_probability"), 0.8, low=0.0, high=1.0
        )
        cfg.llm_provider = str(raw.get("llm_provider") or "").strip()
        cfg.judge_timeout = _as_float(raw.get("judge_timeout"), 15.0, low=3.0, high=120.0)
        cfg.history_count = _as_int(raw.get("history_count"), 8, low=1, high=20)
        cfg.enable_image = _as_bool(raw.get("enable_image"), True)
        cfg.image_caption_provider = str(
            raw.get("image_caption_provider") or ""
        ).strip()
        cfg.prompt_keyword = str(raw.get("prompt_keyword") or "")
        cfg.prompt_window = str(raw.get("prompt_window") or "")
        cfg.fail_open = _as_bool(raw.get("fail_open"), True)
        cfg.debug_log = _as_bool(raw.get("debug_log"), False)
        return cfg

    # ---------- 派生 ----------

    def alias_names(self) -> list[str]:
        """把 self_name 拆成多个名字。

        用户在「她自己的名字」里可能填「小鲸鱼 大肥鱼」「小鲸鱼、大肥鱼」
        「鲸鱼|肥鱼」这类，空格 / 中英文逗号 / 顿号 / 竖线 / 分号都当分隔符。
        返回去重保序的列表，第一项视为主名。全都为空时返回 ["她"]。
        """
        raw = (self.self_name or "").strip()
        if not raw:
            return ["她"]
        parts = [p.strip() for p in _NAME_SPLIT.split(raw) if p.strip()]
        seen: set[str] = set()
        out: list[str] = []
        for name in parts:
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out or ["她"]

    def primary_name(self) -> str:
        """主名（第一个）。"""
        return self.alias_names()[0]

    def debounce_for(self, scenario: str) -> float:
        """取该场景的防抖秒数。

        窗口外（keyword）用 debounce_outside；它没填（0）就跟随 debounce_seconds。
        """
        if scenario == "keyword" and self.debounce_outside > 0:
            return self.debounce_outside
        return self.debounce_seconds

    def all_keywords(self, auto: list[str]) -> list[str]:
        """手动关键词 + 自动关键词，去重保序，忽略空串。"""
        seen: set[str] = set()
        out: list[str] = []
        source = list(self.keywords) + (list(auto) if self.auto_keywords else [])
        for kw in source:
            kw = (kw or "").strip()
            if not kw or kw in seen:
                continue
            seen.add(kw)
            out.append(kw)
        return out

    def group_allowed(self, group_id: str | None) -> bool:
        """白名单为空 = 全部放行。"""
        if not self.whitelist_groups:
            return True
        return bool(group_id) and str(group_id) in self.whitelist_groups


# ---------- 容错小工具 ----------


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value, default: int, low: int | None = None, high: int | None = None) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    if low is not None and num < low:
        return low
    if high is not None and num > high:
        return high
    return num


def _as_float(
    value, default: float, low: float | None = None, high: float | None = None
) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if low is not None and num < low:
        return low
    if high is not None and num > high:
        return high
    return num


def _as_str_list(value) -> list[str]:
    if isinstance(value, str):
        return [v.strip() for v in value.replace("\n", ",").split(",") if v.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []
