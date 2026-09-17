package com.sleeppillow.app.notifications;

import android.Manifest;
import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.PowerManager;

import com.sleeppillow.app.MainActivity;
import com.sleeppillow.app.R;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.Calendar;
import java.util.Locale;
import java.util.TimeZone;

/**
 * Keeps daily questionnaire reminders in Android native storage so they still work when WebView is closed.
 * The notification is only delivered when the corresponding questionnaire has not been marked complete.
 */
public final class QuestionnaireReminderScheduler {
    public static final String TYPE_BED = "bed";
    public static final String TYPE_MORNING = "morning";
    public static final String TYPE_SCHEDULED_TEST = "scheduled_test";

    private static final String PREFS_NAME = "questionnaire_reminders";
    private static final String KEY_BED_TIME = "bed_time";
    private static final String KEY_MORNING_TIME = "morning_time";
    private static final String KEY_DONE_PREFIX = "done_";
    private static final String KEY_LAST_DELIVERY = "last_delivery";
    private static final String KEY_NEXT_BED = "next_bed";
    private static final String KEY_NEXT_MORNING = "next_morning";
    private static final String KEY_SCHEDULED_TEST = "scheduled_test";
    // Android will not raise the importance of an existing channel after an app update.
    private static final String CHANNEL_ID = "questionnaire_reminders_v2";

    private final Context appContext;
    private final SharedPreferences preferences;

    public QuestionnaireReminderScheduler(Context context) {
        appContext = context.getApplicationContext();
        preferences = appContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        createNotificationChannel();
    }

    public void updateReminderTimes(String bedTime, String morningTime) {
        preferences.edit()
                .putString(KEY_BED_TIME, validTimeOrDefault(bedTime, "22:30"))
                .putString(KEY_MORNING_TIME, validTimeOrDefault(morningTime, "07:30"))
                .apply();
        rescheduleAll();
    }

    public void setQuestionnaireCompleted(String type, String localDate, boolean completed) {
        if (!TYPE_BED.equals(type) && !TYPE_MORNING.equals(type)) return;
        String key = KEY_DONE_PREFIX + type + "_" + localDate;
        if (completed) preferences.edit().putBoolean(key, true).apply();
        else preferences.edit().remove(key).apply();
    }

    public void rescheduleAll() {
        schedule(TYPE_BED, preferences.getString(KEY_BED_TIME, "22:30"));
        schedule(TYPE_MORNING, preferences.getString(KEY_MORNING_TIME, "07:30"));
    }

    public void deliverReminder(String type) {
        if (TYPE_SCHEDULED_TEST.equals(type)) {
            if (!canPostNotifications()) {
                recordDelivery(type, "通知权限或通知渠道未开启");
                return;
            }
            postQuestionnaireNotification(TYPE_BED, true);
            recordDelivery(type, "已发送");
            return;
        }
        if (!TYPE_BED.equals(type) && !TYPE_MORNING.equals(type)) return;
        // Every alarm schedules its next occurrence first, including days where the questionnaire is already done.
        schedule(type, timeFor(type));
        // Sleep diaries share the bedtime date: a morning reminder checks last night's form.
        if (preferences.getBoolean(KEY_DONE_PREFIX + type + "_" + questionnaireDateFor(type), false)) {
            recordDelivery(type, "对应睡眠夜已填写，未提醒");
            return;
        }
        if (!canPostNotifications()) {
            recordDelivery(type, "通知权限或通知渠道未开启");
            return;
        }
        postQuestionnaireNotification(type, false);
        recordDelivery(type, "已发送");
    }

    /** Posts a notification immediately so the reminder configuration can be verified without waiting. */
    public boolean sendTestNotification() {
        if (!canPostNotifications()) return false;
        postQuestionnaireNotification(TYPE_BED, true);
        return true;
    }

