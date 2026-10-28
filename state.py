"""窗口状态机。

一个群（umo）一个窗口，内存里存，不落盘。
窗口只有一个来源：她开口说话（@回复 / 关键词唤醒 / AstrBot 主动回复都算）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Window:
    opened_at: float
    expires_at: float
    source: str
    refresh_count: int = 0
    last_msg_count: int = 0


class WindowManager:
    """记录每个会话的唤醒窗口。"""

    def __init__(self) -> None:
        self._windows: dict[str, Window] = {}

    # ---------- 查询 ----------

    def is_active(self, umo: str, now: float | None = None) -> bool:
        win = self._windows.get(umo)
        if win is None:
            return False
        if (now or time.time()) >= win.expires_at:
            self._windows.pop(umo, None)
            return False
        return True

    def remaining(self, umo: str, now: float | None = None) -> int:
        """剩余秒数，没窗口返回 0。"""
        if not self.is_active(umo, now):
            return 0
        return max(0, int(self._windows[umo].expires_at - (now or time.time())))

    def get(self, umo: str) -> Window | None:
        return self._windows.get(umo) if self.is_active(umo) else None

    # ---------- 变更 ----------

    def touch(self, umo: str, seconds: int, source: str = "bot_spoke") -> bool:
        """她开口了：已有窗口就刷新，没有就开一个。

        seconds <= 0 表示关闭窗口功能，直接返回 False 并清掉已有窗口。
        """
        now = time.time()
        if seconds <= 0:
            self._windows.pop(umo, None)
            return False

        win = self._windows.get(umo)
        if win is not None and now < win.expires_at:
            win.expires_at = now + seconds
            win.refresh_count += 1
            win.source = source
        else:
            self._windows[umo] = Window(
                opened_at=now,
                expires_at=now + seconds,
                source=source,
            )
        return True

    def close(self, umo: str) -> None:
        self._windows.pop(umo, None)

    def clear(self) -> None:
        self._windows.clear()

    def snapshot(self) -> dict[str, dict]:
        """给 /wake status 用的只读快照。"""
        now = time.time()
        out: dict[str, dict] = {}
        for umo, win in list(self._windows.items()):
            left = int(win.expires_at - now)
            if left <= 0:
                self._windows.pop(umo, None)
                continue
            out[umo] = {
                "remaining": left,
                "source": win.source,
                "refresh_count": win.refresh_count,
            }
        return out
