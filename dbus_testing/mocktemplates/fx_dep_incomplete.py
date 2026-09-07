"""自测用:故意**不保真**的依赖 mock(缺 Subscribe)。

用于验证 E_MOCK_UNFAITHFUL 诊断能精确报出缺失方法名。
"""

BUS_NAME = "org.example.FxDep"
MAIN_OBJ = "/org/example/FxDep"
MAIN_IFACE = "org.example.FxDep.Manager"
SYSTEM_BUS = False


def load(mock, parameters):  # noqa: ANN001, ANN201 - dbusmock 模板签名
    # 故意只提供一个无关方法,不实现被测服务真正会调的 Subscribe
    mock.AddMethods(MAIN_IFACE, [("Unrelated", "", "", "")])
