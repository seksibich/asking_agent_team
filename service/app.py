"""本地数据服务（FastAPI）——通用功能分发 + 版本机制。

对智能体暴露三个核心端点：
- GET  /health     健康检查 + trade_open + data_version
- GET  /functions  全部功能索引（新增/变更功能会改变 data_version）
- POST /call       {"function": "...", "params": {...}} 统一调用

每个响应都带 data_version（响应体字段 + 响应头 X-Data-Version）。
智能体每次调用后对比版本，不一致则重新拉取 /functions 刷新功能索引并更新记忆。
服务当前部署形态：本地 Docker，地址 http://localhost:18901。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

import audit_log
import common
import db
import registry
import loader
import observability
import selection_tags
import version


def _health_cache_ttl() -> float:
    """解析健康快照缓存秒数；非法配置回退两秒。"""
    try:
        value = float(os.getenv("HEALTH_SNAPSHOT_TTL_SECONDS", "2"))
    except (TypeError, ValueError):
        value = 2.0
    return max(0.2, min(10.0, value))


_HEALTH_CACHE_TTL = _health_cache_ttl()
_HEALTH_CACHE_LOCK = threading.Lock()
_HEALTH_CACHE: dict[str, Any] = {"at": 0.0, "value": None}


def _normalize_key(value: Optional[str]) -> str:
    """规范化环境变量或请求中的 Key，避免部署工具带入首尾空白/外层引号。"""
    if not value:
        return ""
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] in "'\"" and normalized[-1] == normalized[0]:
        normalized = normalized[1:-1].strip()
    return normalized


# 管理员 Key：API_KEY 与 ADMIN_API_KEY 均可用，避免旧变量覆盖云端新配置。
# 只在进程启动时读取；修改云端变量后必须重启/重建服务。
ADMIN_API_KEYS = frozenset(
    key for key in (
        _normalize_key(os.getenv("API_KEY")),
        _normalize_key(os.getenv("ADMIN_API_KEY")),
    ) if key
)
# 访客 Key：可查看、选股、读情绪和查看回测结果，不能改配置或运行全市场预计算
USER_API_KEY = _normalize_key(os.getenv("USER_API_KEY"))

# 仅管理员可调用的敏感功能：修改配置、写入全市场预计算结果
ADMIN_ONLY_FUNCTIONS = {
    "set_factor_weights",       # 修改各模型因子权重
    "set_sentiment_config",     # 修改情绪归一窗口
    "restore_config_version",   # 回滚配置到历史版本
    "precompute_daily_factors", # 启动全市场因子预计算任务
    "precompute_status",        # 查看预计算任务进度与错误摘要
    "precompute_run_errors",    # 查看单日预计算完整错误明细
    "portfolio_get",           # 获取管理员当前关注与持仓
    "portfolio_stock_search",  # 模糊搜索可加入自选的股票
    "portfolio_upload",        # 上传、更新或删除管理员关注与持仓
    "quant_watch_status",      # 盯盘结果含持仓、关注与近期选股
    "quant_watch_get_config",  # 读取管理员盯盘范围和通知设置
    "quant_watch_set_config",  # 修改并版本化盯盘设置
    "quant_watch_scan_once",   # 管理员盘中手动诊断扫描
}

# 选股读取安全红线：访客不得读取关注或持仓；默认查询由 DB 层自动排除。
SENSITIVE_SELECTION_CATEGORIES = frozenset({"watch", "holding"})
SELECTION_CATEGORY_FILTER_FUNCTIONS = frozenset({"selection_dashboard"})

# 动态访客 Key 使用独立摘要表持久化；完整值只在创建时返回一次。

WEB_DIR = Path(__file__).resolve().parent / "web"

_WS_TICKET_LOG_PATTERN = re.compile(r"([?&]ticket=)[^&\s\"']+")


def _redact_ws_ticket(value: Any) -> Any:
    """仅脱敏日志文本中的 WebSocket 一次性票据，不改变请求本身。"""
    if not isinstance(value, str) or "ticket=" not in value:
        return value
    return _WS_TICKET_LOG_PATTERN.sub(r"\1[REDACTED]", value)


class _WebSocketTicketLogFilter(logging.Filter):
    """兼容 uvicorn HTTP 与 WebSocket logger 的参数化日志结构。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_ws_ticket(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_ws_ticket(item) for item in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact_ws_ticket(item)
                           for key, item in record.args.items()}
        return True


