"""自测用:fx_needdep 依赖的服务(**完整**实现,含 Subscribe)。

用于验证 needs: 前置编排成功后被测服务能正常注册。
"""

BUS_NAME = "org.example.FxDep"
MAIN_OBJ = "/org/example/FxDep"
MAIN_IFACE = "org.example.FxDep.Manager"
SYSTEM_BUS = False


def load(mock, parameters):  # noqa: ANN001, ANN201 - dbusmock 模板签名
    mock.AddMethods(
        MAIN_IFACE,
        [
            ("Subscribe", "", "", ""),
            ("Unsubscribe", "", "", ""),
        ],
    )
