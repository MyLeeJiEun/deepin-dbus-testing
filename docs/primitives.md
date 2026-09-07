# 原语与断言参考

`tests.yaml` 是一门**故意很小**的声明式语言:**六个原语 + 五个断言**,用 `steps:` 线性组合。没有条件、没有循环、没有变量插值(理由见文末)。

文件骨架:

```yaml
apiVersion: v1          # 必须是 v1;缺省视为 v1。schema 升级只通过这个字段,不会让已接入仓被动改配置
cases:
  - name: query-returns-pinyin      # 必需,同一文件内不可重名
    call: {method: Query, args: ["深度"]}
    expect: {signature: as, value: ["shendu", "shenduo"]}
```

**未知字段一律报错**(`E_CONFIG_INVALID`),不静默忽略——拼错 `expcet:` 必须当场失败,而不是悄悄少跑一条断言。

用例级字段:

| 字段 | 含义 |
|---|---|
| `name` | 必需,报告与 `-k` 过滤用 |
| `steps` | 多步用例,见[组合](#steps-线性组合) |
| `flaky` | 允许重试次数,见 [flaky](#flaky-n) |
| `skip` | 字符串,给出跳过原因;该用例记为 skip 不计失败 |

## Target 缺省规则

每个原语都要落到一个 **Target**(服务名 + 对象路径 + 接口 + 成员)。四项里通常只有成员必须写:

| 项 | 缺省推导 | 覆盖写法 |
|---|---|---|
| 服务名 | `service.yaml` 的 `services[0]` | `service: org.deepin.dde.Other1` |
| 对象路径 | 由**当前生效的服务名**点换斜杠:`org.deepin.dde.Pinyin1` → `/org/deepin/dde/Pinyin1` | `object: /org/deepin/dde/Pinyin1/Session` (`path:` 是等价别名) |
| 接口 | 等于**当前生效的服务名** | `interface: org.deepin.dde.Pinyin1.Extra` |
| 成员 | 无缺省,必须写 | `method:` / `property:` / `name:` |

注意第二、三行推导用的是**该步骤最终生效的服务名**:写了 `service:` 就按它推,没写才用 `services[0]`。所以跨服务用例只写一行 `service:` 就够:

```yaml
# 只写成员:全部走缺省
- name: minimal
  call: {method: Query, args: ["深度"]}

# 换服务:path 与 interface 随之变成 /org/deepin/dde/Graphic1 与 org.deepin.dde.Graphic1
- name: cross-service
  call: {service: org.deepin.dde.Graphic1, method: Hsv2Rgb, args: [0.0, 1.0, 1.0]}
  expect: {signature: yyy}

# 子对象 + 非同名接口:两项都显式写
- name: sub-object
  get-prop:
    object: /org/deepin/dde/Pinyin1/Session1
    interface: org.deepin.dde.Pinyin1.Session
    property: Locale
  expect: {type: s}
```

## 六原语

### 1. `call` —— 调用方法

```yaml
- name: query-returns-pinyin
  call:
    method: Query           # 必需
    args: ["深度"]           # 缺省 [];单个非列表值等价于单元素列表
  timeout: 5s               # 缺省 5s;接受 5 / 5.5 / "5s" / "500ms" / "2m"
  expect: {signature: as, value: ["shendu", "shenduo"]}
```

`args` 里写 YAML 字面量,框架按 D-Bus 规则编码:字符串→`s`、整数→`i`、浮点→`d`、布尔→`b`、列表→`a?`、映射→`a{?}`。**没有入参类型标注**:需要 `y`(byte)、`u`(uint32)、`o`(object path) 等窄类型入参的方法,现阶段走 `test_extra.py` 逃生舱。

签名从**回复消息**上取(低层 API 直读 `reply.get_signature()`),不是从 introspection 猜的——所以 `expect.signature` 校验的是服务真实发出的东西。

### 2. `get-prop` —— 读属性

```yaml
- name: counter-is-int
  get-prop: {property: Counter}
  expect: {type: i}
```

走标准 `org.freedesktop.DBus.Properties.Get`,返回值已从 variant 里拆出。`attach` 模式下读属性恒被放行。

### 3. `set-prop` —— 写属性

```yaml
- name: counter-roundtrip
  steps:
    - set-prop: {property: Counter, value: 42}
    - get-prop: {property: Counter}
      expect: {value: 42}

- name: name-is-readonly
  set-prop: {property: Name, value: "x"}
  expect: {error: org.freedesktop.DBus.Error.PropertyReadOnly}
```

`value` 是要写入的值(缺省 `null`)。只读属性的正确写法是**断言它拒绝写入**,这是错误面覆盖的一部分。`attach` 模式下 `set-prop` 必须命中 `allow.methods` 白名单,否则整例记 `config-error`。

### 4. `wait-signal` —— 等信号

```yaml
- name: pinged-signal
  wait-signal:
    name: Pinged            # 必需:信号名
    timeout: 3s             # 缺省 5s;也可写在步骤级 timeout:
  expect: {signature: i}
```

超时未收到 → `E_SIGNAL_TIMEOUT`,诊断里会列出**这段时间内实际收到的信号**,便于发现是信号名拼错还是触发方法不对。信号分发在主线程的 GLib 主上下文里做单线程轮询(后台线程跑主循环会与阻塞调用并发崩溃,这是实测结论)。

### 5. `check-contract` —— 契约比对

```yaml
- name: contract
  check-contract: true
```

对被测服务做运行时 introspect、规范化,再与 `contract.xml` 比对;有差异即失败(`E_CONTRACT_DRIFT`),差异以成员级 delta 表 + XML diff 呈现。没有 Target,惯例放在文件最后一条。`attach` 模式下也恒被放行(只读)。

### 6. `mock-state` —— 改依赖 mock 的状态

```yaml
- name: config-value-affects-result
  steps:
    # 给 mock 现场加一个模板里缺的方法(与 E_MOCK_UNFAITHFUL 的修复流程同一手法)
    - mock-state:
        template: org.desktopspec.ConfigManager     # 必需:needs: 里声明过的模板名
        method: AddMethod                           # 必需:mock 侧的控制方法
        args: ["org.desktopspec.ConfigManager", "value", "s", "s", "ret = 'zh_CN'"]
    # 让 mock 发一个信号,驱动被测服务的信号处理分支
    - mock-state:
        template: systemd
        method: EmitSignal
        args: ["org.freedesktop.systemd1.Manager", "UnitNew", "so",
               ["foo.service", "/org/freedesktop/systemd1/unit/foo"]]
    - call: {method: CurrentLocale}
      expect: {value: "zh_CN"}
```

`method` 是 mock 进程暴露的控制方法:dbusmock 通用控制接口的 `AddMethod` / `AddProperty` / `UpdateProperties` / `EmitSignal` / `Reset`,或模板自带的控制方法。`template` 必须先在 `service.yaml` 的 `needs:` 里声明过,否则 `E_CONFIG_INVALID`。

## 五断言

`expect:` 挂在**步骤**上,五个键可任意组合;全部为空表示"只要调用不出错就算过"。

### `error` —— 四种语义,别混

| 写法 | 语义 |
|---|---|
| **不写 `error`** | 隐含"必须无错":调用失败即用例失败,报告附错误名与消息 |
| `error: null` | 与不写等价,**显式**声明"必须成功"。用在需要强调"这条路径不许报错"的边界用例上,读者一眼看出是有意为之 |
| `error: "*"` | **必须出错**,不关心是哪个错。用于"参数非法就该拒绝"这类只关心拒绝行为的断言 |
| `error: org.freedesktop.DBus.Error.PropertyReadOnly` | **必须出错且错误名严格相等**。权限、只读、参数校验等有明确契约的错误必须写全名,`"*"` 会放过错误的错误 |

```yaml
- name: empty-input-is-not-an-error      # 实测:Query("") 返回空列表,不报错
  call: {method: Query, args: [""]}
  expect: {signature: as, value: [], error: null}

- name: unknown-method-rejected
  call: {method: NoSuchMethod}
  expect: {error: org.freedesktop.DBus.Error.UnknownMethod}

- name: bad-args-rejected-somehow
  call: {method: ThumbnailImage, args: ["", "", 0, 0, ""]}
  expect: {error: "*"}
```

### `signature` —— 回复签名严格相等

```yaml
expect: {signature: as}       # 单出参数组
expect: {signature: yyy}      # 三个出参:签名是拼接串,不是列表
expect: {signature: ""}       # 无返回值(void 方法)
```

字符串严格相等比对,不做兼容匹配:`s` 与 `as`、`i` 与 `u` 都算不同。这是最便宜也最该常写的断言——值会随词库、语言环境变,签名不会。

### `type` —— 单值的类型码

```yaml
- name: counter-is-int
  get-prop: {property: Counter}
  expect: {type: i}
```

只对**单返回值**有意义:取该值的 D-Bus 类型码与期望串比较。多返回值的步骤写 `type` 是配置错误(`E_CONFIG_INVALID`),请改用 `signature`。属性断言优先用 `type`,方法断言优先用 `signature`。

### `value` —— 深度相等

```yaml
expect: {value: ["shendu", "shenduo"]}
expect: {value: []}                       # 空列表也是明确的期望
expect: {value: {locale: "zh_CN", ok: true}}
```

比较前**双方都过 `to_native`**,把 dbus-python 的包装类型还原成 Python 原生类型,再做深度相等:

| D-Bus / dbus-python | 原生 |
|---|---|
| `String`、`ObjectPath`、`Signature` | `str` |
| `Int16/32/64`、`UInt16/32/64`、`Byte` | `int` |
| `Double` | `float` |
| `Boolean` | `bool` |
| `Array` | `list` |
| `Dictionary` | `dict` |
| `Struct` | `tuple`(YAML 里写列表,比较时转换) |

所以 YAML 里写普通字面量就行,不需要关心 `dbus.String` 之类。多返回值步骤的 `value` 写成列表,按出参顺序对齐。

### `nonempty` —— 非空

```yaml
- name: query-list-signature
  call: {method: QueryList, args: [["深度"]]}
  expect: {signature: s, nonempty: true}
```

`true` 表示:字符串 / 列表 / 映射长度 > 0,标量则为真值。用在"内容会变但不能为空"的返回值上,比硬编码一个会随词库漂移的 `value` 稳。

### 断言求值顺序(六步,固定)

失败信息取决于这个顺序,别指望别的顺序:

1. **先判 `error` 维度**:不写 / `null` → 必须成功,否则失败并附错误名与消息;`"*"` → 必须失败;`"<名字>"` → 错误名严格相等。
2. **期望出错且确实出错 → 其余断言全部跳过**(出错时没有返回值可断,再断就是假绿)。
3. **`signature`**:字符串严格相等。
4. **`type`**:单返回值取类型码比较;多返回值 → 配置错误。
5. **`value`**:两侧 `to_native` 后深度相等。
6. **`nonempty`**:长度 > 0 / 标量真值。

第 1 步先行意味着:一条期望成功的用例如果调用失败,你看到的是"调用报了什么错",而不是"签名不匹配"这种二级噪音。

## `steps:` 线性组合

需要多步时用 `steps:`,**严格按声明顺序执行**,任一步失败即整例失败(后续步骤不再执行)。`expect` 挂在各自的步骤上:

```yaml
- name: counter-roundtrip
  steps:
    - get-prop: {property: Counter}
      expect: {type: i}
    - set-prop: {property: Counter, value: 42}
    - get-prop: {property: Counter}
      expect: {value: 42}
```

约束:**一个步骤只能含一个原语**。把两个原语写进同一个 `- ` 条目(除下面的组合形式)会报 `E_CONFIG_INVALID`,提示改用 `steps:`。

### `call` + `wait-signal` 组合:先布扣,再调用

信号是异步的,"调用完再去订阅"会丢掉调用瞬间就发出的信号。所以**同一个用例条目里同时写 `call` 与 `wait-signal`** 时,框架保证的顺序是:

```
订阅信号(布扣) → 发起调用 → 校验调用的 expect → 等信号到达或超时
```

```yaml
- name: reload-emits-changed
  call: {method: ReloadApplications}
  wait-signal: {name: InterfacesAdded, timeout: 5s}
  expect: {error: null}        # expect 归属于 call(wait-signal 的期望写在它自己的 timeout/name 上)
```

这是**唯一**允许一个条目出现两个原语的形式,专为"触发—等信号"设计。用 `steps:` 显式写成两步时,顺序就是字面顺序(先跑完 `call`,再开始等信号),适合"信号在若干秒后才发"的场景,不适合瞬时信号。

## `flaky: N`

**默认不重试**——重试会掩盖缺陷。确认某条用例受环境时序影响时,显式声明:

```yaml
- name: slow-signal-sometimes-late
  flaky: 2                    # 最多再试 2 次;首次即过就不重试
  call: {method: Refresh}
  wait-signal: {name: Refreshed}
```

报告里该用例会标 `flaky`(并记录 `attempts`),退出码按**最终结果**算。`flaky` 是技术债的显式记账:它出现在 diff 里,评审时会被看见。

## 超时约定

| 项 | 缺省 | 覆盖方式 |
|---|---|---|
| 单次调用 / 属性读写 | 5s | 步骤级 `timeout:` |
| `wait-signal` | 5s | `wait-signal.timeout` 或步骤级 `timeout:` |
| 服务就绪 | 10s | `service.yaml` 的 `ready.timeout` |

时长接受 `5`、`5.5`、`"5s"`、`"500ms"`、`"2m"`。慢机器和 CI 不要改配置,用 `dbus-testing run --timeout-scale 3` 统一放大。

## 为什么不做条件、循环、变量插值

不是没想到,是**故意不做**:

1. **加了它就是一门弱类型编程语言**,而 YAML 是这门语言里最糟的宿主:没有类型检查、没有调试器、没有栈。真需要分支和循环的测试属于**行为测试**,应该写在 `test_extra.py` 里——那里有真正的语言、`pytest` 和断点。
2. **声明式才可反查**。每条用例是"一个契约事实",框架才能从 `call` / `get-prop` 的 Target 自动提取**执行覆盖**,生成覆盖矩阵而不需要人工标注。一旦目标由变量算出来,覆盖矩阵立刻失真。
3. **期望值必须能被 review**。`value: ["shendu","shenduo"]` 在 diff 里是可评审的事实;`value: ${expected}` 只是一个指针,评审时看不出接口行为变了没有。
4. **`init` 要能生成、`scan` 要能比对**。没有控制流,用例才可由 introspection 自动生成,基线才能逐字节稳定。

因此变量插值只在 `service.yaml` 里支持(`${BUILD_DIR}`、`${REPO_ROOT}`、`${TESTS_DIR}`、`${ENV:NAME}`),那是**环境事实**;`tests.yaml` 里的 `${...}` **不做展开**,会被当成普通字符串。

## 用例级字段

除原语与 `expect` 外,一条用例还接受:

| 字段 | 含义 |
|---|---|
| `name` | **必需**,用例名;同一份 `tests.yaml` 内不可重复(重复会报 `E_CONFIG_INVALID`) |
| `steps` | 线性步骤列表;与"单条目原语"二选一 |
| `flaky: N` | 显式允许重试 N 次;不写就**不重试**。重试成功会在报告里标 `flaky` |
| `skip: 原因` | 跳过该用例,原因会出现在终端与报告里(比注释掉更可追踪) |
| `timeout` | 步骤级超时;也可写在原语内部(如 `call: {method: X, timeout: 300ms}`) |

```yaml
- name: pending-upstream
  skip: 等待 dde-xxx 修复 #1234
  call: {method: Broken}
```

服务侧配置(`service.yaml`)的完整字段见 [service-yaml.md](service-yaml.md)。
