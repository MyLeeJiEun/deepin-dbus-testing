"""dde-daemon inputdevices 模块的存在性替身。

用途:tray-loader 的 keyboard-layout 插件在 init() 里检查
`org.deepin.dde.InputDevices1` 是否已注册,存在才创建导出方法 adaptor
(keyboardplugin.cpp:33-49)。该服务由 dde-daemon 的 inputdevices 模块提供,
密闭测试时需要这个只注册名字的替身。

它只做存在性检查,不调用任何方法,因此替身无需实现任何成员。
"""

BUS_NAME = "org.deepin.dde.InputDevices1"
MAIN_OBJ = "/org/deepin/dde/InputDevices1"
MAIN_IFACE = "org.deepin.dde.InputDevices1"
SYSTEM_BUS = False


def load(mock, _parameters):  # noqa: ANN001, ANN201 - dbusmock 模板签名
    pass
