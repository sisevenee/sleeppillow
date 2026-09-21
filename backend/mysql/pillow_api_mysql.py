#!/usr/bin/env python3
"""HTTP API for multi-device Sleep Pillow telemetry stored in MySQL."""

from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import json
import math
import os
import re
import secrets
import zipfile
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pymysql
from pymysql.cursors import DictCursor

HOST = os.environ.get("PILLOW_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("PILLOW_API_PORT", "8080"))
MYSQL_CONFIG = {
    "host": os.environ.get("PILLOW_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("PILLOW_MYSQL_PORT", "3306")),
    "user": os.environ.get("PILLOW_MYSQL_USER", "pillow_api"),
    "password": os.environ.get("PILLOW_MYSQL_PASSWORD", ""),
    "database": os.environ.get("PILLOW_MYSQL_DATABASE", "pillow"),
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
    "autocommit": True,
}
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_BODY_BYTES = 16 * 1024
MAX_IMAGE_ATTACHMENT_BYTES = 10 * 1024 * 1024
PASSWORD_HASH_ITERATIONS = 310_000
SESSION_LIFETIME_DAYS = 30
SESSION_STALE_AFTER_MINUTES = 15
CHINA_TIMEZONE = timezone(timedelta(hours=8))
ADMIN_DASHBOARD_PATH = Path(__file__).with_name("admin_dashboard.html")
QUESTIONNAIRE_TYPES = frozenset({"psqi_before", "psqi_after", "pre_sleep", "post_wake"})
DAILY_QUESTIONNAIRE_TYPES = frozenset({"pre_sleep", "post_wake"})
QUESTIONNAIRE_TYPE_LABELS = {
    "psqi_before": "PSQI 前测",
    "psqi_after": "PSQI 后测",
    "pre_sleep": "睡前问卷",
    "post_wake": "醒后问卷",
}
MAX_QUESTIONNAIRE_ANSWER_BYTES = 12 * 1024
QUESTIONNAIRE_ATTACHMENT_KEYS = frozenset({"parameter_adjustment_photo"})
SLEEP_MARKER_TYPES = frozenset({"prepare", "wake"})
MAX_ADMIN_EXPORT_DAYS = 31
# The completeness matrix renders one column per day, so a wide range is unusable rather than
# merely slow. A full 4-week experiment block plus slack is the intended ceiling.
MAX_COMPLETENESS_DAYS = 62


def db_connection() -> pymysql.connections.Connection:
    return pymysql.connect(**MYSQL_CONFIG)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_timestamp(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def format_china_local_timestamp(value: datetime) -> str:
    """Serialize DATETIME columns that MySQL writes in its China-local session timezone."""
    return value.replace(tzinfo=CHINA_TIMEZONE).isoformat()


def format_china_local_datetime(value: datetime) -> str:
    """Format a China-local MySQL DATETIME without applying a second eight-hour offset."""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    # Accept the ISO-8601 value sent by new firmware and the original CSV format.
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone, for example +08:00")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def finite_number(value: object, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def extract_bearer_token(headers: Any) -> str | None:
    authorization = headers.get("Authorization", "")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return None
    token = authorization[len(prefix) :].strip()
    return token or None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def password_hash(password: str) -> str:
    """Create a salted PBKDF2 hash; plaintext passwords never enter MySQL."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        computed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, computed)


def telemetry_to_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "deviceId": row["device_id"],
        "deviceName": row["display_name"],
        "timestamp": format_timestamp(row["sampled_at"]),
        "heartRate": float(row["heart_rate"]),
        "respiratoryRate": float(row["respiratory_rate"]),
        "temperature": float(row["temperature"]),
        "sleepStage": row["sleep_stage"],
        "confidence": float(row["confidence"]),
        "timeSource": row["time_source"],
        "analogVoltage": float(row["analog_voltage"]) if row["analog_voltage"] is not None else None,
        "fpgaInput": row["fpga_input"],
        "receivedAt": format_timestamp(row["received_at"]),
    }


def china_now() -> datetime:
    """Return the current China-local time with its timezone retained."""
    return datetime.now(CHINA_TIMEZONE)


def parse_questionnaire_date(value: object) -> date:
    """Accept only a China-calendar date used to group a questionnaire response."""
    if not isinstance(value, str):
        raise ValueError("responseDate must be an ISO date")
    return date.fromisoformat(value.strip())


def parse_admin_export_range(query: dict[str, list[str]]) -> tuple[date, date]:
    """Read an inclusive China-calendar export range with a bounded result size."""
    default_day = china_now().date().isoformat()
    start_text = query.get("start", [default_day])[0]
    end_text = query.get("end", [start_text])[0]
    start_day = parse_questionnaire_date(start_text)
    end_day = parse_questionnaire_date(end_text)
    if end_day < start_day:
        raise ValueError("end must not be earlier than start")
    if (end_day - start_day).days >= MAX_ADMIN_EXPORT_DAYS:
        raise ValueError(f"The export range must be at most {MAX_ADMIN_EXPORT_DAYS} days")
    return start_day, end_day


def china_day_bounds_utc(start_day: date, end_day: date) -> tuple[datetime, datetime]:
    """Convert inclusive China-local dates to a half-open UTC database interval."""
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=CHINA_TIMEZONE)
    end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=CHINA_TIMEZONE)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def format_china_datetime(value: datetime) -> str:
    """Format a UTC-naive MySQL DATETIME consistently for human-readable exports."""
    return value.replace(tzinfo=timezone.utc).astimezone(CHINA_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def csv_bytes(header: list[object], rows: list[list[object]]) -> bytes:
    """Create an Excel-friendly UTF-8 CSV without storing a generated file on the server."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def safe_filename_component(value: object) -> str:
    """Keep user-derived archive paths inside the ZIP and readable on Windows."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value)).strip("._")
    return cleaned[:64] or "user"


def questionnaire_answer_text(answer: object, attachment_paths: dict[int, str] | None = None) -> str:
    """Preserve a submitted answer and make a linked questionnaire image discoverable."""
    if not isinstance(answer, dict):
        return ""
    value = answer.get("value", "")
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    attachment_id = answer.get("attachmentId")
    try:
        attachment_id = int(attachment_id)
    except (TypeError, ValueError):
        attachment_id = 0
    if attachment_id > 0:
        attachment_path = (attachment_paths or {}).get(attachment_id)
        suffix = f"已上传图片：{attachment_path}" if attachment_path else f"已上传图片（编号 {attachment_id}）"
        return f"{text}；{suffix}" if text else suffix
    return text


PSQI_COMPONENT_LABELS = {
    "subjective_quality": "主观睡眠质量",
    "sleep_latency": "入睡时间",
    "sleep_duration": "睡眠时间",
    "habitual_efficiency": "习惯性睡眠效率",
    "sleep_disturbances": "睡眠障碍",
    "sleeping_medicine": "催眠药物使用",
    "daytime_dysfunction": "日间功能障碍",
}
PSQI_FREQUENCY_SCORES = {
    "从未发生": 0,
    "每周≤1次": 1,
    "每周<=1次": 1,
    "每周1–2次": 2,
    "每周1-2次": 2,
    "每周≥3次": 3,
    "每周>=3次": 3,
}


def _psqi_frequency_score(value: str, alternatives: dict[str, int] | None = None) -> int | None:
    normalized = value.strip()
    if alternatives and normalized in alternatives:
        return alternatives[normalized]
    return PSQI_FREQUENCY_SCORES.get(normalized)


def _psqi_clock_minutes(value: str) -> int | None:
    """Parse new HH:MM fields and the common Chinese wording in older questionnaire rows."""
    match = re.search(r"(?<!\d)([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)(?!\d)", value)
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    normalized = value.lower()
    if ("下午" in value or "晚上" in value or "pm" in normalized) and hour < 12:
        hour += 12
    elif ("凌晨" in value or "午夜" in value) and hour == 12:
        hour = 0
    return hour * 60 + minute


def _psqi_sleep_hours(value: str) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", value.strip())
    if match is None:
        return None
    try:
        hours = float(match.group(0))
    except ValueError:
        return None
    return hours if 0 < hours <= 24 else None


def _psqi_component_from_total(total: int) -> int:
    if total <= 0:
        return 0
    if total <= 2:
        return 1
    if total <= 4:
        return 2
    return 3


def calculate_psqi_summary(answers: dict[str, Any]) -> dict[str, Any]:
    """Calculate the standard 0-21 PSQI score without assigning a score to incomplete rows."""
    values = {key: answer_value(answers, key) for key in answers}
    components: dict[str, int | None] = {}

    components["subjective_quality"] = {
        "很好": 0, "较好": 1, "较差": 2, "很差": 3,
    }.get(values.get("overall_quality", "").strip())

    latency_choice = {
        "≤15分钟": 0, "<=15分钟": 0, "16-30分钟": 1, "16–30分钟": 1,
        "31-60分钟": 2, "31–60分钟": 2, "≥60分钟": 3, ">=60分钟": 3,
    }.get(values.get("sleep_latency", "").strip())
    latency_frequency = _psqi_frequency_score(values.get("problem_fall_asleep", ""))
    components["sleep_latency"] = (
        _psqi_component_from_total(latency_choice + latency_frequency)
        if latency_choice is not None and latency_frequency is not None else None
    )

    sleep_hours = _psqi_sleep_hours(values.get("sleep_hours", ""))
    if sleep_hours is None:
        components["sleep_duration"] = None
    elif sleep_hours >= 7:
        components["sleep_duration"] = 0
    elif sleep_hours >= 6:
        components["sleep_duration"] = 1
    elif sleep_hours >= 5:
        components["sleep_duration"] = 2
    else:
        components["sleep_duration"] = 3

    bedtime = _psqi_clock_minutes(values.get("bed_time", ""))
    wake_time = _psqi_clock_minutes(values.get("wake_time", ""))
    if sleep_hours is None or bedtime is None or wake_time is None:
        components["habitual_efficiency"] = None
    else:
        time_in_bed = wake_time - bedtime
        if time_in_bed <= 0:
            time_in_bed += 24 * 60
        efficiency = sleep_hours * 60 / time_in_bed * 100 if time_in_bed else 0
        if efficiency > 100:
            components["habitual_efficiency"] = None
        elif efficiency >= 85:
            components["habitual_efficiency"] = 0
        elif efficiency >= 75:
            components["habitual_efficiency"] = 1
        elif efficiency >= 65:
            components["habitual_efficiency"] = 2
        else:
            components["habitual_efficiency"] = 3

    disturbance_keys = (
        "problem_early_awake", "problem_toilet", "problem_breathing", "problem_snoring",
        "problem_cold", "problem_hot", "problem_nightmare", "problem_pain", "problem_other_frequency",
    )
    disturbance_scores = [_psqi_frequency_score(values.get(key, "")) for key in disturbance_keys]
    components["sleep_disturbances"] = (
        None if any(score is None for score in disturbance_scores)
        else (0 if sum(disturbance_scores) == 0 else 1 if sum(disturbance_scores) <= 9
              else 2 if sum(disturbance_scores) <= 18 else 3)
    )
    components["sleeping_medicine"] = _psqi_frequency_score(values.get("sleeping_medicine", ""))
    fatigue_score = _psqi_frequency_score(values.get("fatigue", ""))
    energy_score = _psqi_frequency_score(values.get("low_energy", ""), {
        "没有": 0, "偶尔有": 1, "有时有": 2, "经常有": 3,
    })
    components["daytime_dysfunction"] = (
        _psqi_component_from_total(fatigue_score + energy_score)
        if fatigue_score is not None and energy_score is not None else None
    )

    component_rows = [
        {"key": key, "label": label, "score": components.get(key)}
        for key, label in PSQI_COMPONENT_LABELS.items()
    ]
    missing = [item["label"] for item in component_rows if item["score"] is None]
    return {
        "status": "complete" if not missing else "incomplete",
        "score": sum(int(item["score"]) for item in component_rows) if not missing else None,
        "components": component_rows,
        "missingComponents": missing,
    }


def questionnaire_export_csv(
    rows: list[dict[str, Any]], attachment_paths: dict[int, str] | None = None,
) -> bytes:
    """Flatten flexible questionnaire JSON into the one-row-per-response CSV administrators expect."""
    parsed_rows: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]] = []
    columns: list[tuple[str, str]] = []
    seen_columns: set[tuple[str, str]] = set()
    title_counts: dict[str, int] = {}
    column_titles: dict[tuple[str, str], str] = {}
    for row in rows:
        try:
            answers = json.loads(row["answers_json"])
        except (TypeError, json.JSONDecodeError):
            answers = {}
        if not isinstance(answers, dict):
            answers = {}
        psqi_summary = calculate_psqi_summary(answers) if row["questionnaire_type"] in ("psqi_before", "psqi_after") else None
        parsed_rows.append((row, answers, psqi_summary))
        for key, answer in answers.items():
            column_key = (str(row["questionnaire_type"]), str(key))
            if column_key in seen_columns:
                continue
            seen_columns.add(column_key)
            columns.append(column_key)
            question = answer.get("question") if isinstance(answer, dict) else None
            title = str(question).strip() if question else str(key)
            column_titles[column_key] = title
            title_counts[title] = title_counts.get(title, 0) + 1

    answer_headers = [
        title if title_counts[title] == 1 else f"{QUESTIONNAIRE_TYPE_LABELS[column_type]}：{title}"
        for column_type, key in columns
        for title in [column_titles[(column_type, key)]]
    ]
    export_rows: list[list[object]] = []
    for row, answers, psqi_summary in parsed_rows:
        score = "" if psqi_summary is None or psqi_summary["score"] is None else psqi_summary["score"]
        score_status = "" if psqi_summary is None else (
            "评分完成" if psqi_summary["status"] == "complete"
            else "待完善：" + "、".join(psqi_summary["missingComponents"])
        )
        values = [
            row["username"],
            QUESTIONNAIRE_TYPE_LABELS.get(row["questionnaire_type"], row["questionnaire_type"]),
            row["response_date"].isoformat(),
            format_china_local_datetime(row["submitted_at"]),
            format_china_local_datetime(row["updated_at"]),
            score,
            score_status,
        ]
        for column_type, key in columns:
            answer = answers.get(key) if row["questionnaire_type"] == column_type else None
            values.append(questionnaire_answer_text(answer, attachment_paths))
        export_rows.append(values)
    return csv_bytes(
        ["账号", "问卷类型", "记录日期（睡眠夜日期）", "提交时间（中国时间）", "最后修改时间（中国时间）", "PSQI总分（0-21）", "PSQI评分状态"] + answer_headers,
        export_rows,
    )


def questionnaire_attachment_extension(content_type: str) -> str:
    """Return a stable extension for image types accepted by the upload endpoint."""
    return {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }.get(content_type, "bin")


def questionnaire_attachment_paths(attachments: list[dict[str, Any]]) -> dict[int, str]:
    """Give every exported image a readable, collision-free relative ZIP path."""
    paths: dict[int, str] = {}
    for attachment in attachments:
        attachment_id = int(attachment["id"])
        response_date = attachment["response_date"].isoformat()
        username = safe_filename_component(attachment["username"])
        questionnaire_type = safe_filename_component(attachment["questionnaire_type"])
        attachment_key = safe_filename_component(attachment["attachment_key"])
        extension = questionnaire_attachment_extension(str(attachment["content_type"]))
        paths[attachment_id] = (
            f"images/{response_date}_{username}_{questionnaire_type}_{attachment_key}_{attachment_id}.{extension}"
        )
    return paths


def questionnaire_export_preview_html(
    rows: list[dict[str, Any]], attachment_paths: dict[int, str],
) -> bytes:
    """Build an offline report that renders exported questionnaire images beside their answers."""
    sections: list[str] = []
    for row in rows:
        try:
            answers = json.loads(row["answers_json"])
        except (TypeError, json.JSONDecodeError):
            answers = {}
        if not isinstance(answers, dict):
            answers = {}
        answer_rows: list[str] = []
        for key, answer in answers.items():
            if not isinstance(answer, dict):
                continue
            question = html.escape(str(answer.get("question") or key))
            value = answer.get("value", "")
            if isinstance(value, (dict, list)):
                value_text = json.dumps(value, ensure_ascii=False)
            else:
                value_text = str(value)
            content = f'<div class="value">{html.escape(value_text)}</div>' if value_text else ""
            try:
                attachment_id = int(answer.get("attachmentId", 0))
            except (TypeError, ValueError):
                attachment_id = 0
            attachment_path = attachment_paths.get(attachment_id)
            if attachment_path:
                escaped_path = html.escape(attachment_path, quote=True)
                content += (
                    f'<a class="image-link" href="{escaped_path}" target="_blank">'
                    f'<img src="{escaped_path}" alt="{question}"><span>点击查看原图</span></a>'
                )
            answer_rows.append(f"<tr><th>{question}</th><td>{content or '-'}</td></tr>")
        label = html.escape(QUESTIONNAIRE_TYPE_LABELS.get(
            row["questionnaire_type"], row["questionnaire_type"]
        ))
        sections.append(
            '<section class="submission">'
            f'<h2>{html.escape(str(row["username"]))} · {label}</h2>'
            f'<p>{row["response_date"].isoformat()} · '
            f'{html.escape(format_china_local_datetime(row["submitted_at"]))}</p>'
            f'<table>{"".join(answer_rows) or "<tr><td>无可读答案</td></tr>"}</table>'
            '</section>'
        )
    document = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>问卷导出（含图片）</title>
<style>
body{margin:0;background:#f4f6f9;color:#1d2a3a;font-family:Arial,"Microsoft YaHei",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px}h1{font-size:24px;margin:0 0 8px}.note{color:#657085;margin:0 0 20px}
.submission{background:#fff;border:1px solid #dfe5ed;border-radius:7px;padding:18px;margin-bottom:18px}
h2{font-size:18px;margin:0 0 5px}.submission>p{color:#657085;margin:0 0 14px}table{width:100%;border-collapse:collapse}
th,td{border-top:1px solid #e7ebf1;padding:10px;text-align:left;vertical-align:top}th{width:36%;font-weight:600}.value{white-space:pre-wrap;line-height:1.5}
.image-link{display:inline-flex;flex-direction:column;align-items:flex-start;gap:6px;margin-top:9px;color:#245bbd;text-decoration:none}
.image-link img{display:block;max-width:min(520px,100%);max-height:360px;border:1px solid #d9e0e9;border-radius:5px;background:#f7f8fa}
@media(max-width:640px){main{padding:12px}.submission{padding:12px}th,td{display:block;width:auto}th{border-bottom:0;padding-bottom:3px}td{border-top:0;padding-top:3px}}
</style></head><body><main><h1>问卷汇总</h1><p class="note">图片与 CSV 均在当前压缩包中；请先完整解压，再打开本页。</p>"""
    document += "".join(sections)
    document += "</main></body></html>"
    return document.encode("utf-8")


def questionnaire_export_zip(
    rows: list[dict[str, Any]], attachments: list[dict[str, Any]],
) -> bytes:
    """Package the analysis CSV, an offline image preview, and original images together."""
    attachment_paths = questionnaire_attachment_paths(attachments)
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("questionnaires.csv", questionnaire_export_csv(rows, attachment_paths))
        archive.writestr(
            "questionnaires_preview.html",
            questionnaire_export_preview_html(rows, attachment_paths),
        )
        archive.writestr(
            "README.txt",
            (
                "1. 先完整解压本 ZIP。\r\n"
                "2. questionnaires.csv 用于 Excel 数据分析，图片答案栏会显示 images 相对路径。\r\n"
                "3. 用浏览器打开 questionnaires_preview.html，可在答案旁直接查看图片。\r\n"
                "4. images 目录保存未转码的原始上传图片。\r\n"
            ).encode("utf-8-sig"),
        )
        for attachment in attachments:
            attachment_id = int(attachment["id"])
            path = attachment_paths.get(attachment_id)
            if path:
                archive.writestr(path, bytes(attachment["image_data"]))
    return archive_buffer.getvalue()


def image_content_type(image_data: bytes) -> str | None:
    """Accept only image formats the App explicitly offers for questionnaire attachments."""
    if image_data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(image_data) >= 12 and image_data[:4] == b"RIFF" and image_data[8:12] == b"WEBP":
        return "image/webp"
    return None


def answer_value(answers: dict[str, Any], key: str) -> str:
    """Read one App-form answer defensively; historical records may have an older shape."""
    answer = answers.get(key)
    return str(answer.get("value", "")) if isinstance(answer, dict) else ""


def resolve_questionnaire_response_date(
    questionnaire_type: str, requested_date: date, answers: dict[str, Any],
) -> date:
    """Use the wake diary's stated experiment date as its canonical sleep-night key.

    Older APKs submitted the China date on which the morning form was filled, even though the
    form itself correctly contained the previous night's experiment date. Normalizing at the
    API boundary keeps those clients from splitting one night across two calendar dates.
    """
    if questionnaire_type != "post_wake":
        return requested_date
    experiment_date = answer_value(answers, "experiment_date").strip()
    if not experiment_date:
        return requested_date
    try:
        return parse_questionnaire_date(experiment_date)
    except ValueError as error:
        raise ValueError("answers.experiment_date must be YYYY-MM-DD") from error


def questionnaire_submission_to_response(row: dict[str, Any], include_answers: bool = False) -> dict[str, Any]:
    response = {
        "id": int(row["id"]),
        "questionnaireType": row["questionnaire_type"],
        "responseDate": row["response_date"].isoformat(),
        "submittedAt": format_china_local_timestamp(row["submitted_at"]),
        "updatedAt": format_china_local_timestamp(row["updated_at"]),
    }
    if "username" in row:
        response["username"] = row["username"]
    answers: dict[str, Any] | None = None
    has_answers = "answers_json" in row
    if has_answers and (include_answers or row["questionnaire_type"] in ("psqi_before", "psqi_after")):
        try:
            candidate = json.loads(row["answers_json"])
            answers = candidate if isinstance(candidate, dict) else {}
        except (TypeError, json.JSONDecodeError):
            # A malformed historic row must never make the administrator page unusable.
            answers = {}
    if include_answers:
        response["answers"] = answers or {}
    if row["questionnaire_type"] in ("psqi_before", "psqi_after") and has_answers:
        summary = calculate_psqi_summary(answers or {})
        response["psqiScore"] = summary["score"]
        response["psqiScoreStatus"] = summary["status"]
        response["psqiComponents"] = summary["components"]
        response["psqiMissingComponents"] = summary["missingComponents"]
    return response


def current_device(connection: pymysql.connections.Connection, user_id: int) -> dict[str, Any] | None:
    """Find the device that a logged-in App user most recently selected."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT d.id, d.device_id, d.display_name
            FROM user_current_devices ucd JOIN devices d ON d.id = ucd.device_id
            WHERE ucd.user_id = %s AND d.is_active = 1
            """,
            (user_id,),
        )
        return cursor.fetchone()


def device_owner_user_id(connection: pymysql.connections.Connection, device_database_id: int) -> int | None:
    """Return the ordinary user who currently has this device selected, if any.

    This is the live source for session attribution: the ESP32 uploads without any user
    identity, so the person holding the device at upload time is the best available answer.
    It cannot recover a session recorded before the device changed hands, which is exactly
    what sleep_sessions.user_id exists to preserve.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT u.id AS user_id
            FROM user_current_devices ucd
            JOIN users u ON u.id = ucd.user_id
            WHERE ucd.device_id = %s AND u.role = 'user' AND u.is_active = 1
            """,
            (device_database_id,),
        )
        row = cursor.fetchone()
    return int(row["user_id"]) if row is not None else None


def attribute_new_session(connection: pymysql.connections.Connection, device_database_id: int) -> int | None:
    """Resolve the owner to stamp onto a newly opened session. Call once per new session."""
    return device_owner_user_id(connection, device_database_id)


def close_stale_sessions(connection: pymysql.connections.Connection, device_database_id: int) -> None:
    """Close only sessions whose device has been silent long enough to be considered off."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sleep_sessions
            SET ended_at = COALESCE(last_sample_at, started_at),
                end_source = 'data_gap', closed_at = UTC_TIMESTAMP()
            WHERE device_id = %s AND ended_at IS NULL
              AND (
                  (last_sample_at IS NOT NULL AND last_sample_at < UTC_TIMESTAMP() - INTERVAL %s MINUTE)
                  OR (last_sample_at IS NULL AND started_at < UTC_TIMESTAMP() - INTERVAL %s MINUTE)
              )
            """,
            (device_database_id, SESSION_STALE_AFTER_MINUTES, SESSION_STALE_AFTER_MINUTES),
        )


