"""异常层次与错误码.

CLI 退出码映射(CI 依赖,不可随意变更):
  0 全部通过
  1 用例失败或契约漂移
  2 配置错误       E_CONFIG_INVALID / E_GUARD_DENIED
  3 环境错误       E_BINARY_NOT_FOUND / E_PROC_DIED / E_READY_TIMEOUT / E_MOCK_UNFAITHFUL
  4 框架内部错误
"""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_ENV = 3
EXIT_INTERNAL = 4


class DbusTestingError(Exception):
    """框架异常基类。所有面向用户的失败都必须是它的子类,以便经 diagnose 出口。"""

    code: str = "E_UNKNOWN"
    exit_code: int = EXIT_INTERNAL

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context


class ConfigError(DbusTestingError):
    code = "E_CONFIG_INVALID"
    exit_code = EXIT_CONFIG


class GuardDenied(DbusTestingError):
    code = "E_GUARD_DENIED"
    exit_code = EXIT_CONFIG


class BinaryNotFound(DbusTestingError):
    code = "E_BINARY_NOT_FOUND"
    exit_code = EXIT_ENV


class ProcessDied(DbusTestingError):
    code = "E_PROC_DIED"
    exit_code = EXIT_ENV


class ReadyTimeout(DbusTestingError):
    code = "E_READY_TIMEOUT"
    exit_code = EXIT_ENV


class MockUnfaithful(DbusTestingError):
    code = "E_MOCK_UNFAITHFUL"
    exit_code = EXIT_ENV


class BusError(DbusTestingError):
    code = "E_BUS"
    exit_code = EXIT_ENV


class ContractDrift(DbusTestingError):
    code = "E_CONTRACT_DRIFT"
    exit_code = EXIT_FAILED


class SignalTimeout(DbusTestingError):
    code = "E_SIGNAL_TIMEOUT"
    exit_code = EXIT_FAILED


class CallTimeout(DbusTestingError):
    code = "E_CALL_TIMEOUT"
    exit_code = EXIT_FAILED


class AssertionFailed(DbusTestingError):
    """声明式断言不成立(用例失败,不是框架错误)。"""

    code = "E_ASSERT"
    exit_code = EXIT_FAILED
