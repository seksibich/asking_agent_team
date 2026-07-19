"""运行监控与每日中文汇总。

记录接口和量化盯盘的低基数事件；原始事件按日 JSONL 保存，
晚间由正式环境定时器调用汇总接口生成 Agent 可直接阅读的 Markdown。
"""
from __future__ import annotations

import gzip
import json
import math
import os
import shutil
import threading
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import common

LOG_ROOT = common.DATA_DIR / "logs" / "monitor"
_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(ZoneInfo(common.TZ))


def _event_path(day: str) -> Path:
    return LOG_ROOT / "events" / day[:4] / day[4:6] / f"{day[6:]}.jsonl"


def _summary_path(day: str) -> Path:
    return LOG_ROOT / "daily" / f"{day}.md"


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(str(os.getenv(name, default)).strip())))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(maximum, float(str(os.getenv(name, default)).strip())))
    except (TypeError, ValueError):
        return default


def _maintenance_config() -> dict[str, Any]:
    enabled = str(os.getenv("MONITOR_LOG_CLEANUP_ENABLED", "true")).strip().lower() \
        not in {"0", "false", "no", "off"}
    retention_days = _int_env("MONITOR_LOG_RETENTION_DAYS", 90, 1, 3650)
    compress_after_days = _int_env(
        "MONITOR_LOG_COMPRESS_AFTER_DAYS", 7, 1, retention_days)
    return {
        "enabled": enabled,
        "retention_days": retention_days,
        "compress_after_days": compress_after_days,
        "disk_warn_percent": _float_env("MONITOR_DISK_WARN_PERCENT", 85.0, 1.0, 100.0),
        "disk_min_free_gb": _float_env("MONITOR_DISK_MIN_FREE_GB", 5.0, 0.0, 100000.0),
    }


def _logical_date(path: Path, logs_root: Path) -> Optional[date]:
    """从按日目录或日报文件名提取逻辑日期，不依赖文件修改时间。"""
    try:
        relative = path.relative_to(logs_root)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) >= 3 and parts[-2] == "daily":
        value = parts[-1].split(".", 1)[0]
        if len(value) == 8 and value.isdigit():
            return datetime.strptime(value, "%Y%m%d").date()
    if len(parts) >= 4:
        year, month = parts[-3], parts[-2]
        day = parts[-1].split(".", 1)[0]
        if len(year) == 4 and len(month) == 2 and len(day) == 2 \
                and year.isdigit() and month.isdigit() and day.isdigit():
            try:
                return date(int(year), int(month), int(day))
            except ValueError:
                return None
    return None


def maintain_logs(day: str) -> dict[str, Any]:
    """压缩和清理审计/监控日志，并返回磁盘容量快照。"""
    target = datetime.strptime(day, "%Y%m%d").date()
    config = _maintenance_config()
    logs_root = LOG_ROOT.parent
    report: dict[str, Any] = {
        **config, "compressed_files": 0, "deleted_files": 0, "errors": [],
    }
    if config["enabled"] and logs_root.exists():
        for path in sorted(logs_root.rglob("*")):
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            logical_day = _logical_date(path, logs_root)
            if logical_day is None:
                continue
            age_days = (target - logical_day).days
            try:
                if age_days >= config["retention_days"]:
                    path.unlink()
                    report["deleted_files"] += 1
                elif age_days >= config["compress_after_days"] \
                        and path.name.endswith(".jsonl"):
                    destination = Path(f"{path}.gz")
                    temporary = Path(f"{destination}.tmp")
                    with path.open("rb") as source, gzip.open(temporary, "wb") as output:
                        shutil.copyfileobj(source, output)
                    temporary.replace(destination)
                    path.unlink()
                    report["compressed_files"] += 1
            except OSError as exc:
                report["errors"].append(
                    f"{path.relative_to(logs_root)}：{type(exc).__name__}: {exc}"[:500])

    usage_root = common.DATA_DIR if common.DATA_DIR.exists() else logs_root
    try:
        usage = shutil.disk_usage(usage_root)
        used_percent = round((usage.used / usage.total * 100) if usage.total else 0.0, 2)
        free_gb = round(usage.free / (1024 ** 3), 2)
        report["storage"] = {
            "used_percent": used_percent,
            "free_gb": free_gb,
            "warning": used_percent >= config["disk_warn_percent"]
            or free_gb < config["disk_min_free_gb"],
        }
    except OSError as exc:
        report["storage"] = {"used_percent": None, "free_gb": None, "warning": True}
        report["errors"].append(f"磁盘容量读取失败：{type(exc).__name__}: {exc}"[:500])
    return report


