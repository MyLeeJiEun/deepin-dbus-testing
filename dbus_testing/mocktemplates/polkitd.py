"""polkitd(org.freedesktop.PolicyKit1)替身。

基于 dbusmock 官方 polkitd 模板思路重写,补齐 polkitqt1 绑定实际会调用的
接口面:官方模板缺 EnumerateActions,PolkitQt1::Authority 单例构造时会
同步调用它,失败则 hasError,之后所有 checkAuthorizationSync 都报
"Authentication failed"(dde-services power system 插件实测)。

- EnumerateActions 返回空动作列表(测试只关心授权判定,不需要动作元数据)
- 默认放行一切 CheckAuthorization(测试常需调用受门禁方法;要测拒绝
  路径可经 mock-state 调 AllowUnknown(false) / SetAllowed([...]))
"""
import dbus

BUS_NAME = "org.freedesktop.PolicyKit1"
MAIN_OBJ = "/org/freedesktop/PolicyKit1/Authority"
MAIN_IFACE = "org.freedesktop.PolicyKit1.Authority"
SYSTEM_BUS = True


def load(mock, parameters):  # noqa: ANN001, ANN201
    mock.AddProperties(
        MAIN_IFACE,
        dbus.Dictionary(
            {
                "BackendName": "local",
                "BackendVersion": "0.8.15",
                "AuthorityFeatures": dbus.UInt32(0),
            },
            signature="sv",
        ),
    )
    # 参数化:allow-unknown 缺省 true(受门禁方法默认可调用)
    allow_unknown = bool((parameters or {}).get("allow-unknown", True))
    mock.AddMethods(
        MAIN_IFACE,
        [
            # (b, b, a{ss}):authorized, is_challenge, details
            (
                "CheckAuthorization",
                "(sa{sv})sa{ss}us",
                "(bba{ss})",
                f"ret = ({allow_unknown!r}, False, {{'test': 'test'}})",
            ),
            # polkitqt1 Authority 构造时同步调用;缺它 hasError
            ("EnumerateActions", "s", "a(us)", "ret = []"),
            ("EnumerateActionsForSubject", "(sa{sv})", "a(us)", "ret = []"),
            ("RegisterAuthenticationAgent", "(sa{sv})ss", "", ""),
            ("RegisterAuthenticationAgentWithOptions", "(sa{sv})ssa{sv}", "", ""),
            ("UnregisterAuthenticationAgent", "(sa{sv})s", "", ""),
            ("AuthenticationAgentResponse2", "us(sa{sv})", "", ""),
        ],
    )
