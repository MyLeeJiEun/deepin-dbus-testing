# service.yaml 字段参考

配置 schema 带 `apiVersion`,框架升级不会让已接入仓被动改配置。当前唯一有效版本:`v1`。
**未知字段一律报错**(拼写错误必须早失败),不静默忽略。

## 变量展开

| 变量 | 含义 | 备注 |
|---|---|---|
| `${BUILD_DIR}` | `--build-dir` 传入的构建目录 | **可选**:未传时,含它的候选被标记"已跳过",不会让整份配置失败;但若 `binary.search` 全部依赖它则报 `E_CONFIG_INVALID` |
| `${REPO_ROOT}` | 向上找到的 git 仓库根 | 找不到 `.git` 时退化为 `tests/dbus` 的父目录 |
| `${TESTS_DIR}` | `service.yaml` 所在目录 | |
| `${ENV:NAME}` | 环境变量 | 未设置时报错(显式引用就必须存在) |

## 完整字段表

```yaml
apiVersion: v1                 # 必需
services:                      # 必需:本进程注册的**全部**服务名(单进程多名字的情形)
  - org.deepin.dde.Pinyin1
process: hans2pinyin           # 可选:显示名,默认取 services[0]
mode: isolate                  # isolate(默认,私有总线密闭) | attach(接真实会话,只读)
kind: process                  # process(默认) | go-loader | dsm | plugin-host
tier: T1                       # 可选:档位标注,只用于报告环境指纹

binary:
  search:                      # 按序查找,第一个"存在且可执行"者胜 —— 构建产物优先
    - ${BUILD_DIR}/hans2pinyin
    - ${BUILD_DIR}/bin/hans2pinyin
    - /usr/lib/deepin-api/hans2pinyin     # 仅兜底,可省
args: ["--foo"]                # 可选:追加给被测二进制的参数

module: systeminfo             # kind: go-loader 必需 —— 要启用的 loader 模块名
plugin: ${BUILD_DIR}/lib/libplugin-xxx.so   # kind: dsm / plugin-host 必需
host-binary: /usr/bin/dde-shell             # kind: plugin-host 可选:显式指定宿主

sandbox:
  home: tmp                    # tmp(默认,隔离 HOME/XDG_*) | inherit
  env:                         # 注入被测进程的环境变量
    QT_QPA_PLATFORM: offscreen # Qt GUI 服务在无显示环境必需
    DSG_APP_ID: org.deepin.dde.xxx

needs:                         # 依赖 mock;**强制在拉起被测进程之前启动**
  - mock: systemd              # python-dbusmock 内置模板名
  - mock: fx_dep               # 或 dbus_testing/mocktemplates/ 下的模板文件名
  - mock: org.example.Foo      # 也可写服务名(会做 . → _ 映射去找模板)
    bus: system                # session(默认) | system
    params: {}                 # 传给模板 load() 的参数
system-bus: false              # true 时额外起一条私有 system bus 并注入地址

ready:
  name-owner:                  # 缺省等于 services;**全部**就绪才算 READY
    - org.deepin.dde.Pinyin1
  timeout: 10s                 # 默认 10s

auth:                          # polkit 是方法级门禁,不是服务级
  polkit:
    - interface: org.deepin.dde.LocaleHelper1
      methods: [SetLocale, GenerateLocale]

allow:                         # attach 模式安全默认:只读
  methods: []                  # 空 = 仅允许 Introspect / Properties.Get / check-contract
                               # 需要调用方法时在这里显式列出(支持 fnmatch)

ignore:                        # 契约比对的忽略项
  paths: ["/org/deepin/dde/Xxx1/session/*"]   # 动态子对象:归并为一个 collapsed 节点
  interfaces: ["org.freedesktop.DBus.*"]       # 默认值就是这条
  methods: ["org.deepin.dde.Xxx1.Deprecated"]

src-xml-globs:                 # check --static 用的源码声明 XML 搜索模式
  - api/dbus/*.xml             # 默认三条:api/dbus/*.xml、xml/*.xml、dbus/*.xml
  - xml/*.xml

isolation: per-run             # per-run(默认,一条总线跑完全部用例) | per-case
restart: never                 # never(默认) | per-case
teardown: kill                 # kill(默认,SIGKILL) | term(SIGTERM)
```

## 总线粒度与超时约定

| 项 | 默认 | 说明 |
|---|---|---|
| 总线粒度 | `isolation: per-run` | 一条私有总线跑完整套用例,快;有状态污染风险的服务改 `per-case` |
| 服务重启 | `restart: never` | 与总线粒度独立配置 |
| 就绪超时 | `ready.timeout: 10s` | 超时会附上"总线上实际已注册的名字"与服务输出尾部 |
| 调用超时 | 5s | 可在用例里按步骤覆盖,见 [primitives.md](primitives.md) |
| 信号等待 | 5s | 同上 |
| 重试 | 不重试 | 只有用例显式写 `flaky: N` 才重试,且报告里会标出 |
| 统一放大 | `--timeout-scale F` | 慢机器/CI 用,不必改配置 |

## 四种 kind 的实测状态

见 [tiers.md](tiers.md) 与 README 的能力表。简述:

- `process` —— 直接 exec 二进制。已验证(Pinyin1 / Graphic1)。
- `go-loader` —— `<daemon> --enable <module> -i`。已验证(dde-session-daemon + systeminfo)。
- `dsm` —— 经可选组件 `dbus-testing-dsm-host` 用 `dlopen` + `DSMRegister` 加载插件。
  已验证(dde-appearance 插件)。**未安装该组件时,请把该服务改为 `mode: attach`**。
- `plugin-host` —— 宿主 + `-p <so>`。**参数尚未实测**,目前建议先用 `mode: attach`。