def _install_ws_ticket_log_filter() -> None:
    """幂等安装票据脱敏，覆盖 uvicorn 的 HTTP 与 WebSocket 日志。"""
    for logger_name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(item, _WebSocketTicketLogFilter) for item in logger.filters):
            logger.addFilter(_WebSocketTicketLogFilter())


_install_ws_ticket_log_filter()
app = FastAPI(title="Stock Data Service", version="1.0.0")

# 数据库初始化结果为进程级事实；初始化失败时量化盯盘不得再次探测数据库。
_DB_READY = False
_DB_INIT_ERROR: Optional[str] = None
_QUANT_WATCH_STARTED = False
_WS_TICKET_TTL_SECONDS = 60
_QUANT_WATCH_WS_SUBSCRIBERS: set[asyncio.Queue[dict[str, Any]]] = set()
_QUANT_WATCH_WS_TASK: Optional[asyncio.Task[Any]] = None
_QUANT_WATCH_WS_LATEST: Optional[dict[str, Any]] = None


def _offer_quant_watch_frame(queue: asyncio.Queue[dict[str, Any]],
                             frame: dict[str, Any]) -> None:
    """慢客户端仅保留最新状态，绝不阻塞其他连接或无限积压。"""
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(frame)


async def _quant_watch_broadcast_loop() -> None:
    """全进程唯一等待数据库状态，并将同一帧分发给全部连接。"""
    global _QUANT_WATCH_WS_TASK, _QUANT_WATCH_WS_LATEST
    current_task = asyncio.current_task()
    sequence = -1
    try:
        import quant_watch
        while _QUANT_WATCH_WS_SUBSCRIBERS:
            try:
                sequence, data = await asyncio.to_thread(
                    quant_watch.wait_for_update, sequence, 20.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)
                continue
            frame = jsonable_encoder({
                "type": "quant_watch", "sequence": sequence, "data": data,
            })
            _QUANT_WATCH_WS_LATEST = frame
            for queue in tuple(_QUANT_WATCH_WS_SUBSCRIBERS):
                _offer_quant_watch_frame(queue, frame)
    finally:
        if _QUANT_WATCH_WS_TASK is current_task:
            _QUANT_WATCH_WS_TASK = None
        if not _QUANT_WATCH_WS_SUBSCRIBERS:
            _QUANT_WATCH_WS_LATEST = None


def _register_quant_watch_subscriber() -> asyncio.Queue[dict[str, Any]]:
    """注册连接并确保唯一广播任务已启动。"""
    global _QUANT_WATCH_WS_TASK
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
    _QUANT_WATCH_WS_SUBSCRIBERS.add(queue)
    if _QUANT_WATCH_WS_LATEST is not None:
        _offer_quant_watch_frame(queue, _QUANT_WATCH_WS_LATEST)
    if _QUANT_WATCH_WS_TASK is None or _QUANT_WATCH_WS_TASK.done():
        _QUANT_WATCH_WS_TASK = asyncio.create_task(
            _quant_watch_broadcast_loop(), name="quant-watch-websocket-broadcast")
    return queue


async def _stop_quant_watch_broadcaster() -> None:
    """取消唯一广播任务，并唤醒仍在 Condition 中等待的后台线程。"""
    global _QUANT_WATCH_WS_TASK, _QUANT_WATCH_WS_LATEST
    task = _QUANT_WATCH_WS_TASK
    if task is not None and not task.done():
        task.cancel()
        try:
            import quant_watch
            quant_watch.wake_update_waiters()
        except Exception:
            pass
        try:
            await task
        except asyncio.CancelledError:
            pass
    if _QUANT_WATCH_WS_TASK is task:
        _QUANT_WATCH_WS_TASK = None
    _QUANT_WATCH_WS_LATEST = None


async def _unregister_quant_watch_subscriber(
        queue: asyncio.Queue[dict[str, Any]]) -> None:
    _QUANT_WATCH_WS_SUBSCRIBERS.discard(queue)
    if not _QUANT_WATCH_WS_SUBSCRIBERS:
        await _stop_quant_watch_broadcaster()


def _issue_ws_ticket(role: str) -> str:
    """签发数据库一次性票据，进程内不保留原始票据或摘要。"""
    return db.issue_quant_watch_ticket(
        role=role, purpose="quant_watch_ws", ttl_seconds=_WS_TICKET_TTL_SECONDS)


