# 诊断手册

框架好不好用,取决于**失败时给什么**。所有面向用户的失败都必须经 `diagnose` 出口,给出**错误码 + 现场 + 可操作提示**;裸 traceback 是缺陷,遇到请报 bug。

用法很简单:看终端输出第一行的错误码 → 在本文档搜这个码 → 按"排查步骤"走。

## 错误码与退出码

下表与 `dbus_testing/errors.py` 逐项一致(该文件是唯一真值,CI 依赖这些退出码,不可随意变更):

| 错误码 | 异常类 | 退出码 | 归类 |
|---|---|---|---|
| `E_CONFIG_INVALID` | `ConfigError` | **2** | 配置错误 |
| `E_GUARD_DENIED` | `GuardDenied` | **2** | 配置错误 |
| `E_BINARY_NOT_FOUND` | `BinaryNotFound` | **3** | 环境错误 |
| `E_PROC_DIED` | `ProcessDied` | **3** | 环境错误 |
| `E_READY_TIMEOUT` | `ReadyTimeout` | **3** | 环境错误 |
| `E_MOCK_UNFAITHFUL` | `MockUnfaithful` | **3** | 环境错误 |
| `E_BUS` | `BusError` | **3** | 环境错误 |
| `E_CONTRACT_DRIFT` | `ContractDrift` | **1** | 用例失败 / 契约漂移 |
| `E_SIGNAL_TIMEOUT` | `SignalTimeout` | **1** | 用例失败 |
| `E_CALL_TIMEOUT` | `CallTimeout` | **1** | 用例失败 |
| `E_ASSERT` | `AssertionFailed` | **1** | 用例失败(断言不成立,不是框架错误) |
| `E_UNKNOWN` | `DbusTestingError`(基类兜底) | **4** | 框架内部错误 |

退出码语义:

| 退出码 | 含义 | 你该做什么 |
|---|---|---|
| `0` | 全部通过 | —— |
| `1` | 用例失败或契约漂移 | 看报告:是被测代码回归,还是期望/基线过期 |
| `2` | 配置错误 | 改 `service.yaml` / `tests.yaml`;这一类**不算**被测代码的问题 |
| `3` | 环境错误 | 构建产物、依赖 mock、总线环境的问题 |
| `4` | 框架内部错误 | 报框架 bug,附 `--verbose` 全量输出 |

