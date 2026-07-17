"""Compact, Chinese console output for the registration child process."""
from __future__ import annotations

import re
import sys
import time
import warnings
from typing import Any, TextIO

_PREFIXES = {
    "browser": "浏览器",
    "run": "注册",
    "mail": "邮箱",
    "profile": "资料",
    "sso": "认证",
    "cpa": "CPA",
    "import": "导入",
}


def suppress_dependency_warnings() -> None:
    """Hide harmless invalid-escape SyntaxWarnings emitted by DrissionPage on Python 3.13."""
    warnings.filterwarnings(
        "ignore",
        message=r"invalid escape sequence.*",
        category=SyntaxWarning,
    )


def _proxy_label(value: str) -> str:
    return "已配置" if value and value not in {"(none)", "none"} else "未配置"


def _translate_mail(message: str) -> str:
    match = re.fullmatch(r"lol allocated mailbox: (.+)", message)
    if match:
        return f"TempMail.lol 已分配邮箱：{match.group(1)}"
    match = re.fullmatch(r"(.+) allocated mailbox: (.+)", message)
    if match:
        return f"{match.group(1)} 已分配邮箱：{match.group(2)}"
    if message.startswith("verification code received: "):
        return f"已收到验证码：{message.removeprefix('verification code received: ')}"
    if message.startswith("verification code filled: "):
        return "\u9a8c\u8bc1\u7801\u5df2\u586b\u5165: " + message.removeprefix("verification code filled: ")
    if message.startswith("verification submit attempted: "):
        action = message.removeprefix("verification submit attempted: ")
        labels = {"native": "\u6d4f\u89c8\u5668\u539f\u751f\u70b9\u51fb", "request-submitted": "\u8868\u5355\u63d0\u4ea4", "js-clicked": "\u9875\u9762\u70b9\u51fb", "form-submitted": "\u8868\u5355\u515c\u5e95\u63d0\u4ea4", "disabled": "\u63d0\u4ea4\u6309\u94ae\u672a\u5c31\u7eea", "not-found": "\u672a\u627e\u5230\u63d0\u4ea4\u6309\u94ae"}
        return "\u9a8c\u8bc1\u7801\u63d0\u4ea4\u5c1d\u8bd5: " + labels.get(action, action)
    if message.startswith("verification page did not advance: "):
        return "\u9a8c\u8bc1\u7801\u9875\u9762\u672a\u8df3\u8f6c\uff0c\u5f53\u524d\u9875\u9762\u72b6\u6001: " + message.removeprefix("verification page did not advance: ")
    match = re.fullmatch(r"waiting for (.+) verification code, timeout (\d+)s", message)
    if match:
        return f"等待 {match.group(1)} 的验证码（超时 {match.group(2)} 秒）"
    match = re.fullmatch(r"(.+) inbox rate limited; waiting ([\d.]+)s before retry", message)
    if match:
        return f"{match.group(1)} 收件箱触发限流，{match.group(2)} 秒后重试"
    match = re.fullmatch(r"inbox baseline captured: (\d+) messages", message)
    if match:
        return "\u5df2\u8bb0\u5f55\u53d1\u9001\u9a8c\u8bc1\u90ae\u4ef6\u524d\u7684\u6536\u4ef6\u7bb1\u57fa\u7ebf\uff1a" + match.group(1) + " \u5c01\u90ae\u4ef6"
    if message.startswith("inbox baseline capture failed: "):
        return "\u6536\u4ef6\u7bb1\u57fa\u7ebf\u8bb0\u5f55\u5931\u8d25\uff1a" + message.removeprefix("inbox baseline capture failed: ")
    if message.startswith("initial inbox check failed: "):
        return "首次收件箱检查失败：" + message.removeprefix("initial inbox check failed: ").replace(": ", "：", 1)
    if message.startswith("inbox polling failed: "):
        return "收件箱轮询失败：" + message.removeprefix("inbox polling failed: ").replace(": ", "：", 1)
    return message


def _translate_run(message: str) -> str:
    if message.startswith(("verification code filled: ", "verification submit attempted: ", "verification page did not advance: ")):
        return _translate_mail(message)
    return message


