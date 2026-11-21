"""测试与调试用：不调用任何服务，按格式返回带语言标记的“译文”。"""

from __future__ import annotations

import re

from .base import Translator


class FakeTranslator(Translator):
    def __init__(self, target: str = "zh", **kwargs):
        super().__init__(**kwargs)
        self.target = target

    def complete(self, system: str, user: str) -> str:
        lines = re.findall(r"^(\d+)\|(.*)$", user.split("请翻译：", 1)[-1], re.M)
        # 整句翻译时句内各段用 ¦ 隔开，逐段加标记，段数保持不变
        return "\n".join(f"{n}|" + "¦".join(f"[{self.target}] {part}" for part in text.split("¦"))
                         for n, text in lines)
