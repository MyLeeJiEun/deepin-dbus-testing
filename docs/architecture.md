# 架构图

一页看清框架的模块划分、运行时序与契约数据流。模块依赖按**实际 import 关系**绘制。

## 1. 模块分层与依赖

```mermaid
graph TD
  subgraph ENT["入口层"]
    CLI["cli.py<br/>init / scan / check / run"]
    PLUG["pytest_plugin.py<br/>自定义 Collector + fixtures"]
  end

  subgraph CFG["配置层"]
    MODEL["model.py<br/>ServiceSpec / Case / Step<br/>frozen dataclass + 行号 + 变量展开"]
    ERR["errors.py<br/>异常层次 + 退出码 0/1/2/3/4"]
  end

  subgraph EXEC["执行层"]
    ENGINE["engine.py<br/>Session / Engine / RunContext<br/>evaluate 五断言 / build_coverage"]
    RES["results.py<br/>CallOutcome / CaseResult<br/>Delta / CoverageMatrix"]
  end

  subgraph CORE["core/ 隔离与通信"]
    BUS["bus.py<br/>PrivateBus(session+system)"]
    LAUNCH["launcher.py<br/>Launcher / ServiceHandle"]
    SBOX["sandbox.py<br/>Sandbox"]
    MOCKS["mockdeps.py<br/>MockDeps"]
    CLIENT["client.py<br/>BusClient"]
    GUARD["guard.py<br/>AttachGuard"]
    DIAG["diagnose.py<br/>Diagnosis"]
  end

  subgraph SCAN["scanner/ 契约"]
    INTRO["introspect.py<br/>snapshot / walk"]
    NORM["normalize.py<br/>七步归一化"]
    DIFF["diff.py<br/>三层比对"]
    SRC["srcxml.py<br/>源码声明收集"]
    DRAFT["draft.py<br/>init 脚手架"]
  end

  subgraph REP["report/"]
    JUNIT["junit.py"]
    JSON["jsonout.py"]
    HTML["html.py"]
  end

  CLI --> ENGINE & MODEL & DRAFT & REP & DIAG
  PLUG --> ENGINE & MODEL
  ENGINE --> BUS & LAUNCH & SBOX & MOCKS & CLIENT & GUARD & DIFF & NORM & INTRO & SRC
  ENGINE --> MODEL & RES & DIAG
  CLI --> BUS & CLIENT & INTRO & NORM & SRC
  LAUNCH --> CLIENT
  MOCKS --> ERR
  INTRO --> CLIENT & MODEL
  DIFF --> NORM & RES
  SRC --> NORM & DIFF
  DRAFT --> NORM
  REP --> RES & MODEL
  MODEL --> ERR
  GUARD --> ERR & MODEL
  DIAG --> ERR
```

## 2. 运行时启动序列(顺序不可颠倒)

```mermaid
sequenceDiagram
  participant S as Session
  participant B as PrivateBus
  participant M as MockDeps
  participant X as Sandbox
  participant L as Launcher
  participant P as 被测进程
  participant C as BusClient

  S->>B: ① start() session(+system)<br/>空 services/ 无标准 servicedir
  S->>M: ② start(临时改 os.environ 注入地址)
  M->>B: python-dbusmock spawn(模板)
  S->>X: ③ __enter__() 返回 env 增量<br/>HOME/XDG_* → 临时目录
  S->>L: ④ launch(env = 系统env + 总线地址 + 沙箱)
  L->>P: Popen(start_new_session=True)
  S->>C: ⑤ BusClient(address)
  S->>L: wait_ready(handle, client)
  L->>P: poll() 存活判定(禁 pgrep)
  L->>C: name_has_owner 轮询
  Note over L,P: 失败→ProcessDied/ReadyTimeout<br/>先 check_faithful 报缺失方法名
  S->>C: ⑥ 交给 Engine.run 跑用例
```

每一步顺序都有实测依据(见各模块 docstring 与 [design.md](design.md) 附录):mock 必须先于被测进程,否则强依赖服务直接 `std::terminate`。

