package com.sleeppillow.app.notifications;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Receives local questionnaire alarms and restores them after time-related system events. */
public final class QuestionnaireReminderReceiver extends BroadcastReceiver {
    public static final String ACTION_REMINDER = "com.sleeppillow.app.action.QUESTIONNAIRE_REMINDER";
    public static final String EXTRA_TYPE = "questionnaire_type";

    @Override
    public void onReceive(Context context, Intent intent) {
        QuestionnaireReminderScheduler scheduler = new QuestionnaireReminderScheduler(context);
        if (intent != null && ACTION_REMINDER.equals(intent.getAction())) {
            scheduler.deliverReminder(intent.getStringExtra(EXTRA_TYPE));
        } else {
            scheduler.rescheduleAll();
        }
    }
}
