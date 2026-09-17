// Runs the real HTML state handlers without Android hardware to cover:
// BLE ready -> Wi-Fi confirmed -> power-off/disconnected -> reconnect.
const fs = require('fs');
const vm = require('vm');

class Element {
  constructor() {
    this.textContent = '';
    this.innerHTML = '';
    this.className = '';
    this.hidden = false;
    this.value = '';
    this.style = { setProperty() {}, removeProperty() {} };
    this.dataset = {};
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  append() {}
  appendChild() {}
  replaceChildren() {}
  addEventListener() {}
  setAttribute() {}
  querySelector() { return new Element(); }
  querySelectorAll() { return []; }
}

const elements = new Map();
const byId = id => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const storage = new Map();
const document = {
  body: new Element(),
  addEventListener() {},
  getElementById: byId,
  querySelectorAll: () => [],
  querySelector: () => new Element(),
  createElement: () => new Element(),
  documentElement: new Element(),
};
const context = {
  console,
  document,
  localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key) },
  setTimeout: () => 1,
  clearTimeout() {},
  setInterval: () => 1,
  clearInterval() {},
  Date,
  JSON,
  Math,
  Intl,
};
context.window = context;
context.window.addEventListener = () => {};
context.window.scrollTo = () => {};

const html = fs.readFileSync('app/src/main/assets/sleep-pillow.html', 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match => match[1]).join('\n');
vm.runInNewContext(script, context, { filename: 'sleep-pillow.html' });

const emit = (event, payload) => context.window.SleepPillowPrototype.onBleEvent(event, JSON.stringify(payload));
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};

emit('ready', { message: 'ready' });
emit('provisioning_status', { status: 'WIFI_CONNECTED|Lab-2.4G', message: 'WIFI_CONNECTED|Lab-2.4G' });
assert(byId('bluetoothStateLabel').textContent === '已连接 ›', 'Connected state was not rendered');
assert(byId('wifiStateLabel').textContent === 'Lab-2.4G ›', 'Confirmed Wi-Fi state was not rendered');
context.window.SleepPillowPrototype.onServerEvent('server_telemetry_latest', JSON.stringify({
  heartRate: 60, respiratoryRate: 12, temperature: 36.5, sleepStage: 'Deep Sleep', confidence: 82.4,
  timestamp: '2026-09-09T21:30:00+08:00', receivedAt: new Date().toISOString(),
}));
assert(byId('remoteSyncLabel').textContent === '同步正常 ›', 'Fresh server upload was not rendered as healthy remote sync');
assert(/秒前 收到智能助眠枕数据$/.test(byId('remoteSyncDetail').textContent), 'Remote sync elapsed time was not formatted as seconds ago');
assert(byId('homeHeartRate').textContent === '60.0', 'Realtime home heart rate was not rendered');
assert(byId('homeRespiratoryRate').textContent === '12.0', 'Realtime home respiratory rate was not rendered');
assert(byId('homeTemperature1').textContent === '36.5', 'Realtime home temperature 1 was not rendered');
assert(byId('homeTemperature2').textContent === '--', 'Unavailable temperature 2 should remain empty');
assert(byId('homeTemperature3').textContent === '--', 'Unavailable temperature 3 should remain empty');
assert(byId('realtimeDataStatus').textContent === '已同步', 'Realtime home data did not show the synced state');
assert(byId('realtimeSampleTime').textContent === '2026-09-09 21:30:00', 'Realtime sample time was not shown in the details panel');
assert(byId('realtimeReceivedTime').textContent !== '--', 'Realtime server receipt time was not shown in the details panel');

context.window.SleepPillowPrototype.onServerEvent('server_telemetry_missing', JSON.stringify({
  message: '服务器尚未收到这台设备的数据。',
}));
assert(byId('homeHeartRate').textContent === '--', 'Missing server telemetry did not clear stale heart-rate data');
assert(byId('realtimeDataStatus').textContent === '等待数据', 'Missing server telemetry did not show the waiting state');
assert(byId('realtimeSampleTime').textContent === '--', 'Missing server telemetry did not clear stale detail time');
context.window.SleepPillowPrototype.onServerEvent('server_telemetry_latest', JSON.stringify({
  heartRate: 60, respiratoryRate: 12, temperature: 36.5, sleepStage: 'Deep Sleep', confidence: 82.4,
  timestamp: '2026-09-09T21:30:00+08:00', receivedAt: new Date().toISOString(),
}));

