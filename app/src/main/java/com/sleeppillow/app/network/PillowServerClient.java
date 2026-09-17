package com.sleeppillow.app.network;

import android.content.ContentResolver;
import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Calendar;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * App 到睡眠枕服务器的最小网络边界。
 *
 * 登录令牌只保存在 Android SharedPreferences，HTML 页面从不读取令牌；网页仅接收
 * 已脱敏的登录/设备/遥测结果。当前地址是开发测试使用的公网 HTTP 地址，正式发放前
 * 必须切换到 HTTPS 域名。
 */
public final class PillowServerClient {
    private static final String BASE_URL = "http://122.51.109.135:8080";
    private static final String PREFS_NAME = "pillow_server_session";
    private static final String KEY_ACCESS_TOKEN = "access_token";
    private static final String KEY_USERNAME = "username";
    private static final String KEY_ROLE = "role";
    private static final String KEY_CURRENT_DEVICE_ID = "current_device_id";
    private static final long REMOTE_SYNC_POLL_INTERVAL_MS = 20_000L;
    private static final int MAX_QUESTIONNAIRE_IMAGE_BYTES = 10 * 1024 * 1024;

    public interface Listener {
        void onServerEvent(String eventName, JSONObject payload);
    }

    private final SharedPreferences preferences;
    private final ContentResolver contentResolver;
    private final Listener listener;
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Runnable remoteSyncPoll = new Runnable() {
        @Override
        public void run() {
            String deviceId = preferences.getString(KEY_CURRENT_DEVICE_ID, "");
            if (hasSession() && !deviceId.isEmpty()) {
                executor.execute(() -> {
                    loadLatest(deviceId);
                    loadDeviceRecording();
                });
            }
            if (hasSession()) {
                mainHandler.postDelayed(this, REMOTE_SYNC_POLL_INTERVAL_MS);
            }
        }
    };

    public PillowServerClient(Context context, Listener listener) {
        preferences = context.getApplicationContext().getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        contentResolver = context.getApplicationContext().getContentResolver();
        this.listener = listener;
    }

    public void login(String username, String password) {
        if (username == null || username.trim().isEmpty() || password == null || password.isEmpty()) {
            emit("server_error", message("请输入账号和密码。"));
            return;
        }
        executor.execute(() -> {
            try {
                JSONObject request = new JSONObject();
                request.put("username", username.trim());
                request.put("password", password);
                JSONObject response = request("POST", "/api/v1/auth/login", request, null);
                String accessToken = response.optString("accessToken");
                if (accessToken.isEmpty()) throw new IOException("服务器没有返回登录会话");
                String role = response.optString("role", "user");
                preferences.edit()
                        .putString(KEY_ACCESS_TOKEN, accessToken)
                        .putString(KEY_USERNAME, response.optString("username"))
                        .putString(KEY_ROLE, role)
                        // 新登录账号不能复用前一账号保存在手机内的当前设备。
                        .remove(KEY_CURRENT_DEVICE_ID)
                        .apply();
                response.remove("accessToken");
                emit("server_login_success", response);
                loadQuestionnaireStatus();
                loadCurrentDevice();
            } catch (IOException | JSONException error) {
                emit("server_error", message(readableError(error)));
            }
        });
    }

    public void selectCurrentDevice(String deviceId) {
        if (!hasSession()) {
            emit("server_error", message("请先登录账号，再连接并选择睡眠枕。"));
            return;
        }
        executor.execute(() -> {
            try {
                JSONObject request = new JSONObject();
                request.put("deviceId", deviceId);
                JSONObject response = request("POST", "/api/v1/me/current-device", request, accessToken());
                preferences.edit().putString(KEY_CURRENT_DEVICE_ID, response.optString("deviceId", deviceId)).apply();
                emit("server_device_selected", response);
                loadLatest(deviceId);
                loadDeviceRecording();
                loadSleepReports();
                startRemoteSyncPolling();
            } catch (IOException | JSONException error) {
                emit("server_error", message(readableError(error)));
            }
        });
    }

