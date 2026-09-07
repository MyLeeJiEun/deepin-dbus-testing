# deepin-dbus-testing

给 DDE 各仓用的 **DBus 接口自动化测试框架**:用纯 YAML 配置,对**刚构建出的二进制**在**私有总线**上跑接口测试,产出 CI 可消费的报告。目标体验与 `ctest` / `go test` 同级——构建完在仓内敲一条命令即可。

它测的是**接口面**:接口是否还在、签名是否变了、属性能不能读写、错误路径是否符合预期、信号是否按约定发出。不是替代单元测试,而是补上"跨进程契约"这一层。

## 两条硬约束(不可协商)

**① 语言无关。** 框架只经总线与被测进程对话,不链接、不注入、不感知被测语言运行时;交互面只有 `org.freedesktop.DBus.*`(Introspectable / Properties / Peer)与业务接口的标准调用。因此 Go、C++/Qt、以及以 loader 模块形式存在的服务用**同一套**配置描述,各仓无需提供任何语言的胶水代码。

**② 不侵入业务代码。** 框架**不修改**被测项目的任何业务代码,**不调用**被测项目的构建系统(从不执行 `cmake` / `make` / `ninja` / `go build` / `dpkg-buildpackage`),只**消费**已有构建产物的路径。写入范围只有两处:`init` 生成的 `tests/dbus/` 骨架,以及 `--report` 指定的输出目录。仓内可选的唯一一行改动是 `tests/CMakeLists.txt` 里的 `add_test(...)`,位于 test 目录内,且可以不加。

这两条由 CI 的自动检查守着:框架代码里不得出现构建命令调用,写文件操作只允许出现在少数被白名单的模块中。

## 三条命令上手

```bash
cd <repo>

# ① 脚手架:探测服务并生成 tests/dbus/ 骨架 + 首批用例草稿
dbus-testing init --binary build/hans2pinyin
#   或对已在运行的服务反向生成(适合先摸底)
dbus-testing init --from-running org.deepin.dde.Pinyin1

# ② 跑起来(--build-dir 指向构建目录,即 ${BUILD_DIR})
dbus-testing run tests/dbus/ --build-dir build

# ③ 挂进 CI(可选,一行)
#   tests/CMakeLists.txt: add_test(NAME dbus-interface COMMAND dbus-testing run ...)
```

详见 [docs/quickstart.md](docs/quickstart.md)。

## 接入契约

框架对各仓的**全部**要求就是这一个目录,与单元测试同级、随仓版本化:

```
<repo>/tests/dbus/
├── service.yaml     # 必需:服务名、构建产物搜索路径、环境、就绪判定
├── contract.xml     # 必需:接口基线(init/scan 生成 + 人工审)
├── tests.yaml       # 可选:用例;没有它就只跑契约校验
└── test_extra.py    # 可选:有外部副作用的行为用 Python 逃生舱
```

`contract.xml` 用的是**规范化后的标准 D-Bus introspection XML**,不自造格式:这样"仓内源码声明 XML / 基线 / 运行时 introspect"三方同格式直比,`busctl introspect --xml-interface`、`gdbus introspect --xml` 也都读得懂。

## 产出物

`run` / `check` 带 `--report <outdir>` 时产出:

```
outdir/
├── junit.xml           # CI 消费:流水线原生识别,门禁直接挂
├── results.json        # 机器可读全量(每 step 结果与耗时、契约 delta、覆盖矩阵)
├── report.html         # 人看:单文件、内嵌数据、零前端依赖
└── contract-live.xml   # 运行时 introspect 快照(审计 / 存档 diff)
```

退出码固定(CI 依赖):`0` 全部通过,`1` 用例失败或契约漂移,`2` 配置错误,`3` 环境错误,`4` 框架内部错误。错误码与排查手册见 [docs/diagnose.md](docs/diagnose.md)。

## 现阶段能力与限制

**档位只由实测认定,不由源码推断**(反例:静态分析曾判断 Graphic1 依赖 X11,实测在无显示的私有总线上注册成功)。下表只列**已实测**的结论,未列项不作承诺:

