"""小模型判断层。

职责单一：给一个场景 + 上下文，问小模型"该不该接话"，返回结构化结果。
不关心窗口、不关心事件、不关心 AstrBot 的钩子。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from . import prompts


@dataclass
class JudgeResult:
    action: str        # "reply" | "skip"
    reason: str = ""
    raw: str = ""

    @property
    def should_reply(self) -> bool:
        return self.action == "reply"


# ---------- 主入口 ----------


async def judge(
    *,
    provider,
    scenario: str,
    self_name: str,
    persona_line: str,
    history: list[tuple[str, str]],
    sender_name: str,
    message: str,
    hit_keywords: list[str] | None = None,
    custom_prompt: str = "",
    timeout: float = 15.0,
    fail_open: bool = True,
) -> JudgeResult:
    """跑一次判断。

    scenario: "keyword"（窗口外关键词命中）或 "window"（窗口内）
    history: [(发言人, 内容), ...]，旧的在前
    custom_prompt: 用户在配置里写的提示词，空则用内置模板
    """
    prompt = _build_prompt(
        scenario=scenario,
        self_name=self_name,
        persona_line=persona_line,
        history=history,
        sender_name=sender_name,
        message=message,
        hit_keywords=hit_keywords or [],
        custom_prompt=custom_prompt,
    )

    try:
        resp = await asyncio.wait_for(
            provider.text_chat(prompt=prompt),
            timeout=timeout,
        )
        text = (getattr(resp, "completion_text", "") or "").strip()
    except asyncio.TimeoutError:
        return JudgeResult("reply" if fail_open else "skip", "判断超时")
    except Exception as exc:  # noqa: BLE001 - 任何异常都走兜底
        return JudgeResult("reply" if fail_open else "skip", f"判断异常: {exc}")

    parsed = _parse_response(text)
    if parsed is None:
        return JudgeResult("reply" if fail_open else "skip", "返回格式无法解析", text)

    action, reason = parsed
    return JudgeResult(action, reason, text)


# ---------- prompt ----------


def _render_template(tpl: str, mapping: dict[str, str]) -> str:
    """用 replace 填充占位符，不用 .format —— 避免用户提示词里的 JSON 花括号被误解析。"""
    out = tpl
    for key, value in mapping.items():
        out = out.replace("{" + key + "}", value)
    return out


def _build_prompt(
    *,
    scenario: str,
    self_name: str,
    persona_line: str,
    history: list[tuple[str, str]],
    sender_name: str,
    message: str,
    hit_keywords: list[str],
    custom_prompt: str = "",
) -> str:
    who = prompts.WHO_AM_I.format(
        self_name=self_name,
        persona_line=persona_line or prompts.DEFAULT_PERSONA_LINE,
    )
    history_text = _render_history(history, self_name)
    mapping = {
        "who_am_i": who,
        "self_name": self_name,
        "persona_line": persona_line or prompts.DEFAULT_PERSONA_LINE,
        "history": history_text,
        "sender_name": sender_name,
        "message": message,
        "hit_keywords": "、".join(hit_keywords) if hit_keywords else "（无）",
    }

    # 用户自定义提示词优先
    if custom_prompt and custom_prompt.strip():
        return _render_template(custom_prompt.strip(), mapping)

    if scenario == "keyword":
        return prompts.KEYWORD_HIT.format(**mapping)
    return prompts.IN_WINDOW.format(**mapping)


def _render_history(history: list[tuple[str, str]], self_name: str) -> str:
    if not history:
        return "（暂无更早的聊天记录）"
    # self_name 可能填了多个名字（「小鲸鱼 大肥鱼」），逐个别名都要认出来
    names = {n for n in re.split(r"[\s,，、|｜;；/／]+", self_name or "") if n}
    names.add("你")
    lines = []
    for speaker, text in history:
        tag = "你" if speaker in names else speaker
        lines.append(f"[{tag}] {text}")
    return "\n".join(lines)


# ---------- 解析 ----------

_JSON_BLOCK = re.compile(r"\{[^{}]*\}", re.S)


def _parse_response(text: str) -> tuple[str, str] | None:
    """从模型的自由文本里抠出 {"action": ..., "reason": ...}。"""
    if not text:
        return None

    candidates: list[str] = []
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text.strip())
    candidates.extend(m.group(0) for m in _JSON_BLOCK.finditer(text))

    for raw in candidates:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        action = str(data.get("action", "")).strip().lower()
        if action not in ("reply", "skip"):
            # 兼容 true/false 或 yes/no
            flag = data.get("reply", data.get("talk_to_me"))
            if isinstance(flag, bool):
                action = "reply" if flag else "skip"
            elif isinstance(flag, str) and flag.lower() in ("true", "false"):
                action = "reply" if flag.lower() == "true" else "skip"
            else:
                continue
        return action, str(data.get("reason", "")).strip()

    # 最后兜底：文本里出现了明确的词
    low = text.lower()
    if '"reply"' in low or "'reply'" in low or "action: reply" in low:
        return "reply", "文本兜底解析"
    if '"skip"' in low or "'skip'" in low or "action: skip" in low:
        return "skip", "文本兜底解析"
    return None
