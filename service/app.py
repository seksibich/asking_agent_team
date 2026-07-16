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

import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import common
import db
import registry
import loader
import selection_tags
import version

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
}

# 选股读取安全红线：访客不得读取关注或持仓；默认查询由 DB 层自动排除。
SENSITIVE_SELECTION_CATEGORIES = frozenset({"watch", "holding"})
SELECTION_CATEGORY_FILTER_FUNCTIONS = frozenset({"selection_dashboard"})

# 动态访客 Key（由管理员在设置页生成/管理）落库 config_kv 的键
USER_KEYS_CONFIG_KEY = "user_api_keys"

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Stock Data Service", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    try:
        import db
        db.init_db()
        print("[startup] db ready:", db.db_url())
    except Exception as e:  # noqa: BLE001
        print(f"[startup] db init skipped: {e}")
    imported = loader.discover()
    print(f"[startup] auth configured: admin_keys={len(ADMIN_API_KEYS)}, "
          f"user_key={bool(USER_API_KEY)}")
    print(f"[startup] loaded {len(imported)} function modules, "
          f"{len(registry.names())} functions, data_version={registry.data_version()}")


def _dynamic_user_keys() -> list[dict[str, Any]]:
    """读取管理员在设置页生成的动态访客 Key 列表（落库 config_kv）。"""
    try:
        import db
        v = db.get_config(USER_KEYS_CONFIG_KEY)
        if isinstance(v, dict):
            keys = v.get("keys")
            return keys if isinstance(keys, list) else []
    except Exception:
        pass
    return []


def _save_user_keys(keys: list[dict[str, Any]]) -> None:
    import db
    db.set_config(USER_KEYS_CONFIG_KEY, {"keys": keys})


def _role_for(x_api_key: Optional[str]) -> Optional[str]:
    """返回调用方角色：admin / user / None（未授权）。
    未配置任何凭据时，未输入 token 只按只读用户处理；已配置管理员 Key 时空 token 仍未授权。"""
    candidate = _normalize_key(x_api_key)
    if candidate and any(secrets.compare_digest(candidate, key) for key in ADMIN_API_KEYS):
        return "admin"
    dyn = _dynamic_user_keys()
    if not ADMIN_API_KEYS and not USER_API_KEY and not dyn:
        return "user"   # 未配置凭据时允许只读访问，但不开放管理员操作
    if USER_API_KEY and candidate and secrets.compare_digest(candidate, USER_API_KEY):
        return "user"
    for k in dyn:
        dynamic_key = _normalize_key(k.get("key"))
        if not k.get("disabled") and candidate and dynamic_key and secrets.compare_digest(candidate, dynamic_key):
            return "user"
    return None


def _check_key(x_api_key: Optional[str]) -> str:
    role = _role_for(x_api_key)
    if role is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return role


def _require_admin(x_api_key: Optional[str]) -> None:
    if _role_for(x_api_key) != "admin":
        raise HTTPException(status_code=403, detail="forbidden: 需要管理员 Key")


def _versioned(body: dict[str, Any]) -> JSONResponse:
    """统一编码日期、Decimal 等数据库类型，避免写入成功后响应序列化为 HTTP 500。"""
    body["data_version"] = registry.data_version()
    return JSONResponse(content=jsonable_encoder(body),
                        headers={"X-Data-Version": registry.data_version()})


class CallReq(BaseModel):
    function: str
    params: Optional[dict[str, Any]] = None


@app.get("/health")
def health():
    common.clean_expired_cache()
    day = common.today_str()
    tushare_ready = bool(common.TUSHARE_TOKEN)
    try:
        open_ = common.is_trade_open(day) if tushare_ready else None
    except Exception:
        open_ = None
    db_ready = True
    portfolio_version = "unavailable"
    try:
        import db
        db.get_engine().connect().close()
        portfolio_version = db.get_portfolio_version()
    except Exception:
        db_ready = False
    return _versioned({"status": "ok", "date": day, "trade_open": open_,
                       "tushare_ready": tushare_ready, "db_ready": db_ready,
                       "portfolio_version": portfolio_version,
                       "selection_tag_version": selection_tags.TAG_VERSION,
                       "functions": len(registry.names()),
                       # agent 文档版本对齐：语义版本 + 部署 git 版本（供 agent 增量更新自身文档）
                       "agent_doc_version": version.agent_doc_version(),
                       "git_revision": version.git_revision()})


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
    """列出全部动态访客 Key（仅管理员）。管理员可见完整 Key 以便分发给访客。"""
    _require_admin(x_api_key)
    keys = _dynamic_user_keys()
    out = [{"id": k.get("id"), "label": k.get("label", ""), "key": k.get("key"),
            "created_at": k.get("created_at"), "disabled": bool(k.get("disabled"))}
           for k in keys]
    return _versioned({"keys": out, "env_user_key_enabled": bool(USER_API_KEY)})