## 3. 契约三方比对(归一化是唯一事实来源)

```mermaid
graph LR
  A["仓内源码声明 XML<br/>api/dbus/*.xml"] --> N1[normalize 七步]
  B["contract.xml 基线"] --> N2[normalize]
  C["运行时 introspect<br/>walk + ignore.paths 归并"] --> N3[normalize]
  N1 --> SD["static_diff<br/>check --static"]
  N2 --> SD
  N2 --> LD["diff<br/>check / check-contract"]
  N3 --> LD
  N1 & N2 & N3 --> COV["build_coverage<br/>声明覆盖 + 执行覆盖"]
  SD --> R1["Delta[]<br/>path/interface/member"]
  LD --> R1
  R1 --> J[junit.xml]
  R1 --> JO[results.json]
  R1 --> H[report.html]
  N3 --> LX[contract-live.xml]
```

三方同为**规范化后的标准 D-Bus introspection XML**,故可逐字符直比。归一化七步见
[development.md](development.md),`ignore.paths` 命中路径归并为 `collapsed="true"` 节点。

## 4. 一次 `run` 的产物与退出码

```mermaid
graph TD
  RUN["dbus-testing run tests/dbus/ --build-dir build"] --> SUITE["run_suite"]
  SUITE --> OUT["SuiteOutcome<br/>result + spec + cases"]
  OUT --> SUM["stdout 摘要<br/>逐用例 PASS/FAIL + 覆盖率"]
  OUT --> REP2["--report outdir/"]
  REP2 --> F1["junit.xml — CI 门禁"]
  REP2 --> F2["results.json — 机器可读全量"]
  REP2 --> F3["report.html — 单文件零依赖"]
  REP2 --> F4["contract-live.xml — 审计快照"]
  OUT --> EC{"result.ok ?"}
  EC -->|是| E0["0"]
  EC -->|用例失败/契约漂移| E1["1"]
  EC -->|配置错误| E2["2"]
  EC -->|环境错误| E3["3"]
  EC -->|框架内部错误| E4["4"]
```

## 5. 关键边界

图里看不到、但决定行为边界的六处:

| 边界 | 位置 | 含义 |
|---|---|---|
| 进程边界 | `Popen` + 私有总线 | 框架与二进制只经 IPC 对话,不链接、不注入、不感知被测语言 |
| 环境边界 | `core/sandbox.py` | 仅隔离 `HOME`/`XDG_*`;`XDG_DATA_DIRS` 保持继承。**不**隔离 `/etc`、`/tmp`、网络 |
| 总线边界 | `core/bus.py` | 无标准 servicedir → 真实服务激活不进来;名字表每条 daemon 独立,同名不冲突 |
| 只读边界 | `core/guard.py` | `mode: attach` 默认只放行 `get-prop` / `check-contract` / `wait-signal` |
| 写入边界 | `tests/test_noninvasive.py` | 写操作限白名单模块;被测仓只读(`chmod 0o500` 后仍跑通) |
| 契约边界 | `scanner/normalize.py` | 只覆盖 introspection 面:不含 annotation、时序/并发语义、总线策略 |

## 6. 文件地图

```
dbus_testing/
├── cli.py            四命令入口、报告落盘、退出码映射
├── pytest_plugin.py  tests.yaml → 原生 pytest 用例 + 逃生舱 fixtures
├── model.py          配置模型与校验(未知字段报错)
├── engine.py         会话生命周期、用例执行、断言求值、覆盖矩阵
├── results.py        执行结果数据结构
├── errors.py         异常层次与错误码
├── core/             总线、拉起、沙箱、依赖 mock、客户端、护栏、诊断
├── scanner/          introspect、归一化、比对、源码声明、init 草稿
├── report/           junit / json / html 三种产出
└── mocktemplates/    框架自带依赖替身(systemd_dde、dconfig、dde_inputdevices…)
```