    /** Schedules a short real AlarmManager test, distinct from the immediate notification button. */
    public JSONObject scheduleDelayedTestNotification() {
        Calendar trigger = Calendar.getInstance();
        trigger.add(Calendar.MINUTE, 2);
        trigger.set(Calendar.SECOND, 0);
        trigger.set(Calendar.MILLISECOND, 0);
        // Avoid accidentally scheduling for the already elapsed current minute at an exact minute boundary.
        if (trigger.getTimeInMillis() <= System.currentTimeMillis()) trigger.add(Calendar.MINUTE, 1);

        Intent alarm = new Intent(appContext, QuestionnaireReminderReceiver.class)
                .setAction(QuestionnaireReminderReceiver.ACTION_REMINDER)
                .putExtra(QuestionnaireReminderReceiver.EXTRA_TYPE, TYPE_SCHEDULED_TEST);
        PendingIntent pendingIntent = PendingIntent.getBroadcast(
                appContext, 103, alarm, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        schedulePendingIntent(trigger.getTimeInMillis(), pendingIntent);
        String scheduledFor = formatDateTime(trigger);
        preferences.edit().putString(KEY_SCHEDULED_TEST, scheduledFor).apply();
        JSONObject payload = new JSONObject();
        try {
            payload.put("scheduledFor", scheduledFor);
        } catch (JSONException ignored) {
            // Return a valid empty payload if JSON construction ever fails.
        }
        return payload;
    }

    /** Returns the native values actually used by AlarmManager, for display in the settings page. */
    public JSONObject statusPayload() {
        JSONObject status = new JSONObject();
        try {
            status.put("notificationsAllowed", canPostNotifications());
            status.put("exactAlarmsAllowed", exactAlarmsAllowed());
            status.put("backgroundReminderAllowed", backgroundReminderAllowed());
            status.put("bedReminder", timeFor(TYPE_BED));
            status.put("morningReminder", timeFor(TYPE_MORNING));
            status.put("bedQuestionnaireDate", questionnaireDateFor(TYPE_BED));
            status.put("morningQuestionnaireDate", questionnaireDateFor(TYPE_MORNING));
            status.put("bedCompletedToday", preferences.getBoolean(
                    KEY_DONE_PREFIX + TYPE_BED + "_" + questionnaireDateFor(TYPE_BED), false));
            status.put("morningCompletedLastNight", preferences.getBoolean(
                    KEY_DONE_PREFIX + TYPE_MORNING + "_" + questionnaireDateFor(TYPE_MORNING), false));
            status.put("lastDelivery", preferences.getString(KEY_LAST_DELIVERY, "尚未触发"));
            status.put("nextBed", preferences.getString(KEY_NEXT_BED, "--"));
            status.put("nextMorning", preferences.getString(KEY_NEXT_MORNING, "--"));
            status.put("scheduledTest", preferences.getString(KEY_SCHEDULED_TEST, "--"));
        } catch (JSONException ignored) {
            // JSONObject only receives simple local values above; retain an empty object if it ever fails.
        }
        return status;
    }

    private void postQuestionnaireNotification(String type, boolean test) {
        if (!canPostNotifications()) return;
        boolean bed = TYPE_BED.equals(type);
        Intent launch = new Intent(appContext, MainActivity.class)
                .setAction(MainActivity.ACTION_OPEN_QUESTIONNAIRE)
                .putExtra(MainActivity.EXTRA_QUESTIONNAIRE_TYPE, type)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent contentIntent = PendingIntent.getActivity(
                appContext,
                bed ? 301 : 302,
                launch,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(appContext, CHANNEL_ID)
                : new Notification.Builder(appContext);
        builder.setSmallIcon(R.drawable.ic_launcher)
                .setContentTitle(test ? "问卷提醒测试" : (bed ? "睡前问卷提醒" : "醒后问卷提醒"))
                .setContentText(test ? "系统通知正常，可以按设定时间提醒。"
                        : (bed ? "今晚睡前问卷尚未填写，点击前往完成。" : "昨夜醒后问卷尚未填写，点击前往完成。"))
                .setContentIntent(contentIntent)
                .setAutoCancel(true)
                .setCategory(Notification.CATEGORY_REMINDER);
        builder.setPriority(Notification.PRIORITY_HIGH);
        ((NotificationManager) appContext.getSystemService(Context.NOTIFICATION_SERVICE))
                .notify(test ? 203 : (bed ? 201 : 202), builder.build());
    }

    private boolean canPostNotifications() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && appContext.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            return false;
        }
        NotificationManager manager = (NotificationManager) appContext.getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager == null) return false;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N && !manager.areNotificationsEnabled()) return false;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = manager.getNotificationChannel(CHANNEL_ID);
            return channel == null || channel.getImportance() != NotificationManager.IMPORTANCE_NONE;
        }
        return true;
    }

    private boolean exactAlarmsAllowed() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true;
        AlarmManager manager = (AlarmManager) appContext.getSystemService(Context.ALARM_SERVICE);
        return manager != null && manager.canScheduleExactAlarms();
    }

    private boolean backgroundReminderAllowed() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return true;
        PowerManager manager = (PowerManager) appContext.getSystemService(Context.POWER_SERVICE);
        return manager != null && manager.isIgnoringBatteryOptimizations(appContext.getPackageName());
    }

    private void recordDelivery(String type, String result) {
        String label = TYPE_BED.equals(type) ? "睡前" : (TYPE_MORNING.equals(type) ? "晨起" : "定时测试");
        Calendar now = Calendar.getInstance();
        String timestamp = String.format(Locale.ROOT, "%02d:%02d:%02d", now.get(Calendar.HOUR_OF_DAY), now.get(Calendar.MINUTE), now.get(Calendar.SECOND));
        preferences.edit().putString(KEY_LAST_DELIVERY, label + " " + timestamp + "：" + result).apply();
    }

    private void schedule(String type, String time) {
        String[] parts = validTimeOrDefault(time, "00:00").split(":");
        Calendar next = Calendar.getInstance();
        next.set(Calendar.HOUR_OF_DAY, Integer.parseInt(parts[0]));
        next.set(Calendar.MINUTE, Integer.parseInt(parts[1]));
        next.set(Calendar.SECOND, 0);
        next.set(Calendar.MILLISECOND, 0);
        if (next.getTimeInMillis() <= System.currentTimeMillis()) next.add(Calendar.DATE, 1);

        preferences.edit()
                .putString(TYPE_BED.equals(type) ? KEY_NEXT_BED : KEY_NEXT_MORNING, formatDateTime(next))
                .apply();

        Intent alarm = new Intent(appContext, QuestionnaireReminderReceiver.class)
                .setAction(QuestionnaireReminderReceiver.ACTION_REMINDER)
                .putExtra(QuestionnaireReminderReceiver.EXTRA_TYPE, type);
        PendingIntent pendingIntent = PendingIntent.getBroadcast(
                appContext,
                TYPE_BED.equals(type) ? 101 : 102,
                alarm,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        schedulePendingIntent(next.getTimeInMillis(), pendingIntent);
    }

    private void schedulePendingIntent(long triggerAtMillis, PendingIntent pendingIntent) {
        AlarmManager manager = (AlarmManager) appContext.getSystemService(Context.ALARM_SERVICE);
        if (manager == null) return;
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                /*
                 * Questionnaire reminders are user-visible, time-specific appointments. Routing them through
                 * AlarmClock makes Android treat them as clock-grade alarms, which is more reliable on phones
                 * that aggressively delay ordinary background broadcasts. The receiver still only posts a
                 * notification; this does not start the system alarm ringtone.
                 */
                Intent showApp = new Intent(appContext, MainActivity.class)
                        .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                PendingIntent showIntent = PendingIntent.getActivity(
                        appContext, 801, showApp, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
                );
                manager.setAlarmClock(new AlarmManager.AlarmClockInfo(triggerAtMillis, showIntent), pendingIntent);
            } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && !manager.canScheduleExactAlarms()) {
                manager.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAtMillis, pendingIntent);
            } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                manager.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAtMillis, pendingIntent);
            } else {
                manager.setExact(AlarmManager.RTC_WAKEUP, triggerAtMillis, pendingIntent);
            }
        } catch (SecurityException ignored) {
            manager.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAtMillis, pendingIntent);
        }
    }

    private String timeFor(String type) {
        return TYPE_BED.equals(type) ? preferences.getString(KEY_BED_TIME, "22:30")
                : preferences.getString(KEY_MORNING_TIME, "07:30");
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "问卷提醒",
                NotificationManager.IMPORTANCE_HIGH
        );
        channel.setDescription("睡前和醒后问卷填写提醒");
        ((NotificationManager) appContext.getSystemService(Context.NOTIFICATION_SERVICE))
                .createNotificationChannel(channel);
    }

    private static String validTimeOrDefault(String value, String fallback) {
        if (value != null && value.matches("(?:[01]\\d|2[0-3]):[0-5]\\d")) return value;
        return fallback;
    }

    private static String questionnaireDateFor(String type) {
        Calendar now = Calendar.getInstance(TimeZone.getTimeZone("Asia/Shanghai"), Locale.US);
        if (TYPE_MORNING.equals(type)) now.add(Calendar.DATE, -1);
        return String.format(Locale.ROOT, "%04d-%02d-%02d", now.get(Calendar.YEAR), now.get(Calendar.MONTH) + 1, now.get(Calendar.DAY_OF_MONTH));
    }

    private static String formatDateTime(Calendar calendar) {
        return String.format(Locale.ROOT, "%04d-%02d-%02d %02d:%02d", calendar.get(Calendar.YEAR), calendar.get(Calendar.MONTH) + 1,
                calendar.get(Calendar.DAY_OF_MONTH), calendar.get(Calendar.HOUR_OF_DAY), calendar.get(Calendar.MINUTE));
    }
}
