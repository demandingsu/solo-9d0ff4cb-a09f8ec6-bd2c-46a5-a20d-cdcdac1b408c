"""合库工作台路由：首次双库上传、单独换库、编号对照查看、合库下载。

状态全部持久化在 DATA_DIR/merge 下（分室文件 + merged.db），
不依赖进程内存，重启后仍可下载合库与执行换库。
"""
from __future__ import annotations

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .core import EXPORT_TABLES
from .merge import (
    BRANCHES,
    BRANCH_LABELS,
    MergeError,
    MergeStore,
)

router = APIRouter()


def _store(request: Request) -> MergeStore:
    return request.app.state.merge_store


def _max_upload_mb(request: Request) -> int:
    return getattr(request.app.state, "max_upload_mb", 200)


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _render(request: Request, name: str, context: dict, status: int = 200):
    return _templates(request).TemplateResponse(request, name, context, status_code=status)


def _workbench_context(request: Request, **extra) -> dict:
    store = _store(request)
    status = store.status()
    ctx = {
        "status": status,
        "branches": BRANCHES,
        "branch_labels": BRANCH_LABELS,
        "tables": EXPORT_TABLES,
        "error": None,
        "info": None,
    }
    ctx.update(extra)
    return ctx


@router.get("/merge", response_class=HTMLResponse)
def workbench(request: Request):
    return _render(request, "merge.html", _workbench_context(request))


@router.post("/merge/init", response_class=HTMLResponse)
async def merge_init(
    request: Request,
    jia_file: UploadFile | None = None,
    yi_file: UploadFile | None = None,
):
    store = _store(request)
    limit = _max_upload_mb(request) * 1024 * 1024
    uploads = {}
    if jia_file is not None:
        uploads["jia"] = (jia_file.filename or "", await jia_file.read())
    if yi_file is not None:
        uploads["yi"] = (yi_file.filename or "", await yi_file.read())
    oversized = [
        BRANCH_LABELS[b] for b, (_name, data) in uploads.items() if len(data) > limit
    ]
    if oversized:
        ctx = _workbench_context(
            request,
            error=f"{'、'.join(oversized)}分室文件超过大小限制"
            f"（{_max_upload_mb(request)} MB）",
        )
        return _render(request, "merge.html", ctx, status=400)
    try:
        store.initialize(uploads)
    except MergeError as exc:
        ctx = _workbench_context(
            request, error=_format_merge_error(exc)
        )
        return _render(request, "merge.html", ctx, status=400)
    return RedirectResponse("/merge", status_code=303)


@router.post("/merge/replace/{branch}", response_class=HTMLResponse)
async def merge_replace(request: Request, branch: str, file: UploadFile | None = None):
    store = _store(request)
    if branch not in BRANCHES:
        return _render(
            request,
            "merge.html",
            _workbench_context(request, error=f"未知分室：{branch}"),
            status=404,
        )
    filename = file.filename if file else ""
    data = await file.read() if file else b""
    if not data:
        return _render(
            request,
            "merge.html",
            _workbench_context(request, error="未选择文件或文件为空"),
            status_code=400,
        )
    if len(data) > _max_upload_mb(request) * 1024 * 1024:
        return _render(
            request,
            "merge.html",
            _workbench_context(
                request,
                error=f"文件超过大小限制（{_max_upload_mb(request)} MB）",
            ),
            status_code=400,
        )
    try:
        store.replace_branch(branch, filename, data)
    except MergeError as exc:
        ctx = _workbench_context(
            request, error=_format_merge_error(exc)
        )
        return _render(request, "merge.html", ctx, status=400)
    return RedirectResponse("/merge", status_code=303)


@router.get("/merge/mapping", response_class=HTMLResponse)
def merge_mapping(request: Request):
    store = _store(request)
    branch = request.query_params.get("branch") or None
    table = request.query_params.get("table") or None
    if branch not in (None, *BRANCHES):
        branch = None
    if table not in (None, *EXPORT_TABLES):
        table = None
    rows = store.list_mapping(branch, table)
    ctx = _workbench_context(
        request,
        mapping=rows,
        filter_branch=branch,
        filter_table=table,
    )
    return _render(request, "merge_mapping.html", ctx)


@router.get("/merge/download")
def merge_download(request: Request):
    store = _store(request)
    if not store.is_initialized():
        return RedirectResponse("/merge", status_code=303)
    return FileResponse(
        store.merged_path,
        filename="merged.db",
        media_type="application/x-sqlite3",
    )


def _format_merge_error(exc: MergeError) -> str:
    v = exc.validation
    if v is None:
        return str(exc)
    parts = [str(exc)]
    if v.dangling:
        parts.append(f"悬空引用共 {len(v.dangling)} 条，相关编号见上")
    return "。".join(parts)