def _consume_ws_ticket(ticket: str) -> Optional[str]:
    """通过数据库原子消费管理员量化盯盘 WebSocket 票据。"""
    return db.consume_quant_watch_ticket(
        ticket, role="admin", purpose="quant_watch_ws")


def _normalized_authority(value: str, default_scheme: str) -> Optional[tuple[str, int]]:
    """规范化 hostname 与默认端口，拒绝非法端口或缺失主机名。"""
    try:
        parsed = urlsplit(value if "://" in value else f"{default_scheme}://{value}")
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if not hostname:
            return None
        scheme = (parsed.scheme or default_scheme).lower()
        if scheme not in {"http", "https", "ws", "wss"}:
            return None
        default_port = 443 if scheme in {"https", "wss"} else 80
        return hostname, parsed.port or default_port
    except ValueError:
        return None


def _websocket_origin_allowed(websocket: WebSocket) -> bool:
    """有 Origin 时仅允许其主机和显式/默认端口与 WebSocket Host 一致。"""
    origin = str(websocket.headers.get("origin") or "").strip()
    if not origin:
        return True
    if origin == "null":
        return False
    origin_authority = _normalized_authority(origin, "http")
    host_authority = _normalized_authority(
        str(websocket.headers.get("host") or ""), str(websocket.url.scheme or "ws"))
    return origin_authority is not None and origin_authority == host_authority


@app.on_event("startup")
def _startup() -> None:
    global _DB_READY, _DB_INIT_ERROR, _QUANT_WATCH_STARTED
    _install_ws_ticket_log_filter()
    _DB_READY = False
    _DB_INIT_ERROR = None
    _QUANT_WATCH_STARTED = False
    # 缓存清理只在启动执行，探针保持只读且低开销。
    _safe_health_value(common.clean_expired_cache, None)
    try:
        db.init_db()
        _DB_READY = True
        print("[startup] db ready:", db.db_url())
    except Exception as exc:  # noqa: BLE001
        _DB_INIT_ERROR = f"{type(exc).__name__}: {exc}"[:500]
        print(f"[startup] db init skipped: {exc}")
    imported = loader.discover()
    try:
        import daily_scheduler
        daily_scheduler.start()
        print("[startup] daily finalize scheduler enabled: 16:00 Asia/Shanghai")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] daily finalize scheduler skipped: {exc}")
    if _DB_READY:
        try:
            import quant_watch
            quant_watch.start()
            _QUANT_WATCH_STARTED = True
            print("[startup] quant watch scheduler enabled")
        except Exception as exc:  # noqa: BLE001
            print(f"[startup] quant watch scheduler skipped: {exc}")
    else:
        print("[startup] quant watch scheduler skipped: database unavailable")
    print(f"[startup] auth configured: admin_keys={len(ADMIN_API_KEYS)}, "
          f"user_key={bool(USER_API_KEY)}")
    print(f"[startup] loaded {len(imported)} function modules, "
          f"{len(registry.names())} functions, data_version={registry.data_version()}")


@app.on_event("shutdown")
async def _shutdown() -> None:
    _QUANT_WATCH_WS_SUBSCRIBERS.clear()
    await _stop_quant_watch_broadcaster()
    if _QUANT_WATCH_STARTED:
        try:
            import quant_watch
            await asyncio.to_thread(quant_watch.stop)
        except Exception:
            pass
    try:
        import daily_scheduler
        await asyncio.to_thread(daily_scheduler.stop)
    except Exception:
        pass


def _role_for(x_api_key: Optional[str]) -> Optional[str]:
    """返回调用方角色：admin / user / None（未授权）。
    未配置任何凭据时，未输入 token 只按只读用户处理；已配置管理员 Key 时空 token 仍未授权。"""
    candidate = _normalize_key(x_api_key)
    if candidate and any(secrets.compare_digest(candidate, key) for key in ADMIN_API_KEYS):
        return "admin"
    try:
        dynamic_key_configured = bool(db.list_user_api_keys())
    except Exception:
        dynamic_key_configured = False
    if not ADMIN_API_KEYS and not USER_API_KEY and not dynamic_key_configured:
        return "user"   # 未配置凭据时允许只读访问，但不开放管理员操作
    if USER_API_KEY and candidate and secrets.compare_digest(candidate, USER_API_KEY):
        return "user"
    try:
        if candidate and db.verify_user_api_key(candidate):
            return "user"
    except Exception:
        pass
    return None


def _check_key(x_api_key: Optional[str]) -> str:
    role = _role_for(x_api_key)
    if role is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return role