def attach_telemetry_to_session(
    connection: pymysql.connections.Connection, device_database_id: int, sampled_at: datetime
) -> None:
    """Create or update the active session for an uploaded device sample.

    Telemetry is the source of truth for automatic sessions. A new sample after a long gap closes
    the preceding session at its last actual sample, then begins a new one at this sample.
    """
    # A queued offline row can arrive out of order. A newer active session already covers it,
    # so do not let a late retry create a historical duplicate session.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id FROM sleep_sessions
            WHERE device_id = %s AND started_at <= %s AND (ended_at IS NULL OR ended_at >= %s)
            ORDER BY started_at DESC, id DESC LIMIT 1
            """,
            (device_database_id, sampled_at, sampled_at),
        )
        containing_session = cursor.fetchone()
        if containing_session is not None:
            cursor.execute(
                """
                UPDATE sleep_sessions
                SET last_sample_at = GREATEST(COALESCE(last_sample_at, %s), %s)
                WHERE id = %s AND ended_at IS NULL
                """,
                (sampled_at, sampled_at, containing_session["id"]),
            )
            return
        cursor.execute(
            """
            SELECT id, last_sample_at
            FROM sleep_sessions
            WHERE device_id = %s AND ended_at IS NULL
            ORDER BY started_at DESC, id DESC LIMIT 1
            """,
            (device_database_id,),
        )
        active_session = cursor.fetchone()
        if active_session is None:
            # The device data is the sole authority for a monitoring session. In particular,
            # an App-side “prepare to sleep” or “wake” marker must never suppress continuous
            # telemetry from an ESP32 that is still powered and collecting data.
            # Attribution is fixed once, at session creation. A later device handover must not
            # rewrite who this historical night belonged to.
            cursor.execute(
                """
                INSERT INTO sleep_sessions (device_id, user_id, started_at, last_sample_at, start_source)
                VALUES (%s, %s, %s, %s, 'telemetry')
                """,
                (device_database_id, attribute_new_session(connection, device_database_id), sampled_at, sampled_at),
            )
            return
        last_sample = active_session["last_sample_at"]
        # Compare data timestamps instead of server arrival time. This keeps a session continuous
        # when ESP32 uploads an offline queue several minutes late after Wi-Fi recovers.
        if last_sample is not None and sampled_at > last_sample + timedelta(minutes=SESSION_STALE_AFTER_MINUTES):
            cursor.execute(
                """
                UPDATE sleep_sessions
                SET ended_at = last_sample_at, end_source = 'data_gap', closed_at = UTC_TIMESTAMP()
                WHERE id = %s
                """,
                (active_session["id"],),
            )
            cursor.execute(
                """
                INSERT INTO sleep_sessions (device_id, user_id, started_at, last_sample_at, start_source)
                VALUES (%s, %s, %s, %s, 'telemetry')
                """,
                (device_database_id, attribute_new_session(connection, device_database_id), sampled_at, sampled_at),
            )
            return
        cursor.execute(
            """
            UPDATE sleep_sessions
            SET last_sample_at = GREATEST(COALESCE(last_sample_at, %s), %s)
            WHERE id = %s
            """,
            (sampled_at, sampled_at, active_session["id"]),
        )


def active_device_recording(connection: pymysql.connections.Connection, device_database_id: int) -> dict[str, Any] | None:
    """Return the current continuous ESP32 recording after closing a powered-off stale session."""
    close_stale_sessions(connection, device_database_id)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, started_at, last_sample_at
            FROM sleep_sessions
            WHERE device_id = %s AND ended_at IS NULL
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            (device_database_id,),
        )
        return cursor.fetchone()


def sleep_session_to_response(row: dict[str, Any]) -> dict[str, Any]:
    """Format one completed session and its measured aggregate for App display."""
    first_sample = row["first_sample_at"]
    last_sample = row["last_sample_at"]
    duration_seconds = 0
    if first_sample is not None and last_sample is not None:
        duration_seconds = max(0, int((last_sample - first_sample).total_seconds()))
    return {
        "id": row["id"],
        "startedAt": format_timestamp(row["started_at"]),
        "endedAt": format_timestamp(row["ended_at"]),
        "firstSampleAt": format_timestamp(first_sample) if first_sample else None,
        "lastSampleAt": format_timestamp(last_sample) if last_sample else None,
        "durationSeconds": duration_seconds,
        "recordCount": int(row["record_count"]),
        "averageHeartRate": round(float(row["average_heart_rate"]), 1) if row["average_heart_rate"] is not None else None,
        "averageRespiratoryRate": round(float(row["average_respiratory_rate"]), 1) if row["average_respiratory_rate"] is not None else None,
        "averageTemperature": round(float(row["average_temperature"]), 1) if row["average_temperature"] is not None else None,
        "startSource": row["start_source"],
        "endSource": row["end_source"],
    }


def load_stage_segments(
    connection: pymysql.connections.Connection, device_database_id: int, started_at: datetime, ended_at: datetime
) -> list[dict[str, Any]]:
    """Merge consecutive ESP32 sleep-stage samples into timeline segments.

    A gap larger than 90 seconds starts a new segment even when its label is unchanged. That keeps
    a missing-data interval from being displayed as continuously measured sleep.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT sampled_at, sleep_stage
            FROM telemetry
            WHERE device_id = %s AND sampled_at >= %s AND sampled_at <= %s
            ORDER BY sampled_at ASC, id ASC
            """,
            (device_database_id, started_at, ended_at),
        )
        samples = cursor.fetchall()
    if not samples:
        return []
    segments: list[dict[str, Any]] = []
    segment_stage = samples[0]["sleep_stage"]
    segment_start = samples[0]["sampled_at"]
    previous_at = segment_start
    for sample in samples[1:]:
        sampled_at = sample["sampled_at"]
        stage_changed = sample["sleep_stage"] != segment_stage
        data_gap = sampled_at - previous_at > timedelta(seconds=90)
        if stage_changed or data_gap:
            segment_end = sampled_at if stage_changed else previous_at + timedelta(seconds=10)
            if segment_end > segment_start:
                segments.append({
                    "stage": segment_stage,
                    "startedAt": format_timestamp(segment_start),
                    "endedAt": format_timestamp(segment_end),
                    "durationSeconds": int((segment_end - segment_start).total_seconds()),
                })
            segment_stage = sample["sleep_stage"]
            segment_start = sampled_at
        previous_at = sampled_at
    segment_end = previous_at + timedelta(seconds=10)
    if segment_end > segment_start:
        segments.append({
            "stage": segment_stage,
            "startedAt": format_timestamp(segment_start),
            "endedAt": format_timestamp(segment_end),
            "durationSeconds": int((segment_end - segment_start).total_seconds()),
        })
    return segments


def is_awake_stage(stage: object) -> bool:
    """Recognize the awake label emitted by current and future ESP32 firmware variants."""
    value = str(stage or "").strip().lower()
    return value in {"awake", "wake", "清醒"}


def load_completed_sessions(
    connection: pymysql.connections.Connection, device_database_id: int, start_at: datetime, limit: int,
    include_stage_segments: bool = False, end_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Load completed sessions with metrics derived from the telemetry rows in each session."""
    clauses = ["s.device_id = %s", "s.ended_at IS NOT NULL", "s.ended_at >= %s"]
    parameters: list[Any] = [device_database_id, start_at]
    if end_at is not None:
        clauses.append("s.ended_at < %s")
        parameters.append(end_at)
    parameters.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT s.id, s.started_at, s.ended_at, s.start_source, s.end_source,
                   MIN(t.sampled_at) AS first_sample_at, MAX(t.sampled_at) AS last_sample_at,
                   COUNT(t.id) AS record_count, AVG(t.heart_rate) AS average_heart_rate,
                   AVG(t.respiratory_rate) AS average_respiratory_rate,
                   AVG(t.temperature) AS average_temperature
            FROM sleep_sessions s
            LEFT JOIN telemetry t ON t.device_id = s.device_id
                AND t.sampled_at >= s.started_at AND t.sampled_at <= s.ended_at
            WHERE """ + " AND ".join(clauses) + """
            GROUP BY s.id, s.started_at, s.ended_at, s.start_source, s.end_source
            HAVING COUNT(t.id) > 0
            ORDER BY s.ended_at DESC, s.id DESC
            LIMIT %s
            """,
            parameters,
        )
        sessions = [sleep_session_to_response(row) for row in cursor.fetchall()]
    if include_stage_segments:
        for session in sessions:
            attach_stage_segments(connection, device_database_id, session)
    return sessions


def attach_stage_segments(
    connection: pymysql.connections.Connection, device_database_id: int, session: dict[str, Any]
) -> None:
    """Add timeline data to one already formatted session response."""
    segments = load_stage_segments(
        connection,
        device_database_id,
        datetime.fromisoformat(session["startedAt"].replace("Z", "+00:00")).replace(tzinfo=None),
        datetime.fromisoformat(session["endedAt"].replace("Z", "+00:00")).replace(tzinfo=None),
    )
    session["stageSegments"] = segments
    # “睡眠时长” excludes only intervals explicitly classified as awake. It stays separate from
    # the session's raw monitoring duration, which includes awake time and any data gaps.
    session["sleepDurationSeconds"] = sum(
        int(segment["durationSeconds"]) for segment in segments if not is_awake_stage(segment["stage"])
    )


def select_last_night_report(sessions: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """Pick the main overnight report without allowing a short daytime test to replace it.

    A night is assigned to the China-local date on which it ends. The expected end window is
    00:00-12:00. A candidate must either cross midnight or start before 06:00, and must contain
    at least two hours of measured data. This keeps a 10:08--11:03 hardware test out of the
    “昨夜睡眠” card. Before noon, an unfinished current night naturally falls back to the
    preceding morning's completed report.
    """
    today = now.date()
    for target_day in (today, today - timedelta(days=1)):
        candidates: list[dict[str, Any]] = []
        for session in sessions:
            ended_at = datetime.fromisoformat(session["endedAt"].replace("Z", "+00:00")).astimezone(CHINA_TIMEZONE)
            started_at = datetime.fromisoformat(session["startedAt"].replace("Z", "+00:00")).astimezone(CHINA_TIMEZONE)
            crosses_midnight = started_at.date() < ended_at.date()
            starts_overnight = started_at.hour < 6
            long_enough = int(session["durationSeconds"]) >= 2 * 60 * 60
            if (
                ended_at.date() == target_day
                and 0 <= ended_at.hour < 12
                and long_enough
                and (crosses_midnight or starts_overnight)
            ):
                candidates.append(session)
        if candidates:
            return max(candidates, key=lambda session: (session["durationSeconds"], session["recordCount"]))
    return None


def china_date_key(value: datetime | None) -> str | None:
    """Render a stored UTC timestamp as the China-local calendar date it belongs to."""
    if value is None:
        return None
    return format_china_datetime(value).split(" ", 1)[0]


def load_completeness_matrix(
    connection: pymysql.connections.Connection, start_day: date, end_day: date,
) -> dict[str, Any]:
    """Build the participant x sleep-night data-completeness matrix.

    A row is one active ordinary participant; a column is one China-local sleep-night date.
    Every column is present even when a participant contributed nothing, because a visibly
    empty cell is the signal the researcher is looking for.

    Cell status:
      missing — no monitoring record and no questionnaire for that night
      partial — something arrived, but at least one of the three expected pieces is absent
      complete — a sleep session plus both the bedtime and next-morning questionnaire
    """
    day_count = (end_day - start_day).days + 1
    if day_count < 1:
        raise ValueError("start must not be later than end")
    if day_count > MAX_COMPLETENESS_DAYS:
        raise ValueError(f"Date range must not exceed {MAX_COMPLETENESS_DAYS} days")
    days = [(start_day + timedelta(days=offset)).isoformat() for offset in range(day_count)]

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, username FROM users
            WHERE role = 'user' AND is_active = 1
            ORDER BY username
            """
        )
        participants = cursor.fetchall()

        # A sleep night is keyed by the China-local date on which monitoring started. That is
        # the bedtime questionnaire's date; the next-morning questionnaire uses the same key.
        # SUM() over a TIMESTAMPDIFF yields a plain integer, unlike a SUM over a DATETIME
        # difference, which the driver hands back as a Decimal of the form HHMMSS.
        cursor.execute(
            """
            SELECT COALESCE(s.user_id, ucd.user_id) AS user_id,
                   DATE(DATE_ADD(s.started_at, INTERVAL 8 HOUR)) AS sleep_night,
                   COUNT(DISTINCT s.id) AS session_count,
                   SUM(TIMESTAMPDIFF(SECOND, s.started_at, COALESCE(s.ended_at, s.last_sample_at))) AS total_seconds
            FROM sleep_sessions s
            JOIN devices d ON d.id = s.device_id
            LEFT JOIN user_current_devices ucd ON ucd.device_id = d.id
            WHERE s.last_sample_at IS NOT NULL
              AND COALESCE(s.user_id, ucd.user_id) IS NOT NULL
              AND DATE(DATE_ADD(s.started_at, INTERVAL 8 HOUR))
                   BETWEEN %s AND %s
            GROUP BY user_id, sleep_night
            """,
            (start_day, end_day),
        )
        session_rows = cursor.fetchall()

        # Questionnaire submitters per night, keyed by user rather than by device. A sleep diary
        # is filled by a named participant, so this shows who was actually in the study that night.
        cursor.execute(
            """
            SELECT user_id, questionnaire_type,
                   DATE_FORMAT(response_date, '%%Y-%%m-%%d') AS response_date
            FROM questionnaire_submissions
            WHERE response_date BETWEEN %s AND %s
            """,
            (start_day, end_day),
        )
        questionnaire_rows = cursor.fetchall()

        # Device-level sessions per night, independent of attribution. Used only to detect a night
        # that was physically recorded while the session was credited to someone else: the raw
        # ESP32 data exists, so calling that night "missing" would overstate the gap and send the
        # researcher looking for a hardware problem that never happened.
        cursor.execute(
            """
            SELECT DATE(DATE_ADD(s.started_at, INTERVAL 8 HOUR)) AS sleep_night,
                   COUNT(DISTINCT s.id) AS session_count,
                   GROUP_CONCAT(DISTINCT COALESCE(u.username, '未归属') ORDER BY u.username) AS owners
            FROM sleep_sessions s
            LEFT JOIN users u ON u.id = s.user_id
            WHERE s.last_sample_at IS NOT NULL
              AND DATE(DATE_ADD(s.started_at, INTERVAL 8 HOUR))
                   BETWEEN %s AND %s
            GROUP BY sleep_night
            """,
            (start_day, end_day),
        )
        device_night_rows = cursor.fetchall()

    sessions_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for row in session_rows:
        key = (int(row["user_id"]), row["sleep_night"].isoformat())
        entry = sessions_by_key.setdefault(key, {"sessionCount": 0, "monitoringSeconds": 0})
        entry["sessionCount"] += int(row["session_count"])
        entry["monitoringSeconds"] += max(0, int(row["total_seconds"] or 0))

    questionnaires_by_key: dict[tuple[int, str], set[str]] = {}
    for row in questionnaire_rows:
        key = (int(row["user_id"]), row["response_date"])
        questionnaires_by_key.setdefault(key, set()).add(row["questionnaire_type"])

    device_nights: dict[str, dict[str, Any]] = {
        row["sleep_night"].isoformat(): {
            "sessionCount": int(row["session_count"]),
            "owners": row["owners"] or "",
        }
        for row in device_night_rows
    }

    matrix: list[dict[str, Any]] = []
    for participant in participants:
        user_id = int(participant["id"])
        cells: list[dict[str, Any]] = []
        for day in days:
            key = (user_id, day)
            session = sessions_by_key.get(key)
            completed = questionnaires_by_key.get(key, set())
            device_night = device_nights.get(day)
            cell = {
                "date": day,
                "hasSession": session is not None,
                "sessionCount": session["sessionCount"] if session else 0,
                "monitoringSeconds": session["monitoringSeconds"] if session else 0,
                "hasPreSleep": "pre_sleep" in completed,
                "hasPostWake": "post_wake" in completed,
                # How many sessions the device recorded that night, whoever they were credited to.
                "nightSessionCount": device_night["sessionCount"] if device_night else 0,
                "nightOwners": device_night["owners"] if device_night else "",
            }
            if cell["hasSession"] and cell["hasPreSleep"] and cell["hasPostWake"]:
                cell["status"] = "complete"
            elif not cell["hasSession"] and not cell["hasPreSleep"] and not cell["hasPostWake"]:
                cell["status"] = "missing"
            else:
                cell["status"] = "partial"
            cell["mismatch"] = (
                not cell["hasSession"]
                and cell["nightSessionCount"] > 0
                and (cell["hasPreSleep"] or cell["hasPostWake"])
            )
            cells.append(cell)
        matrix.append({"username": participant["username"], "cells": cells})

    totals = {"complete": 0, "partial": 0, "missing": 0, "mismatch": 0}
    for row in matrix:
        for cell in row["cells"]:
            totals[cell["status"]] += 1
            if cell["mismatch"]:
                totals["mismatch"] += 1
    return {
        "startDate": start_day.isoformat(),
        "endDate": end_day.isoformat(),
        "dates": days,
        "participants": matrix,
        "totals": totals,
    }


