# SleepPillow Android

这是睡眠枕 App 的 Android 原生骨架。当前阶段先用 WebView 承载 `sleep-pillow.html` 原型，方便尽快在手机或模拟器上跑通页面流程；后续可以逐步把关键页面替换成原生 Android 页面，并接入 BLE、Wi-Fi 配网、睡眠数据和报告生成逻辑。

## 当前结构

- `app/src/main/assets/sleep-pillow.html`: 现有交互原型。
- `app/src/main/java/com/sleeppillow/app/MainActivity.java`: App 入口，加载原型页面，并提供 HTML 和 Android 互相通信的桥。
- `app/src/main/java/com/sleeppillow/app/data/PrototypeStateStore.java`: 保存 HTML 原型发给 Android 的运行状态，例如睡眠是否进行中、助眠计时是否停止。
- `app/src/main/java/com/sleeppillow/app/ble/SleepPillowBleManager.java`: 后续 ESP32 BLE 通信入口。
- `app/src/main/java/com/sleeppillow/app/data`: 睡眠记录与报告数据模型占位。
- `docs/FRAMEWORK.md`: 中文框架说明，解释每个目录和后续开发顺序。

## 推荐下一步

1. 用 Android Studio 打开本目录。
2. 让 Android Studio 安装/同步 Gradle、Android Gradle Plugin 和 Android SDK。
3. 运行 `app` 到模拟器或 Android 手机。
4. 确认 HTML 原型展示正常后，先实现 BLE 扫描和绑定流程。
5. 与 ESP32 固件约定 GATT Service UUID、Characteristic UUID 和 JSON/二进制命令格式。
6. 把 `MainActivity.PrototypeBridge.onPrototypeEvent()` 里的日志替换成真实 BLE/服务器调用。

## 设备通信优先级

第一阶段建议只接这些命令：

- 扫描和绑定睡眠枕。
- 发送 Wi-Fi SSID/密码到 ESP32。
- 开始睡眠记录。
- 记录 30 分钟固定刺激计时：开始睡眠时自动开启，结束睡眠时自动停止。
- 接收实时心率、呼吸率和睡眠状态。
- 停止睡眠记录并生成本地报告。

## 当前原型状态

- HTML 里的睡眠记录状态保存在 `localStorage`，Android 侧也会用 `PrototypeStateStore` 镜像保存关键事件。
- 首页点击开始睡眠后，会同步启动助眠页里的 30 分钟固定刺激计时；结束睡眠时会同步停止这次计时。
- 问卷星链接会交给系统浏览器打开，填完后可以切回 App。
- 第一版报告不做 App 自己计算的综合评分，只展示睡眠效率等直接指标。