| 形态 | 状态 | 实测依据 |
|---|---|---|
| `kind: process` + `mode: isolate`(T1 主线) | **已验证** | Pinyin1(Go)私有总线注册并真实调用 `Query("深度") → ["shendu","shenduo"]`;Graphic1(Go)注册 + 签名断言 + 契约校验通过 |
| `kind: go-loader`(loader 模块形态) | **已验证** | `dde-session-daemon --enable systeminfo -i` 在私有总线上注册 `SystemInfo1`,6 用例全绿(属性面 + 契约),未需 system bus mock |
| `kind: dsm`(DSM 插件) | **已验证** | 经 `hosts/dsm-host` 加载真实 `libplugin-dde-appearance.so`,私有总线注册 `Appearance1`,7 对象 / 3 接口,4 用例全绿;宿主替身兼容"插件自己用 sessionBus 注册"与"由宿主注册"两种写法 |
| 依赖 mock(`needs:`)前置编排 | **已验证** | 缺 `Subscribe` 时精确报出 `org.example.FxDep.Manager.Subscribe` 并给出模板片段;补齐后被测服务正常注册 |
| `mode: attach`(接真实会话,只读兜底) | **已验证** | 真实会话 introspect ApplicationManager1 / SystemInfo1 / Appearance1 三形态成功;AM 的 `check`(运行时 vs 基线)与 `check --static`(源码声明 vs 基线)均跑通,后者不需拉起服务 |
| 动态子对象归并 | **已验证** | AM 的 `/org/desktopspec/ApplicationManager1/*` 在基线中归并为单个 `collapsed` 节点,不会把上百个子对象全 introspect |
| Qt GUI 服务 | **已验证需 `QT_QPA_PLATFORM=offscreen`** | 无显示时报 `could not load the Qt platform plugin "xcb"`;诊断会直接给出这条提示 |
| 私有 system bus(`system-bus: true`) | **未实测** | 机制已实现(注入 `DBUS_SYSTEM_BUS_ADDRESS` + `<allow own="*"/>`),但尚无真实 system bus 服务跑通的证据 |
| `kind: plugin-host`(dde-shell / dde-tray-loader) | **待实测** | 宿主能否定向加载自建 `.so` 尚无结论;无法定向则该形态只支持 `attach` |
| 强依赖服务密闭(`ApplicationManager1` isolate) | **已验证** | 挂上框架自带的 `systemd_dde` mock 模板后,AM 在私有总线上注册成功,6 用例全绿;isolate 与 attach 抓到的接口面逐字节一致。样板见 `examples/application-manager1` |

逐服务档位与证据见 [docs/tiers.md](docs/tiers.md)。

已知限制:

- **有外部副作用的行为不配置化**。写文件、改系统状态、跨服务联动等,走 `test_extra.py` 逃生舱。准确口径是"契约面、属性面、错误面 100% 配置化",不是"90% 测试不写代码"。
- **用例语言故意很小**:六原语 + 五断言,`steps:` 线性组合;不做条件、循环、变量插值(理由见 [docs/primitives.md](docs/primitives.md))。
- `tests.yaml` 里**没有入参类型标注**,byte / uint 等窄类型入参暂时只能走逃生舱(样板 `examples/graphic1/tests.yaml` 里有一条按此跳过的用例)。
- 私有总线**默认不挂标准 servicedir**:密闭测试禁止意外激活真实服务,被测服务的依赖必须显式写进 `needs:`。

## 文档

- **[docs/guide.md](docs/guide.md) —— 完整指南:架构 / 接入 / 使用(读这一篇就够)**
- [docs/quickstart.md](docs/quickstart.md) —— 五分钟接入(C++ 仓 / Go 仓 / ctest 集成 / 产物)
- [docs/authoring.md](docs/authoring.md) —— **配置编写规则**:每个字段填什么、依据是什么、卡住怎么办
- [docs/primitives.md](docs/primitives.md) —— 六原语与五断言完整参考
- [docs/service-yaml.md](docs/service-yaml.md) —— `service.yaml` 全字段与超时/总线粒度约定
- [docs/diagnose.md](docs/diagnose.md) —— 错误码、退出码与排查手册
- [docs/tiers.md](docs/tiers.md) —— DDE 服务分档看板(档位只由实测认定)
- [docs/design.md](docs/design.md) —— 设计方案(背景、选型调研、架构决策与依据)
- [docs/development.md](docs/development.md) —— 开发文档(实现规格、模块 API、阶段验收)
- `examples/pinyin1`(T1)、`examples/graphic1`(T1)、`examples/application-manager1`(T3 + 依赖 mock) —— 可直接抄的样板配置

## 许可

LGPL-3.0-or-later,见 [LICENSE](LICENSE)。
