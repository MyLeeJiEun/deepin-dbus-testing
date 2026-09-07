# 五分钟接入

目标:在你的仓里从零到"跑绿一条接口测试"。前提是**已经能构建出被测二进制**——框架不碰你的构建系统,只消费构建产物路径。

## 0. 装依赖(一次性)

```bash
# deepin 25 自带前两项
sudo apt install python3-dbus python3-gi dbus-daemon
sudo apt install python3-dbus-testing            # 或者:pip install --user deepin-dbus-testing
sudo apt install python3-dbusmock                # 可选:服务有外部依赖需要 mock 时
```

要求 Python ≥ 3.11。装好后 `dbus-testing --help` 应该有输出。

## 1. `init`:生成骨架和用例草稿

`init` 是**唯一会写入被测仓的命令**,且只写 `tests/dbus/`(或 `--out` 指向的仓外目录)。它有两种用法:

**用法 A · 从构建产物探测(主线)**

```bash
cd <repo>
cmake --build build                              # 或 go build -o out/ ./...
dbus-testing init --binary build/hans2pinyin
```

它会在私有总线上把这个二进制拉起来,记录它**实际注册**的服务名与对象路径,再 introspect 落盘。启动失败时按诊断规则重试(例如自动补 `QT_QPA_PLATFORM=offscreen`)并把生效配置写回 `sandbox.env`。

**用法 B · 从已在运行的服务反向生成(摸底用)**

```bash
dbus-testing init --from-running org.deepin.dde.Pinyin1
```

不拉起进程,直接对会话总线上现成的服务 introspect。适合"还不知道这个服务能不能密闭跑起来"的阶段:先把契约基线拿到手,`mode` 先留 `attach`,以后再切 `isolate`。

其它选项:`--out <dir>`(默认 `tests/dbus`,指向仓外即完全不写被测仓)、`--build-dir <dir>`(展开配置里的 `${BUILD_DIR}`)。

生成物:

```
<repo>/tests/dbus/
├── service.yaml      # 可探测项已填好:services / ready.name-owner / sandbox.env
├── contract.xml      # 接口基线草稿(待人工审)
├── tests.yaml        # 用例草稿,注释态,取消注释即生效
└── test_extra.py     # 可选逃生舱的空模板
```

你的实际工作量是**删改草稿**,不是从零写配置。草稿里"无入参、有出参的方法"和"可读属性"这两类是**可直接启用**的;有入参的方法草稿会带占位参数和提示,需要你填真实参数。

## 2. 审基线 + 启用草稿

打开 `contract.xml` 看一眼接口是不是你期望暴露的(多出来的内部接口就是设计问题,现在发现比上线后便宜)。然后打开 `tests.yaml`,把要启用的用例取消注释。用例语法见 [primitives.md](primitives.md);可直接抄的样板在 `examples/pinyin1`、`examples/graphic1`。

## 3. 跑起来

```bash
# 秒级:源码声明 XML vs 基线,不拉起服务,先跑这条
dbus-testing check tests/dbus/ --static

# 完整:拉起构建产物,跑用例 + 运行时契约比对
dbus-testing run tests/dbus/ --build-dir build --report artifacts/dbus
```

`check --static` 读的是仓内源码声明 XML(默认 `api/dbus/*.xml`、`xml/*.xml`、`dbus/*.xml`,可用 `src-xml-globs` 改),只读不写。**仓里没有源码声明 XML 时它输出 SKIP,不是失败**——报告里会标注"该仓无源码声明可比"。

### C++ 仓的真实命令

```bash
cmake --build build && dbus-testing run tests/dbus/ --build-dir build
```

`--build-dir build` 就是 `service.yaml` 里 `${BUILD_DIR}` 的值,`binary.search` 按序找第一个存在的候选:

```yaml
binary:
  search:
    - ${BUILD_DIR}/bin/my-service      # 构建产物优先
    - ${BUILD_DIR}/src/my-service
    - /usr/lib/deepin-daemon/my-service # 系统路径仅兜底
```

### Go 仓的真实命令

```bash
go build -o out/ ./... && dbus-testing run tests/dbus/ --build-dir out
```

注意 `-o out/`:`go build ./...` **不产出二进制**(只做编译检查),必须用 `-o` 指定输出目录,否则 `--build-dir` 下什么都没有,框架会报 `E_BINARY_NOT_FOUND` 并列出每条候选路径的判定结果。

### 挂进 ctest(一行)

`tests/CMakeLists.txt` 里加:

```cmake
add_test(NAME dbus-interface
         COMMAND dbus-testing run ${CMAKE_SOURCE_DIR}/tests/dbus --build-dir ${CMAKE_BINARY_DIR})
```

之后 `ctest -R dbus-interface` 与其它单元测试一起跑。这是仓内**唯一**可选的一行改动,位于 test 目录内。

### 挂进流水线

```yaml
- name: dbus-interface-test          # 置于构建/装包阶段之后
  script:
    - dbus-testing check tests/dbus/ --static                      # 秒级,先跑
    - dbus-testing run tests/dbus/ --build-dir build --report artifacts/dbus
  artifacts: [artifacts/dbus/junit.xml, artifacts/dbus/report.html]
```

灰度期建议先 warning-only:退出码不阻断,只收报告;等基线稳定再改成门禁。

## 4. 产物目录

`--report <outdir>` 产出四个文件,各有明确消费者:

| 文件 | 给谁 | 内容 |
|---|---|---|
| `junit.xml` | CI | 每例一个 `<testcase>`;失败是 `<failure type=E_XXX>`,配置错误(如 attach 护栏拒绝)是 `<error>`;`<properties>` 里有环境指纹:**二进制实际解析路径**、总线地址、构建目录、mode、档位 |
| `results.json` | 机器 | 全量结果:每 step 的断言详情与耗时、契约 delta 列表、覆盖矩阵。做趋势对比用这个 |
| `report.html` | 人 | 单文件零前端依赖。三段:概要 / 接口覆盖矩阵(**声明覆盖**与**执行覆盖**分列)/ 用例明细(带 `tests.yaml:行号` 与 XML diff) |
| `contract-live.xml` | 审计 | 本次运行时 introspect 的规范化快照,可直接与 `contract.xml` 做 diff 存档 |

不带 `--report` 时只往终端打结果。终端只给结论和可操作提示,细节在 HTML 里。

## 5. 接口变了怎么办

契约漂移(`E_CONTRACT_DRIFT`)分两种,报告里的 diff 表会指清是哪种:

```bash
# 情况一:接口是**有意**变的 —— 更新基线,并让基线变更进 code review
dbus-testing scan tests/dbus/ --build-dir build --emit
git diff tests/dbus/contract.xml          # 这份 diff 就是接口变更评审材料

# 情况二:接口是**误改**的 —— 修代码,别动基线
```

## 6. 接入检查单

- [ ] `dbus-testing init` 跑完没报错,`service.yaml` 的 `services:` 与你预期的服务名一致
- [ ] `contract.xml` 人工审过,没有不该暴露的接口
- [ ] `dbus-testing check tests/dbus/ --static` 通过(或明确 SKIP)
- [ ] `dbus-testing run tests/dbus/ --build-dir <构建目录>` 绿
- [ ] `tests/dbus/` 已提交进仓,CI 或 ctest 里已挂上
- [ ] 跑不起来时:先看 [diagnose.md](diagnose.md) 的错误码表,再用 `--keep-bus` 保留现场手工 `busctl` 复现
