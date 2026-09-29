---
name: dbus-testing-init
description: 在代码仓库下使用 dbus-testing 工具为 DBus 服务建立接口测试：定位构建产物、生成脚手架（service.yaml/contract.xml/tests.yaml）、补全用例并验证通过后提交。
user-invocable: true
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
---

# DBus 接口测试接入 Skill（dbus-testing）

你的任务是在一个代码仓库里，对该仓编译出的 DBus 服务二进制建立接口测试。整个流程是：
**定位构建产物 → init 生成脚手架 → 审阅契约基线 → 补全用例 → run 验证 → 提交 `tests/dbus/`**。

## 0. 前置检查

依次确认，缺什么先补什么：

```bash
which dbus-testing || pip install -e <dbus-testing 仓库路径>
# 注意:只能用 console script `dbus-testing`;python3 -m dbus_testing 不可用(包无 __main__)
which dbus-daemon || echo "缺 dbus 包,isolate 模式不可用"
python3 -c "import dbusmock" || echo "缺 python-dbusmock(>=0.30),needs mock 不可用"
```

向用户确认**构建产物目录**（CMake 的 `build/`、Go 的输出路径等）。找不到就先构建，或让用户指路。

## 1. 选择 init 模式

**默认走 `--binary`；只有程序确定无法在测试环境拉起（要 root/硬件/强依赖真实系统）才走 `--from-running`。**

```bash
# 程序能拉起(绝大多数情况):
dbus-testing init --binary <构建产物路径> --build-dir <构建目录> --out tests/dbus
# 服务需要额外参数(如 --enable 模块):放在 -- 之后
# 注意:--out 等框架参数必须写在 -- 之前;-- 之后的全部进被测服务参数
dbus-testing init --binary <产物> --build-dir build --out tests/dbus -- --enable timedate

# 程序起不来(要 root/硬件):对正在运行的服务反向生成(只读契约校验)
dbus-testing init --from-running <服务名> --out tests/dbus
```

init 会在私有总线上真实拉起二进制，探测注册的服务名与接口面，生成：

```
tests/dbus/
├── service.yaml    # 怎么拉起服务
├── contract.xml    # 接口契约基线(API review 载体,必须人工审阅)
└── tests.yaml      # 用例草稿
```

### init 失败时的判型

| 现象 | 原因 | 处理 |
|---|---|---|
| init 没传 `--build-dir`(**不报错,exit 0**) | `${BUILD_DIR}` 候选被标记跳过,落到 `--binary` 给的绝对路径兜底 → 若那是系统安装路径,**测的是系统版而非刚编的** | init 时必须传 `--build-dir`,并在 §5 核对 binary 字段 |
| `E_PROC_DIED`,输出含 `could not load the Qt platform plugin` | Qt 服务缺显示环境 | 框架已自动补 `QT_QPA_PLATFORM=offscreen` 重试;仍失败读输出尾部找真实原因 |
| `E_PROC_DIED`,输出含依赖报错(如 systemd1/dconfig) | 强依赖缺失 | 先在 `service.yaml` 补 `needs:` mock(见 §3),或改 `--from-running` |
| `E_READY_TIMEOUT` | 服务起了但没在 session 总线注册名字 | 若它注册在 system 总线,init 会自动探测并生成 `system-bus: true`;仍失败用 `--keep-bus` + `busctl` 排查,或改 `--from-running` |
| 服务要 root/硬件,无法密闭 | — | 改 `--from-running` |
| `E_CALL_TIMEOUT`(**run 阶段**,易误判为用例错) | 调用 5s 无回复:服务死锁/卡在等一个没 mock 的依赖 | `--keep-bus` 手工 `busctl` 复现;手工也卡 = 抓到真 bug,别调大超时糊过去 |

## 2. 审阅 contract.xml（不要跳过）

- 它是接口 API review 的载体：确认接口/方法/属性/信号与源码声明一致
- 有源码声明 XML（`api/dbus/*.xml` 等）的仓，可跑 `dbus-testing check tests/dbus --static` 交叉验证
- 确认后随代码提交

## 3. 审阅并补全 service.yaml

按服务形态增补：

```yaml
# Go loader 模块(dde-session-daemon 系):init 已探测,确认 module 正确
kind: go-loader
module: timedate

# 依赖 mock:服务启动时会调用的外部服务必须前置,否则直接 abort
needs:
  - mock: systemd_dde        # 框架自带模板见 dbus_testing/mocktemplates/
  - mock: polkitd
    bus: system              # polkitd/dconfig 等系统依赖挂 system 总线
system-bus: true             # 服务本身注册在 system 总线时必须加(init 探测到会自动生成)

# Qt GUI 服务无显示环境时:
sandbox:
  env: { QT_QPA_PLATFORM: offscreen }

# 跑不起来的服务退路:
mode: attach                 # 只读契约校验(此时 allow.methods 控制白名单)
```