def _translate_cpa(message: str) -> str:
    if message.startswith("[cpa] "):
        message = message[6:]
    if message.startswith("queued OIDC export for "):
        return "已加入 OIDC 导出队列：" + message.removeprefix("queued OIDC export for ")
    match = re.fullmatch(r"start OIDC mint email=(.+) output=(.+) proxy=(\w+) protocol=(\w+)", message)
    if match:
        mode = {"preferred": "协议优先", "only": "仅协议", "disabled": "浏览器授权"}.get(match.group(4), match.group(4))
        return f"开始 OIDC 导出：邮箱={match.group(1)}，目录={match.group(2)}，代理={_proxy_label(match.group(3))}，模式={mode}"
    match = re.fullmatch(r"mint start: (.+) proxy=(.*)", message)
    if match:
        return f"开始授权：邮箱={match.group(1)}，代理={_proxy_label(match.group(2))}"
    mapping = {
        "mint protocol SUCCESS": "协议授权成功",
        "mint fallback → browser": "协议授权未完成，切换浏览器授权",
        "mint protocol skipped (no sso cookie) → browser": "缺少 SSO Cookie，切换浏览器授权",
        "mint protocol disabled → browser": "已关闭协议优先，使用浏览器授权",
        "standalone chromium started": "授权浏览器已启动",
        "token poll SUCCESS — stop_event set": "已获取 OIDC 授权结果",
        "stop_event set — leave browser loop": "授权完成，结束浏览器等待",
        "browser finished via stop_event": "浏览器授权流程完成",
        "device done page — waiting for token poll": "设备授权已确认，等待令牌返回",
        "turnstile not ready": "Turnstile 尚未就绪",
    }
    if message in mapping:
        return mapping[message]
    match = re.fullmatch(r"mint try protocol \(SSO HTTP device flow\) attempt=(\d+)/(\d+)", message)
    if match:
        return f"尝试协议授权（第 {match.group(1)}/{match.group(2)} 次）"
    match = re.fullmatch(r"mint protocol rate-limited, sleep (\d+)s before retry", message)
    if match:
        return f"协议授权触发限流，{match.group(1)} 秒后重试"
    if message.startswith("mint protocol failed: "):
        return "协议授权失败：" + message.removeprefix("mint protocol failed: ")
    if message.startswith("mint protocol exception: "):
        return "协议授权异常：" + message.removeprefix("mint protocol exception: ")
    match = re.fullmatch(r"device user_code=.+ expires_in=(\d+) proxy=.*", message)
    if match:
        return f"已创建设备授权请求（有效期 {match.group(1)} 秒）"
    if message.startswith("headed browser DISPLAY="):
        return "以有头模式启动授权浏览器"
    if message.startswith("browser proxy="):
        return "授权浏览器代理：已配置"
    if message == "browser proxy=(none)":
        return "授权浏览器代理：未配置"
    match = re.fullmatch(r"(?:injected cookies bulk via .+|cookie inject count)=(\d+)", message)
    if match:
        return f"已注入登录会话 Cookie（{match.group(1)} 项）"
    if message.startswith("post-inject session url="):
        return "登录会话已恢复，正在打开授权页面"
    if message.startswith("open device url:"):
        return "已打开设备授权页面"
    if message.startswith("url:") or message.startswith("visible:"):
        return "授权页面状态已更新"
    match = re.fullmatch(r"oauth poll: authorization_pending \(sleep (\d+)s\)", message)
    if match:
        return f"等待授权确认（{match.group(1)} 秒后再次检查）"
    if message.startswith("clicked JS exact ") or message.startswith("clicked REAL exact "):
        label = message.split("'", 2)[1] if "'" in message else "按钮"
        return f"已点击“{label}”"
    match = re.fullmatch(r"wrote (.+)", message)
    if match:
        return f"OIDC 档案已写入：{match.group(1)}"
    match = re.fullmatch(r"probe models: ok=True status=(\d+) has_grok_45=True.*", message)
    if match:
        return f"Grok 4.5 可用性检查通过（HTTP {match.group(1)}）"
    if message.startswith("OIDC export completed: "):
        return "OIDC 导出完成：" + message.removeprefix("OIDC export completed: ")
    if message.startswith("OIDC export failed: "):
        return "OIDC 导出失败：" + message.removeprefix("OIDC export failed: ")
    if message.startswith("mint gap protection: waiting "):
        return "导出间隔保护：" + message.removeprefix("mint gap protection: waiting ").replace("s", " 秒")
    if message.startswith("drain timeout;"):
        return "导出队列等待超时，剩余任务将随注册进程结束"
    if message.startswith("background export exception: "):
        return "后台导出异常：" + message.removeprefix("background export exception: ")
    return message


class _PrettyStream:
    def __init__(self, target: TextIO) -> None:
        self._target = target
        self._buffer = ""
        self._last: dict[str, tuple[str, float]] = {}

    def write(self, value: str) -> int:
        self._buffer += value
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit_line(line.rstrip("\r"))
        return len(value)

    def flush(self) -> None:
        if self._buffer:
            self._emit_line(self._buffer.rstrip("\r"))
            self._buffer = ""
        self._target.flush()

    def _emit_line(self, line: str) -> None:
        if not line:
            return
        match = re.match(r"^\[([a-z]+)]\s*(.*)$", line)
        if not match:
            self._target.write(line + "\n")
            return
        raw_component, message = match.groups()
        component = _PREFIXES.get(raw_component, raw_component.upper())
        if raw_component == "mail":
            message = _translate_mail(message)
        elif raw_component == "run":
            message = _translate_run(message)
        elif raw_component == "cpa":
            message = _translate_cpa(message)
        elif raw_component == "browser":
            message = self._compact_turnstile(message)
            if message is None:
                return
        elif raw_component == "import" and "?" in message:
            message = "账号已导入账号池"
        stamp = time.strftime("%H:%M:%S")
        self._target.write(f"[{stamp}] [{component}] {message}\n")
        self._target.flush()

    def _compact_turnstile(self, message: str) -> str | None:
        if message == "检测到 Turnstile，正在等待/触发当前页面的正常验证":
            key, compact = "turnstile-detected", "检测到 Turnstile，等待页面完成正常验证"
        elif message.startswith("Turnstile 控件暂不可交互:") or message.startswith("等待 Turnstile:"):
            state = re.search(r'"state":\s*"([^"]+)"', message)
            inputs = re.search(r'"response_inputs":\s*(\d+)', message)
            current = state.group(1) if state else "unknown"
            count = inputs.group(1) if inputs else "0"
            key, compact = "turnstile-wait", f"Turnstile 等待中（状态：{current}，响应框：{count}）"
        else:
            return message
        previous, at = self._last.get(key, ("", 0.0))
        now = time.monotonic()
        if previous == compact and now - at < 5.0:
            return None
        self._last[key] = (compact, now)
        return compact

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


def install_console() -> None:
    suppress_dependency_warnings()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass
    if not isinstance(sys.stdout, _PrettyStream):
        sys.stdout = _PrettyStream(sys.stdout)  # type: ignore[assignment]
