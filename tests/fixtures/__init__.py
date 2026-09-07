"""fixture 服务集合:框架自测用的最小 DBus 服务(§3.5),均可直接 exec。

每个脚本从 DBUS_SESSION_BUS_ADDRESS 连私有会话总线,未设置就退出,不会污染桌面总线。
"""

from __future__ import annotations

from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent


def fixture_path(name: str) -> Path:
    """返回 fixture 脚本的绝对路径;name 可带或不带 .py 后缀。"""
    path = FIXTURE_DIR / (name if name.endswith(".py") else f"{name}.py")
    if not path.is_file():
        raise FileNotFoundError(f"fixture 不存在: {path}")
    return path