    public void loadCurrentDevice() {
        if (!hasSession()) return;
        executor.execute(() -> {
            try {
                JSONObject response = request("GET", "/api/v1/me/current-device", null, accessToken());
                String deviceId = response.optString("deviceId");
                preferences.edit().putString(KEY_CURRENT_DEVICE_ID, deviceId).apply();
                emit("server_current_device", response);
                loadLatest(deviceId);
                loadDeviceRecording();
                loadSleepReports();
                startRemoteSyncPolling();
            } catch (IOException error) {
                if (error.getMessage().startsWith("HTTP 404")) {
                    preferences.edit().remove(KEY_CURRENT_DEVICE_ID).apply();
                    emit("server_no_current_device", message("尚未通过蓝牙选择睡眠枕。"));
                } else {
                    emit("server_error", message(readableError(error)));
                }
            }
        });
    }

    public void logout() {
        if (!hasSession()) return;
        mainHandler.removeCallbacks(remoteSyncPoll);
        executor.execute(() -> {
            try {
                request("POST", "/api/v1/auth/logout", new JSONObject(), accessToken());
            } catch (IOException ignored) {
                // 即使网络不可用也应清除本地会话，下一次登录会得到新会话。
            }
            preferences.edit().clear().apply();
            emit("server_logged_out", new JSONObject());
        });
    }

    public void close() {
        mainHandler.removeCallbacks(remoteSyncPoll);
        executor.shutdownNow();
    }

    /**
     * Loads completion state for the current sleep night.  Bedtime belongs to today while a
     * next-morning response belongs to yesterday's bedtime date; PSQI stays experiment-wide.
     */
    public void loadQuestionnaireStatus() {
        if (!hasSession()) return;
        executor.execute(() -> {
            try {
                JSONObject response = request(
                        "GET",
                        "/api/v1/me/questionnaires?preDate=" + formatChinaDate(0)
                                + "&postDate=" + formatChinaDate(-1),
                        null,
                        accessToken()
                );
                emit("server_questionnaire_status", response);
            } catch (IOException error) {
                emit("server_questionnaire_error", message(readableError(error)));
            }
        });
    }

    /** Stores a completed App questionnaire without exposing the login token to WebView JavaScript. */
    public void submitQuestionnaire(String questionnaireType, String responseDate, JSONObject answers) {
        if (!hasSession()) {
            emit("server_questionnaire_error", message("请先登录账号，再提交问卷。"));
            return;
        }
        if (questionnaireType == null || questionnaireType.trim().isEmpty() || answers == null || answers.length() == 0) {
            emit("server_questionnaire_error", message("问卷内容不完整，暂时无法提交。"));
            return;
        }
        executor.execute(() -> {
            try {
                JSONObject request = new JSONObject();
                request.put("questionnaireType", questionnaireType);
                request.put("responseDate", responseDate == null || responseDate.isEmpty() ? formatChinaDate() : responseDate);
                request.put("answers", answers);
                JSONObject response = request("POST", "/api/v1/me/questionnaires", request, accessToken());
                emit("server_questionnaire_submitted", response);
                // The response confirms this one submission; a status reload also refreshes all badges.
                loadQuestionnaireStatus();
            } catch (IOException | JSONException error) {
                emit("server_questionnaire_error", message(readableError(error)));
            }
        });
    }

    /** Uploads a conditional questionnaire image separately from the small JSON answer payload. */
    public void uploadQuestionnaireAttachment(
            String questionnaireType, String responseDate, String attachmentKey, Uri imageUri
    ) {
        if (!hasSession()) {
            emit("server_questionnaire_attachment_error", message("请先登录账号，再上传图片。"));
            return;
        }
        if (questionnaireType == null || questionnaireType.trim().isEmpty()
                || responseDate == null || responseDate.trim().isEmpty()
                || attachmentKey == null || attachmentKey.trim().isEmpty() || imageUri == null) {
            emit("server_questionnaire_attachment_error", message("请先选择需要上传的图片。"));
            return;
        }
        executor.execute(() -> {
            try {
                byte[] image = readQuestionnaireImage(imageUri);
                JSONObject response = requestImageAttachment(
                        questionnaireType.trim(), responseDate.trim(), attachmentKey.trim(), image, accessToken()
                );
                response.put("attachmentKey", attachmentKey.trim());
                emit("server_questionnaire_attachment_uploaded", response);
            } catch (IOException | JSONException error) {
                emit("server_questionnaire_attachment_error", message(readableError(error)));
            }
        });
    }

