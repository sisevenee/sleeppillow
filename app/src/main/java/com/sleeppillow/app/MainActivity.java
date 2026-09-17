package com.sleeppillow.app;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlarmManager;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.util.Log;
import android.view.WindowInsets;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import com.sleeppillow.app.ble.SleepPillowBleManager;
import com.sleeppillow.app.data.PrototypeStateStore;
import com.sleeppillow.app.network.PillowServerClient;
import com.sleeppillow.app.notifications.QuestionnaireReminderScheduler;

import org.json.JSONException;
import org.json.JSONObject;

/**
 * App 当前的唯一入口页面。
 *
 * 这一版 Android 代码仍然采用“原生 Android 外壳 + HTML 原型”的方式：
 * - Android 层负责权限、WebView 容器、外部链接、生命周期、原型事件记录，以及以后接 BLE/服务器的入口。
 * - HTML 层负责你现在看到的首页、助眠、睡眠记录、问卷、日夜模式等界面。
 *
 * 这样做的好处是：页面可以快速迭代，后面硬件和服务器接入时也有原生代码位置可以承接。
 */
public class MainActivity extends Activity {
    private static final String TAG = "SleepPillow";
    private static final int PERMISSION_REQUEST_CODE = 1001;
    private static final int QUESTIONNAIRE_IMAGE_CHOOSER_REQUEST_CODE = 1002;

    /**
     * Android assets 目录在 WebView 里的固定地址。
     *
     * 文件实际位置：
     * app/src/main/assets/sleep-pillow.html
     */
    private static final String PROTOTYPE_URL = "file:///android_asset/sleep-pillow.html";

    /**
     * HTML 通过 window.SleepPillowNative 调用这个名字对应的原生对象。
     *
     * HTML 的“准备入睡”会通知这里以保存主观时间标记；设备是否开始采集
     * 由 ESP32 上电及其遥测数据决定，再由服务器同步到页面。
     */
    private static final String NATIVE_BRIDGE_NAME = "SleepPillowNative";
    /** Notification tap action and extra. The receiver uses these to open the correct questionnaire. */
    public static final String ACTION_OPEN_QUESTIONNAIRE = "com.sleeppillow.app.action.OPEN_QUESTIONNAIRE";
    public static final String EXTRA_QUESTIONNAIRE_TYPE = "questionnaire_type";

    private WebView prototypeWebView;
    private PrototypeStateStore prototypeStateStore;
    private SleepPillowBleManager sleepPillowBleManager;
    private PillowServerClient pillowServerClient;
    private QuestionnaireReminderScheduler questionnaireReminderScheduler;
    private int bottomSystemInsetCssPx = 0;
    private String pendingQuestionnaireType;
    private boolean openExactAlarmSettingsAfterNotificationPermission;
    private ValueCallback<Uri[]> questionnaireImageChooserCallback;
    private Uri selectedQuestionnaireImageUri;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        captureQuestionnaireNotificationIntent(getIntent());

        // BLE 扫描/连接需要运行时权限。当前 HTML 原型不依赖真实 BLE，
        // 所以即使用户拒绝权限，原型页面也可以继续展示。
        requestBlePermissions();

        prototypeStateStore = new PrototypeStateStore(this);
        questionnaireReminderScheduler = new QuestionnaireReminderScheduler(this);
        questionnaireReminderScheduler.rescheduleAll();
        sleepPillowBleManager = new SleepPillowBleManager(this, this::sendBleEventToPrototype);
        pillowServerClient = new PillowServerClient(this, this::sendServerEventToPrototype);
        prototypeWebView = createPrototypeWebView();
        setContentView(prototypeWebView);
        prototypeWebView.requestApplyInsets();

