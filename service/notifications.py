"""量化盯盘通知渠道适配器。

真实 webhook 只从环境变量读取；数据库仅保存是否启用和渠道名。
默认关闭，逐渠道返回脱敏诊断，并校验平台业务响应码。
"""
from __future__ import annotations

import os
from typing import Any

import requests

_CHANNELS = {
    "feishu": "QUANT_WATCH_FEISHU_WEBHOOK",
    "wecom": "QUANT_WATCH_WECOM_WEBHOOK",
}


def available_channels() -> dict[str, bool]:
    """返回渠道是否已配置，不回显 webhook。"""
    return {name: bool(os.getenv(env_name, "").strip())
            for name, env_name in _CHANNELS.items()}


def _payload(channel: str, text: str) -> dict[str, Any]:
    if channel == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    if channel == "wecom":
        return {"msgtype": "text", "text": {"content": text}}
    raise ValueError(f"不支持的通知渠道：{channel}")


def _business_result(channel: str, response: requests.Response) -> dict[str, Any]:
    """解析 webhook 业务码；响应正文只提取状态字段，避免记录敏感内容。"""
    try:
        body = response.json()
    except ValueError:
        return {"ok": False, "status": response.status_code, "error": "响应不是有效 JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "status": response.status_code, "error": "响应格式异常"}
    if channel == "feishu":
        code = body.get("code", body.get("StatusCode"))
        message = body.get("msg", body.get("StatusMessage", "业务响应失败"))
    else:
        code = body.get("errcode")
        message = body.get("errmsg", "业务响应失败")
    ok = code == 0
    result = {"ok": ok, "status": response.status_code, "business_code": code}
    if not ok:
        result["error"] = str(message)[:120]
    return result


def send_text(channels: list[str], text: str) -> dict[str, Any]:
    """向已配置渠道发送纯文本；逐渠道隔离失败且不记录 webhook 地址。"""
    results: dict[str, Any] = {}
    for channel in dict.fromkeys(channels):
        env_name = _CHANNELS.get(channel)
        if not env_name:
            results[channel] = {"ok": False, "error": "不支持的渠道"}
            continue
        url = os.getenv(env_name, "").strip()
        if not url:
            results[channel] = {"ok": False, "error": "未配置 webhook"}
            continue
        try:
            response = requests.post(url, json=_payload(channel, text), timeout=(3.05, 5))
            response.raise_for_status()
            results[channel] = _business_result(channel, response)
        except requests.Timeout:
            results[channel] = {"ok": False, "error": "请求超时"}
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            results[channel] = {"ok": False, "status": status, "error": "HTTP 请求失败"}
        except requests.RequestException:
            results[channel] = {"ok": False, "error": "网络请求失败"}
        except Exception as exc:  # 通知失败不得中断扫描，也不得记录可能含凭据的原始异常。
            results[channel] = {"ok": False, "error": f"通知处理失败：{type(exc).__name__}"}
    return results