def load_admin_sleep_export_sessions(
    connection: pymysql.connections.Connection, start_utc: datetime, end_utc: datetime,
    username: str = "",
) -> list[dict[str, Any]]:
    """Load finished or safely stale sessions for every attributed ordinary user.

    A session is grouped by the China-local day on which its last sample occurred. This means an
    22:00--08:00 recording appears under the morning date, regardless of when the user went to bed.
    Stale open sessions are read as finished without mutating database state during a download.

    Attribution prefers s.user_id, which was stamped when the session was created. The fallback
    to user_current_devices only covers rows recorded before that column existed: it answers
    "who owns this device now", which is wrong after a handover, so it must never take priority.
    """
    user_clause = " AND u.username = %s" if username else ""
    parameters: list[Any] = [
        start_utc, end_utc, SESSION_STALE_AFTER_MINUTES, start_utc, end_utc,
    ]
    if username:
        parameters.append(username)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT u.id AS user_id, u.username, d.id AS device_database_id,
                   d.device_id, d.display_name, s.id AS session_id, s.started_at,
                   COALESCE(s.ended_at, s.last_sample_at) AS ended_at,
                   s.start_source, COALESCE(s.end_source, 'data_gap') AS end_source,
                   MIN(t.sampled_at) AS first_sample_at, MAX(t.sampled_at) AS last_sample_at,
                   COUNT(t.id) AS record_count, AVG(t.heart_rate) AS average_heart_rate,
                   AVG(t.respiratory_rate) AS average_respiratory_rate,
                   AVG(t.temperature) AS average_temperature
            FROM sleep_sessions s
            JOIN devices d ON d.id = s.device_id
            LEFT JOIN user_current_devices ucd ON ucd.device_id = d.id
            JOIN users u ON u.id = COALESCE(s.user_id, ucd.user_id)
                AND u.role = 'user' AND u.is_active = 1
            LEFT JOIN telemetry t ON t.device_id = s.device_id
                AND t.sampled_at >= s.started_at
                AND t.sampled_at <= COALESCE(s.ended_at, s.last_sample_at)
            WHERE s.last_sample_at IS NOT NULL
               AND (
                    (s.ended_at IS NOT NULL AND s.ended_at >= %s AND s.ended_at < %s)
                 OR (s.ended_at IS NULL
                     AND s.last_sample_at < UTC_TIMESTAMP() - INTERVAL %s MINUTE
                     AND s.last_sample_at >= %s AND s.last_sample_at < %s)
               )
              """ + user_clause + """
            GROUP BY u.id, u.username, d.id, d.device_id, d.display_name, s.id,
                     s.started_at, s.ended_at, s.last_sample_at, s.start_source, s.end_source
            HAVING COUNT(t.id) > 0
            ORDER BY ended_at ASC, u.username ASC, s.id ASC
            """,
            parameters,
        )
        return cursor.fetchall()


def load_sleep_export_samples(
    connection: pymysql.connections.Connection, session: dict[str, Any]
) -> list[dict[str, Any]]:
    """Read the raw records belonging to one exported sleep session in time order."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, sampled_at, heart_rate, respiratory_rate, temperature, sleep_stage,
                   confidence, time_source, analog_voltage, fpga_input, received_at
            FROM telemetry
            WHERE device_id = %s AND sampled_at >= %s AND sampled_at <= %s
            ORDER BY sampled_at ASC, id ASC
            """,
            (session["device_database_id"], session["started_at"], session["ended_at"]),
        )
        return cursor.fetchall()


def sleep_duration_seconds_from_samples(samples: list[dict[str, Any]]) -> int:
    """Use the same awake/gap rules as the App's stage timeline for export totals."""
    if not samples:
        return 0
    total = 0
    stage = samples[0]["sleep_stage"]
    segment_start = samples[0]["sampled_at"]
    previous_at = segment_start
    for sample in samples[1:]:
        sampled_at = sample["sampled_at"]
        stage_changed = sample["sleep_stage"] != stage
        data_gap = sampled_at - previous_at > timedelta(seconds=90)
        if stage_changed or data_gap:
            segment_end = sampled_at if stage_changed else previous_at + timedelta(seconds=10)
            if segment_end > segment_start and not is_awake_stage(stage):
                total += int((segment_end - segment_start).total_seconds())
            stage = sample["sleep_stage"]
            segment_start = sampled_at
        previous_at = sampled_at
    if previous_at + timedelta(seconds=10) > segment_start and not is_awake_stage(stage):
        total += int((previous_at + timedelta(seconds=10) - segment_start).total_seconds())
    return total


