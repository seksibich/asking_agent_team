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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import common
import registry
import loader

API_KEY = os.getenv("API_KEY", "")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

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
    print(f"[startup] loaded {len(imported)} function modules, "
          f"{len(registry.names())} functions, data_version={registry.data_version()}")


def _check_key(x_api_key: Optional[str]) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="unauthorized")


def _versioned(body: dict[str, Any]) -> JSONResponse:
    body["data_version"] = registry.data_version()
    return JSONResponse(content=body, headers={"X-Data-Version": registry.data_version()})


class CallReq(BaseModel):
    function: str
    params: Optional[dict[str, Any]] = None


@app.get("/health")
def health():
    common.clean_expired_cache()
    day = common.today_str()
    try:
        open_ = common.is_trade_open(day)
    except Exception:
        open_ = None
    return _versioned({"status": "ok", "date": day, "trade_open": open_,
                       "functions": len(registry.names())})


@app.get("/functions")
def functions(x_api_key: Optional[str] = Header(None)):
    _check_key(x_api_key)
    return _versioned(registry.functions_index())


@app.post("/call")
def call(req: CallReq, x_api_key: Optional[str] = Header(None)):
    _check_key(x_api_key)
    try:
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