> `E_ASSERT`(五断言不成立)与 `E_BUS`(总线连不上/异常)不在下面的规则表里:前者是用例的正常失败路径,报告里直接给"期望 vs 实际";后者走通用出口,先按 [`E_BUS`](#e_bus) 一节自查。

## 规则表逐条展开

### `E_BINARY_NOT_FOUND` —— 构建产物找不到

**触发**:`binary.search` 的所有候选路径都不满足"存在且是文件"。

**典型输出**(示意,实际会列出每条候选与判定结果):

```
E_BINARY_NOT_FOUND: 未找到被测二进制(binary.search 的 3 个候选全部未命中)
  1. /home/u/repo/out/hans2pinyin        不存在
  2. /home/u/repo/out/bin/hans2pinyin    不存在
  3. /usr/lib/deepin-api/hans2pinyin     不存在
  提示: 检查 --build-dir 是否指向真实构建目录,或者先构建;候选来自 service.yaml 的 binary.search
```

**排查步骤**

1. `--build-dir` 传对了吗?它就是配置里 `${BUILD_DIR}` 的值。`ls <build-dir>` 确认里面真有东西。
2. Go 仓最常见的原因:用了 `go build ./...`,它**不产出二进制**。必须 `go build -o out/ ./...` 再 `--build-dir out`。
3. C++ 仓看产物实际落在哪层:`build/`、`build/bin/`、`build/src/` 都常见,`find <build-dir> -maxdepth 3 -type f -perm -u+x -name '<name>*'` 一眼看清。
4. 装包测试场景可以只留系统路径候选,但**别把系统路径放在构建产物之前**——那样你测的是旧版本,而且不会有任何报错。

**修正**:把真实产物路径补进 `binary.search`(保持"构建产物在前、系统路径兜底"的顺序),或改正 `--build-dir`。

### `E_PROC_DIED` —— 被测进程秒退

被测进程在就绪之前退出。诊断按 stderr 关键字分三种,提示不同。

**典型输出**(示意):

```
E_PROC_DIED: 被测进程启动后退出(exit=1),就绪判定未完成
  stderr 尾 20 行:
    qt.qpa.plugin: Could not find the Qt platform plugin "xcb" in ""
    This application failed to start because no Qt platform plugin could be initialized.
  提示: 无显示环境需要离屏渲染,在 service.yaml 的 sandbox.env 加 QT_QPA_PLATFORM=offscreen
```

#### 分支 A · stderr 含 `platform plugin` / `could not connect to display`

Qt 服务在无显示环境起不来(实测:AM 无显示时报 `could not load the Qt platform plugin "xcb"`)。

**修正**:

```yaml
sandbox:
  env:
    QT_QPA_PLATFORM: offscreen
```

`init` 会自动尝试这条并把生效配置写回,手写配置时容易漏。

#### 分支 B · stderr 含 `org.freedesktop.systemd1` / `ConfigManager`

外部依赖缺失导致服务**直接 abort**(实测:AM 依赖失败处是 `std::terminate()`,日志 `Process org.freedesktop.systemd1 exited with status 1`)。私有总线**默认不挂标准 servicedir**,所以这些依赖不会被自动激活——这是有意的:密闭测试禁止意外拉起真实系统服务。

**修正**:把依赖显式前置成 mock,框架保证 mock 先于被测进程启动:

```yaml
needs:
  - mock: systemd
  - mock: org.desktopspec.ConfigManager     # mocktemplates/ 里的自定义模板
```

补完后若报 `E_MOCK_UNFAITHFUL`,按那一节继续补方法。

#### 分支 C · 其它

**排查步骤**

1. 读 stderr 尾部——90% 的原因就在里面。
2. `--verbose` 看解析到的二进制路径、总线地址、mock 启动顺序,确认拉起的是你以为的那个文件。
3. 用 `--keep-bus` 保留现场,手工复现(见[调试开关](#调试开关)):

   ```bash
   dbus-testing run tests/dbus/ --build-dir build --keep-bus
   export DBUS_SESSION_BUS_ADDRESS=<输出里的地址>
   /path/to/binary            # 手工前台跑,完整日志直接看
   ```
4. 怀疑是沙箱 HOME 引起的(读不到配置/证书),先临时改 `sandbox: {home: inherit}` 验证假设,**验证完改回 `tmp`**——继承 HOME 会让测试结果依赖开发者桌面状态。

### `E_READY_TIMEOUT` —— 就绪超时

`ready.timeout`(缺省 10s)内 `ready.name-owner` 里的名字没有全部拿到 owner。存活判定用 Popen 句柄 + name-owner 双判(**不用 `pgrep`**,实测会匹配到桌面会话里的同名进程,假阳性)。

**典型输出**(示意):

```
E_READY_TIMEOUT: 10.0s 内服务名未就绪
  期望: ['org.deepin.dde.Pinyin1']
  实际已注册: ['org.deepin.api.Pinyin', ':1.3']
  提示: 总线上有别的名字,说明 services:/ready.name-owner 写错了,改成实际名字
```

#### 分支 A · 总线上有别的名字

进程活着并注册成功,只是名字和配置不一致。

**修正**:把 `services:` / `ready.name-owner` 改成**实际注册的名字**。别反过来改被测服务——服务注册什么名字是它的契约。不确定实际名字时:

```bash
dbus-testing run tests/dbus/ --build-dir build --keep-bus
busctl --address="$DBUS_SESSION_BUS_ADDRESS" list --acquired
```

#### 分支 B · 总线上没有任何名字

服务根本没注册成功,**转按 `E_PROC_DIED` 排查**(先看 stderr 尾部)。若日志显示它还在做初始化(扫盘、加载词库、等 D-Bus 之外的东西),那是真慢:

```yaml
ready:
  timeout: 30s
```

CI 慢机器不要改配置,用 `--timeout-scale 3` 统一放大。

### `E_MOCK_UNFAITHFUL` —— 依赖 mock 不保真

mock 起来了,但被测服务调了一个模板里没有的方法。实测过的原型:预置 dbusmock 官方 `systemd` 模板后 AM 仍 SIGABRT,mock 侧报 `UnknownMethod: Subscribe is not a valid method of interface org.freedesktop.systemd1.Manager`。

**典型输出**(示意):

```
E_MOCK_UNFAITHFUL: 依赖 mock 缺少被测服务实际调用的方法
  缺失: ['org.freedesktop.systemd1.Manager.Subscribe']
  提示: 在 mocktemplates/systemd.py 补上该方法,或用 mock-state 现场补:
    mock-state: {template: systemd, method: AddMethod,
                 args: ["org.freedesktop.systemd1.Manager", "Subscribe", "", "", ""]}
```

**排查步骤**

1. 缺失方法名诊断已经给全了,不用猜。
2. 判断这个方法要不要返回值:只被调一次、返回值不影响流程的,空实现即可;影响流程的要返回可信的假数据。

**修正**(二选一)

- **临时**:在用例里用 `mock-state` + `AddMethod` 现场补(见 [primitives.md](primitives.md#6-mock-state--改依赖-mock-的状态))。
- **长期**:补进 `mocktemplates/<服务名>.py` 并**回贡**。`mocktemplates/` 是跨仓共享资产:你补的 `systemd` 方法,下一个仓直接受益。这是本框架里仅次于框架本身的第二个共享物,别只在自己仓里打补丁。

### `E_CONTRACT_DRIFT` —— 契约漂移

运行时 introspect 规范化后与 `contract.xml` 不一致。规范化会剥掉注释与 annotation、折叠 `ignore.interfaces` 命中的标准接口、归并 `ignore.paths` 命中的动态子对象,并对接口与成员排序(`<arg>` **保持声明顺序**,顺序是语义),所以差异一定是真差异,不是格式抖动。

**典型输出**(示意):

```
E_CONTRACT_DRIFT: 契约与基线不一致(3 处)
  [removed] method  org.deepin.dde.Pinyin1.QueryList
  [changed] method  org.deepin.dde.Pinyin1.Query        before: in=s,out=as   after: in=ss,out=as
  [added]   property org.deepin.dde.Pinyin1.CacheSize    type=u,access=read
  提示: 有意变更 → scan --emit 更新基线并纳入 review;否则这是回归
```

**排查步骤 / 修正**

1. 先判断这次接口变更是**有意**的还是**误改**的——这是唯一需要人判断的地方。
2. 有意变更:更新基线,并让基线 diff 进 code review。

   ```bash
   dbus-testing scan tests/dbus/ --build-dir build --emit
   git diff tests/dbus/contract.xml       # 这份 diff 就是接口变更评审材料
   ```
3. 误改:修代码,**别动基线**。删方法、改签名、改属性 `access` 都是下游兼容性问题。
4. 不想被动态子对象或内部接口刷屏,用 `ignore:` 收窄比对范围,而不是把基线改宽:

   ```yaml
   ignore:
     paths: ["/org/deepin/dde/Pinyin1/session/*"]
     interfaces: ["org.freedesktop.DBus.*"]
     methods: ["org.deepin.dde.Pinyin1.DeprecatedQuery"]
   ```
5. 服务密闭跑不起来(T3)也别放弃契约防线:`check --static` 用仓内源码声明 XML 与基线比,不拉起服务、秒级完成。

### `E_GUARD_DENIED` —— attach 护栏拒绝

`mode: attach`(接真实会话总线)下,默认**只读**:`introspect` / `get-prop` / `check-contract` 恒允许;`call` / `set-prop` / `mock-state` 必须命中 `allow.methods`(fnmatch 匹配 `接口.成员`)。被拒的用例记 **`config-error`,不计失败**——它是配置没放行,不是被测代码有问题。

**典型输出**(示意):

```
E_GUARD_DENIED: attach 模式拒绝写操作
  被拒: org.deepin.dde.Appearance1.SetMonitorBackground
  allow.methods: []
  提示: attach 默认只读。确需调用请显式放行,注意它会改真实桌面状态
```

**修正**

```yaml
mode: attach
allow:
  methods:
    - org.deepin.dde.Appearance1.Get*        # 支持 fnmatch
```

放行前想清楚:`attach` 跑在**真实会话总线**上,被放行的方法会真的改你的桌面。**更好的做法是把服务改成能密闭跑**(补 `needs:` 的 mock),`isolate` 下所有操作恒放行。

### `E_SIGNAL_TIMEOUT` —— 信号没等到

**典型输出**(示意):

```
E_SIGNAL_TIMEOUT: 5.0s 内未收到 org.deepin.dde.Pinyin1.Pinged
  这段时间收到的信号: ['org.freedesktop.DBus.Properties.PropertiesChanged']
  提示: 检查信号名与触发方法是否对应,或调大 timeout
```

**排查步骤**

1. 对比"收到的信号列表"与你等的名字:名字拼错、大小写不对、接口写错(信号发在子接口上)当场就能看出来。
2. 收到的是 `PropertiesChanged` 而你在等业务信号 → 服务大概只发了属性变更,业务信号可能压根没实现。
3. 一个都没收到 → 触发方法调了吗?顺序对吗?**瞬时信号必须用 `call` + `wait-signal` 组合形式**(框架保证先订阅再调用),写成 `steps:` 两步会丢掉调用瞬间就发出的信号。
4. 真的慢:`wait-signal: {name: X, timeout: 15s}`,或 CI 上 `--timeout-scale`。
5. 手工确认服务到底发不发这个信号:

   ```bash
   busctl --address="$DBUS_SESSION_BUS_ADDRESS" monitor org.deepin.dde.Pinyin1
   ```

### `E_CALL_TIMEOUT` —— 调用超时

**典型输出**(示意):

```
E_CALL_TIMEOUT: 调用超时 5.0s
  目标: org.deepin.dde.Pinyin1.Query @ /org/deepin/dde/Pinyin1
  提示: 服务可能阻塞;--keep-bus 后手工 busctl call 复现
```

**排查步骤**

1. `--keep-bus` 保留现场,手工复现,同时看服务日志:

   ```bash
   busctl --address="$DBUS_SESSION_BUS_ADDRESS" call \
     org.deepin.dde.Pinyin1 /org/deepin/dde/Pinyin1 org.deepin.dde.Pinyin1 Query s "深度"
   ```
2. 手工也卡住 → 是**被测服务的缺陷**(死锁、同步等外部资源、等一个被 mock 掉的依赖)。这是框架帮你抓到的真问题,别调大超时糊过去。
3. 只在 CI 上卡 → `--timeout-scale`。
4. 别用"跨连接同步调用"的方式在被测服务里自调:实测 C++/Qt 侧客户端与注册端跨连接同步调用会死锁。

### `E_CONFIG_INVALID` —— 配置错误

**触发**:`apiVersion` 不支持、必填字段缺失、**未知字段**(拼写错误一律早失败,不静默忽略)、时长格式非法、一个步骤写了多个原语、用例名重复、`type` 断言用在多返回值上等。

**典型输出**(示意):

```
E_CONFIG_INVALID: tests/dbus/service.yaml 出现未知字段: sandobx
E_CONFIG_INVALID: tests.yaml:42 一个步骤只能含一个原语,实际含 call, get-prop;多步请用 steps: 列表
```

**修正**:按消息改配置。字段全集见 `examples/` 两份样板与 [primitives.md](primitives.md);schema 变更只通过 `apiVersion` 走,老配置不会被动失效。

### `E_BUS`

**触发**:私有总线起不来、连不上、或总线连接中途异常。

**排查步骤**

1. `dbus-daemon` 装了吗?`command -v dbus-daemon`(deb 包在 `dbus-daemon`)。
2. `/tmp` 可写吗?私有总线的 socket 与配置都在 `tempfile.gettempdir()` 之下。
3. 手工起一条私有总线看报什么(见[busctl 速查](#busctl-与-dbusmock-速查))。
4. 别用 `dbus-run-session` 包裹跑测试:实测本机 teardown 会挂起,框架也不这么用。

### `E_UNKNOWN` —— 框架内部错误

不该出现。请带上 `--verbose` 全量输出、`service.yaml`、被测二进制版本报 bug。裸 traceback 同样算 bug。

## 调试开关

四个公共开关,`init` / `scan` / `check` / `run` 都能用。

### `--verbose`

日志切到 DEBUG:总线地址、**解析到的二进制路径**、mock 启动顺序、每次调用耗时。"我以为它跑的是新构建的二进制"这类误会,一条 `--verbose` 就能澄清。

```bash
dbus-testing run tests/dbus/ --build-dir build --verbose
```

### `--keep-bus`

跑完**不清理**:私有总线、被测进程、沙箱目录都留着,并打印接现场用的环境变量。失败现场直接手工复现,不用重跑整条流水线。

```bash
dbus-testing run tests/dbus/ --build-dir build --keep-bus
# 输出里会有:
#   export DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-testing-xxxx/bus.socket
#   sandbox: /tmp/dbus-testing-xxxx/home
export DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-testing-xxxx/bus.socket
busctl --address="$DBUS_SESSION_BUS_ADDRESS" list --acquired
```

用完记得把留下的进程收掉(`kill` 输出里给的 pid),否则会攒一堆孤儿总线。

### `--shell`

`--keep-bus` 的顺手版:跑完直接进一个**已注入总线环境变量**的 `$SHELL`,`busctl` / `gdbus` 开箱可用,退出 shell 即清理。

```bash
dbus-testing run tests/dbus/ --build-dir build --shell
# 进入子 shell 后:
busctl introspect org.deepin.dde.Pinyin1 /org/deepin/dde/Pinyin1
```

### `--timeout-scale F`

把**所有**超时(就绪、调用、信号)统一乘以 `F`,**不改配置**。慢机器和 CI 用它,别把 `timeout:` 写大——配置里的超时值是接口的性能预期,不该被最慢的那台机器绑架。

```bash
dbus-testing run tests/dbus/ --build-dir build --timeout-scale 3
```

## busctl 与 dbusmock 速查

```bash
# 手工起一条私有总线并保留(框架内部用的就是这条命令的等价形式)
export DBUS_SESSION_BUS_ADDRESS=$(dbus-daemon --session --fork --print-address=1 | head -1)

# 查已注册名字(--acquired 区分真实占用与可激活)
busctl --address="$DBUS_SESSION_BUS_ADDRESS" list --acquired

# introspect:表格形式(看签名最快)
busctl --address="$DBUS_SESSION_BUS_ADDRESS" introspect <svc> <path>
# introspect:XML 形式(与 contract.xml 同格式,可直接 diff)
busctl --address="$DBUS_SESSION_BUS_ADDRESS" introspect <svc> <path> --xml-interface

# 直接调用:注意 <sig> 是**入参**签名
busctl --address="$DBUS_SESSION_BUS_ADDRESS" call <svc> <path> <iface> <method> <sig> <args>
busctl --user call org.deepin.dde.Pinyin1 /org/deepin/dde/Pinyin1 \
       org.deepin.dde.Pinyin1 Query s "深度"        # → as 2 "shendu" "shenduo"

# 看信号有没有真的发出来
busctl --address="$DBUS_SESSION_BUS_ADDRESS" monitor <svc>

# 起一个 dbusmock 模板到当前总线上(手工验证 needs: 该写什么)
python3 -m dbusmock --session <name> <path> <iface>

# 拿到确切的 D-Bus 错误名(busctl 只给人话,gdbus 给错误名)
gdbus call --session --dest <svc> --object-path <path> --method <iface>.<method>
```

接会话总线时把 `--address="$DBUS_SESSION_BUS_ADDRESS"` 换成 `--user` 即可。**在真实会话总线上只做 introspect 与读属性**,调用会改你自己的桌面状态。
