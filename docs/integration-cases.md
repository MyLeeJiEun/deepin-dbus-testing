# 接入案例:三个上游仓的完整记录

三个代表性 DDE 仓已在 GitHub Actions 跑通,本文记录接入过程中的**实际决策与坑**,
供后续接入其它仓时对照。基础流程见 [quickstart.md](quickstart.md),字段语义见
[primitives.md](primitives.md)。

| 仓 | 形态 | 被测物 | 用例数 | 状态 |
|----|------|--------|--------|------|
| dde-appearance | dsm | libplugin-dde-appearance.so | 31 | ✅ CI 绿 |
| dde-daemon | go-loader | dde-session-daemon --enable timedate | 27 | ✅ CI 绿 |
| dde-services | dsm ×4 插件 | xsettings 等(见下) | 26 | ✅ CI 矩阵绿 |

---

## 1. 容器:一律 linuxdeepin/deepin:crimson

**不要用 debian:bookworm。** 两条实测教训:

- dde-daemon 在 bookworm 里被测进程**启动即崩**(exit 2):桌面守护进程初始化
  依赖 Deepin 运行时环境(GSettings schema、locale 处理、系统服务布局)。
- deepin 镜像的 apt 源与上游构建环境同源,dtk6/Qt6/libpolkit-qt6 版本对齐,
  Build-Depends 照抄 debian/control 就能装齐。

镜像内**没有**的常用包,首次接入按需补:
`build-essential g++`(无编译器)、`make`、`python3-pip python3-setuptools
python3-wheel`、`python3-dbusmock`(needs mock 硬依赖)、`python3-yaml`
(--no-deps 安装框架时运行时依赖必须 apt 备齐)、`gdb`(抓崩溃栈)。

dde-appearance / dde-services 还需要 `deepin-desktop-schemas deepin-wallpapers
adwaita-icon-theme`(插件初始化读图标主题/壁纸/schema)。

---

## 2. 各仓接入要点

### dde-appearance(C++/Qt,单插件)

- **SIGSEGV(根因一)**:`getCurLang()` 对未设置的 `LANGUAGE` 直接
  `strlen(NULL)`。service.yaml 沙箱补 `LANG: C.UTF-8`、`LANGUAGE: en_US`。
- **exit(-1)(根因二)**:XSettings 的 `ConfigPtr` 拿不到 dde-dconfig-daemon
  时为 NULL 直接退出。`needs: - mock: dconfig` 用框架替身补齐。
- 诊断手段:容器内 `fetch_crash_stack.sh`(私有总线 + gdb 直接拉 dsm-host
  复现)拿到的栈比读源码猜准得多。

### dde-daemon(Go,go-loader)

- **vendor/ 在 .gitignore 里**:CI checkout 后不存在,`GOFLAGS=-mod=vendor`
  直接报 inconsistent vendoring。构建前先 `go mod vendor`。
- **Go 版本**:1.20 的 vendor 一致性检查与仓库 vendor/modules.txt 语义不符,
  用 1.22 对齐上游。
- **system 总线依赖**:`initDsgConfig()` 在 system 总线调
  `org.desktopspec.ConfigManager.acquireManager`,拿不到就退出——
  `needs` 前置 dconfig/systemd mock,框架自动拉起私有 system 总线。
- **数据文件**:timedate 的 GetZoneList 读 `/usr/share/dde/zoneinfo/zone1970.tab`,
  仓内 `misc/zoneinfo` 手工装(平时由 deb 包提供)。

### dde-services(C++/Qt,多插件,CI matrix)

- **产物名无统一规律**:`libplugin-dde-xsettings.so` / `libplugin-qt-*.so` /
  `libplugin-ambient-brightness.so` 混杂,CI 里按 case 映射,别猜前缀。
- **总线名 ≠ plugin json 的 name**:service-manager 的分组名
  (`org.deepin.service.*`)不注册到总线,真正总线名看 plugin.cpp 的
  registerService 调用(wallpaperslideshow 踩过)。
- **单插件多对象**:wallpapercache 一次注册 WallpaperCache/ImageEffect1/
  ImageBlur1 三个对象,用例里显式 `object:`/`interface:`。