@app.post("/admin/user-keys")
def create_user_key(req: UserKeyCreate, x_api_key: Optional[str] = Header(None)):
    """生成一个新的访客 Key（仅管理员）。"""
    _require_admin(x_api_key)
    keys = _dynamic_user_keys()
    item = {"id": secrets.token_hex(6),
            "label": (req.label or "").strip() or "访客",
            "key": _gen_user_key(),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "disabled": False}
    keys.append(item)
    _save_user_keys(keys)
    return _versioned({"created": True, "item": item})


@app.post("/admin/user-keys/toggle")
def toggle_user_key(req: UserKeyOp, x_api_key: Optional[str] = Header(None)):
    """启用/停用某个访客 Key（仅管理员）。"""
    _require_admin(x_api_key)
    keys = _dynamic_user_keys()
    found = False
    for k in keys:
        if k.get("id") == req.id:
            k["disabled"] = not bool(k.get("disabled"))
            found = True
    if found:
        _save_user_keys(keys)
    return _versioned({"toggled": found, "id": req.id})


@app.post("/admin/user-keys/delete")
def delete_user_key(req: UserKeyOp, x_api_key: Optional[str] = Header(None)):
    """删除某个访客 Key（仅管理员）。删除后该 Key 立即失效。"""
    _require_admin(x_api_key)
    keys = _dynamic_user_keys()
    new_keys = [k for k in keys if k.get("id") != req.id]
    deleted = len(new_keys) != len(keys)
    if deleted:
        _save_user_keys(new_keys)
    return _versioned({"deleted": deleted, "id": req.id})


class SelectionDeleteReq(BaseModel):
    id: int
    confirm_code: str


@app.post("/admin/selections/delete")
def delete_selection(req: SelectionDeleteReq, x_api_key: Optional[str] = Header(None)):
    """按数字主键永久删除选股及关联收益；历史回测快照保留，仅管理员可用。"""
    _require_admin(x_api_key)
    if req.id <= 0:
        raise HTTPException(status_code=400, detail="选股记录 id 必须为正整数")
    result = db.delete_selection(req.id, req.confirm_code)
    if result.get("reason") == "not_found":
        raise HTTPException(status_code=404, detail="选股记录不存在或已删除")
    if result.get("reason") == "confirm_mismatch":
        raise HTTPException(status_code=400, detail="确认股票代码不匹配，已取消删除")
    if not result.get("deleted"):
        raise HTTPException(status_code=500, detail="选股记录删除失败")
    return _versioned(result)


@app.post("/call")
def call(req: CallReq, x_api_key: Optional[str] = Header(None)):
    role = _check_key(x_api_key)
    if role != "admin" and req.function in ADMIN_ONLY_FUNCTIONS:
        raise HTTPException(
            status_code=403,
            detail=f"forbidden: 功能 '{req.function}' 需管理员 Key（用户 Key 不可调用管理员专属功能）")
    requested_category = str((req.params or {}).get("category") or "").strip()
    if (role != "admin" and req.function in SELECTION_CATEGORY_FILTER_FUNCTIONS
            and requested_category in SENSITIVE_SELECTION_CATEGORIES):
        raise HTTPException(
            status_code=403,
            detail=f"forbidden: 选股类别 '{requested_category}' 仅管理员可查看")
    try:
        # 全部注册函数共享同一请求级读取范围；即使未来新增 DB 查询调用点，
        # 访客也只能在 SQL 层读取 auto/manual，不能通过省略 category 绕过。
        with db.selection_read_scope(include_sensitive=role == "admin"):
            data = registry.call(req.function, req.params)
    except registry.ParamError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except common.ServiceError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:  # tushare 权限/积分等
        msg = str(e)
        if "积分" in msg or "quota" in msg.lower() or "权限" in msg or "抱歉" in msg:
            raise HTTPException(status_code=402, detail=f"tushare quota/permission: {msg}")
        raise HTTPException(status_code=500, detail=msg)
    return _versioned({"ok": True, "function": req.function,
                       "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "data": data})


@app.exception_handler(HTTPException)
def http_exc_handler(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code,
                        content={"ok": False, "error": exc.detail, "status": exc.status_code,
                                 "data_version": registry.data_version()})


@app.get("/")
def root():
    return RedirectResponse(url="/ui/")


# Web 前端与服务同源部署：http://<base>/ui/
if WEB_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")
