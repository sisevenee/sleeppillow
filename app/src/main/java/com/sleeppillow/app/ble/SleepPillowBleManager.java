package com.sleeppillow.app.ble;

import android.Manifest;
import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelUuid;

import org.json.JSONException;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.Locale;
import java.util.UUID;

/**
 * 睡眠枕 BLE 通信入口。
 *
 * 当前实现支持扫描、连接、发现 GATT 服务、订阅 ESP32 状态通知，以及写入 Wi-Fi 凭据。
 * 睡眠采集和助眠控制仍等待下一版 ESP32 通信协议后再接入。
 */
public class SleepPillowBleManager {
    public static final String DEVICE_NAME_PREFIX = "SLEEPPILLOW";
    public static final UUID SERVICE_UUID = UUID.fromString("7f5c0001-5b7a-4f2e-9e1b-2a6d5c1f0001");
    public static final UUID WIFI_WRITE_UUID = UUID.fromString("7f5c0002-5b7a-4f2e-9e1b-2a6d5c1f0001");
    public static final UUID STATUS_NOTIFY_UUID = UUID.fromString("7f5c0003-5b7a-4f2e-9e1b-2a6d5c1f0001");
    private static final UUID GENERIC_ACCESS_UUID = UUID.fromString("00001800-0000-1000-8000-00805f9b34fb");
    private static final UUID DEVICE_NAME_UUID = UUID.fromString("00002a00-0000-1000-8000-00805f9b34fb");
    private static final UUID CCCD_UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");
    private static final long SCAN_TIMEOUT_MS = 15_000L;
    private static final int PREFERRED_MTU = 247;

    public interface Listener { void onBleEvent(String eventName, JSONObject payload); }

    private final Context appContext;
    private final Listener listener;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Runnable stopScanRunnable = this::stopScan;
    private BluetoothLeScanner scanner;
    private BluetoothGatt bluetoothGatt;
    private BluetoothGattCharacteristic wifiWriteCharacteristic;
    private BluetoothGattCharacteristic statusNotifyCharacteristic;
    private BluetoothGattCharacteristic deviceNameCharacteristic;
    private boolean scanning;
    private boolean classicReceiverRegistered;
    private boolean serviceDiscoveryRequested;

    public SleepPillowBleManager(Context context, Listener listener) {
        appContext = context.getApplicationContext();
        this.listener = listener;
    }