def _require_admin(x_api_key: Optional[str]) -> None:
    if _role_for(x_api_key) != "admin":
        raise HTTPException(status_code=403, detail="forbidden: 需要管理员 Key")


def _should_audit_path(path: str) -> bool:
    """只记录业务接口，跳过静态资源和框架文档。"""
    return path != "/" and not path.startswith(("/ui", "/docs", "/redoc", "/openapi.json"))


@app.middleware("http")
async def audit_http_requests(request: Request, call_next):
    """统一记录接口状态与耗时；日志失败不得影响请求。"""
    started = time.perf_counter()
    request_id = uuid.uuid4().hex
    request.state.request_id = request_id
    try:
        response = await call_next(request)
    except Exception:
        if _should_audit_path(request.url.path):
            audit_log.append("api", {
                "event": "request_failed", "request_id": request_id,
                "method": request.method, "path": request.url.path, "status": 500,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "role": getattr(request.state, "role", None),
                "key_fingerprint": audit_log.key_fingerprint(
                    request.headers.get("X-API-Key")),
                "function": getattr(request.state, "function", None),
                "params": getattr(request.state, "params", None),
            })
        duration_ms = (time.perf_counter() - started) * 1000
        observability.record_http(
            request.url.path, getattr(request.state, "function", None), 500, duration_ms)
        raise
    response.headers["X-Request-ID"] = request_id
    if _should_audit_path(request.url.path):
        role = getattr(request.state, "role", None)
        if role is None:
            try:
                role = _role_for(request.headers.get("X-API-Key"))
            except Exception:
                role = None
        audit_log.append("api", {
            "event": "request_completed" if response.status_code < 400 else "request_rejected",
            "request_id": request_id, "method": request.method,
            "path": request.url.path, "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "role": role,
            "key_fingerprint": audit_log.key_fingerprint(request.headers.get("X-API-Key")),
            "function": getattr(request.state, "function", None),
            "params": getattr(request.state, "params", None),
        })
    observability.record_http(
        request.url.path, getattr(request.state, "function", None),
        response.status_code, (time.perf_counter() - started) * 1000)
    return response


def _audit_function(function: str, params: dict[str, Any], role: str,
                    duration_ms: float, data: Optional[dict[str, Any]] = None,
                    error: Optional[str] = None, status: int = 200) -> None:
    """为回测与选股写入可供后续分析的专业审计摘要。"""
    if function in {"selection_backtest", "predictions_backtest"}:
        payload: dict[str, Any] = {
            "event": "completed" if error is None else "failed",
            "function": function, "params": params, "role": role,
            "success": error is None, "status": status,
            "duration_ms": round(duration_ms, 2), "error": error,
        }
        if data:
            payload["result"] = {key: data.get(key) for key in (
                "source", "fetched_at", "snapshot_id", "sample_hash",
                "return_calc_version", "total_selections", "recomputed_samples",
                "backfilled_prices", "by_category_return", "by_category_excess",
                "auto_by_driver_excess", "optimization_gate", "tuning_hints",
                "trade_date", "total", "correct", "accuracy_pct", "accuracy_by_driver",
            ) if key in data}
        audit_log.append("backtest", payload)
    elif function == "log_selection":
        logged = bool((data or {}).get("logged"))
        inserted = bool((data or {}).get("inserted"))
        event = ("failed" if error is not None else "inserted" if logged and inserted
                 else "duplicate" if logged else "rejected")
        record = (data or {}).get("record") or {}
        audit_log.append("selection", {
            "event": event, "function": function, "role": role,
            "success": error is None and logged, "status": status,
            "duration_ms": round(duration_ms, 2), "error": error,
            "reason": (data or {}).get("reason"),
            "selection_id": record.get("id"), "date": record.get("sel_date"),
            "code": record.get("code") or params.get("code"),
            "name": record.get("name") or params.get("name"),
            "category": record.get("category") or params.get("category"),
            "screening_run_id": ((record.get("extra") or {}).get("screening_run_id")
                                 or params.get("screening_run_id")),
        })


def _safe_health_value(getter, fallback: Any) -> Any:
    """安全读取单个健康字段，任何探测失败都不得覆盖原业务响应。"""
    try:
        return getter()
    except Exception:
        return fallback