    /**
     * Records the participant's subjective preparation/wake time without changing the ESP32 data
     * session. Device power and telemetry alone define the actual monitoring start and end.
     */
    public void recordSleepMarker(String markerType, long markedAtMillis) {
        if (!hasSession()) return;
        if (!"prepare".equals(markerType) && !"wake".equals(markerType)) return;
        executor.execute(() -> {
            try {
                JSONObject request = new JSONObject();
                request.put("timestamp", formatUtcTimestamp(markedAtMillis));
                JSONObject response = request(
                        "POST", "/api/v1/me/sleep-markers/" + markerType, request, accessToken()
                );
                emit("server_sleep_marker_recorded", response);
            } catch (IOException | JSONException error) {
                // A local marker still remains visible in the App. It can be compared with the
                // automatic device session once the participant next has network access.
                emit("server_sleep_marker_error", message(readableError(error)));
            }
        });
    }

    private void loadLatest(String deviceId) {
        if (deviceId == null || deviceId.trim().isEmpty()) return;
        try {
            JSONObject response = request("GET", "/api/v1/devices/" + deviceId + "/latest", null, accessToken());
            emit("server_telemetry_latest", response);
        } catch (IOException error) {
            if (error.getMessage().startsWith("HTTP 404")) {
                emit("server_telemetry_missing", message("服务器尚未收到这台设备的数据。"));
            } else {
                emit("server_remote_sync_error", message(readableError(error)));
            }
        }
    }

    /** Reads whether the selected ESP32 is actively producing a continuous monitoring record. */
    private void loadDeviceRecording() {
        if (!hasSession() || preferences.getString(KEY_CURRENT_DEVICE_ID, "").isEmpty()) return;
        try {
            JSONObject response = request("GET", "/api/v1/me/device-recording", null, accessToken());
            emit("server_device_recording", response);
        } catch (IOException error) {
            if (error.getMessage().startsWith("HTTP 404")) {
                emit("server_no_current_device", message("尚未通过蓝牙选择睡眠枕。"));
            } else {
                emit("server_device_recording_error", message(readableError(error)));
            }
        }
    }

    private void loadSleepReports() {
        loadSleepReports(null, "home");
    }

    /** Loads a specific China-calendar day, week, or month without changing the home's latest summary. */
    public void loadSleepReportsForDate(String anchorDate, String viewMode) {
        if (anchorDate == null || anchorDate.trim().isEmpty()) {
            emit("server_sleep_reports_error", message("请选择有效的记录日期。"));
            return;
        }
        loadSleepReports(anchorDate.trim(), viewMode == null ? "day" : viewMode);
    }

    private void loadSleepReports(String anchorDate, String viewMode) {
        if (!hasSession() || preferences.getString(KEY_CURRENT_DEVICE_ID, "").isEmpty()) return;
        try {
            String path = "/api/v1/me/sleep-reports";
            if (anchorDate != null && !anchorDate.isEmpty()) path += "?date=" + anchorDate;
            JSONObject response = request("GET", path, null, accessToken());
            response.put("viewMode", viewMode);
            emit("server_sleep_reports", response);
        } catch (IOException | JSONException error) {
            // Report loading must not replace the latest-real-time data error state.
            JSONObject errorPayload = message(readableError(error));
            try {
                // HTML keeps day/week/month cursors independently, so an error must return
                // which cursor should be unlocked instead of guessing from the active tab.
                errorPayload.put("viewMode", viewMode);
            } catch (JSONException ignored) {
                // A valid message payload is still enough to show the failure to the user.
            }
            emit("server_sleep_reports_error", errorPayload);
        }
    }

