#!/usr/bin/env python3
"""fx_crash —— 模拟无显示环境下 Qt 服务秒退,验证 E_PROC_DIED 与 offscreen 提示。

stderr 文本照抄 dde-application-manager 在无 X 显示时的实测输出(附录 A.1),
诊断规则靠 `platform plugin` 关键字命中。不连总线,直接以 1 退出。
"""

from __future__ import annotations

import sys

MESSAGE = (
    'qt.qpa.plugin: could not load the Qt platform plugin "xcb" in "" even though it was found.\n'
    "This application failed to start because no Qt platform plugin could be initialized.\n"
)


def main() -> int:
    sys.stderr.write(MESSAGE)
    sys.stderr.flush()
    return 1


if __name__ == "__main__":
    sys.exit(main())