def sleep_export_zip(
    connection: pymysql.connections.Connection, sessions: list[dict[str, Any]]
) -> bytes:
    """Build a ZIP with a management overview plus one raw CSV per China day and user."""
    overview_rows: list[list[object]] = []
    grouped_rows: dict[tuple[str, int], list[list[object]]] = {}
    grouped_names: dict[tuple[str, int], str] = {}
    for session in sessions:
        samples = load_sleep_export_samples(connection, session)
        if not samples:
            continue
        sleep_day = format_china_datetime(session["ended_at"]).split(" ", 1)[0]
        monitoring_seconds = max(
            0, int((samples[-1]["sampled_at"] - samples[0]["sampled_at"]).total_seconds())
        )
        sleep_seconds = sleep_duration_seconds_from_samples(samples)
        overview_rows.append([
            sleep_day,
            session["username"],
            session["device_id"],
            session["display_name"],
            format_china_datetime(session["started_at"]),
            format_china_datetime(session["ended_at"]),
            monitoring_seconds,
            sleep_seconds,
            int(session["record_count"]),
            "" if session["average_heart_rate"] is None else f"{float(session['average_heart_rate']):.1f}",
            "" if session["average_respiratory_rate"] is None else f"{float(session['average_respiratory_rate']):.1f}",
            "" if session["average_temperature"] is None else f"{float(session['average_temperature']):.1f}",
            session["start_source"],
            session["end_source"],
        ])
        group_key = (sleep_day, int(session["user_id"]))
        grouped_names[group_key] = safe_filename_component(session["username"])
        raw_rows = grouped_rows.setdefault(group_key, [])
        for sample in samples:
            raw_rows.append([
                session["session_id"],
                session["username"],
                session["device_id"],
                format_china_datetime(session["started_at"]),
                format_china_datetime(session["ended_at"]),
                format_china_datetime(sample["sampled_at"]),
                f"{float(sample['heart_rate']):.1f}",
                f"{float(sample['respiratory_rate']):.1f}",
                f"{float(sample['temperature']):.1f}",
                sample["sleep_stage"],
                f"{float(sample['confidence']):.1f}",
                sample["time_source"] or "",
                "" if sample["analog_voltage"] is None else f"{float(sample['analog_voltage']):.3f}",
                "" if sample["fpga_input"] is None else sample["fpga_input"],
                format_china_datetime(sample["received_at"]),
            ])

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "sleep_overview.csv",
            csv_bytes(
                [
                    "睡眠日期（以结束日计，中国时间）", "账号", "设备编号", "设备名称",
                    "监测开始（中国时间）", "监测结束（中国时间）", "监测时长（秒）",
                    "睡眠时长（秒，不含清醒）", "采样条数", "平均心率（bpm）",
                    "平均呼吸率（次/分）", "平均温度（℃）", "开始来源", "结束来源",
                ],
                overview_rows,
            ),
        )
        raw_header = [
            "会话编号", "账号", "设备编号", "会话开始（中国时间）", "会话结束（中国时间）",
            "采样时间（中国时间）", "心率（bpm）", "呼吸率（次/分）", "温度（℃）", "睡眠阶段",
            "置信度（%）", "时间来源", "模拟电压（V）", "FPGA输入", "服务器接收时间（中国时间）",
        ]
        for (sleep_day, user_id), rows in sorted(grouped_rows.items()):
            username = grouped_names[(sleep_day, user_id)]
            if username == "user":
                username = f"user{user_id}"
            archive.writestr(f"{sleep_day}_{username}_sleep.csv", csv_bytes(raw_header, rows))
    return archive_buffer.getvalue()