def _build_health_snapshot() -> dict[str, Any]:
    """生成与 /health 顶层字段一致的只读健康快照，并对探测项独立容错。"""
    day = _safe_health_value(common.today_str, common.shanghai_now().strftime("%Y%m%d"))
    tushare_ready = bool(_safe_health_value(lambda: common.TUSHARE_TOKEN, ""))
    clock = (_safe_health_value(common.market_clock, None) if tushare_ready else None)
    clock = clock if isinstance(clock, dict) else {}
    is_trading_day = clock.get("is_trading_day")

    db_ready = False
    portfolio_version = "unavailable"
    daily_finalize: dict[str, Any] = {"status": "unavailable"}
    quant_watch_status: dict[str, Any] = (
        {"status": "unavailable", "reason": "数据库初始化失败"}
        if not _DB_READY else {"status": "degraded", "reason": "数据库当前不可用"})
    if _DB_READY:
        try:
            db.get_engine().connect().close()
            db_ready = True
            portfolio_version = _safe_health_value(db.get_portfolio_version, "unavailable")
            import daily_scheduler
            import quant_watch
            daily_finalize = _safe_health_value(daily_scheduler.status, {"status": "unavailable"})
            quant_watch_status = _safe_health_value(
                quant_watch.health_summary, {"status": "degraded"})
        except Exception:
            quant_watch_status = {"status": "degraded", "reason": "数据库当前不可用"}

    return {
        "status": "ok",
        "date": day,
        "trade_open": is_trading_day,
        "is_trading_day": is_trading_day,
        "market_phase": clock.get("phase"),
        "is_continuous_trading": clock.get("is_continuous_trading"),
        "last_calendar_trade_date": clock.get("last_calendar_trade_date"),
        "last_closed_trade_date": clock.get("last_closed_trade_date"),
        "last_data_ready_date": clock.get("last_data_ready_date"),
        "final_ready_time": clock.get("final_ready_time"),
        "daily_finalize": daily_finalize,
        "quant_watch": quant_watch_status,
        "market_time": clock.get("market_time") or common.now_str(),
        "tushare_ready": tushare_ready,
        "db_ready": db_ready,
        "portfolio_version": portfolio_version,
        "selection_tag_version": _safe_health_value(
            lambda: selection_tags.TAG_VERSION, "unknown"
        ),
        "functions": _safe_health_value(lambda: len(registry.names()), 0),
        "agent_doc_version": _safe_health_value(version.agent_doc_version, "unknown"),
        "git_revision": _safe_health_value(version.git_revision, "unknown"),
        "data_version": _safe_health_value(registry.data_version, "unknown"),
    }


def _health_snapshot() -> dict[str, Any]:
    """短时复用快照；慢探测在锁外执行，避免并发请求排队。"""
    now_mono = time.monotonic()
    with _HEALTH_CACHE_LOCK:
        cached = _HEALTH_CACHE.get("value")
        if (isinstance(cached, dict)
                and now_mono - float(_HEALTH_CACHE.get("at") or 0) < _HEALTH_CACHE_TTL):
            return dict(cached)
    value = _build_health_snapshot()
    with _HEALTH_CACHE_LOCK:
        cached = _HEALTH_CACHE.get("value")
        cached_at = float(_HEALTH_CACHE.get("at") or 0)
        if isinstance(cached, dict) and time.monotonic() - cached_at < _HEALTH_CACHE_TTL:
            return dict(cached)
        _HEALTH_CACHE.update({"at": time.monotonic(), "value": value})
    return dict(value)


def _versioned(
    body: dict[str, Any],
    *,
    status_code: int = 200,
    headers: Optional[dict[str, str]] = None,
    include_health: bool = True,
) -> JSONResponse:
    """统一编码与附加版本/健康数据，避免健康探测失败覆盖原业务结果。"""
    payload = dict(body)
    current_data_version = _safe_health_value(registry.data_version, "unknown")
    payload["data_version"] = current_data_version
    if include_health:
        payload["health"] = _health_snapshot()

    response_headers = dict(headers or {})
    response_headers["X-Data-Version"] = current_data_version
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(payload),
        headers=response_headers,
    )


