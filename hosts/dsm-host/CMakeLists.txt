cmake_minimum_required(VERSION 3.16)
project(dbus-testing-dsm-host VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 REQUIRED COMPONENTS Core DBus)

add_executable(dbus-testing-dsm-host main.cpp)
target_link_libraries(dbus-testing-dsm-host PRIVATE Qt6::Core Qt6::DBus)

include(GNUInstallDirs)
install(TARGETS dbus-testing-dsm-host
        RUNTIME DESTINATION ${CMAKE_INSTALL_LIBEXECDIR}/dbus-testing)