def record(event_type: str, values: dict[str, Any]) -> None:
    """追加脱敏监控事件；失败绝不影响业务。"""
    try:
        now = _now()
        day = now.strftime("%Y%m%d")
        record_value = {
            "event_type": str(event_type),
            "recorded_at": now.isoformat(timespec="milliseconds"),
            **values,
        }
        line = json.dumps(record_value, ensure_ascii=False, default=str,
                          separators=(",", ":")) + "\n"
        path = _event_path(day)
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception:
        return


def record_http(path: str, function: Optional[str], status: int,
                duration_ms: float) -> None:
    record("http", {
        "path": str(path), "function": str(function or ""),
        "status": int(status), "duration_ms": round(float(duration_ms), 2),
    })


def record_quant_watch(status: str, duration_ms: float, *, manual: bool,
                       payload: Optional[dict[str, Any]] = None,
                       error: Optional[str] = None) -> None:
    summary = (payload or {}).get("market_summary") or {}
    record("quant_watch", {
        "status": str(status), "manual": bool(manual),
        "duration_ms": round(float(duration_ms), 2),
        "scanned_count": int(summary.get("scanned_count") or 0),
        "qualified_count": int(summary.get("qualified_count") or 0),
        "priority_alert_count": int(summary.get("priority_alert_count") or 0),
        "error": str(error or "")[:500],
    })


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return round(float(ordered[index]), 2)


def _read_events(day: str) -> list[dict[str, Any]]:
    path = _event_path(day)
    if not path.exists():
        return []
    events = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    return events


def _http_status_kind(status: int) -> str:
    if status >= 500 or status in {408, 429}:
        return "failure"
    if status >= 400:
        return "rejection"
    return "success"


def _http_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [item for item in events if item.get("event_type") == "http"]
    durations = [float(item.get("duration_ms") or 0) for item in rows]
    statuses = Counter(int(item.get("status") or 0) for item in rows)
    function_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "rejections": 0, "failures": 0, "durations": []})
    rejection_count = 0
    failure_count = 0
    for item in rows:
        name = str(item.get("function") or item.get("path") or "未知请求")
        kind = _http_status_kind(int(item.get("status") or 0))
        stat = function_stats[name]
        stat["count"] += 1
        stat["rejections"] += int(kind == "rejection")
        stat["failures"] += int(kind == "failure")
        stat["durations"].append(float(item.get("duration_ms") or 0))
        rejection_count += int(kind == "rejection")
        failure_count += int(kind == "failure")
    functions = []
    for name, stat in function_stats.items():
        functions.append({
            "name": name, "count": stat["count"],
            "rejections": stat["rejections"], "failures": stat["failures"],
            "p95_ms": _percentile(stat["durations"], 0.95),
            "max_ms": round(max(stat["durations"] or [0]), 2),
        })
    functions.sort(
        key=lambda item: (item["failures"], item["rejections"], item["p95_ms"], item["count"]),
        reverse=True)
    return {
        "count": len(rows), "rejections": rejection_count,
        "failures": failure_count, "errors": failure_count,
        "p50_ms": _percentile(durations, 0.50), "p95_ms": _percentile(durations, 0.95),
        "max_ms": round(max(durations or [0]), 2), "status_counts": dict(statuses),
        "top_functions": functions[:10],
    }


def _quant_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [item for item in events if item.get("event_type") == "quant_watch"]
    durations = [float(item.get("duration_ms") or 0) for item in rows]
    statuses = Counter(str(item.get("status") or "unknown") for item in rows)
    errors = [str(item.get("error")) for item in rows if item.get("error")]
    completed = [item for item in rows if item.get("status") in {"success", "degraded"}]
    return {
        "count": len(rows), "status_counts": dict(statuses),
        "p50_ms": _percentile(durations, 0.50), "p95_ms": _percentile(durations, 0.95),
        "max_ms": round(max(durations or [0]), 2),
        "avg_scanned": round(sum(int(item.get("scanned_count") or 0) for item in completed)
                             / max(1, len(completed)), 1),
        "qualified": sum(int(item.get("qualified_count") or 0) for item in completed),
        "priority_alerts": sum(int(item.get("priority_alert_count") or 0) for item in completed),
        "recent_errors": errors[-5:],
    }


