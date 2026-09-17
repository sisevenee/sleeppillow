@echo off
REM 一键启动 Android 模拟器。
REM 适合汇报前先把虚拟手机窗口打开，后续安装/运行 App 仍建议在 Android Studio 点 Run。

set "ANDROID_SDK_ROOT=C:\Users\29456\AppData\Local\Android\Sdk"
set "ANDROID_AVD_HOME=C:\Users\29456\.android\avd"

"%ANDROID_SDK_ROOT%\emulator\emulator.exe" -avd Pixel_6

pause
