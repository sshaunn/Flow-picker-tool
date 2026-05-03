"""Customer-facing translations for scheduler / worker error codes.

The technical strings (``unusual_activity`` / ``audio_generation_failed``
/ ``unusual_activity_strike_3`` etc.) are great for log greps and dev
diagnosis but unreadable for the customer. This module maps them into
plain Chinese: a one-line description plus an operator suggestion the
customer can act on without escalating to a developer.

Anything not in the maps falls back to the raw code so we never hide
information — just translate when we know how.
"""

from __future__ import annotations

from typing import Optional


# (description, suggestion) pairs. Suggestion is None when the system
# handles it automatically and there's nothing for the customer to do.
_TASK_ERROR_MESSAGES: dict[str, tuple[str, Optional[str]]] = {
    "unusual_activity": (
        "Google 检测到该账号最近活动异常，已暂停采集。",
        "已自动进入冷却。冷却结束后系统会自动重试。如频繁出现，可减少同时运行的账号数。",
    ),
    "audio_generation_failed": (
        "Veo 音频生成失败（提示词触发了 Google 内容审查）。",
        "已自动同轮重试。如果反复出现，建议改写提示词中可能敏感的部分。",
    ),
    "generation_failed": (
        "Flow 生成步骤失败（页面操作超时或元素找不到）。",
        "通常是网络抖动或 Flow 临时问题，下一轮会自动重试。",
    ),
    "download_failed": (
        "视频已生成但下载失败。",
        "本任务停止以免烧账号配额。已生成的视频在 Flow 网页上可手动找回。",
    ),
    "login_required": (
        "Google 登录已过期，账号需要重新登录。",
        "请去账号详情页点击「登录并识别 Project」重新登录。",
    ),
    "captcha_or_verification": (
        "Flow 触发了人机验证 / 验证码。",
        "请去账号详情页点击「登录并识别 Project」，在弹出窗口里手动通过验证。",
    ),
    "service_unavailable": (
        "Flow 服务暂时不可用。",
        "已自动重试。如果持续无法访问，可能是 Google 侧维护，稍后再试即可。",
    ),
    "page_failure": (
        "Flow 页面操作失败。",
        "已自动重试。如果连续多次出现，账号可能进入冷却。",
    ),
}


# Workstation cooldown_reason → friendly text. Strike-N variants share
# the same prefix logic; we resolve them dynamically below.
_WS_COOLDOWN_REASONS: dict[str, tuple[str, Optional[str]]] = {
    "consecutive_failure": (
        "该账号连续多次失败，进入冷却。",
        "等待冷却结束后会自动恢复。",
    ),
    "page_failure_window": (
        "短时间内页面错误次数过多，进入冷却。",
        "等待冷却结束后会自动恢复。",
    ),
    "manual_check": (
        "该账号已被标记为需要人工处理。",
        "请去账号详情页点击「登录并识别 Project」重新登录以恢复。",
    ),
}


def task_error_friendly(error_type: Optional[str]) -> Optional[tuple[str, Optional[str]]]:
    """Return (description, suggestion) for a task error_type, or None
    if there's no error to report."""
    if not error_type:
        return None
    if error_type in _TASK_ERROR_MESSAGES:
        return _TASK_ERROR_MESSAGES[error_type]
    return (error_type, None)


def ws_cooldown_friendly(reason: Optional[str]) -> Optional[tuple[str, Optional[str]]]:
    """Return (description, suggestion) for a workstation cooldown_reason."""
    if not reason:
        return None
    # strike-N reasons: "unusual_activity_strike_2", "unusual_activity_strike_4"
    if reason.startswith("unusual_activity_strike_"):
        try:
            n = int(reason.split("_")[-1])
        except ValueError:
            n = 0
        return (
            f"Google 风控第 {n} 次冷却中。",
            "冷却结束后系统会自动重试。"
            if n < 4
            else "已临近上限，再触发 1 次将转为人工处理状态，届时需要重新登录。",
        )
    if reason in _WS_COOLDOWN_REASONS:
        return _WS_COOLDOWN_REASONS[reason]
    # Unknown / composite reasons (e.g. "...:exhausted") — show as-is.
    return (reason, None)
