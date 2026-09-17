# 睡眠枕 Android 框架说明

这份工程现在是一个“原生 Android 外壳 + HTML 原型”的起步版本。它的目标不是一步到位做完整产品，而是先让页面能在模拟器/手机里跑起来，再逐步接入设备能力。

## 1. 当前运行链路

App 启动时会进入 `MainActivity`：

1. Android 系统启动 `com.sleeppillow.app/.MainActivity`。
2. `MainActivity.onCreate()` 请求 BLE 所需权限。
3. `MainActivity.createPrototypeWebView()` 创建 WebView。
4. WebView 加载 `file:///android_asset/sleep-pillow.html`。
5. HTML 里的 JavaScript 负责登录、页面切换、问卷、本地状态等演示交互。
6. HTML 通过 `window.SleepPillowNative.onPrototypeEvent(...)` 把关键事件通知给 Android。

所以现在看到的界面，其实还是原来的 `sleep-pillow.html`，只是它被装进了 Android App 里面。

## 2. 目录怎么理解

```text
D:\APP
  settings.gradle
  build.gradle
  app/
    build.gradle
    src/main/
      AndroidManifest.xml
      assets/
        sleep-pillow.html
      java/com/sleeppillow/app/
        MainActivity.java
        ble/
          SleepPillowBleManager.java
        data/
          PrototypeStateStore.java
          SleepSession.java
          SleepReport.java
          SleepDataRepository.java
      res/
        drawable/
        mipmap-anydpi-v26/
        values/
  docs/
    FRAMEWORK.md
```

`settings.gradle` 是整个工程的模块登记表。现在只有一个 `:app` 模块。

根目录的 `build.gradle` 管 Android Gradle Plugin 的版本。

`app/build.gradle` 管这个 App 的包名、最低系统版本、目标系统版本和 Java 版本。

`AndroidManifest.xml` 是 App 的总登记表，权限、入口页面、图标和主题都在这里声明。

`assets/sleep-pillow.html` 是当前原型页面。如果只改文案、页面样式、演示流程，优先改这个文件。

`MainActivity.java` 是 Android 原生入口。如果要让 HTML 调用蓝牙、系统权限、文件、音频等原生能力，会从这里开始接桥。现在已经预留了 `PrototypeBridge`，用于接收“开始睡眠”“结束睡眠”“助眠计时开始/结束/停止”等事件。

`ble/` 包专门放蓝牙相关代码，后面 ESP32 通信不要散到页面里。

`data/` 包专门放睡眠记录、报告、问卷结果、本地数据库等数据逻辑。当前 `PrototypeStateStore` 会先用 Android `SharedPreferences` 保存 HTML 原型发来的关键状态。

## 3. 以后改哪里

### 改页面原型

改：

```text
app/src/main/assets/sleep-pillow.html
```

例如想改首页文字、按钮、问卷题目、睡眠报告假数据，都先从这个 HTML 文件开始。

### 改 App 名称

改：

```text
app/src/main/res/values/strings.xml
```

里面的 `app_name` 会影响桌面显示名称。

### 改包名

主要改：

```text
app/build.gradle
```

里面的 `applicationId`。注意正式发布后包名不要随便改，否则手机会认为是另一个 App。

### 改权限

改：

```text
app/src/main/AndroidManifest.xml
```

BLE、Wi-Fi、网络、定位等权限都在这里声明。

### 接入蓝牙设备

从这里开始：

```text
app/src/main/java/com/sleeppillow/app/ble/SleepPillowBleManager.java
```

建议先实现这些能力：

1. 扫描 SleepPillow 设备。
2. 连接指定设备。
3. 发现 GATT 服务和特征值。
4. 写入 Wi-Fi 配网命令。
5. 订阅实时心率、呼吸率、睡眠状态。
6. 结束睡眠后同步整晚数据。

### 接入真实报告

从这里开始：

```text
app/src/main/java/com/sleeppillow/app/data/SleepDataRepository.java
```

现在它返回的是演示数据。以后可以改成：

1. 从 BLE 收到原始数据。
2. 保存到本地数据库。
3. 计算平均心率、平均呼吸、睡眠效率、清醒次数。
4. 生成 `SleepReport` 给页面显示。

第一版先不做 App 自己计算的综合睡眠评分，避免为了评分解释增加额外工作量；正式问卷里的 `PSQI 评分表` 名称仍然保留，因为它是量表本身的名称。

## 4. 推荐开发顺序

第一阶段先保留 HTML 原型：

1. App 能安装、能打开。
2. 页面流程能跑通。
3. HTML 和原生之间加 JavaScript Bridge。
4. BLE 扫描和连接能跑通。
5. Wi-Fi 配网能跑通。
6. 实时数据能显示在页面上。

当前版本的 Bridge 已经能收到这些原型事件：

```text
sleep_session_started
sleep_session_ended
stim_timer_started
stim_timer_finished
stim_timer_stopped
```

现在这些事件只写 Android 日志；等接硬件和服务器时，就把日志位置替换成 BLE 命令、本地保存和接口请求。

`stim_timer_stopped` 对应你刚刚确认的规则：用户在首页或实时监测页结束睡眠时，本次 30 分钟助眠计时也要同步停止。

第二阶段再逐步原生化：

1. 首页换成原生 Android 页面。
2. 设备页换成原生 Android 页面。
3. 实时监测页换成原生 Android 页面。
4. 报告页换成原生 Android 页面。
5. 问卷和设置页最后再换。

这样做的好处是：你现在能马上演示，同时不会把后续硬件开发卡死在 UI 重写上。

## 5. 打包位置

调试 APK 默认输出在：

```text
D:\APP\app\build\outputs\apk\debug\app-debug.apk
```

我也会习惯性把好找的测试包放到：

```text
D:\APP\outputs\SleepPillow-demo-debug.apk
```

正式发布包后面需要单独做签名 release APK。
