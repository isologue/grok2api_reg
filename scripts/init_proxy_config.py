#!/usr/bin/env python3
"""在 grok2api 启动前校验并写入 WARP/Privoxy 代理配置。"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import sys
import tomllib
from datetime import datetime, timezone

DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "/app/data"))
CONFIG_PATH = pathlib.Path(
    os.environ.get("CONFIG_LOCAL_PATH", str(DATA_DIR / "config.toml"))
)

PROXY_CONFIG = """[proxy.egress]
mode = "single_proxy"
proxy_url = "http://privoxy:8118"
resource_proxy_url = "http://privoxy:8118"
proxy_pool = []
resource_proxy_pool = []
skip_ssl_verify = false

[proxy.clearance]
mode = "flaresolverr"
cf_cookies = ""
user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
browser = "chrome136"
flaresolverr_url = "http://flaresolverr:8191"
timeout_sec = 60
refresh_interval = 3600
"""


def _hex_preview(raw: bytes, limit: int = 16) -> str:
    return raw[:limit].hex(" ") or "<empty>"


def _backup_config() -> pathlib.Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = CONFIG_PATH.with_name(f"config.toml.invalid.{stamp}")
    index = 1
    while backup.exists():
        backup = CONFIG_PATH.with_name(f"config.toml.invalid.{stamp}.{index}")
        index += 1
    shutil.copy2(CONFIG_PATH, backup)
    return backup


def _read_valid_config() -> tuple[str, dict]:
    """读取并校验配置；仅自动修复 UTF-8 BOM/零宽 BOM 这类可逆污染。"""
    raw = CONFIG_PATH.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(
            "[init-config] 配置文件不是有效 UTF-8，未修改原文件；"
            f"首字节：{_hex_preview(raw)}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    try:
        return text, tomllib.loads(text)
    except tomllib.TOMLDecodeError as original_exc:
        # 某些 Windows 编辑器或旧脚本会把 U+FEFF 写入表头中间，
        # 例如 "[\ufeffproxy.egress]"，Python tomllib 会在第 1 行第 2 列失败。
        normalized = text.replace("\ufeff", "")
        if normalized != text:
            try:
                parsed = tomllib.loads(normalized)
            except tomllib.TOMLDecodeError:
                pass
            else:
                backup = _backup_config()
                CONFIG_PATH.write_text(normalized, encoding="utf-8", newline="\n")
                print(
                    "[init-config] 已修复 config.toml 中的 UTF-8 BOM/零宽字符；"
                    f"原文件备份为 {backup.name}"
                )
                return normalized, parsed

        print(
            "[init-config] config.toml 不是有效 TOML，未修改原文件；"
            f"{original_exc}；首字节：{_hex_preview(raw)}",
            file=sys.stderr,
        )
        print(
            "[init-config] 请先备份并修复 /app/data/config.toml，"
            "再启动 grok2api。",
            file=sys.stderr,
        )
        raise SystemExit(1) from original_exc


DATA_DIR.mkdir(parents=True, exist_ok=True)

if not CONFIG_PATH.exists():
    CONFIG_PATH.write_text(PROXY_CONFIG + "\n", encoding="utf-8", newline="\n")
    print("[init-config] 已创建 config.toml 并写入代理配置")
else:
    content, parsed = _read_valid_config()
    egress = parsed.get("proxy", {}).get("egress", {})
    clearance = parsed.get("proxy", {}).get("clearance", {})
    proxy_url = str(egress.get("proxy_url", ""))
    flaresolverr_url = str(clearance.get("flaresolverr_url", ""))

    if "privoxy" not in proxy_url or "flaresolverr" not in flaresolverr_url:
        # 只替换两个代理段，保留其他后台配置和用户设置。
        updated = re.sub(
            r"\[proxy\.egress\].*?(?=\[|\Z)",
            "",
            content,
            flags=re.DOTALL,
        )
        updated = re.sub(
            r"\[proxy\.clearance\].*?(?=\[|\Z)",
            "",
            updated,
            flags=re.DOTALL,
        )
        updated = updated.rstrip() + "\n" + PROXY_CONFIG + "\n"
        try:
            tomllib.loads(updated)
        except tomllib.TOMLDecodeError as exc:
            print(
                f"[init-config] 代理配置更新后不是有效 TOML：{exc}",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        CONFIG_PATH.write_text(updated, encoding="utf-8", newline="\n")
        print("[init-config] 已更新 config.toml 中的代理配置")
    else:
        print("[init-config] 代理配置已存在，跳过")