def build_daily_summary(day: Optional[str] = None) -> dict[str, Any]:
    """聚合指定上海自然日、执行日志生命周期并落盘中文 Markdown。"""
    target = str(day or _now().strftime("%Y%m%d")).replace("-", "")
    if len(target) != 8 or not target.isdigit():
        raise ValueError("date 必须是 YYYYMMDD")
    try:
        datetime.strptime(target, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("date 必须是有效的 YYYYMMDD") from exc
    events = _read_events(target)
    http = _http_summary(events)
    quant = _quant_summary(events)
    maintenance = maintain_logs(_now().strftime("%Y%m%d"))
    storage = maintenance["storage"]
    needs_attention = bool(
        http["failures"] or quant["recent_errors"]
        or storage.get("warning") or maintenance["errors"])
    health_text = "需关注" if needs_attention else "正常"
    lifecycle_text = (
        f"保留 {maintenance['retention_days']} 日，"
        f"{maintenance['compress_after_days']} 日后压缩；"
        f"本次压缩 {maintenance['compressed_files']} 个、删除 {maintenance['deleted_files']} 个文件"
        if maintenance["enabled"] else "生命周期维护已冻结，不执行压缩或删除"
    )
    storage_text = (
        f"磁盘已用 {storage['used_percent']}%，可用 {storage['free_gb']} GB"
        if storage.get("used_percent") is not None else "磁盘容量读取失败"
    )
    lines = [
        f"# {target} 服务与量化盯盘运行汇总", "",
        f"> 生成时间：{common.now_str()}　整体状态：**{health_text}**", "",
        "## 一眼结论", "",
        f"- 接口请求 {http['count']} 次，服务故障 {http['failures']} 次，业务拒绝 {http['rejections']} 次；95% 请求在 {http['p95_ms']} 毫秒内完成。",
        f"- 盯盘触发 {quant['count']} 次，成功/降级 {quant['status_counts'].get('success', 0) + quant['status_counts'].get('degraded', 0)} 次，异常 {quant['status_counts'].get('error', 0)} 次。",
        f"- 完成扫描平均覆盖 {quant['avg_scanned']} 只股票，当日累计达标 {quant['qualified']} 次、优先标的异动 {quant['priority_alerts']} 次。",
        f"- 日志：{lifecycle_text}；{storage_text}{'，已达到告警条件' if storage.get('warning') else ''}。",
        "", "## 接口稳定性", "",
        "- 业务拒绝指参数、鉴权或资源类 4xx，不单独判定服务故障；408、429 与 5xx 计入服务故障。",
        f"- 耗时：中位数 {http['p50_ms']} 毫秒；95% 分位 {http['p95_ms']} 毫秒；最慢 {http['max_ms']} 毫秒。",
        f"- 状态分布：{http['status_counts'] or '无请求'}。", "",
        "| 业务 | 调用次数 | 业务拒绝 | 服务故障 | 95%耗时（毫秒） | 最慢（毫秒） |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    lines.extend(
        f"| {item['name']} | {item['count']} | {item['rejections']} | {item['failures']} | {item['p95_ms']} | {item['max_ms']} |"
        for item in http["top_functions"]
    )
    if not http["top_functions"]:
        lines.append("| 当日无业务请求 | 0 | 0 | 0 | 0 | 0 |")
    lines.extend([
        "", "## 量化盯盘性能", "",
        f"- 状态分布：{quant['status_counts'] or '无扫描'}。",
        f"- 耗时：中位数 {quant['p50_ms']} 毫秒；95% 分位 {quant['p95_ms']} 毫秒；最慢 {quant['max_ms']} 毫秒。",
        "", "## 日志生命周期与容量", "",
        f"- {lifecycle_text}。", f"- {storage_text}。",
        f"- 生命周期异常：{maintenance['errors'] or '无'}。",
        "", "## 最近错误", "",
    ])
    lines.extend(f"- {value}" for value in quant["recent_errors"])
    if not quant["recent_errors"]:
        lines.append("- 未记录量化盯盘错误。")
    lines.extend([
        "", "## 分析提示", "",
        "- 先看服务故障、容量告警与最近错误，再看盯盘 95% 耗时是否持续抬升。",
        "- 单日业务拒绝或偶发慢请求不直接判定故障；连续多个交易日恶化时，再按业务名回查审计日志。",
        "- 本汇总只记录运行质量，不保存访问凭据、原始全市场行情或用户持仓内容。", "",
    ])
    path = _summary_path(target)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8")
        temporary.replace(path)
    return {
        "date": target, "status": health_text, "generated_at": common.now_str(),
        "http": http, "quant_watch": quant, "log_maintenance": maintenance,
        "summary_file": str(path.relative_to(common.DATA_DIR)),
    }
