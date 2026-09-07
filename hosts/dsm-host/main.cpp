// SPDX-License-Identifier: LGPL-3.0-or-later
//
// dsm-host —— deepin-service-manager 宿主替身(框架的**唯一** C++ 组件,可选安装)
//
// 存在理由:deepin-service-manager 的配置目录是编译期宏 SERVICE_CONFIG_DIR,
// 命令行只有 -g/-n/-s/--elf-qt-version-check,没有运行时重定向手段,
// 因此无法让它加载 ${BUILD_DIR} 里刚构建出来的插件 .so。
//
// 插件 ABI(已在 deepin-service-manager 源码中确认):
//     typedef int (*DSMRegister)(const char *name, void *data);
// 其中 data 实参是 QDBusConnection*(serviceqtdbus.cpp: objFunc(name, (void *)policy->dbus))。
//
// 兼容性:部分插件在 DSMRegister 内直接用 QDBusConnection::sessionBus() 而非传入连接;
// 只要调用方把 DBUS_SESSION_BUS_ADDRESS 指向私有总线(框架的 launcher 会做),两种写法都能工作。

#include <QCommandLineOption>
#include <QCommandLineParser>
#include <QCoreApplication>
#include <QDBusConnection>
#include <QDBusConnectionInterface>
#include <QDBusError>
#include <QDebug>
#include <QLibrary>

namespace {
constexpr auto kConnectionTag = "dbus_testing_dsm_host";

using DSMRegister = int (*)(const char *name, void *data);
}  // namespace

int main(int argc, char *argv[])
{
    QCoreApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("dbus-testing-dsm-host"));
    app.setApplicationVersion(QStringLiteral("0.1.0"));

    QCommandLineParser parser;
    parser.setApplicationDescription(
        QStringLiteral("加载 deepin-service-manager 插件 .so 并在指定总线上注册(测试用宿主替身)"));
    parser.addHelpOption();
    parser.addVersionOption();

    QCommandLineOption pluginOption(QStringLiteral("plugin"),
                                    QStringLiteral("插件 .so 路径(通常在构建目录下)"),
                                    QStringLiteral("path"));
    QCommandLineOption nameOption(QStringLiteral("name"),
                                  QStringLiteral("要注册的 DBus 服务名"),
                                  QStringLiteral("service"));
    QCommandLineOption busOption(QStringLiteral("bus"),
                                 QStringLiteral("总线地址;缺省用 DBUS_SESSION_BUS_ADDRESS"),
                                 QStringLiteral("address"));
    parser.addOption(pluginOption);
    parser.addOption(nameOption);
    parser.addOption(busOption);
    parser.process(app);

    if (!parser.isSet(pluginOption) || !parser.isSet(nameOption)) {
        qCritical("必须同时提供 --plugin 与 --name");
        return 2;
    }

    const QString pluginPath = parser.value(pluginOption);
    const QString serviceName = parser.value(nameOption);
    const QString address = parser.isSet(busOption)
        ? parser.value(busOption)
        : QString::fromLocal8Bit(qgetenv("DBUS_SESSION_BUS_ADDRESS"));

    if (address.isEmpty()) {
        qCritical("没有总线地址:请传 --bus 或设置 DBUS_SESSION_BUS_ADDRESS");
        return 2;
    }

    QDBusConnection connection =
        QDBusConnection::connectToBus(address, QLatin1String(kConnectionTag));
    if (!connection.isConnected()) {
        qCritical("连接总线失败: %s", qUtf8Printable(connection.lastError().message()));
        return 3;
    }

    QLibrary library(pluginPath);
    if (!library.load()) {
        qCritical("加载插件失败: %s", qUtf8Printable(library.errorString()));
        return 3;
    }

    auto entry = reinterpret_cast<DSMRegister>(library.resolve("DSMRegister"));
    if (entry == nullptr) {
        qCritical("插件缺少 DSMRegister 符号: %s", qUtf8Printable(library.errorString()));
        return 3;
    }

    // 与 deepin-service-manager 一致:传入一个堆上的 QDBusConnection*
    auto *handle = new QDBusConnection(connection);
    const int ret = entry(qUtf8Printable(serviceName), static_cast<void *>(handle));
    if (ret != 0) {
        qCritical("DSMRegister 返回 %d(注册失败)", ret);
        return 4;
    }

    // 有两类插件写法:
    //  a) 规范写法:用传入的 connection registerObject,服务名由宿主注册 —— 这里补注册;
    //  b) 常见写法(如 dde-appearance):插件内部直接用 QDBusConnection::sessionBus()
    //     自己把对象和服务名都注册了 —— 此时名字已被本进程持有,不能再注册一次。
    auto *iface = connection.interface();
    const bool alreadyOwned =
        iface != nullptr && iface->isServiceRegistered(serviceName).value();
    if (alreadyOwned) {
        qInfo("dsm-host: 服务名 %s 已由插件自行注册(sessionBus 写法)",
              qUtf8Printable(serviceName));
    } else if (!connection.registerService(serviceName)) {
        const QString err = connection.lastError().message();
        qCritical("注册服务名 %s 失败: %s", qUtf8Printable(serviceName),
                  err.isEmpty() ? "(无错误详情:名字可能已被他人占用)" : qUtf8Printable(err));
        return 4;
    }

    // 就绪日志锚点;框架的就绪判定仍以 name-owner 为准
    qInfo("dsm-host: 已注册 %s", qUtf8Printable(serviceName));
    fflush(stdout);

    return QCoreApplication::exec();
}