        if (savedInstanceState == null) {
            // 第一次启动时加载原型页面。
            prototypeWebView.loadUrl(PROTOTYPE_URL);
        } else {
            // 屏幕旋转或系统重建 Activity 时，尽量恢复 WebView 当前页面状态。
            prototypeWebView.restoreState(savedInstanceState);
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        if (prototypeWebView != null) {
            // 保存 WebView 的浏览栈和表单状态，减少旋转/重建时的跳页感。
            prototypeWebView.saveState(outState);
        }
        super.onSaveInstanceState(outState);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != QUESTIONNAIRE_IMAGE_CHOOSER_REQUEST_CODE) return;
        Uri[] selected = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
        // The form has one current conditional image field. Its URI stays inside the Android layer;
        // JavaScript receives only the server-issued attachment ID after upload succeeds.
        selectedQuestionnaireImageUri = selected != null && selected.length > 0 ? selected[0] : null;
        if (questionnaireImageChooserCallback != null) {
            questionnaireImageChooserCallback.onReceiveValue(selected);
            questionnaireImageChooserCallback = null;
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        // 用户从“闹钟和提醒”系统设置返回时，立即按新的权限状态重新安排为精确定时。
        if (questionnaireReminderScheduler != null) questionnaireReminderScheduler.rescheduleAll();
        if (prototypeWebView != null) {
            // WebView 回到前台时恢复页面脚本/音频。
            prototypeWebView.onResume();
            prototypeWebView.requestApplyInsets();

            // HTML 里的睡眠计时和助眠倒计时按真实时间戳计算。
            // App 从后台回来时主动刷新一次，避免页面停留在旧数字上。
            prototypeWebView.evaluateJavascript(
                    "window.SleepPillowPrototype && window.SleepPillowPrototype.refreshFromNative && window.SleepPillowPrototype.refreshFromNative();",
                    null
            );
            if (questionnaireReminderScheduler != null) {
                sendNativeEventToPrototype("questionnaire_reminder_status", questionnaireReminderScheduler.statusPayload());
            }
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        captureQuestionnaireNotificationIntent(intent);
        openQuestionnaireFromNotificationIfNeeded();
    }

    @Override
    protected void onPause() {
        if (prototypeWebView != null) {
            // onPause 会让 WebView 进入暂停态。注意：这不影响 ESP32 的设备记录。
            // 用户准备入睡状态保存在 localStorage，设备记录由服务器遥测状态判断。
            prototypeWebView.onPause();
        }
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        if (sleepPillowBleManager != null) {
            sleepPillowBleManager.close();
        }
        if (pillowServerClient != null) {
            pillowServerClient.close();
        }
        if (prototypeWebView != null) {
            // Activity 销毁时释放 WebView，避免长期持有页面和 Activity 引用。
            prototypeWebView.destroy();
            prototypeWebView = null;
        }
        super.onDestroy();
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (prototypeWebView == null) {
            super.onBackPressed();
            return;
        }

        /*
         * Android 系统返回键默认会直接退出 Activity。
         * 但现在页面是一个单页 HTML App，内部有自己的页面栈：
         * 例如“首页 -> 实时监测 -> 返回首页”不应改变准备入睡标记或设备记录。
         *
         * 所以这里先问 HTML：你能不能自己处理返回？
         * - 能处理：HTML 自己回到上一页。
         * - 不能处理：再交给 Android，退出 App。
         */
        prototypeWebView.evaluateJavascript(
                "(function(){"
                        + "if(window.SleepPillowPrototype && window.SleepPillowPrototype.canGoBack && window.SleepPillowPrototype.canGoBack()){"
                        + "window.SleepPillowPrototype.back();return true;"
                        + "}"
                        + "return false;"
                        + "})()",
                handled -> {
                    if (!"true".equals(handled)) {
                        finishFromSystemBack();
                    }
                }
        );
    }

    private void finishFromSystemBack() {
        // 放在单独方法里，是为了避免在 evaluateJavascript 的回调里直接写 super 调用。
        // 读起来更清楚，也方便以后改成“二次确认退出 App”。
        super.onBackPressed();
    }

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    private WebView createPrototypeWebView() {
        WebView webView = new WebView(this);
        WebSettings settings = webView.getSettings();

        // HTML 原型里有页面切换、问卷状态、睡眠计时等脚本逻辑，所以必须开启 JavaScript。
        settings.setJavaScriptEnabled(true);

        // 开启 DOM Storage 后，HTML 里的 localStorage 才能保存：
        // 问卷完成状态、日夜模式、睡眠记录是否正在进行、助眠倒计时结束时间等。
        settings.setDomStorageEnabled(true);

        // 当前助眠音频只是原型按钮；未来接真实音频时，这个设置允许 App 内播放更自然。
        settings.setMediaPlaybackRequiresUserGesture(false);

        // Android 15 以后更强调沉浸式/边到边显示，部分手机会让 WebView 内容伸到系统手势条下面。
        // 这里把系统底部导航栏高度传给 HTML，让底部 Tab 自动往上留出安全距离。
        installSystemBarInsetBridge(webView);

        // 这个桥接对象给 HTML 调用。
        // 现在会把事件保存到 Android 本地状态，并分发给 BLE 管理类里的占位方法。
        webView.addJavascriptInterface(
                new PrototypeBridge(prototypeStateStore, sleepPillowBleManager, questionnaireReminderScheduler),
                NATIVE_BRIDGE_NAME
        );

        // 自定义页面加载规则：本地原型继续留在 App 内，问卷星等 https 链接交给系统浏览器。
        webView.setWebViewClient(new PrototypeWebViewClient());
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(
                    WebView view, ValueCallback<Uri[]> filePathCallback, FileChooserParams fileChooserParams
            ) {
                if (questionnaireImageChooserCallback != null) {
                    questionnaireImageChooserCallback.onReceiveValue(null);
                }
                questionnaireImageChooserCallback = filePathCallback;
                Intent chooserIntent = new Intent(Intent.ACTION_OPEN_DOCUMENT)
                        .addCategory(Intent.CATEGORY_OPENABLE)
                        .setType("image/*")
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
                try {
                    startActivityForResult(chooserIntent, QUESTIONNAIRE_IMAGE_CHOOSER_REQUEST_CODE);
                    return true;
                } catch (ActivityNotFoundException error) {
                    questionnaireImageChooserCallback = null;
                    filePathCallback.onReceiveValue(null);
                    Log.w(TAG, "No image picker is available", error);
                    return false;
                }
            }
        });