class PillowApiHandler(BaseHTTPRequestHandler):
    server_version = "SleepPillowAPI/1.0"

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_binary(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        """Return an authenticated questionnaire image without writing it to a public directory."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_download(self, content_type: str, filename: str, body: bytes) -> None:
        """Return an authenticated in-memory export with a browser download filename."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        super().log_message(format, *args)

    def send_html(self, status: HTTPStatus, content: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def require_user(self) -> dict[str, Any] | None:
        supplied = extract_bearer_token(self.headers)
        if supplied is None:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            return None
        with db_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT s.id AS session_id, u.id AS user_id, u.username, u.role
                FROM auth_sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = %s AND s.expires_at > UTC_TIMESTAMP()
                  AND s.revoked_at IS NULL AND u.is_active = 1
                """,
                (token_hash(supplied),),
            )
            user = cursor.fetchone()
        if user is None:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Session expired or invalid"})
        return user

    def require_admin(self) -> dict[str, Any] | None:
        user = self.require_user()
        if user is not None and user["role"] != "admin":
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Administrator access is required"})
            return None
        return user

    def require_device_token(self, device_id: str) -> int | None:
        supplied = extract_bearer_token(self.headers)
        if supplied is None:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            return None
        with db_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.id
                FROM devices d
                JOIN device_credentials c ON c.device_id = d.id
                WHERE d.device_id = %s AND d.is_active = 1
                  AND c.token_hash = %s AND c.revoked_at IS NULL
                """,
                (device_id, token_hash(supplied)),
            )
            row = cursor.fetchone()
        if row is None:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            return None
        return int(row["id"])

    def read_json_body(self) -> dict[str, Any] | None:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self.send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length is required"})
            return None
        try:
            size = int(content_length)
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"})
            return None
        if not 1 <= size <= MAX_BODY_BYTES:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request body must be 1-16384 bytes"})
            return None
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be valid JSON"})
            return None
        if not isinstance(payload, dict):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON body must be an object"})
            return None
        return payload

    def read_image_attachment_body(self) -> bytes | None:
        """Read one bounded binary image after its authenticated metadata is checked."""
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self.send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length is required"})
            return None
        try:
            size = int(content_length)
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"})
            return None
        if not 1 <= size <= MAX_IMAGE_ATTACHMENT_BYTES:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Image must be 1-10485760 bytes"})
            return None
        image_data = self.rfile.read(size)
        if len(image_data) != size:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Incomplete image upload"})
            return None
        return image_data

    @staticmethod
    def route_device_id(path: str, suffix: str) -> str | None:
        prefix = "/api/v1/devices/"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        candidate = path[len(prefix) : -len(suffix)]
        return candidate if DEVICE_ID_RE.fullmatch(candidate) else None

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/admin":
            try:
                self.send_html(HTTPStatus.OK, ADMIN_DASHBOARD_PATH.read_bytes())
            except OSError:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Admin dashboard is not installed"})
            return
        if parsed.path == "/health":
            try:
                with db_connection() as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
            except pymysql.MySQLError:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "database_unavailable"})
                return
            self.send_json(HTTPStatus.OK, {"status": "ok", "time": format_timestamp(utc_now())})
            return

        user = self.require_user()
        if user is None:
            return

        attachment_prefix = "/api/v1/questionnaire-attachments/"
        if parsed.path.startswith(attachment_prefix):
            attachment_text = parsed.path[len(attachment_prefix) :]
            try:
                attachment_id = int(attachment_text)
            except ValueError:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Attachment not found"})
                return
            if attachment_id < 1:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Attachment not found"})
                return
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT user_id, content_type, image_data FROM questionnaire_attachments WHERE id = %s",
                    (attachment_id,),
                )
                attachment = cursor.fetchone()
            if attachment is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Attachment not found"})
                return
            if user["role"] != "admin" and int(attachment["user_id"]) != int(user["user_id"]):
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "Attachment belongs to another user"})
                return
            self.send_binary(HTTPStatus.OK, attachment["content_type"], bytes(attachment["image_data"]))
            return

        if parsed.path == "/api/v1/me":
            self.send_json(HTTPStatus.OK, {
                "username": user["username"], "role": user["role"],
            })
            return

        if parsed.path == "/api/v1/me/current-device":
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT d.device_id, d.display_name, d.is_active, ucd.selected_at
                    FROM user_current_devices ucd JOIN devices d ON d.id = ucd.device_id
                    WHERE ucd.user_id = %s
                    """,
                    (user["user_id"],),
                )
                row = cursor.fetchone()
            if row is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "No current device selected"})
            else:
                self.send_json(HTTPStatus.OK, {
                    "deviceId": row["device_id"], "displayName": row["display_name"],
                    "isActive": bool(row["is_active"]), "selectedAt": format_timestamp(row["selected_at"]),
                })
            return

        if parsed.path == "/api/v1/me/device-recording":
            with db_connection() as connection:
                device = current_device(connection, int(user["user_id"]))
                if device is None:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "No current device selected"})
                    return
                recording = active_device_recording(connection, int(device["id"]))
            self.send_json(HTTPStatus.OK, {
                "deviceId": device["device_id"],
                "isRecording": recording is not None,
                "startedAt": format_timestamp(recording["started_at"]) if recording else None,
                "lastSampleAt": format_timestamp(recording["last_sample_at"]) if recording and recording["last_sample_at"] else None,
            })
            return

        if parsed.path == "/api/v1/me/questionnaires":
            # The App needs only completion state, not the answers it already submitted.
            # One sleep night = one date key. The bedtime form is filed under the night it
            # starts, and the next-morning form is filed under that same night, so both rows
            # carry the same response_date. PSQI stays one response per experimental phase.
            query = parse_qs(parsed.query)
            legacy_date = query.get("date", [china_now().date().isoformat()])[0]
            requested_pre_date = query.get("preDate", [legacy_date])[0]
            requested_post_date = query.get("postDate", [legacy_date])[0]
            try:
                pre_sleep_date = parse_questionnaire_date(requested_pre_date)
                post_wake_date = parse_questionnaire_date(requested_post_date)
            except ValueError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "preDate and postDate must be YYYY-MM-DD"})
                return
            # A caller that sends one date for both halves is already speaking the right
            # language; only refuse when the two disagree, since a night cannot have two keys.
            if pre_sleep_date != post_wake_date:
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "preDate and postDate must be the same sleep night"},
                )
                return
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, questionnaire_type, response_date, submitted_at, updated_at
                    FROM questionnaire_submissions
                    WHERE user_id = %s
                      AND ((questionnaire_type IN ('pre_sleep', 'post_wake') AND response_date = %s)
                           OR questionnaire_type IN ('psqi_before', 'psqi_after'))
                    ORDER BY submitted_at DESC
                    """,
                    (user["user_id"], pre_sleep_date),
                )
                rows = cursor.fetchall()
            self.send_json(HTTPStatus.OK, {
                "responseDate": pre_sleep_date.isoformat(),
                "preSleepDate": pre_sleep_date.isoformat(),
                "postWakeDate": post_wake_date.isoformat(),
                "items": [questionnaire_submission_to_response(row) for row in rows],
            })
            return

        if parsed.path == "/api/v1/me/sleep-reports":
            # All report periods use China-local calendar boundaries, while MySQL stores UTC.
            # This makes a session ending at 07:00 China time belong to that morning's report.
            query = parse_qs(parsed.query)
            requested_anchor = query.get("date", [""])[0].strip()
            if requested_anchor:
                try:
                    anchor_date = parse_questionnaire_date(requested_anchor)
                except ValueError:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "date must be YYYY-MM-DD"})
                    return
                now_china = datetime.combine(anchor_date, datetime.min.time(), tzinfo=CHINA_TIMEZONE)
            else:
                now_china = china_now()
            day_start_china = now_china.replace(hour=0, minute=0, second=0, microsecond=0)
            week_start_china = day_start_china - timedelta(days=day_start_china.weekday())
            week_start_utc = week_start_china.astimezone(timezone.utc).replace(tzinfo=None)
            month_start_utc = day_start_china.replace(day=1).astimezone(timezone.utc).replace(tzinfo=None)
            history_start_utc = (day_start_china - timedelta(days=366)).astimezone(timezone.utc).replace(tzinfo=None)
            day_end_utc = (day_start_china + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
            week_end_utc = (week_start_china + timedelta(days=7)).astimezone(timezone.utc).replace(tzinfo=None)
            if day_start_china.month == 12:
                next_month_start_china = day_start_china.replace(year=day_start_china.year + 1, month=1, day=1)
            else:
                next_month_start_china = day_start_china.replace(month=day_start_china.month + 1, day=1)
            month_end_utc = next_month_start_china.astimezone(timezone.utc).replace(tzinfo=None)
            with db_connection() as connection:
                device = current_device(connection, int(user["user_id"]))
                if device is None:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "No current device selected"})
                    return
                recording = active_device_recording(connection, int(device["id"]))
                history = load_completed_sessions(
                    connection, int(device["id"]), history_start_utc, 500, end_at=day_end_utc
                )
                latest = history[:1]
                last_night = select_last_night_report(history, now_china)
                if latest:
                    attach_stage_segments(connection, int(device["id"]), latest[0])
                if last_night is not None and (not latest or last_night is not latest[0]):
                    attach_stage_segments(connection, int(device["id"]), last_night)
                # A week may include a main night plus several short daytime tests. Keep all of
                # them so the calendar and the day view can show “日间小憩” without hiding nights.
                week = load_completed_sessions(
                    connection, int(device["id"]), week_start_utc, 50, include_stage_segments=True,
                    end_at=week_end_utc,
                )
                month = load_completed_sessions(
                    connection, int(device["id"]), month_start_utc, 50, include_stage_segments=True,
                    end_at=month_end_utc,
                )
            self.send_json(HTTPStatus.OK, {
                "deviceId": device["device_id"],
                "deviceName": device["display_name"],
                "timezone": "Asia/Shanghai",
                "generatedAt": format_timestamp(utc_now()),
                "anchorDate": day_start_china.date().isoformat(),
                "activeSession": {
                    "id": int(recording["id"]),
                    "startedAt": format_timestamp(recording["started_at"]),
                    "lastSampleAt": format_timestamp(recording["last_sample_at"]) if recording["last_sample_at"] else None,
                } if recording else None,
                "latest": latest[0] if latest else None,
                "lastNight": last_night,
                "week": week,
                "month": month,
            })
            return

        if parsed.path == "/api/v1/admin/exports/questionnaires":
            if user["role"] != "admin":
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "Administrator access is required"})
                return
            query = parse_qs(parsed.query)
            requested_type = query.get("type", [""])[0].strip()
            requested_user = query.get("username", [""])[0].strip()
            if requested_type and requested_type not in QUESTIONNAIRE_TYPES:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Unknown questionnaire type"})
                return
            if len(requested_user) > 64:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid username"})
                return
            try:
                start_day, end_day = parse_admin_export_range(query)
            except ValueError as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            clauses = ["s.response_date >= %s", "s.response_date <= %s"]
            parameters: list[Any] = [start_day, end_day]
            if requested_type:
                clauses.append("s.questionnaire_type = %s")
                parameters.append(requested_type)
            if requested_user:
                clauses.extend(["u.username = %s", "u.role = 'user'"])
                parameters.append(requested_user)
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT s.id, s.user_id, s.questionnaire_type, s.response_date, s.answers_json,
                           s.submitted_at, s.updated_at, u.username
                    FROM questionnaire_submissions s
                    JOIN users u ON u.id = s.user_id
                    WHERE """ + " AND ".join(clauses) + """
                    ORDER BY s.response_date ASC, u.username ASC, s.questionnaire_type ASC, s.id ASC
                    """,
                    parameters,
                )
                rows = cursor.fetchall()
                attachment_ids: set[int] = set()
                for row in rows:
                    try:
                        answers = json.loads(row["answers_json"])
                    except (TypeError, json.JSONDecodeError):
                        answers = {}
                    if not isinstance(answers, dict):
                        continue
                    for answer in answers.values():
                        if not isinstance(answer, dict):
                            continue
                        try:
                            attachment_id = int(answer.get("attachmentId", 0))
                        except (TypeError, ValueError):
                            attachment_id = 0
                        if attachment_id > 0:
                            attachment_ids.add(attachment_id)
                attachments: list[dict[str, Any]] = []
                if attachment_ids:
                    placeholders = ",".join(["%s"] * len(attachment_ids))
                    cursor.execute(
                        """
                        SELECT a.id, a.user_id, a.questionnaire_type, a.response_date,
                               a.attachment_key, a.content_type, a.image_data, u.username
                        FROM questionnaire_attachments a
                        JOIN users u ON u.id = a.user_id
                        WHERE a.id IN (""" + placeholders + ") ORDER BY a.id",
                        list(sorted(attachment_ids)),
                    )
                    selected_keys = {
                        (int(row["user_id"]), row["questionnaire_type"], row["response_date"])
                        for row in rows
                    }
                    attachments = [
                        attachment for attachment in cursor.fetchall()
                        if (
                            int(attachment["user_id"]), attachment["questionnaire_type"],
                            attachment["response_date"],
                        ) in selected_keys
                    ]
            if not rows:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "No questionnaire submissions in the selected dates"})
                return
            user_suffix = safe_filename_component(requested_user) if requested_user else "all_users"
            type_suffix = requested_type or "all_questionnaires"
            filename = (
                f"questionnaires_{start_day.isoformat()}_to_{end_day.isoformat()}_"
                f"{user_suffix}_{type_suffix}_with_images.zip"
            )
            self.send_download("application/zip", filename, questionnaire_export_zip(rows, attachments))
            return

        if parsed.path == "/api/v1/admin/exports/sleep":
            if user["role"] != "admin":
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "Administrator access is required"})
                return
            query = parse_qs(parsed.query)
            requested_user = query.get("username", [""])[0].strip()
            if len(requested_user) > 64:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid username"})
                return
            try:
                start_day, end_day = parse_admin_export_range(query)
            except ValueError as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            start_utc, end_utc = china_day_bounds_utc(start_day, end_day)
            with db_connection() as connection:
                sessions = load_admin_sleep_export_sessions(
                    connection, start_utc, end_utc, requested_user,
                )
                if not sessions:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "No completed sleep records in the selected dates"})
                    return
                archive = sleep_export_zip(connection, sessions)
            user_suffix = safe_filename_component(requested_user) if requested_user else "all_users"
            filename = f"sleep_{start_day.isoformat()}_to_{end_day.isoformat()}_{user_suffix}.zip"
            self.send_download("application/zip", filename, archive)
            return

        if parsed.path == "/api/v1/admin/completeness":
            if user["role"] != "admin":
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "Administrator access is required"})
                return
            query = parse_qs(parsed.query)
            requested_start = query.get("start", [""])[0].strip()
            requested_end = query.get("end", [""])[0].strip()
            if not requested_start or not requested_end:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "start and end must be YYYY-MM-DD"})
                return
            try:
                start_day = parse_questionnaire_date(requested_start)
                end_day = parse_questionnaire_date(requested_end)
            except ValueError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "start and end must be YYYY-MM-DD"})
                return
            with db_connection() as connection:
                try:
                    payload = load_completeness_matrix(connection, start_day, end_day)
                except ValueError as error:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
            self.send_json(HTTPStatus.OK, payload)
            return

        if parsed.path == "/api/v1/admin/users":
            if user["role"] != "admin":
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "Administrator access is required"})
                return
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT u.username, u.role, u.is_active, u.created_at,
                           d.device_id AS current_device_id, d.display_name AS current_device_name,
                           ucd.selected_at
                    FROM users u
                    LEFT JOIN user_current_devices ucd ON ucd.user_id = u.id
                    LEFT JOIN devices d ON d.id = ucd.device_id
                    ORDER BY FIELD(u.role, 'admin', 'user'), u.username
                    """
                )
                rows = cursor.fetchall()
            self.send_json(HTTPStatus.OK, {"items": [{
                "username": row["username"], "role": row["role"], "isActive": bool(row["is_active"]),
                "createdAt": format_timestamp(row["created_at"]),
                "currentDeviceId": row["current_device_id"], "currentDeviceName": row["current_device_name"],
                "selectedAt": format_timestamp(row["selected_at"]) if row["selected_at"] else None,
            } for row in rows]})
            return

        if parsed.path == "/api/v1/admin/questionnaires":
            if user["role"] != "admin":
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "Administrator access is required"})
                return
            query = parse_qs(parsed.query)
            requested_date = query.get("date", [china_now().date().isoformat()])[0]
            requested_user = query.get("username", [""])[0].strip()
            requested_type = query.get("type", [""])[0].strip()
            try:
                response_date = parse_questionnaire_date(requested_date)
            except ValueError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "date must be YYYY-MM-DD"})
                return
            if requested_type and requested_type not in QUESTIONNAIRE_TYPES:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Unknown questionnaire type"})
                return
            with db_connection() as connection, connection.cursor() as cursor:
                # The selected date is a sleep-night date (the date of the bedtime form).
                # Daily diary rows stay in this result; PSQI is deliberately returned in a
                # separate collection so it is not repeated in every daily review.
                daily_clauses = [
                    "s.response_date = %s",
                    "s.questionnaire_type IN ('pre_sleep', 'post_wake')",
                ]
                daily_parameters: list[Any] = [response_date]
                if requested_user:
                    daily_clauses.append("u.username = %s")
                    daily_parameters.append(requested_user)
                if requested_type:
                    daily_clauses.append("s.questionnaire_type = %s")
                    daily_parameters.append(requested_type)
                cursor.execute(
                    """
                    SELECT s.id, s.questionnaire_type, s.response_date, s.answers_json,
                           s.submitted_at, s.updated_at, u.username
                    FROM questionnaire_submissions s
                    JOIN users u ON u.id = s.user_id
                    WHERE """ + " AND ".join(daily_clauses) + " ORDER BY u.username, s.questionnaire_type, s.updated_at DESC",
                    daily_parameters,
                )
                daily_rows = cursor.fetchall()
                psqi_clauses = ["s.questionnaire_type IN ('psqi_before', 'psqi_after')"]
                psqi_parameters: list[Any] = []
                if requested_user:
                    psqi_clauses.append("u.username = %s")
                    psqi_parameters.append(requested_user)
                if requested_type:
                    psqi_clauses.append("s.questionnaire_type = %s")
                    psqi_parameters.append(requested_type)
                cursor.execute(
                    """
                    SELECT s.id, s.questionnaire_type, s.response_date, s.answers_json,
                           s.submitted_at, s.updated_at, u.username
                    FROM questionnaire_submissions s
                    JOIN users u ON u.id = s.user_id
                    WHERE """ + " AND ".join(psqi_clauses) + " ORDER BY u.username, s.questionnaire_type, s.updated_at DESC",
                    psqi_parameters,
                )
                psqi_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT u.username,
                           MAX(CASE WHEN s.questionnaire_type = 'pre_sleep' THEN 1 ELSE 0 END) AS pre_sleep_completed,
                           MAX(CASE WHEN s.questionnaire_type = 'post_wake' THEN 1 ELSE 0 END) AS post_wake_completed,
                           EXISTS(SELECT 1 FROM questionnaire_submissions q
                                  WHERE q.user_id = u.id AND q.questionnaire_type = 'psqi_before') AS psqi_before_completed,
                           EXISTS(SELECT 1 FROM questionnaire_submissions q
                                  WHERE q.user_id = u.id AND q.questionnaire_type = 'psqi_after') AS psqi_after_completed
                    FROM users u
                    LEFT JOIN questionnaire_submissions s
                      ON s.user_id = u.id AND s.response_date = %s
                      AND s.questionnaire_type IN ('pre_sleep', 'post_wake')
                    WHERE u.role = 'user' AND u.is_active = 1
                    GROUP BY u.id, u.username
                    ORDER BY u.username
                    """,
                    (response_date,),
                )
                completion_rows = cursor.fetchall()
            self.send_json(HTTPStatus.OK, {
                "responseDate": response_date.isoformat(),
                "sleepNightDate": response_date.isoformat(),
                "items": [questionnaire_submission_to_response(row, include_answers=True) for row in daily_rows],
                "psqiItems": [questionnaire_submission_to_response(row, include_answers=True) for row in psqi_rows],
                "completion": [{
                    "username": row["username"],
                    "preSleepCompleted": bool(row["pre_sleep_completed"]),
                    "postWakeCompleted": bool(row["post_wake_completed"]),
                    "psqiBeforeCompleted": bool(row["psqi_before_completed"]),
                    "psqiAfterCompleted": bool(row["psqi_after_completed"]),
                } for row in completion_rows],
            })
            return

        if parsed.path == "/api/v1/devices":
            if user["role"] != "admin":
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "Administrator access is required"})
                return
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT d.device_id, d.display_name, d.is_active, d.created_at,
                           MAX(t.received_at) AS last_received_at
                    FROM devices d LEFT JOIN telemetry t ON t.device_id = d.id
                    GROUP BY d.id, d.device_id, d.display_name, d.is_active, d.created_at
                    ORDER BY d.device_id
                    """
                )
                rows = cursor.fetchall()
            self.send_json(HTTPStatus.OK, {"items": [{
                "deviceId": row["device_id"], "displayName": row["display_name"],
                "isActive": bool(row["is_active"]), "createdAt": format_timestamp(row["created_at"]),
                "lastReceivedAt": format_timestamp(row["last_received_at"]) if row["last_received_at"] else None,
            } for row in rows]})
            return

        device_id = self.route_device_id(parsed.path, "/latest")
        if device_id:
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT t.*, d.device_id, d.display_name
                    FROM telemetry t JOIN devices d ON d.id = t.device_id
                    WHERE d.device_id = %s ORDER BY t.sampled_at DESC, t.id DESC LIMIT 1
                    """,
                    (device_id,),
                )
                row = cursor.fetchone()
            if row is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "No telemetry found for this device"})
            else:
                self.send_json(HTTPStatus.OK, telemetry_to_response(row))
            return

        device_id = self.route_device_id(parsed.path, "/telemetry")
        if device_id:
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            # 管理员按整晚或多日导出时需要超过页面展示用的 100 条记录。
            # 100,000 条约为 11 天半的十秒采样数据，避免一次请求无限制占用服务器内存。
            limit = min(max(limit, 1), 100_000)
            start_at = None
            end_at = None
            try:
                if "start" in query:
                    start_at = parse_timestamp(query["start"][0])
                if "end" in query:
                    end_at = parse_timestamp(query["end"][0])
            except ValueError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "start and end must be ISO-8601 timestamps with a timezone"})
                return
            if start_at is not None and end_at is not None and start_at >= end_at:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "start must be earlier than end"})
                return
            clauses = ["d.device_id = %s"]
            parameters: list[Any] = [device_id]
            if start_at is not None:
                clauses.append("t.sampled_at >= %s")
                parameters.append(start_at)
            if end_at is not None:
                clauses.append("t.sampled_at <= %s")
                parameters.append(end_at)
            parameters.append(limit)
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """ 
                    SELECT t.*, d.device_id, d.display_name
                    FROM telemetry t JOIN devices d ON d.id = t.device_id
                    WHERE """ + " AND ".join(clauses) + " ORDER BY t.sampled_at DESC, t.id DESC LIMIT %s",
                    parameters,
                )
                rows = cursor.fetchall()
            self.send_json(HTTPStatus.OK, {
                "deviceId": device_id, "items": [telemetry_to_response(row) for row in reversed(rows)],
                "limit": limit, "truncated": len(rows) == limit,
            })
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/v1/auth/login":
            payload = self.read_json_body()
            if payload is None:
                return
            username = str(payload.get("username", "")).strip()
            password = payload.get("password")
            if not 1 <= len(username) <= 64 or not isinstance(password, str):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Username and password are required"})
                return
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, username, password_hash, role FROM users WHERE username = %s AND is_active = 1",
                    (username,),
                )
                account = cursor.fetchone()
            if account is None or not verify_password(password, account["password_hash"]):
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Incorrect username or password"})
                return
            session_token = secrets.token_urlsafe(32)
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO auth_sessions (user_id, token_hash, expires_at)
                    VALUES (%s, %s, UTC_TIMESTAMP() + INTERVAL %s DAY)
                    """,
                    (account["id"], token_hash(session_token), SESSION_LIFETIME_DAYS),
                )
            self.send_json(HTTPStatus.OK, {
                "accessToken": session_token, "expiresInDays": SESSION_LIFETIME_DAYS,
                "username": account["username"], "role": account["role"],
            })
            return

        device_id = self.route_device_id(parsed.path, "/telemetry")
        if device_id is not None:
            database_device_id = self.require_device_token(device_id)
            if database_device_id is None:
                return
            payload = self.read_json_body()
            if payload is None:
                return

            required = ("timestamp", "heartRate", "respiratoryRate", "temperature", "sleepStage", "confidence")
            missing = [field for field in required if field not in payload]
            if missing:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing required fields", "fields": missing})
                return
            try:
                sampled_at = parse_timestamp(payload["timestamp"])
                heart_rate = finite_number(payload["heartRate"], "heartRate")
                respiratory_rate = finite_number(payload["respiratoryRate"], "respiratoryRate")
                temperature = finite_number(payload["temperature"], "temperature")
                sleep_stage = str(payload["sleepStage"]).strip()
                confidence = finite_number(payload["confidence"], "confidence")
                time_source = str(payload.get("timeSource", ""))[:32] or None
                analog_voltage = finite_number(payload["analogVoltage"], "analogVoltage") if payload.get("analogVoltage") is not None else None
                fpga_input = int(payload["fpgaInput"]) if payload.get("fpgaInput") is not None else None
            except (TypeError, ValueError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Telemetry has an invalid field or timestamp"})
                return
            if not sleep_stage or len(sleep_stage) > 64 or fpga_input not in (None, 0, 1):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid sleepStage or fpgaInput"})
                return

            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO telemetry (
                        device_id, sampled_at, heart_rate, respiratory_rate, temperature,
                        sleep_stage, confidence, time_source, analog_voltage, fpga_input, received_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP())
                    ON DUPLICATE KEY UPDATE
                        heart_rate = VALUES(heart_rate), respiratory_rate = VALUES(respiratory_rate),
                        temperature = VALUES(temperature), sleep_stage = VALUES(sleep_stage),
                        confidence = VALUES(confidence), time_source = VALUES(time_source),
                        analog_voltage = VALUES(analog_voltage), fpga_input = VALUES(fpga_input),
                        received_at = UTC_TIMESTAMP()
                    """,
                    (database_device_id, sampled_at, heart_rate, respiratory_rate, temperature, sleep_stage,
                     confidence, time_source, analog_voltage, fpga_input),
                )
                cursor.execute(
                    "SELECT id FROM telemetry WHERE device_id = %s AND sampled_at = %s",
                    (database_device_id, sampled_at),
                )
                telemetry_id = cursor.fetchone()["id"]
                # Session grouping happens after successful idempotent storage. Re-uploading a
                # queued row may update a session's last sample, but never creates duplicate data.
                attach_telemetry_to_session(connection, database_device_id, sampled_at)
            self.send_json(HTTPStatus.CREATED, {"id": telemetry_id, "deviceId": device_id, "status": "stored"})
            return

        user = self.require_user()
        if user is None:
            return

        if parsed.path == "/api/v1/me/questionnaire-attachments":
            questionnaire_type = self.headers.get("X-Questionnaire-Type", "").strip()
            attachment_key = self.headers.get("X-Questionnaire-Attachment-Key", "").strip()
            if questionnaire_type not in QUESTIONNAIRE_TYPES or attachment_key not in QUESTIONNAIRE_ATTACHMENT_KEYS:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Unknown questionnaire attachment"})
                return
            try:
                response_date = parse_questionnaire_date(self.headers.get("X-Questionnaire-Response-Date", ""))
            except ValueError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "X-Questionnaire-Response-Date must be YYYY-MM-DD"})
                return
            image_data = self.read_image_attachment_body()
            if image_data is None:
                return
            content_type = image_content_type(image_data)
            if content_type is None:
                self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Only JPEG, PNG, and WebP images are supported"})
                return
            digest = hashlib.sha256(image_data).hexdigest()
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO questionnaire_attachments (
                        user_id, questionnaire_type, response_date, attachment_key,
                        content_type, content_length, content_sha256, image_data
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        content_type = VALUES(content_type), content_length = VALUES(content_length),
                        content_sha256 = VALUES(content_sha256), image_data = VALUES(image_data),
                        updated_at = CURRENT_TIMESTAMP()
                    """,
                    (
                        user["user_id"], questionnaire_type, response_date, attachment_key,
                        content_type, len(image_data), digest, image_data,
                    ),
                )
                cursor.execute(
                    """
                    SELECT id FROM questionnaire_attachments
                    WHERE user_id = %s AND questionnaire_type = %s
                      AND response_date = %s AND attachment_key = %s
                    """,
                    (user["user_id"], questionnaire_type, response_date, attachment_key),
                )
                attachment = cursor.fetchone()
            self.send_json(HTTPStatus.CREATED, {
                "id": int(attachment["id"]), "attachmentKey": attachment_key,
                "contentType": content_type, "size": len(image_data),
            })
            return

        if parsed.path == "/api/v1/auth/logout":
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute("UPDATE auth_sessions SET revoked_at = UTC_TIMESTAMP() WHERE id = %s", (user["session_id"],))
            self.send_json(HTTPStatus.OK, {"status": "logged_out"})
            return
        if parsed.path == "/api/v1/me/current-device":
            payload = self.read_json_body()
            if payload is None:
                return
            selected_device_id = str(payload.get("deviceId", "")).strip()
            if not DEVICE_ID_RE.fullmatch(selected_device_id):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid deviceId"})
                return
            with db_connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT id, device_id, display_name FROM devices WHERE device_id = %s AND is_active = 1", (selected_device_id,))
                device = cursor.fetchone()
                if device is None:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Active device not found"})
                    return
                cursor.execute(
                    """
                    INSERT INTO user_current_devices (user_id, device_id, selected_at)
                    VALUES (%s, %s, UTC_TIMESTAMP())
                    ON DUPLICATE KEY UPDATE device_id = VALUES(device_id), selected_at = UTC_TIMESTAMP()
                    """,
                    (user["user_id"], device["id"]),
                )
            self.send_json(HTTPStatus.OK, {
                "deviceId": device["device_id"], "displayName": device["display_name"], "status": "selected",
            })
            return

        if parsed.path == "/api/v1/me/questionnaires":
            payload = self.read_json_body()
            if payload is None:
                return
            questionnaire_type = str(payload.get("questionnaireType", "")).strip()
            if questionnaire_type not in QUESTIONNAIRE_TYPES:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Unknown questionnaire type"})
                return
            try:
                response_date = parse_questionnaire_date(payload.get("responseDate"))
            except ValueError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "responseDate must be YYYY-MM-DD"})
                return
            answers = payload.get("answers")
            if not isinstance(answers, dict) or not answers:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "answers must be a non-empty object"})
                return
            try:
                response_date = resolve_questionnaire_response_date(questionnaire_type, response_date, answers)
            except ValueError as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            try:
                answers_json = json.dumps(answers, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "answers must be JSON-compatible"})
                return
            if len(answers_json.encode("utf-8")) > MAX_QUESTIONNAIRE_ANSWER_BYTES:
                self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Questionnaire answers are too large"})
                return
            with db_connection() as connection, connection.cursor() as cursor:
                # The sleep diary requires an actual adjustment photo whenever “是” is selected.
                # Do not accept a guessed attachment ID from a different user or calendar date.
                if questionnaire_type == "pre_sleep" and answer_value(answers, "parameter_adjustment") == "是":
                    attachment_answer = answers.get("parameter_adjustment_photo")
                    try:
                        attachment_id = int(attachment_answer["attachmentId"])
                    except (KeyError, TypeError, ValueError):
                        self.send_json(HTTPStatus.BAD_REQUEST, {"error": "A parameter adjustment photo is required"})
                        return
                    cursor.execute(
                        """
                        SELECT id FROM questionnaire_attachments
                        WHERE id = %s AND user_id = %s AND questionnaire_type = %s
                          AND response_date = %s AND attachment_key = 'parameter_adjustment_photo'
                        """,
                        (attachment_id, user["user_id"], questionnaire_type, response_date),
                    )
                    if cursor.fetchone() is None:
                        self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Parameter adjustment photo was not found"})
                        return
                if questionnaire_type in ("psqi_before", "psqi_after"):
                    # One before-test and one after-test per participant. Reopening the form
                    # corrects the existing response instead of silently adding a second scale.
                    cursor.execute(
                        """
                        SELECT id FROM questionnaire_submissions
                        WHERE user_id = %s AND questionnaire_type = %s
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (user["user_id"], questionnaire_type),
                    )
                    existing_submission = cursor.fetchone()
                    if existing_submission is not None:
                        cursor.execute(
                            """
                            UPDATE questionnaire_submissions
                            SET answers_json = %s, updated_at = CURRENT_TIMESTAMP()
                            WHERE id = %s
                            """,
                            (answers_json, existing_submission["id"]),
                        )
                    else:
                        cursor.execute(
                            """
                            INSERT INTO questionnaire_submissions (
                                user_id, questionnaire_type, response_date, answers_json
                            ) VALUES (%s, %s, %s, %s)
                            """,
                            (user["user_id"], questionnaire_type, response_date, answers_json),
                        )
                else:
                    cursor.execute(
                        """
                        INSERT INTO questionnaire_submissions (
                            user_id, questionnaire_type, response_date, answers_json
                        ) VALUES (%s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE answers_json = VALUES(answers_json), updated_at = CURRENT_TIMESTAMP()
                        """,
                        (user["user_id"], questionnaire_type, response_date, answers_json),
                    )
                if questionnaire_type in ("psqi_before", "psqi_after"):
                    cursor.execute(
                        """
                        SELECT id, questionnaire_type, response_date, submitted_at, updated_at
                        FROM questionnaire_submissions
                        WHERE user_id = %s AND questionnaire_type = %s
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (user["user_id"], questionnaire_type),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT id, questionnaire_type, response_date, submitted_at, updated_at
                        FROM questionnaire_submissions
                        WHERE user_id = %s AND questionnaire_type = %s AND response_date = %s
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (user["user_id"], questionnaire_type, response_date),
                    )
                submission = cursor.fetchone()
            self.send_json(HTTPStatus.OK, questionnaire_submission_to_response(submission))
            return

        marker_type_by_path = {
            "/api/v1/me/sleep-markers/prepare": "prepare",
            "/api/v1/me/sleep-markers/wake": "wake",
            # Retain the old App routes as harmless manual markers. Older APKs must not be able
            # to split an ESP32-powered data recording merely because a participant taps a button.
            "/api/v1/me/sleep-sessions/start": "prepare",
            "/api/v1/me/sleep-sessions/end": "wake",
        }
        marker_type = marker_type_by_path.get(parsed.path)
        if marker_type in SLEEP_MARKER_TYPES:
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                marked_at = parse_timestamp(payload["timestamp"]) if "timestamp" in payload else utc_now().replace(tzinfo=None)
            except ValueError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "timestamp must be ISO-8601 with a timezone"})
                return
            with db_connection() as connection, connection.cursor() as cursor:
                device = current_device(connection, int(user["user_id"]))
                if device is None:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "No current device selected"})
                    return
                database_device_id = int(device["id"])
                cursor.execute(
                    """
                    INSERT INTO sleep_manual_markers (user_id, device_id, marker_type, marked_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (user["user_id"], database_device_id, marker_type, marked_at),
                )
                marker_id = cursor.lastrowid
            self.send_json(HTTPStatus.CREATED, {
                "id": marker_id, "deviceId": device["device_id"], "markerType": marker_type,
                "markedAt": format_timestamp(marked_at), "status": "recorded",
            })
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found"})


def main() -> None:
    required = ("PILLOW_MYSQL_PASSWORD",)
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    httpd = ThreadingHTTPServer((HOST, PORT), PillowApiHandler)
    print(f"Sleep Pillow API listening on {HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