def _error_response(
    status_code: int,
    error: Any,
    *,
    details: Any = None,
    headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    """构造统一错误响应，保留原状态码并附加完整健康快照。"""
    body: dict[str, Any] = {"ok": False, "error": error, "status": status_code}
    if details is not None:
        body["details"] = details
    return _versioned(body, status_code=status_code, headers=headers)


class CallReq(BaseModel):
    function: str
    params: Optional[dict[str, Any]] = None


@app.get("/live")
def live():
    """进程存活探针：不访问数据库和外部数据源。"""
    return JSONResponse({"status": "alive", "time": common.now_str()})


@app.get("/health")
def health():
    """兼容 Agent 的业务健康快照；依赖未就绪时仍返回可诊断信息。"""
    return _versioned(_health_snapshot(), include_health=False)


def _readiness_snapshot() -> tuple[dict[str, Any], int]:
    health_value = _health_snapshot()
    load_report = loader.report()
    load_errors = list(load_report.get("errors") or [])
    checks = {
        "database": bool(health_value.get("db_ready")),
        "data_source": bool(health_value.get("tushare_ready")),
        "functions": int(health_value.get("functions") or 0) > 0,
        "module_load": not load_errors,
    }
    ready = all(checks.values())
    body = {
        "status": "ready" if ready else "not_ready", "checks": checks,
        "market_phase": health_value.get("market_phase"),
        "functions": health_value.get("functions"),
        "load_error_modules": [str(item.get("module") or "") for item in load_errors],
        "time": common.now_str(),
    }
    return body, 200 if ready else 503


@app.get("/ready")
def ready():
    """流量就绪探针：数据库、数据源、功能装载均通过才返回 200。"""
    body, status_code = _readiness_snapshot()
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body),
                        headers={"Cache-Control": "no-store"})


