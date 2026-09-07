# DDE 服务分档看板

档位**只由实测认定**,不得由源码推断。每条记录必须给出实测证据。

- **T1** 独立二进制、无/极少外部依赖 → 密闭(`mode: isolate`)直接可跑
- **T2** 依赖有现成 mock 或需私有 system bus / loader 形态 → 少量配置即可密闭
- **T3** 强依赖失败即 abort、或需 root / 硬件 / polkit → 需高保真 mock 或宿主替身;
  未就绪前用 `check --static` + `mode: attach` 拿防漂移能力

| 服务 | 档 | kind | 状态 | 实测证据 |
|---|---|---|---|---|
| `org.deepin.dde.Pinyin1` | T1 | process | ✅ 密闭跑通 | 私有总线注册成功;`Query("深度") → ["shendu","shenduo"]`;5 用例全绿(`examples/pinyin1`) |
| `org.deepin.dde.Graphic1` | T1 | process | ✅ 密闭跑通 | 私有总线注册成功;签名断言 + 契约校验通过(`examples/graphic1`)。**推翻了"依赖 X11"的静态推断** |
| `org.deepin.dde.SystemInfo1` | T2 | go-loader | ✅ 密闭跑通 | `dde-session-daemon --enable systeminfo -i` 在私有总线上注册成功;6 用例全绿(属性面 + 契约);未需 system bus mock |
| `org.deepin.dde.Timedate1` + `Format1` + `Daemon1` | T2 | go-loader | ✅ 密闭跑通 | `dde-session-daemon --enable timedate -i` 在私有总线上**同时注册三个服务名**;`init` 自动探测出全部三个名字并生成 27 条可直接运行用例(成员总数 35),开箱 27/27 全绿。配置在 `dde-daemon/tests/dbus/` |
| `org.deepin.dde.Appearance1` | T3 | dsm | ✅ 密闭跑通 | 经 `dsm-host` 加载真实 `libplugin-dde-appearance.so`,私有总线注册成功;7 对象 / 3 接口;4 用例全绿 |
| `org.desktopspec.ApplicationManager1` | T3 | process | ✅ 密闭跑通(需 `systemd_dde` mock) | 挂 `needs: [systemd_dde]` 后私有总线注册成功,6 用例全绿(ObjectManager 签名 / List 属性 / ReloadApplications / 错误路径 / 契约);**isolate 抓到的接口面与 attach 抓到的逐字节一致**。补齐过程:官方 dbusmock systemd 模板缺 `Subscribe` → 加上后卡在 `no such property Environment` → 补 `Manager.Environment` 后通过(诊断逐步指出了每一处缺失) |
| `org.deepin.dde.Device1` | T3 | process | ⏳ 待实测 | system bus + root + `/dev/rfkill` + 蓝牙硬件 |
| `org.deepin.dde.PasswdConf1` | T2(读面)/T3(写面) | process | ✅ 密闭跑通 | `system-bus: true` + `needs: [polkitd(system)]`:私有 system bus 上注册成功,5 用例全绿(签名/值断言、polkit 拒绝与放行、契约)。读方法不需要 root;写方法会真实写 `/etc/deepin/dde.conf`,且需 root,归 T3 |
| `org.desktopspec.ConfigManager`(dconfig-daemon) | T3 | process | ⏳ 待实测 | system bus,`.conf` 策略限定 `deepin-daemon`/root 可 own |
| `org.deepin.dde.Keyboard1`(tray-loader 插件) | T3 | plugin-host | ✅ 密闭跑通(条件:依赖存在性替身) | `trayplugin-loader -p <单个插件 .so>` offscreen 下加载成功;插件对 `InputDevices1` 的存在性检查由 `dde_inputdevices` 替身满足;4 用例全绿(属性/契约)。样板 `examples/tray-keyboard`。注意:wayland 会话下 3 个录制类插件被宿主黑名单跳过 |
| dde-shell 各面板服务 | T3 | plugin-host | ⏳ 待验证 | DPluginLoader/applet 机制与 tray-loader 的 `-p` 不同,待单独实测;在此之前只支持 `attach` |

## 待补服务(尚未建档)

dde-control-center、dde-session、dde-clipboard、dde-launchpad、dde-network-core、
dde-session-shell、dde-session-ui、dde-polkit-agent、deepin-screensaver、deepin-face、
dde-application-wizard、deepin-service-manager、dde-app-services、dde-api(其余子服务)、
dde-daemon(其余模块)、dde-services、deepin-pw-check。

建档方法:

```bash
# 1) 能直接拉起的:探测并生成配置
dbus-testing init --binary <构建产物路径> --out /tmp/probe
# 2) 拉不起来的:先接真实会话拿只读契约
dbus-testing init --from-running <服务名> --out /tmp/probe
# 3) 按实测结果填 tier 字段,并把证据补进本表
```
