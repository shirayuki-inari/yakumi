"""登录：单一的工作台密码（用于部署在远程服务器、通过公网访问的场景）。

- 密码来自环境变量 YAKUMI_WEB_PASSWORD（明文，适合容器部署），或配置 [web] password_hash
  （用 yakumi passwd 生成的哈希）。两者都没有时不需要登录。
- 登录状态保存在签名的 Cookie 里，签名密钥保存在 ~/.config/yakumi/web_secret（只有自己可读），重启后仍然有效。
  Cookie 里带有密码的指纹，修改密码后所有已登录的设备都需要重新登录。
- 同一来源连续输错密码会被暂时拒绝。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from werkzeug.security import check_password_hash, generate_password_hash

from ..files import config_dir

PASSWORD_ENV = "YAKUMI_WEB_PASSWORD"
MAX_FAILURES = 5
FAILURE_WINDOW = 600  # 秒


def hash_password(password: str) -> str:
    return generate_password_hash(password)


class Auth:
    def __init__(self, web_cfg: dict[str, Any]):
        env = os.environ.get(PASSWORD_ENV, "")
        stored = str(web_cfg.get("password_hash") or "")
        self._check: Callable[[str], bool] | None
        if env:
            self._check = lambda p: hmac.compare_digest(p.encode(), env.encode())
            material = "env:" + env
        elif stored:
            self._check = lambda p: _safe_check(stored, p)
            material = "hash:" + stored
        else:
            self._check = None
            material = ""
        self.source = "环境变量 " + PASSWORD_ENV if env else ("配置文件" if stored else "")
        self.secret = load_secret()
        # 指纹随密码变化，写进登录 Cookie；修改密码后旧的登录状态失效
        self.fingerprint = hmac.new(self.secret, material.encode(), hashlib.sha256).hexdigest()[:32]
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._check is not None

    def verify(self, password: str) -> bool:
        return bool(self._check and password and self._check(password))

    def wait_seconds(self, client: str) -> int:
        """这个来源还需要等多久才能再试（秒），0 表示可以尝试。"""
        now = time.time()
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW]
            self._failures[client] = recent
            if len(recent) < MAX_FAILURES:
                return 0
            return int(FAILURE_WINDOW - (now - recent[0])) + 1

    def record_failure(self, client: str) -> None:
        with self._lock:
            self._failures.setdefault(client, []).append(time.time())
            if len(self._failures) > 10000:  # 防止被大量来源撑大
                self._failures.clear()

    def reset(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


def _safe_check(stored: str, password: str) -> bool:
    try:
        return check_password_hash(stored, password)
    except (ValueError, TypeError):
        return False


def secret_path() -> Path:
    return config_dir() / "web_secret"


def load_secret() -> bytes:
    """读取（第一次时生成）签名密钥。"""
    path = secret_path()
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return data


__all__ = ["Auth", "PASSWORD_ENV", "hash_password", "load_secret"]