        return webView;
    }

    private final class PrototypeWebViewClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            return shouldOpenOutsideApp(request.getUrl());
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            super.onPageFinished(view, url);

            // 页面每次重新加载后，CSS 变量会恢复默认值，所以这里重新注入一次底部安全区。
            applyBottomSafeAreaToPrototype(view, bottomSystemInsetCssPx);
            openQuestionnaireFromNotificationIfNeeded();
            if (questionnaireReminderScheduler != null) {
                sendNativeEventToPrototype("questionnaire_reminder_status", questionnaireReminderScheduler.statusPayload());
            }
        }

        @Override
        @SuppressWarnings("deprecation")
        public boolean shouldOverrideUrlLoading(WebView view, String url) {
            return shouldOpenOutsideApp(Uri.parse(url));
        }
    }

    private boolean shouldOpenOutsideApp(Uri uri) {
        if (uri == null) {
            return false;
        }

        String scheme = uri.getScheme();
        boolean isWebLink = "http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme);
        if (!isWebLink) {
            return false;
        }

        // 问卷星链接、以后外部帮助文档等都走系统浏览器，用户填完问卷后回到 App 更清楚。
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (ActivityNotFoundException error) {
            Log.w(TAG, "No browser can open: " + uri, error);
        }
        return true;
    }

    private void installSystemBarInsetBridge(WebView webView) {
        webView.setOnApplyWindowInsetsListener((view, insets) -> {
            bottomSystemInsetCssPx = getNavigationBarBottomInsetCssPx(insets);
            applyBottomSafeAreaToPrototype(webView, bottomSystemInsetCssPx);
            return insets;
        });
    }

    private int getNavigationBarBottomInsetCssPx(WindowInsets insets) {
        if (insets == null) {
            return 0;
        }

        int bottomInsetPx;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            bottomInsetPx = insets.getInsets(WindowInsets.Type.navigationBars()).bottom;
        } else {
            bottomInsetPx = insets.getSystemWindowInsetBottom();
        }

        /*
         * WindowInsets 返回的是 Android 物理像素；HTML CSS 里使用的是 CSS px。
         * WebView 里 CSS px 大致等于 dp，所以要除以 density，否则高分辨率手机会被垫太高。
         */
        float density = getResources().getDisplayMetrics().density;
        return density <= 0 ? bottomInsetPx : Math.round(bottomInsetPx / density);
    }

    private void applyBottomSafeAreaToPrototype(WebView webView, int bottomInsetCssPx) {
        if (webView == null) {
            return;
        }

        String script;
        if (bottomInsetCssPx > 0) {
            script = "document.documentElement.style.setProperty('--android-safe-bottom','"
                    + bottomInsetCssPx
                    + "px');";
        } else {
            // 没拿到系统栏高度时，删掉变量，让 HTML 使用自己的 22px 兜底值。
            script = "document.documentElement.style.removeProperty('--android-safe-bottom');";
        }
        webView.evaluateJavascript(script, null);
    }

    private void captureQuestionnaireNotificationIntent(Intent intent) {
        if (intent != null && ACTION_OPEN_QUESTIONNAIRE.equals(intent.getAction())) {
            pendingQuestionnaireType = intent.getStringExtra(EXTRA_QUESTIONNAIRE_TYPE);
        }
    }

    /** Opens the local questionnaire page only after WebView has loaded its public JavaScript bridge. */
    private void openQuestionnaireFromNotificationIfNeeded() {
        if (prototypeWebView == null || pendingQuestionnaireType == null) return;
        String type = pendingQuestionnaireType;
        pendingQuestionnaireType = null;
        prototypeWebView.evaluateJavascript(
                "window.SleepPillowPrototype&&window.SleepPillowPrototype.openQuestionnairesFromNotification&&"
                        + "window.SleepPillowPrototype.openQuestionnairesFromNotification("
                        + JSONObject.quote(type)
                        + ");",
                null
        );
    }

    private void requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            openExactAlarmSettingsAfterNotificationPermission = true;
            runOnUiThread(() -> requestPermissions(
                    new String[] { Manifest.permission.POST_NOTIFICATIONS },
                    PERMISSION_REQUEST_CODE
            ));
            return;
        }
        requestExactAlarmPermissionIfNeeded();
    }

    /** Android 12+ may delay inexact alarms by a long window, so request the special exact-alarm access explicitly. */
    private void requestExactAlarmPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return;
        AlarmManager manager = (AlarmManager) getSystemService(ALARM_SERVICE);
        if (manager == null || manager.canScheduleExactAlarms()) return;
        try {
            Intent settings = new Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM)
                    .setData(Uri.parse("package:" + getPackageName()));
            startActivity(settings);
        } catch (ActivityNotFoundException error) {
            Log.w(TAG, "Exact alarm settings are unavailable", error);
        }
    }

    /** Opens Android's per-app battery-exemption confirmation for reliable user-requested reminders. */
    private void requestBackgroundReminderPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return;
        PowerManager manager = (PowerManager) getSystemService(POWER_SERVICE);
        if (manager != null && manager.isIgnoringBatteryOptimizations(getPackageName())) return;
        try {
            Intent settings = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                    .setData(Uri.parse("package:" + getPackageName()));
            startActivity(settings);
        } catch (ActivityNotFoundException error) {
            Log.w(TAG, "Battery optimization settings are unavailable", error);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == PERMISSION_REQUEST_CODE && openExactAlarmSettingsAfterNotificationPermission) {
            openExactAlarmSettingsAfterNotificationPermission = false;
            requestExactAlarmPermissionIfNeeded();
        }
    }

    /** Sends native BLE state changes back to the HTML prototype without exposing Android APIs to it. */
    private void sendBleEventToPrototype(String eventName, JSONObject payload) {
        if (prototypeWebView == null) {
            return;
        }
        String payloadJson = payload == null ? "{}" : payload.toString();
        String script = "window.SleepPillowPrototype&&window.SleepPillowPrototype.onBleEvent&&"
                + "window.SleepPillowPrototype.onBleEvent("
                + JSONObject.quote(eventName)
                + ","
                + JSONObject.quote(payloadJson)
                + ");";
        prototypeWebView.evaluateJavascript(script, null);
    }

    /** Sends server results to the local HTML UI without exposing the session token to JavaScript. */
    private void sendServerEventToPrototype(String eventName, JSONObject payload) {
        runOnUiThread(() -> {
            if (prototypeWebView == null) {
                return;
            }
            String payloadJson = payload == null ? "{}" : payload.toString();
            String script = "window.SleepPillowPrototype&&window.SleepPillowPrototype.onServerEvent&&"
                    + "window.SleepPillowPrototype.onServerEvent("
                    + JSONObject.quote(eventName)
                    + ","
                    + JSONObject.quote(payloadJson)
                    + ");";
            prototypeWebView.evaluateJavascript(script, null);
        });
    }

    /** Sends local Android notification status to the HTML settings page. */
    private void sendNativeEventToPrototype(String eventName, JSONObject payload) {
        runOnUiThread(() -> {
            if (prototypeWebView == null) return;
            String payloadJson = payload == null ? "{}" : payload.toString();
            String script = "window.SleepPillowPrototype&&window.SleepPillowPrototype.onNativeEvent&&"
                    + "window.SleepPillowPrototype.onNativeEvent("
                    + JSONObject.quote(eventName)
                    + ","
                    + JSONObject.quote(payloadJson)
                    + ");";
            prototypeWebView.evaluateJavascript(script, null);
        });
    }

    private final class PrototypeBridge {
        private final PrototypeStateStore stateStore;
        private final SleepPillowBleManager bleManager;
        private final PillowServerClient serverClient;
        private final QuestionnaireReminderScheduler reminderScheduler;

        PrototypeBridge(
                PrototypeStateStore stateStore,
                SleepPillowBleManager bleManager,
                QuestionnaireReminderScheduler reminderScheduler
        ) {
            this.stateStore = stateStore;
            this.bleManager = bleManager;
            this.serverClient = pillowServerClient;
            this.reminderScheduler = reminderScheduler;
        }

        /**
         * HTML 调用入口：
         * window.SleepPillowNative.onPrototypeEvent("sleep_session_started", "{\"startedAt\":...}")
         *
         * 当前做三件事：
         * 1. 把事件写入 SharedPreferences，便于 App 重启后查看最近状态。
         * 2. 分发给 SleepPillowBleManager 的占位方法，提前把“页面事件 -> 硬件动作”的边界搭好。
         * 3. 打印 Logcat 日志，方便开发时确认 HTML 和 Android 是否通信成功。
         *
         * 以后要接真实能力时，可以在这里继续扩展：
         * - sleep_session_started：记录用户准备入睡的主观时间；设备记录由 ESP32 上电后的遥测自动开始。
         * - sleep_session_ended：记录用户醒来的主观时间；设备记录由最后一条遥测自动结束。
         * - stim_timer_started：记录 30 分钟固定刺激的开始/结束时间。
         * - stim_timer_finished：30 分钟自然倒计时结束。
         * - stim_timer_stopped：记录用户手动结束一次 App 内助眠计时。
         */
        @JavascriptInterface
        public void onPrototypeEvent(String eventName, String payloadJson) {
            // Wi-Fi 凭据是敏感数据：只转给 BLE 管理器，不保存、不打印日志。
            if ("ble_provision_wifi".equals(eventName)) {
                dispatchPrototypeEventToBle(eventName, payloadJson);
                Log.d(TAG, "BLE provisioning request received");
                return;
            }
            if ("server_login".equals(eventName)) {
                if (serverClient != null) {
                    serverClient.login(readString(payloadJson, "username"), readString(payloadJson, "password"));
                }
                return;
            }
            if ("server_select_current_device".equals(eventName)) {
                if (serverClient != null) {
                    serverClient.selectCurrentDevice(readString(payloadJson, "deviceId"));
                }
                return;
            }
            if ("server_logout".equals(eventName)) {
                if (serverClient != null) {
                    serverClient.logout();
                }
                return;
            }
            if ("server_questionnaire_status_requested".equals(eventName)) {
                if (serverClient != null) {
                    serverClient.loadQuestionnaireStatus();
                }
                return;
            }
            if ("server_questionnaire_submit".equals(eventName)) {
                if (serverClient != null) {
                    try {
                        JSONObject payload = new JSONObject(payloadJson == null ? "{}" : payloadJson);
                        serverClient.submitQuestionnaire(
                                payload.optString("questionnaireType"),
                                payload.optString("responseDate"),
                                payload.optJSONObject("answers")
                        );
                    } catch (JSONException error) {
                        JSONObject errorPayload = new JSONObject();
                        try {
                            errorPayload.put("message", "问卷内容格式无效。");
                        } catch (JSONException ignored) {
                            // Keep a valid empty payload if construction unexpectedly fails.
                        }
                        sendServerEventToPrototype("server_questionnaire_error", errorPayload);
                    }
                }
                return;
            }
            if ("server_questionnaire_attachment_upload".equals(eventName)) {
                if (serverClient == null) return;
                if (selectedQuestionnaireImageUri == null) {
                    JSONObject errorPayload = new JSONObject();
                    try {
                        errorPayload.put("message", "请先选择参数调整图片。");
                    } catch (JSONException ignored) {
                        // Keep a valid empty payload if JSON construction unexpectedly fails.
                    }
                    sendServerEventToPrototype("server_questionnaire_attachment_error", errorPayload);
                    return;
                }
                serverClient.uploadQuestionnaireAttachment(
                        readString(payloadJson, "questionnaireType"),
                        readString(payloadJson, "responseDate"),
                        readString(payloadJson, "attachmentKey"),
                        selectedQuestionnaireImageUri
                );
                return;
            }
            if ("server_sleep_reports_for_date".equals(eventName)) {
                if (serverClient != null) {
                    serverClient.loadSleepReportsForDate(
                            readString(payloadJson, "date"),
                            readString(payloadJson, "viewMode")
                    );
                }
                return;
            }
            if (PrototypeStateStore.EVENT_SLEEP_SESSION_STARTED.equals(eventName)) {
                if (serverClient != null) {
                    serverClient.recordSleepMarker("prepare", PrototypeStateStore.readLong(
                            payloadJson, "startedAt", System.currentTimeMillis()
                    ));
                }
                // Continue below so the established local marker state stays in sync.
            }
            if (PrototypeStateStore.EVENT_SLEEP_SESSION_ENDED.equals(eventName)) {
                if (serverClient != null) {
                    serverClient.recordSleepMarker("wake", PrototypeStateStore.readLong(
                            payloadJson, "endedAt", System.currentTimeMillis()
                    ));
                }
                // The App still records the local marker if the phone is temporarily offline.
            }
            if ("questionnaire_reminders_updated".equals(eventName)) {
                if (reminderScheduler != null) {
                    reminderScheduler.updateReminderTimes(
                            readString(payloadJson, "bedReminder"),
                            readString(payloadJson, "morningReminder")
                    );
                }
                requestNotificationPermissionIfNeeded();
                if (reminderScheduler != null) {
                    sendNativeEventToPrototype("questionnaire_reminder_status", reminderScheduler.statusPayload());
                }
                return;
            }
            if ("questionnaire_reminder_status_requested".equals(eventName)) {
                requestNotificationPermissionIfNeeded();
                if (reminderScheduler != null) {
                    sendNativeEventToPrototype("questionnaire_reminder_status", reminderScheduler.statusPayload());
                }
                return;
            }
            if ("questionnaire_reminder_test".equals(eventName)) {
                requestNotificationPermissionIfNeeded();
                boolean sent = reminderScheduler != null && reminderScheduler.sendTestNotification();
                JSONObject result = new JSONObject();
                try {
                    result.put("sent", sent);
                } catch (JSONException ignored) {
                    // Preserve a valid empty payload if JSON construction ever fails.
                }
                sendNativeEventToPrototype("questionnaire_reminder_test_sent", result);
                if (reminderScheduler != null) {
                    sendNativeEventToPrototype("questionnaire_reminder_status", reminderScheduler.statusPayload());
                }
                return;
            }
            if ("questionnaire_reminder_delayed_test".equals(eventName)) {
                if (reminderScheduler != null) {
                    sendNativeEventToPrototype("questionnaire_reminder_delayed_test_scheduled",
                            reminderScheduler.scheduleDelayedTestNotification());
                    sendNativeEventToPrototype("questionnaire_reminder_status", reminderScheduler.statusPayload());
                }
                return;
            }
            if ("questionnaire_background_reminder_requested".equals(eventName)) {
                requestBackgroundReminderPermissionIfNeeded();
                if (reminderScheduler != null) {
                    sendNativeEventToPrototype("questionnaire_reminder_status", reminderScheduler.statusPayload());
                }
                return;
            }
            if ("questionnaire_completion_changed".equals(eventName)) {
                if (reminderScheduler != null) {
                    reminderScheduler.setQuestionnaireCompleted(
                            readString(payloadJson, "type"),
                            readString(payloadJson, "date"),
                            readBoolean(payloadJson, "completed")
                    );
                }
                return;
            }
            stateStore.handlePrototypeEvent(eventName, payloadJson);
            dispatchPrototypeEventToBle(eventName, payloadJson);

            /*
             * 等服务器接口确定后，可以在这里根据 eventName 调用：
             * - POST /sleep-sessions：创建一次睡眠记录。
             * - PATCH /sleep-sessions/{id}/end：结束记录并上传摘要。
             * - POST /stimulations：记录本次 30 分钟固定刺激开始/结束/停止。
             */
            Log.d(TAG, "Prototype event: " + eventName + " " + payloadJson
                    + " | " + stateStore.getDebugSummary());
        }

        private void dispatchPrototypeEventToBle(String eventName, String payloadJson) {
            if (eventName == null || bleManager == null) {
                return;
            }

            switch (eventName) {
                case "ble_scan":
                    bleManager.startScan();
                    break;
                case "ble_connect":
                    bleManager.connect(readString(payloadJson, "address"));
                    break;
                case "ble_provision_wifi":
                    bleManager.sendWifiCredentials(
                            readString(payloadJson, "ssid"),
                            readString(payloadJson, "password")
                    );
                    break;
                case "ble_get_wifi_status":
                    bleManager.requestWifiStatus();
                    break;
                case "ble_get_device_info":
                    // 设备 ID 必须由当前实际连接的 ESP32 返回，不能信任页面缓存的名称或 MAC 地址。
                    bleManager.requestDeviceInfo();
                    break;
                case PrototypeStateStore.EVENT_SLEEP_SESSION_STARTED:
                    // ESP32 上电就开始采集；此处只保留用户“准备入睡”的本地标记。
                    break;
                case PrototypeStateStore.EVENT_SLEEP_SESSION_ENDED:
                    // ESP32 断电或停止上传才结束数据记录；这里不向设备发送停止命令。
                    break;
                case PrototypeStateStore.EVENT_STIM_TIMER_STARTED:
                    // 第一版只是 App 侧 30 分钟倒计时；真实硬件仍由实体开关固定模式运行。
                    long startedAt = PrototypeStateStore.readLong(
                            payloadJson,
                            "startedAt",
                            System.currentTimeMillis()
                    );
                    long endAt = PrototypeStateStore.readLong(
                            payloadJson,
                            "endAt",
                            startedAt + 30 * 60 * 1000L
                    );
                    bleManager.startFixedStimulationTimer(
                            startedAt,
                            endAt
                    );
                    break;
                case PrototypeStateStore.EVENT_STIM_TIMER_STOPPED:
                    // 只有 App 明确发送停止计时事件时，才停止本地 30 分钟倒计时。
                    bleManager.stopFixedStimulationTimer(
                            PrototypeStateStore.readLong(payloadJson, "stoppedAt", System.currentTimeMillis())
                    );
                    break;
                case PrototypeStateStore.EVENT_STIM_TIMER_FINISHED:
                    // 30 分钟自然结束时，目前不需要给第一版硬件下发命令，先只保留本地状态。
                    break;
                default:
                    // 其他原型事件先忽略，后面新增功能时再按需接入。
                    break;
            }
        }

        private String readString(String payloadJson, String key) {
            try {
                return new JSONObject(payloadJson == null ? "{}" : payloadJson).optString(key, "");
            } catch (JSONException error) {
                return "";
            }
        }

        private boolean readBoolean(String payloadJson, String key) {
            try {
                return new JSONObject(payloadJson == null ? "{}" : payloadJson).optBoolean(key, false);
            } catch (JSONException error) {
                return false;
            }
        }
    }

    private void requestBlePermissions() {
        // Android 12(API 31) 开始，蓝牙扫描和连接权限变成 BLUETOOTH_SCAN/CONNECT。
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            requestIfMissing(new String[] {
                    Manifest.permission.BLUETOOTH_SCAN,
                    Manifest.permission.BLUETOOTH_CONNECT
            });
        // Android 6(API 23) 到 Android 11(API 30)，BLE 扫描通常需要定位权限。
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            requestIfMissing(new String[] {
                    Manifest.permission.ACCESS_FINE_LOCATION
            });
        }
    }

    private void requestIfMissing(String[] permissions) {
        for (String permission : permissions) {
            if (checkSelfPermission(permission) != PackageManager.PERMISSION_GRANTED) {
                // 只要发现任意一个权限缺失，就一次性请求这一组权限。
                requestPermissions(permissions, PERMISSION_REQUEST_CODE);
                return;
            }
        }
    }
}