判断服务依赖哪些 mock：读服务源码里的 DBus 调用（Go 看 `go-dbus-factory`/`dbusutil`，C++ 看 `QDBusInterface`/`QDBusConnection`），对每个**启动路径上**就会调用的外部服务，找 `dbus_testing/mocktemplates/` 里的同名模板挂进 `needs:`。

## 4. 补全 tests.yaml 用例

草稿规则：无参方法、只读属性已可直接运行；需要真实参数的以注释态列出（含入参签名）。逐个补全：

```yaml
apiVersion: v1
cases:
  # 1) 核心方法:从源码读出真实参数与预期返回值,写值断言(不只是签名)
  - name: query-pinyin
    call: {method: Query, args: ["深度"]}
    expect: {signature: as, value: ["shendu", "shenduo"]}

  # 2) 属性:可写属性做 roundtrip,只读属性断言类型/拒绝写
  - name: counter-roundtrip
    steps:
      - set-prop: {property: Counter, value: 7}
      - get-prop: {property: Counter}
        expect: {value: 7}
  - name: name-readonly
    set-prop: {property: Name, value: "x"}
    expect: {error: "*"}          # 确认实际错误名后收紧

  # 3) 信号:触发动作写在同一条目里(布扣自动先于触发)
  - name: ping-emits
    call: {method: Ping}
    wait-signal: {name: Pinged, timeout: 5s}
    # 序列断言:多次触发后 index: 1 取第 2 次载荷

  # 4) 错误路径:非法参数/未授权,断言具体错误名
  - name: query-empty-rejected
    call: {method: Query, args: [""]}
    expect: {error: org.example.X.Error.Empty}

  # 5) 惯例放最后:运行时接口必须等于基线
  - name: contract
    check-contract: true
```

要求：
- 断言从强到弱尽量用 `value`，其次 `signature`，`nonempty` 兜底，`error: "*"` 是占位、确认后收紧
- 每条用例一个明确的验证意图，命名 kebab-case 表意
- 预期错误的场景必须覆盖（错误路径也是接口契约）
- 不确定真实参数/返回值时**读源码**，不要编造

## 5. 验证

```bash
dbus-testing run tests/dbus --build-dir <构建目录> --report out/
```

**构建目录纪律(R4 系列实测)**:init 和 run 都必须传 `--build-dir`,且两者指向同一目录。
run 缺 `--build-dir` 时不报错——`${BUILD_DIR}` 候选被跳过、走 init 记下的绝对路径兜底,
产物被挪走或重建到别处才会炸。跑完核对报告 `env.binary` 字段:必须指向构建目录,
出现 `/usr/bin/...` 说明测的是系统安装版。

- 全绿 → 把 `contract.xml` 与本轮 diff 一起提交
- 有失败 → 先读诊断（`summary/detail/hint` 三段），常见判型：
  - `E_ASSERT 返回值不符` → 多半是用例参数/预期错了，改用例；确认服务行为本身有错则是抓到真 bug
  - `E_CONTRACT_DRIFT` → 接口变了：有意变更走 `scan --emit` 更新基线并 review；无意变更就是回归
  - `E_MOCK_UNFAITHFUL: 缺少 xxx 方法` → 给 mock 模板补方法
  - 反复失败排查不动 → `--keep-bus` 保留现场手工 `busctl` 排查；跑完 `dbus-testing reap` 收尾
- `check --static`（秒级，不起服务）适合挂 CI 门禁：源码声明 vs 基线
- **确认 `src-xml-globs` 命中你的源码声明 XML 路径**（默认 `api/dbus/*.xml`、`xml/*.xml`、`dbus/*.xml`）：未命中时 check 输出 `SKIP 静态契约校验` 且**退出码 0**——门禁形同虚设。路径不匹配就在 service.yaml 里补 `src-xml-globs`，并给 CI 命令加 `--strict` 让这种情况直接失败，而不是靠人记得配

## 6. 提交

`tests/dbus/` 三个文件 + 本轮用例改动一起提交。CI 接入：

```yaml
script:
  - dbus-testing check tests/dbus --static --strict   # 源码声明 XML 未命中时门禁直接失败
  - dbus-testing run tests/dbus --build-dir build --strict
```

## 红线

- **不要**静默放宽断言让用例变绿（`error: "*"` 占位必须收紧后才算完成）
- **不要**手工编辑 contract.xml 的接口内容——它只能来自 `scan --emit`，人工只做审阅
- **不要**把真实系统总线卷进测试：isolate 是默认，`system-bus: true` 连的也是私有总线；`attach` 只做只读校验
- 跑完排障后记得 `dbus-testing reap` 或确认 `--keep-bus` 的现场已处理
