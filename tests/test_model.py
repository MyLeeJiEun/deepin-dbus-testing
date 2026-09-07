"""配置加载与校验(P0 验收)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbus_testing.errors import ConfigError
from dbus_testing.model import (
    UNSET_MARKER,
    load_cases,
    load_service,
    parse_duration,
    service_name_to_path,
)

BASE = """apiVersion: v1
services:
  - org.example.Demo
binary:
  search:
    - /bin/true
"""


def _write(tmp_path: Path, text: str, name: str = "service.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_config_loads(tmp_path: Path) -> None:
    spec = load_service(_write(tmp_path, BASE))
    assert spec.services == ("org.example.Demo",)
    assert spec.mode == "isolate"
    assert spec.kind == "process"
    # ready.name-owner 缺省等于 services
    assert spec.ready.name_owner == ("org.example.Demo",)
    assert spec.default_path == "/org/example/Demo"


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="未知字段"):
        load_service(_write(tmp_path, BASE + "typoField: 1\n"))


def test_unknown_api_version_is_rejected(tmp_path: Path) -> None:
    bad = BASE.replace("apiVersion: v1", "apiVersion: v99")
    with pytest.raises(ConfigError, match="apiVersion"):
        load_service(_write(tmp_path, bad))


def test_missing_required_field(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="services"):
        load_service(_write(tmp_path, "apiVersion: v1\n"))


def test_build_dir_variable_expands(tmp_path: Path) -> None:
    text = (
        "apiVersion: v1\nservices: [org.example.Demo]\n"
        "binary:\n  search:\n    - ${BUILD_DIR}/x\n"
    )
    spec = load_service(_write(tmp_path, text), build_dir=tmp_path)
    assert spec.binary_search == (f"{tmp_path}/x",)


def test_build_dir_unset_is_marked_not_fatal(tmp_path: Path) -> None:
    """未传 --build-dir 时,${BUILD_DIR} 候选被标记跳过,而不是整份配置失败。"""
    text = (
        "apiVersion: v1\nservices: [org.example.Demo]\n"
        "binary:\n  search:\n    - ${BUILD_DIR}/x\n    - /bin/true\n"
    )
    spec = load_service(_write(tmp_path, text))
    assert UNSET_MARKER in spec.binary_search[0]
    assert spec.binary_search[1] == "/bin/true"


def test_all_candidates_need_build_dir_is_error(tmp_path: Path) -> None:
    text = (
        "apiVersion: v1\nservices: [org.example.Demo]\n"
        "binary:\n  search:\n    - ${BUILD_DIR}/x\n"
    )
    with pytest.raises(ConfigError, match="--build-dir"):
        load_service(_write(tmp_path, text))


def test_go_loader_requires_module(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="module"):
        load_service(_write(tmp_path, BASE + "kind: go-loader\n"))


def test_dsm_requires_plugin(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="plugin"):
        load_service(_write(tmp_path, BASE + "kind: dsm\n"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(5, 5.0), (2.5, 2.5), ("5s", 5.0), ("500ms", 0.5), ("2m", 120.0)],
)
def test_parse_duration(raw: object, expected: float) -> None:
    assert parse_duration(raw, "t") == expected


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ConfigError):
        parse_duration("soon", "t")


def test_service_name_to_path() -> None:
    assert service_name_to_path("org.deepin.dde.Pinyin1") == "/org/deepin/dde/Pinyin1"


# --------------------------------------------------------------------------- tests.yaml


def test_case_target_defaults(tmp_path: Path) -> None:
    spec = load_service(_write(tmp_path, BASE))
    _write(
        tmp_path,
        "apiVersion: v1\ncases:\n  - name: c\n    call: {method: Echo}\n",
        "tests.yaml",
    )
    cases = load_cases(tmp_path / "tests.yaml", spec)
    target = cases[0].steps[0].target
    assert target is not None
    assert (target.service, target.path, target.interface, target.member) == (
        "org.example.Demo",
        "/org/example/Demo",
        "org.example.Demo",
        "Echo",
    )


def test_case_line_number_is_recorded(tmp_path: Path) -> None:
    spec = load_service(_write(tmp_path, BASE))
    _write(
        tmp_path,
        "apiVersion: v1\ncases:\n  - name: first\n    call: {method: A}\n"
        "  - name: second\n    call: {method: B}\n",
        "tests.yaml",
    )
    cases = load_cases(tmp_path / "tests.yaml", spec)
    assert cases[0].line == 3
    assert cases[1].line == 5


def test_error_semantics_parsing(tmp_path: Path) -> None:
    spec = load_service(_write(tmp_path, BASE))
    _write(
        tmp_path,
        "apiVersion: v1\ncases:\n"
        "  - name: implicit\n    call: {method: A}\n"
        "  - name: explicit-null\n    call: {method: B}\n    expect: {error: null}\n"
        "  - name: any\n    call: {method: C}\n    expect: {error: \"*\"}\n"
        "  - name: named\n    call: {method: D}\n"
        "    expect: {error: org.freedesktop.DBus.Error.Failed}\n",
        "tests.yaml",
    )
    cases = load_cases(tmp_path / "tests.yaml", spec)
    assert cases[0].steps[0].expect.expects_error is False
    assert cases[1].steps[0].expect.error is None
    assert cases[1].steps[0].expect.expects_error is False
    assert cases[2].steps[0].expect.error == "*"
    assert cases[3].steps[0].expect.error == "org.freedesktop.DBus.Error.Failed"


def test_two_primitives_in_one_entry_only_for_wait_signal(tmp_path: Path) -> None:
    spec = load_service(_write(tmp_path, BASE))
    _write(
        tmp_path,
        "apiVersion: v1\ncases:\n  - name: combo\n    call: {method: Ping}\n"
        "    wait-signal: {name: Pinged}\n",
        "tests.yaml",
    )
    cases = load_cases(tmp_path / "tests.yaml", spec)
    assert [s.op for s in cases[0].steps] == ["call", "wait-signal"]


def test_duplicate_case_names_rejected(tmp_path: Path) -> None:
    spec = load_service(_write(tmp_path, BASE))
    _write(
        tmp_path,
        "apiVersion: v1\ncases:\n  - name: dup\n    call: {method: A}\n"
        "  - name: dup\n    call: {method: B}\n",
        "tests.yaml",
    )
    with pytest.raises(ConfigError, match="重复"):
        load_cases(tmp_path / "tests.yaml", spec)


def test_missing_tests_yaml_is_empty(tmp_path: Path) -> None:
    spec = load_service(_write(tmp_path, BASE))
    assert load_cases(tmp_path / "nope.yaml", spec) == []