- **cmake 全仓配置拖依赖**:只测一个插件,但 `cmake -S .` 会配置所有子目录——
  power 插件把 libxss/libudev/libpolkit-qt6 拖进必装清单。

---

## 3. 离屏(offscreen)环境的行为边界

`QT_QPA_PLATFORM=offscreen` 下各插件降级方式不同,**用例策略必须逐个确认**:

| 插件 | X 不可用时的行为 | 用例策略 |
|------|------------------|----------|
| xsettings | `xcb_connect` 失败;Set* 写 X property **静默失败**(changeSettingProp 里 getAtom 失败 return false) | Set 只验证可调用;roundtrip 断言不成立 |
| wallpaperslideshow | `isValidScreen` 恒 false | Set 走"monitor 不存在"拒绝分支,断言可调用 |
| ambient-brightness | `net.hadess.SensorProxy` 不在,Supported=false | 无传感器降级是**插件自定义契约**,直接断言 `Supported=false` |

判断方法:读 X 调用点的失败分支(xcbutils.cpp 的 `getAtom`/`changeSettingProp`),
区分"崩溃/退出"(要 mock 或补环境)与"静默降级"(断言降级值)。

---

## 4. dconfig 依赖:mock 而非真实守护进程

CI 容器里没有 dde-dconfig-daemon。两种情况:

- **拿不到 Manager 就退出**(dde-appearance / dde-daemon):
  `needs: - mock: dconfig` + 框架自动拉起私有 system 总线。
- **对 value() 类型有断言**(go 侧 `v.Value().(bool)`):mock 必须返回
  类型正确的 Variant。用 `values` 参数传**类型化编码字符串**
  (dbusmock 的 a{sv} 传参会丢类型,见下):

```yaml
needs:
  - mock: dconfig
    bus: system
    params:
      values:
        enabled: "b:false"     # b:bool / i:int / d:double / s:string / j:json
```

未列出的 key 返回空 JSON("[]"),被测服务走默认配置分支。

---

## 5. call.signature:显式入参签名

dbus-python 对参数做类型推断,两种情况会错,需要显式指定:

```yaml
# ① 自定义序列化类型:QList<quint16> 是 aq,推断成 ai 后 dbus 报 No such method
- call:
    method: SetColor
    args: ["prop", [65535, 0, 0]]
    signature: "saq"

# ② 空容器:空 list 无法推断签名(Unable to guess signature)
- call:
    method: GetProcessedImagePaths
    args: ["/path", []]
    signature: "sav"     # QVariantList 的真实签名是 av,以 introspect 为准
```

正确签名以 `contract-live.xml`(introspect 快照)为准。

---

## 6. CI workflow 模板要点(三仓的共同骨架)

```yaml
container: linuxdeepin/deepin:crimson
# ① apt 装 Build-Depends + python3-dbusmock + gdb + 桌面资源
# ② 构建(吃 --build-dir 约定)
# ③ git clone 框架源码 → pip install --no-build-isolation --no-deps
# ④ kind=dsm 时:cmake 编译框架 hosts/dsm-host 装到 /usr/local/bin
# ⑤ dbus-testing run tests/dbus --build-dir ... --report artifacts/
# ⑥ 上传 artifact(if: always())
```

debug 用:失败时跑私有总线 + gdb 拉起 dsm-host 抓栈(参考
dde-appearance 的 `tests/dbus/debug-stack.sh`)。

---

## 7. 排障备忘(实测效率排序)

1. **gdb 抓栈** > 读源码猜。框架报 exit code 时先抓栈(dde-appearance 的
   SIGSEGV 靠栈定位到 `strlen(NULL)`,靠猜会走弯路)。
2. **框架诊断先读全**:`E_MOCK_UNFAITHFUL` 会区分"mock 没起来"
   (如未装 python-dbusmock)与"缺方法";`E_PROC_DIED` 附带进程输出尾部。
3. **用独立探针验证 mock 能力面**:对 dconfig mock 写一个最小 DBus 客户端
   逐方法调用(本会话曾用此发现 dbusmock AddObject/AddMethods 作用域问题)。
4. **git archive 模拟 CI**:本地 `git archive HEAD | tar -x` 后按 CI 步骤跑,
   能提前暴露 gitignore 引起的 checkout 差异(vendor、test_extra.py 等)。