context.window.SleepPillowPrototype.openInfoDetails('wifi');
assert(byId('infoDetailsTitle').textContent === 'Wi-Fi 联网说明', 'Wi-Fi details title was not rendered');
assert(/独立联网/.test(byId('infoDetailsSummary').textContent), 'Wi-Fi details summary was not rendered');
context.window.SleepPillowPrototype.closeInfoDetails();

context.window.SleepPillowPrototype.onNativeEvent('questionnaire_reminder_status', JSON.stringify({
  notificationsAllowed: true, exactAlarmsAllowed: true, backgroundReminderAllowed: true, bedReminder: '17:42', morningReminder: '17:45',
  bedCompletedToday: false, morningCompletedToday: false, lastDelivery: '晨起 17:45:00：已发送',
  nextBed: '2026-09-09 17:42', nextMorning: '2026-09-09 17:45', scheduledTest: '--',
}));
assert(byId('reminderStatus').textContent === '通知：已允许；定时：精确；后台：已解除限制；睡前：17:42；晨起：17:45；今日问卷：睡前未填写、晨起未填写；下次睡前：2026-09-09 17:42；下次晨起：2026-09-09 17:45；上次触发：晨起 17:45:00：已发送；定时测试：--',
  'Native reminder status was not rendered');

context.window.SleepPillowPrototype.requestSleepStart();
assert(byId('confirmTitle').textContent === '完成睡前问卷？', 'Starting sleep did not prompt for the unfinished bedtime questionnaire');
assert(byId('confirmCancel').textContent === '仍然开始', 'Bedtime questionnaire prompt did not offer to continue recording');
assert(byId('confirmOk').textContent === '去填写', 'Bedtime questionnaire prompt did not offer to open the questionnaire');
context.window.SleepPillowPrototype.showWakeQuestionnairePrompt();
assert(byId('confirmTitle').textContent === '完成醒后问卷？', 'Ending sleep did not prompt for the unfinished morning questionnaire');
assert(byId('confirmCancel').textContent === '稍后填写', 'Morning questionnaire prompt did not offer to defer completion');

emit('disconnected', { message: '蓝牙连接已断开。' });
assert(byId('bluetoothStateLabel').textContent === '未连接 ›', 'Disconnected Bluetooth state was not rendered');
assert(byId('deviceMainStatus').textContent === '蓝牙未连接', 'Top device card retained a stale connected state');
assert(byId('wifiStateLabel').textContent === '状态待确认 ›', 'Wi-Fi was incorrectly kept as connected after disconnect');
assert(byId('remoteSyncLabel').textContent === '同步正常 ›', 'Remote sync was incorrectly cleared when Bluetooth disconnected');

context.window.SleepPillowPrototype.onServerEvent('server_telemetry_latest', JSON.stringify({
  heartRate: 60, respiratoryRate: 12, temperature: 36.5, sleepStage: 'Deep Sleep', confidence: 82.4,
  timestamp: '2026-09-09T21:30:00+08:00', receivedAt: new Date(Date.now() - 61_000).toISOString(),
}));
assert(byId('remoteSyncLabel').textContent === '未收到最新数据 ›', 'Stale server upload was not rendered as unhealthy remote sync');

emit('ready', { message: 'ready' });
emit('provisioning_status', { status: 'WIFI_CONNECTED|Lab-2.4G', message: 'WIFI_CONNECTED|Lab-2.4G' });
assert(byId('bluetoothStateLabel').textContent === '已连接 ›', 'Bluetooth did not recover after reconnect');
assert(byId('wifiStateLabel').textContent === 'Lab-2.4G ›', 'Wi-Fi did not recover after reconnect confirmation');

console.log('BLE lifecycle simulation passed: ready -> disconnected -> ready');