    private static String formatUtcTimestamp(long timestampMillis) {
        SimpleDateFormat formatter = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US);
        formatter.setTimeZone(TimeZone.getTimeZone("UTC"));
        return formatter.format(new Date(timestampMillis));
    }

    private static String formatChinaDate() {
        return formatChinaDate(0);
    }

    private static String formatChinaDate(int offsetDays) {
        Calendar calendar = Calendar.getInstance(TimeZone.getTimeZone("Asia/Shanghai"), Locale.US);
        calendar.add(Calendar.DATE, offsetDays);
        SimpleDateFormat formatter = new SimpleDateFormat("yyyy-MM-dd", Locale.US);
        formatter.setTimeZone(TimeZone.getTimeZone("Asia/Shanghai"));
        return formatter.format(calendar.getTime());
    }

    private void startRemoteSyncPolling() {
        mainHandler.removeCallbacks(remoteSyncPoll);
        mainHandler.postDelayed(remoteSyncPoll, REMOTE_SYNC_POLL_INTERVAL_MS);
    }

    private JSONObject request(String method, String path, JSONObject body, String token) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(BASE_URL + path).openConnection();
        connection.setRequestMethod(method);
        connection.setConnectTimeout(8_000);
        connection.setReadTimeout(10_000);
        connection.setRequestProperty("Accept", "application/json");
        if (token != null && !token.isEmpty()) connection.setRequestProperty("Authorization", "Bearer " + token);
        if (body != null) {
            byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setFixedLengthStreamingMode(bytes.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(bytes);
            }
        }
        int status = connection.getResponseCode();
        String responseText = readFully(status >= 200 && status < 300 ? connection.getInputStream() : connection.getErrorStream());
        if (status < 200 || status >= 300) throw new IOException("HTTP " + status + (responseText.isEmpty() ? "" : ": " + responseText));
        try {
            return new JSONObject(responseText);
        } catch (JSONException error) {
            throw new IOException("服务器返回的数据格式无效", error);
        } finally {
            connection.disconnect();
        }
    }

    private byte[] readQuestionnaireImage(Uri imageUri) throws IOException {
        try (InputStream input = contentResolver.openInputStream(imageUri);
             ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            if (input == null) throw new IOException("无法读取所选图片");
            byte[] buffer = new byte[16 * 1024];
            int total = 0;
            int count;
            while ((count = input.read(buffer)) != -1) {
                total += count;
                if (total > MAX_QUESTIONNAIRE_IMAGE_BYTES) {
                    throw new IOException("图片不能超过 10 MB");
                }
                output.write(buffer, 0, count);
            }
            if (total == 0) throw new IOException("所选图片为空");
            return output.toByteArray();
        }
    }

    private JSONObject requestImageAttachment(
            String questionnaireType, String responseDate, String attachmentKey, byte[] image, String token
    ) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(
                BASE_URL + "/api/v1/me/questionnaire-attachments"
        ).openConnection();
        try {
            connection.setRequestMethod("POST");
            connection.setConnectTimeout(8_000);
            connection.setReadTimeout(30_000);
            connection.setDoOutput(true);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Content-Type", "application/octet-stream");
            connection.setRequestProperty("Authorization", "Bearer " + token);
            connection.setRequestProperty("X-Questionnaire-Type", questionnaireType);
            connection.setRequestProperty("X-Questionnaire-Response-Date", responseDate);
            connection.setRequestProperty("X-Questionnaire-Attachment-Key", attachmentKey);
            connection.setFixedLengthStreamingMode(image.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(image);
            }
            int status = connection.getResponseCode();
            String responseText = readFully(status >= 200 && status < 300
                    ? connection.getInputStream() : connection.getErrorStream());
            if (status < 200 || status >= 300) {
                throw new IOException("HTTP " + status + (responseText.isEmpty() ? "" : ": " + responseText));
            }
            try {
                return new JSONObject(responseText);
            } catch (JSONException error) {
                throw new IOException("服务器返回的数据格式无效", error);
            }
        } finally {
            connection.disconnect();
        }
    }

    private boolean hasSession() {
        return !accessToken().isEmpty();
    }

    private String accessToken() {
        return preferences.getString(KEY_ACCESS_TOKEN, "");
    }

    private void emit(String eventName, JSONObject payload) {
        if (listener != null) listener.onServerEvent(eventName, payload);
    }

    private static JSONObject message(String text) {
        JSONObject payload = new JSONObject();
        try { payload.put("message", text); } catch (JSONException ignored) { }
        return payload;
    }

    private static String readableError(Exception error) {
        String value = error.getMessage() == null ? "网络请求失败，请检查网络后重试。" : error.getMessage();
        if (value.startsWith("HTTP 401")) return "账号或密码错误，或登录已失效。";
        if (value.startsWith("HTTP 403")) return "当前账号没有此操作权限。";
        if (value.startsWith("HTTP 404")) return "服务器未找到对应设备或数据。";
        return value.length() > 140 ? "服务器请求失败，请稍后重试。" : value;
    }

    private static String readFully(InputStream stream) throws IOException {
        if (stream == null) return "";
        StringBuilder result = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) result.append(line);
        }
        return result.toString();
    }
}