    /** 同时扫描所有 BLE 广播和传统蓝牙发现结果。 */
    @SuppressLint("MissingPermission")
    public void startScan() {
        if (!hasBluetoothPermission()) { emit("permission_required", "请允许蓝牙扫描和连接权限后重试。"); return; }
        BluetoothAdapter adapter = getAdapter();
        if (adapter == null || !adapter.isEnabled()) { emit("bluetooth_unavailable", "请先打开手机蓝牙。"); return; }
        scanner = adapter.getBluetoothLeScanner();
        stopScan();
        scanning = true;
        emit("scan_started", "正在搜索附近的蓝牙设备…");
        if (scanner != null) {
            scanner.startScan(Collections.emptyList(), new ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), scanCallback);
        }
        emitBondedDevices(adapter);
        registerClassicReceiver();
        adapter.cancelDiscovery();
        if (!adapter.startDiscovery() && scanner == null) {
            emit("scan_error", "手机无法启动蓝牙扫描。");
        }
        mainHandler.postDelayed(stopScanRunnable, SCAN_TIMEOUT_MS);
    }

    @SuppressLint("MissingPermission")
    public void stopScan() {
        mainHandler.removeCallbacks(stopScanRunnable);
        if (scanning && scanner != null) scanner.stopScan(scanCallback);
        BluetoothAdapter adapter = getAdapter();
        if (adapter != null && adapter.isDiscovering()) adapter.cancelDiscovery();
        unregisterClassicReceiver();
        if (scanning) emit("scan_finished", "扫描结束。");
        scanning = false;
    }

    /** 在 Activity 销毁时释放扫描和 GATT 连接资源。 */
    public void close() {
        stopScan();
        closeGatt();
    }

    /** 扫描结果的地址由页面传入，连接后自动发现服务并订阅配网状态。 */
    @SuppressLint("MissingPermission")
    public void connect(String deviceAddress) {
        if (!hasBluetoothPermission()) { emit("permission_required", "请允许蓝牙连接权限后重试。"); return; }
        if (deviceAddress == null || deviceAddress.trim().isEmpty()) { emit("connection_error", "未找到可连接的睡眠枕。"); return; }
        BluetoothAdapter adapter = getAdapter();
        if (adapter == null || !adapter.isEnabled()) { emit("bluetooth_unavailable", "请先打开手机蓝牙。"); return; }
        stopScan();
        closeGatt();
        try {
            emit("connecting", "正在连接睡眠枕…");
            bluetoothGatt = adapter.getRemoteDevice(deviceAddress).connectGatt(appContext, false, gattCallback, BluetoothDevice.TRANSPORT_LE);
        } catch (IllegalArgumentException error) { emit("connection_error", "设备地址无效，请重新扫描。"); }
    }

    /**
     * nRF Connect 已验证的 ESP32 配网协议为 UTF-8 文本：WIFI|SSID|PASSWORD。
     * Wi-Fi 密码只在内存中组装，绝不写日志或本地存储。
     */
    @SuppressLint("MissingPermission")
    public void sendWifiCredentials(String ssid, String password) {
        if (bluetoothGatt == null || wifiWriteCharacteristic == null) { emit("provisioning_error", "请先连接睡眠枕，再配置 Wi-Fi。"); return; }
        if (ssid == null || ssid.trim().isEmpty() || password == null || password.isEmpty()) { emit("provisioning_error", "请输入 Wi-Fi 名称和密码。"); return; }
        try {
            if (ssid.contains("|") || password.contains("|")) {
                emit("provisioning_error", "Wi-Fi 名称和密码暂不支持包含竖线字符。\n");
                return;
            }
            String credentials = "WIFI|" + ssid + "|" + password;
            wifiWriteCharacteristic.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
            wifiWriteCharacteristic.setValue(credentials.getBytes(StandardCharsets.UTF_8));
            if (bluetoothGatt.writeCharacteristic(wifiWriteCharacteristic)) emit("provisioning_sending", "正在将 Wi-Fi 信息发送给睡眠枕…");
            else emit("provisioning_error", "Wi-Fi 信息发送失败，请重新连接设备后重试。");
        } catch (RuntimeException error) { emit("provisioning_error", "Wi-Fi 信息发送失败，请重试。"); }
    }

    /** 查询 ESP32 当前真实 Wi-Fi 状态，用于设备已开机自动重连后的 App 状态恢复。 */
    @SuppressLint("MissingPermission")
    public void requestWifiStatus() {
        if (bluetoothGatt == null || wifiWriteCharacteristic == null) return;
        try {
            wifiWriteCharacteristic.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
            wifiWriteCharacteristic.setValue("WIFI_STATUS".getBytes(StandardCharsets.UTF_8));
            bluetoothGatt.writeCharacteristic(wifiWriteCharacteristic);
        } catch (RuntimeException ignored) {
            // 状态查询失败不影响已建立的 BLE 连接，也不覆盖页面的上一次状态。
        }
    }

    /**
     * 读取设备烧录在 eFuse 中派生出的 deviceId。
     * 只有 GATT 已真实连接且特征值已就绪时才会发送，不能由页面缓存的 MAC 地址替代。
     */
    @SuppressLint("MissingPermission")
    public void requestDeviceInfo() {
        // 蓝牙 GATT 同一时刻只能有一个特征值写入。连接就绪时页面会先请求 Wi-Fi 状态，
        // 因此稍后再写 DEVICE_INFO，避免两条命令互相覆盖。
        mainHandler.postDelayed(() -> {
            if (bluetoothGatt == null || wifiWriteCharacteristic == null) return;
            try {
                wifiWriteCharacteristic.setWriteType(BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
                wifiWriteCharacteristic.setValue("DEVICE_INFO".getBytes(StandardCharsets.UTF_8));
                bluetoothGatt.writeCharacteristic(wifiWriteCharacteristic);
            } catch (RuntimeException ignored) {
                // 设备信息查询失败不影响已经建立的 BLE 连接。
            }
        }, 350L);
    }

    private final ScanCallback scanCallback = new ScanCallback() {
        @Override
        @SuppressLint("MissingPermission")
        public void onScanResult(int callbackType, ScanResult result) {
            BluetoothDevice device = result.getDevice();
            String name = device.getName();
            // 某些 ESP32 只在广播包而非 BluetoothDevice 中携带设备名。
            if ((name == null || name.trim().isEmpty()) && result.getScanRecord() != null) {
                name = result.getScanRecord().getDeviceName();
            }
            boolean nameMatches = name != null && name.toUpperCase(Locale.US).startsWith(DEVICE_NAME_PREFIX);
            boolean serviceMatches = false;
            if (result.getScanRecord() != null && result.getScanRecord().getServiceUuids() != null) {
                for (ParcelUuid serviceUuid : result.getScanRecord().getServiceUuids()) {
                    if (SERVICE_UUID.equals(serviceUuid.getUuid())) {
                        serviceMatches = true;
                        break;
                    }
                }
            }
            // ESP32 可能只在连接后的 Generic Access 服务中提供名称，广播包中未必携带名称或
            // 自定义 Service UUID。因此先把全部 BLE 扫描结果交给用户选择，连接后再以 UUID 验证。
            JSONObject payload = payload(name);
            put(payload, "name", name == null || name.trim().isEmpty() ? "" : name);
            put(payload, "address", device.getAddress());
            put(payload, "rssi", result.getRssi());
            put(payload, "matchesSleepPillow", nameMatches || serviceMatches);
            put(payload, "transport", "BLE");
            emit("device_found", payload);
        }

        @Override
        public void onScanFailed(int errorCode) { emit("scan_error", "扫描失败（错误码 " + errorCode + "）。"); }
    };

    private final BroadcastReceiver classicDeviceReceiver = new BroadcastReceiver() {
        @Override
        @SuppressWarnings("deprecation")
        @SuppressLint("MissingPermission")
        public void onReceive(Context context, Intent intent) {
            if (!BluetoothDevice.ACTION_FOUND.equals(intent.getAction())) return;
            BluetoothDevice device = intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE);
            if (device == null) return;
            String name = device.getName();
            JSONObject payload = payload(name == null ? "" : name);
            put(payload, "name", name == null ? "" : name);
            put(payload, "address", device.getAddress());
            put(payload, "matchesSleepPillow", name != null && name.toUpperCase(Locale.US).startsWith(DEVICE_NAME_PREFIX));
            put(payload, "transport", "传统蓝牙");
            emit("device_found", payload);
        }
    };

    @SuppressLint("MissingPermission")
    private void emitBondedDevices(BluetoothAdapter adapter) {
        for (BluetoothDevice device : adapter.getBondedDevices()) {
            String name = device.getName();
            JSONObject payload = payload(name == null ? "" : name);
            put(payload, "name", name == null ? "" : name);
            put(payload, "address", device.getAddress());
            put(payload, "matchesSleepPillow", name != null && name.toUpperCase(Locale.US).startsWith(DEVICE_NAME_PREFIX));
            put(payload, "transport", "已配对设备");
            emit("device_found", payload);
        }
    }

    private void registerClassicReceiver() {
        if (classicReceiverRegistered) return;
        IntentFilter filter = new IntentFilter(BluetoothDevice.ACTION_FOUND);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            appContext.registerReceiver(classicDeviceReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            appContext.registerReceiver(classicDeviceReceiver, filter);
        }
        classicReceiverRegistered = true;
    }

    private void unregisterClassicReceiver() {
        if (!classicReceiverRegistered) return;
        appContext.unregisterReceiver(classicDeviceReceiver);
        classicReceiverRegistered = false;
    }

    private final BluetoothGattCallback gattCallback = new BluetoothGattCallback() {
        @Override
        @SuppressLint("MissingPermission")
        public void onConnectionStateChange(BluetoothGatt gatt, int status, int newState) {
            // 旧连接的延迟回调不能关闭用户刚刚重新连接的新 GATT 通道。
            if (gatt != bluetoothGatt) {
                gatt.close();
                return;
            }
            if (status != BluetoothGatt.GATT_SUCCESS) {
                emit("connection_error", "设备连接失败（错误码 " + status + "）。");
                // 断电、超出范围等情况经常以非成功状态返回；必须同时通知页面清除旧状态。
                emit("disconnected", "蓝牙连接已断开。");
                closeGatt();
            } else if (newState == BluetoothGatt.STATE_CONNECTED) {
                emit("connected", "蓝牙已连接，正在准备通信…");
                serviceDiscoveryRequested = false;
                if (!gatt.requestMtu(PREFERRED_MTU)) discoverServices(gatt);
            } else if (newState == BluetoothGatt.STATE_DISCONNECTED) {
                emit("disconnected", "蓝牙连接已断开。");
                closeGatt();
            }
        }

        @Override
        @SuppressLint("MissingPermission")
        public void onMtuChanged(BluetoothGatt gatt, int mtu, int status) {
            // 使用设备实际协商到的 MTU；即使低于 247 也继续发现服务。
            discoverServices(gatt);
        }

        @Override
        @SuppressLint("MissingPermission")
        public void onServicesDiscovered(BluetoothGatt gatt, int status) {
            if (status != BluetoothGatt.GATT_SUCCESS || gatt.getService(SERVICE_UUID) == null) {
                emit("connection_error", "设备协议不匹配，请检查 ESP32 固件 UUID。");
                return;
            }
            wifiWriteCharacteristic = gatt.getService(SERVICE_UUID).getCharacteristic(WIFI_WRITE_UUID);
            statusNotifyCharacteristic = gatt.getService(SERVICE_UUID).getCharacteristic(STATUS_NOTIFY_UUID);
            if (gatt.getService(GENERIC_ACCESS_UUID) != null) {
                deviceNameCharacteristic = gatt.getService(GENERIC_ACCESS_UUID).getCharacteristic(DEVICE_NAME_UUID);
            }
            if (wifiWriteCharacteristic == null || statusNotifyCharacteristic == null) {
                emit("connection_error", "设备缺少 Wi-Fi 配网所需特征值。");
                return;
            }
            enableStatusNotifications(gatt);
        }

        @Override
        public void onDescriptorWrite(BluetoothGatt gatt, BluetoothGattDescriptor descriptor, int status) {
            if (CCCD_UUID.equals(descriptor.getUuid()) && status == BluetoothGatt.GATT_SUCCESS) {
                if (deviceNameCharacteristic != null
                        && (deviceNameCharacteristic.getProperties() & BluetoothGattCharacteristic.PROPERTY_READ) != 0
                        && gatt.readCharacteristic(deviceNameCharacteristic)) {
                    return;
                }
                emit("ready", "设备已连接，可以配置 Wi-Fi。");
            }
            else if (CCCD_UUID.equals(descriptor.getUuid())) emit("connection_error", "无法订阅设备状态通知。");
        }

        @Override
        public void onCharacteristicRead(BluetoothGatt gatt, BluetoothGattCharacteristic characteristic, int status) {
            if (!DEVICE_NAME_UUID.equals(characteristic.getUuid())) return;
            if (status == BluetoothGatt.GATT_SUCCESS) {
                String deviceName = new String(characteristic.getValue(), StandardCharsets.UTF_8).trim();
                if (!deviceName.isEmpty()) emit("device_name", deviceName);
            }
            emit("ready", "设备已连接，可以配置 Wi-Fi。");
        }

        @Override
        public void onCharacteristicWrite(BluetoothGatt gatt, BluetoothGattCharacteristic characteristic, int status) {
            if (WIFI_WRITE_UUID.equals(characteristic.getUuid()) && status != BluetoothGatt.GATT_SUCCESS) {
                emit("provisioning_error", "设备未接收 Wi-Fi 信息，请重试。");
            }
        }

        @Override
        public void onCharacteristicChanged(BluetoothGatt gatt, BluetoothGattCharacteristic characteristic) {
            if (!STATUS_NOTIFY_UUID.equals(characteristic.getUuid())) return;
            String raw = new String(characteristic.getValue(), StandardCharsets.UTF_8).trim();
            JSONObject payload = payload(raw);
            put(payload, "raw", raw);
            try {
                JSONObject status = new JSONObject(raw);
                put(payload, "status", status.optString("status", ""));
                put(payload, "message", status.optString("message", raw));
            } catch (JSONException ignored) { put(payload, "status", raw); }
            emit("provisioning_status", payload);
        }
    };

    @SuppressLint("MissingPermission")
    private void discoverServices(BluetoothGatt gatt) {
        if (serviceDiscoveryRequested) return;
        serviceDiscoveryRequested = true;
        if (!gatt.discoverServices()) emit("connection_error", "无法读取设备蓝牙服务。");
    }

    @SuppressLint("MissingPermission")
    private void enableStatusNotifications(BluetoothGatt gatt) {
        if (!gatt.setCharacteristicNotification(statusNotifyCharacteristic, true)) {
            emit("connection_error", "无法开启设备状态通知。");
            return;
        }
        BluetoothGattDescriptor descriptor = statusNotifyCharacteristic.getDescriptor(CCCD_UUID);
        if (descriptor == null) { emit("connection_error", "设备缺少状态通知配置。"); return; }
        descriptor.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
        if (!gatt.writeDescriptor(descriptor)) emit("connection_error", "设备状态订阅失败。");
    }

    private BluetoothAdapter getAdapter() {
        BluetoothManager manager = appContext.getSystemService(BluetoothManager.class);
        return manager == null ? null : manager.getAdapter();
    }

    private boolean hasBluetoothPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            return appContext.checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED
                    && appContext.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED;
        }
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.M || appContext.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    @SuppressLint("MissingPermission")
    private void closeGatt() {
        wifiWriteCharacteristic = null;
        statusNotifyCharacteristic = null;
        deviceNameCharacteristic = null;
        serviceDiscoveryRequested = false;
        if (bluetoothGatt != null) { bluetoothGatt.close(); bluetoothGatt = null; }
    }

    private void emit(String eventName, String text) { emit(eventName, payload(text)); }
    private void emit(String eventName, JSONObject value) { if (listener != null) mainHandler.post(() -> listener.onBleEvent(eventName, value)); }
    private static JSONObject payload(String text) { JSONObject object = new JSONObject(); put(object, "message", text); return object; }
    private static void put(JSONObject object, String key, Object value) { try { object.put(key, value); } catch (JSONException ignored) { } }

    public void startSleepSession() {
        // TODO: 通知设备开始睡眠记录，并订阅实时数据通知。
        // 实时数据可以包括 heartRate、breathRate、sleepStage、timestamp 等字段。
        // 当前产品规则：开始睡眠记录时，App 侧也会同步开启一次 30 分钟固定刺激计时。
    }

    public void stopSleepSession() {
        // TODO: 通知设备结束睡眠记录，并同步缓存的整晚数据。
        // 同步完成后交给 data 包生成 SleepReport。
        // 当前产品规则：结束睡眠记录时，App 侧要同步停止本次固定刺激计时。
        // 由于第一版硬件仍然是实体开关控制，真实硬件是否停止要由用户关闭开关确认。
    }

    public void startFixedStimulationTimer(long startedAtMillis, long endAtMillis) {
        // TODO: 记录 App 侧的固定刺激计时开始事件。
        // 第一版硬件通过实体开关启动固定模式，App 不直接调节强度或模式。
        // 后续如果硬件支持 App 控制，可以在这里写入 BLE 命令，例如 START_STIMULATION。
    }

    public void stopFixedStimulationTimer(long stoppedAtMillis) {
        // TODO: 记录 App 侧的固定刺激计时停止事件。
        // 这个方法对应 HTML 原型里的 stim_timer_stopped：
        // 用户在首页或实时监测页确认结束睡眠后，本次助眠倒计时也要停止。
        // 如果未来硬件支持远程关闭刺激，可以在这里发送 BLE 停止命令。
    }
}