@app.get("/admin/monitor/daily")
def monitor_daily(date: Optional[str] = None,
                  x_api_key: Optional[str] = Header(None)):
    """生成并返回指定日的中文运行汇总，仅管理员可读取。"""
    _require_admin(x_api_key)
    try:
        summary = observability.build_daily_summary(date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _versioned({"ok": True, "summary": summary})


@app.get("/functions")
def functions(x_api_key: Optional[str] = Header(None)):
    _check_key(x_api_key)
    return _versioned(registry.functions_index())


@app.get("/whoami")
def whoami(x_api_key: Optional[str] = Header(None)):
    """返回当前 Key 的角色，供前端按权限控制 UI。访客可查看回测结果。"""
    role = _check_key(x_api_key)
    return _versioned({"role": role, "is_admin": role == "admin",
                       "admin_only": sorted(ADMIN_ONLY_FUNCTIONS)})


class UserKeyCreate(BaseModel):
    label: Optional[str] = None


class UserKeyOp(BaseModel):
    id: str


def _gen_user_key() -> str:
    return "sk-stockagent-user-" + secrets.token_hex(20)


@app.get("/admin/user-keys")
def list_user_keys(x_api_key: Optional[str] = Header(None)):
    """列出动态访客 Key 元数据；只返回掩码，明文不可恢复。"""
    _require_admin(x_api_key)
    return _versioned({"keys": db.list_user_api_keys(),
                       "env_user_key_enabled": bool(USER_API_KEY)})


@app.post("/admin/user-keys")
def create_user_key(req: UserKeyCreate, x_api_key: Optional[str] = Header(None)):
    """生成访客 Key；完整 Key 仅在本次创建响应中返回。"""
    _require_admin(x_api_key)
    raw_key = _gen_user_key()
    label = (req.label or "").strip() or "访客"
    if len(label) > 64:
        raise HTTPException(status_code=400, detail="访客名称最多 64 个字符")
    item = db.create_user_api_key(secrets.token_hex(6), label, raw_key)
    audit_log.append("api", {
        "event": "user_key_created", "key_id": item["id"],
        "label": item["label"], "role": "admin",
    })
    return _versioned({"created": True, "item": {**item, "key": raw_key}})


@app.post("/admin/user-keys/toggle")
def toggle_user_key(req: UserKeyOp, x_api_key: Optional[str] = Header(None)):
    """原子启用或停用一个访客 Key。"""
    _require_admin(x_api_key)
    disabled = db.toggle_user_api_key(req.id)
    audit_log.append("api", {
        "event": "user_key_toggled", "key_id": req.id,
        "disabled": disabled, "found": disabled is not None, "role": "admin",
    })
    return _versioned({"toggled": disabled is not None, "id": req.id,
                       "disabled": disabled})


@app.post("/admin/user-keys/delete")
def delete_user_key(req: UserKeyOp, x_api_key: Optional[str] = Header(None)):
    """删除某个访客 Key；删除后立即失效。"""
    _require_admin(x_api_key)
    deleted = db.delete_user_api_key(req.id)
    audit_log.append("api", {
        "event": "user_key_deleted", "key_id": req.id,
        "deleted": deleted, "role": "admin",
    })
    return _versioned({"deleted": deleted, "id": req.id})


class SelectionDeleteReq(BaseModel):
    id: int
    confirm_code: str


class SelectionQuotesReq(BaseModel):
    items: list[dict[str, Any]]


@app.post("/admin/selections/delete")
def delete_selection(req: SelectionDeleteReq, request: Request,
                     x_api_key: Optional[str] = Header(None)):
    """按数字主键永久删除选股及关联收益；历史回测快照保留，仅管理员可用。"""
    _require_admin(x_api_key)
    request.state.role = "admin"
    if req.id <= 0:
        raise HTTPException(status_code=400, detail="选股记录 id 必须为正整数")
    result = db.delete_selection(req.id, req.confirm_code)
    if result.get("reason") == "not_found":
        raise HTTPException(status_code=404, detail="选股记录不存在或已删除")
    if result.get("reason") == "confirm_mismatch":
        audit_log.append("selection", {
            "event": "delete_rejected", "selection_id": req.id,
            "code": req.confirm_code, "reason": "confirm_mismatch", "role": "admin",
        })
        raise HTTPException(status_code=400, detail="确认股票代码不匹配，已取消删除")
    if not result.get("deleted"):
        raise HTTPException(status_code=500, detail="选股记录删除失败")
    audit_log.append("selection", {
        "event": "deleted", "selection_id": req.id,
        "code": req.confirm_code, "role": "admin", "result": result,
    })
    return _versioned(result)


@app.post("/selections/quotes")
def refresh_selection_quotes(req: SelectionQuotesReq, request: Request,
                             x_api_key: Optional[str] = Header(None)):
    """仅刷新调用方当前可见列表的行情与缺失选股价，不重新执行看板查询。"""
    role = _check_key(x_api_key)
    request.state.role = role
    if len(req.items) > 1000:
        raise HTTPException(status_code=400, detail="单次最多刷新 1000 条选股记录")
    try:
        import selection_backtest
        with db.selection_read_scope(include_sensitive=role == "admin"):
            result = selection_backtest.refresh_selection_quotes(req.items)
    except common.ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    except Exception:
        raise HTTPException(status_code=500, detail="行情刷新失败")
    result.update({"source": "selection_quotes", "fetched_at": datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S")})
    return _versioned(result)


@app.get("/admin/logs/download")
def download_logs(scene: str, request: Request, scope: str = "date",
                  date: Optional[str] = None, date_from: Optional[str] = None,
                  date_to: Optional[str] = None,
                  x_api_key: Optional[str] = Header(None)):
    """按场景和单日、区间或全量流式下载 JSONL 审计日志，仅管理员可用。"""
    _require_admin(x_api_key)
    request.state.role = "admin"
    try:
        files = audit_log.resolve_files(scene, scope, date, date_from, date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    suffix = (str(date or "today").replace("-", "") if scope == "date"
              else f"{str(date_from or 'start').replace('-', '')}-{str(date_to or 'end').replace('-', '')}"
              if scope == "range" else "all")
    filename = f"stock-agent-{scene}-{scope}-{suffix}.jsonl"
    return StreamingResponse(
        audit_log.stream_jsonl(files), media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/quant-watch/ticket")
def quant_watch_ticket(request: Request, x_api_key: Optional[str] = Header(None)):
    """管理员用 HTTP Key 换取数据库一次性 WebSocket 短期票据。"""
    _require_admin(x_api_key)
    request.state.role = "admin"
    headers = {"Cache-Control": "no-store"}
    if not _DB_READY:
        return _error_response(503, "量化盯盘数据库不可用", headers=headers)
    try:
        ticket = _issue_ws_ticket("admin")
    except Exception:
        return _error_response(503, "量化盯盘票据签发失败", headers=headers)
    return _versioned({"ok": True, "data": {
        "ticket": ticket, "expires_in": int(_WS_TICKET_TTL_SECONDS),
    }}, headers=headers)


@app.websocket("/ws/quant-watch")
async def quant_watch_socket(websocket: WebSocket, ticket: str = ""):
    """消费一次性票据后订阅进程级唯一广播，不为每个连接占用等待线程。"""
    if not _websocket_origin_allowed(websocket):
        await websocket.close(code=4403, reason="Origin 与 Host 不一致")
        return
    if not _DB_READY:
        await websocket.close(code=1013, reason="量化盯盘数据库不可用")
        return
    try:
        role = _consume_ws_ticket(ticket)
    except Exception:
        role = None
    if role != "admin":
        await websocket.close(code=4401, reason="票据无效或已过期")
        return
    await websocket.accept()
    queue: Optional[asyncio.Queue[dict[str, Any]]] = None
    try:
        queue = _register_quant_watch_subscriber()
        while True:
            frame = await queue.get()
            await websocket.send_json(frame)
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        raise
    except Exception:
        try:
            await websocket.close(code=1011, reason="盯盘连接异常")
        except Exception:
            pass
    finally:
        if queue is not None:
            await _unregister_quant_watch_subscriber(queue)


@app.post("/call")
def call(req: CallReq, request: Request, x_api_key: Optional[str] = Header(None)):
    started = time.perf_counter()
    role = _check_key(x_api_key)
    params = req.params or {}
    request.state.role = role
    request.state.function = req.function
    request.state.params = params
    if role != "admin" and req.function in ADMIN_ONLY_FUNCTIONS:
        _audit_function(req.function, params, role,
                        (time.perf_counter() - started) * 1000,
                        error="管理员专属功能", status=403)
        raise HTTPException(
            status_code=403,
            detail=f"forbidden: 功能 '{req.function}' 需管理员 Key（用户 Key 不可调用管理员专属功能）")
    requested_category = str(params.get("category") or "").strip()
    if (role != "admin" and req.function in SELECTION_CATEGORY_FILTER_FUNCTIONS
            and requested_category in SENSITIVE_SELECTION_CATEGORIES):
        _audit_function(req.function, params, role,
                        (time.perf_counter() - started) * 1000,
                        error="敏感选股类别", status=403)
        raise HTTPException(
            status_code=403,
            detail=f"forbidden: 选股类别 '{requested_category}' 仅管理员可查看")
    try:
        # 全部注册函数共享同一请求级读取范围；即使未来新增 DB 查询调用点，
        # 访客也只能在 SQL 层读取 auto/manual，不能通过省略 category 绕过。
        with db.selection_read_scope(include_sensitive=role == "admin"):
            if req.function.startswith("quant_watch_") and not _DB_READY:
                raise RuntimeError("量化盯盘数据库不可用")
            data = registry.call(req.function, params)
    except registry.ParamError as exc:
        _audit_function(req.function, params, role,
                        (time.perf_counter() - started) * 1000,
                        error=str(exc), status=400)
        raise HTTPException(status_code=400, detail=str(exc))
    except common.ServiceError as exc:
        _audit_function(req.function, params, role,
                        (time.perf_counter() - started) * 1000,
                        error=exc.message, status=exc.status)
        raise HTTPException(status_code=exc.status, detail=exc.message)
    except RuntimeError as exc:
        _audit_function(req.function, params, role,
                        (time.perf_counter() - started) * 1000,
                        error=str(exc), status=503)
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # tushare 权限/积分等
        msg = str(exc)
        status = 402 if ("积分" in msg or "quota" in msg.lower()
                         or "权限" in msg or "抱歉" in msg) else 500
        public_error = f"tushare quota/permission: {msg}" if status == 402 else "服务内部错误"
        _audit_function(req.function, params, role,
                        (time.perf_counter() - started) * 1000,
                        error=public_error, status=status)
        raise HTTPException(status_code=status, detail=public_error)
    _audit_function(req.function, params, role,
                    (time.perf_counter() - started) * 1000, data=data)
    return _versioned({"ok": True, "function": req.function,
                       "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "data": data})


@app.exception_handler(StarletteHTTPException)
def http_exc_handler(request, exc: StarletteHTTPException):
    """统一处理业务 HTTP 异常及未知路由、方法错误。"""
    return _error_response(
        exc.status_code,
        exc.detail,
        headers=dict(exc.headers or {}),
    )


@app.exception_handler(RequestValidationError)
def validation_exc_handler(request, exc: RequestValidationError):
    """统一处理请求体、路径和查询参数校验错误。"""
    return _error_response(
        422,
        "请求参数校验失败",
        details=exc.errors(),
    )


@app.exception_handler(Exception)
def unhandled_exc_handler(request, exc: Exception):
    """兜住未捕获异常，不向调用方泄漏内部异常细节。"""
    return _error_response(500, "服务内部错误")


@app.get("/")
def root():
    return RedirectResponse(url="/ui/")


# Web 前端与服务同源部署：http://<base>/ui/
if WEB_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")
