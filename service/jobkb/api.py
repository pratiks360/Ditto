"""HTTP surface. Binds to localhost; the extension is the only client."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field as PField

from . import ingest
from .config import settings
from .learn import CapturedField, Submission, learn
from .okf import Record
from .openrouter import ModelError, router
from .planner import Field, Planner
from .store import get_store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jobkb.api")

_client: httpx.AsyncClient | None = None
_planner: Planner | None = None


def planner() -> Planner:
    global _planner
    if _planner is None:
        _planner = Planner(get_store())
    return _planner


def client() -> httpx.AsyncClient:
    if _client is None:
        raise HTTPException(503, "service is still starting")
    return _client


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient()
    store = get_store()
    for note in ingest.sync_from_disk(store):   # JOBKB_RESUME / JOBKB_RESUME_FILE
        log.info("resume: %s", note)
    store.refresh_indexes()
    planner()
    if settings.has_key:
        # Build the free-model ladder before the first form arrives, so the
        # first fill is not the one that pays for discovery.
        await router.ensure_ladder(_client)
    else:
        log.warning("no OPENROUTER_API_KEY — retrieval only, no routing or drafting")
    yield
    await _client.aclose()


app = FastAPI(title=settings.app_title, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://.*|moz-extension://.*|http://localhost(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth(request: Request, call_next):
    if settings.token and request.url.path not in ("/health", "/docs", "/openapi.json"):
        if request.headers.get("x-jobkb-token") != settings.token:
            raise HTTPException(401, "bad or missing X-JobKB-Token")
    return await call_next(request)


# -- models ---------------------------------------------------------------


class FieldIn(BaseModel):
    id: str
    label: str = ""
    inputType: str = "text"
    tag: str = "input"
    required: bool = False
    placeholder: str = ""
    pattern: str = ""
    hint: str = ""
    maxLength: int = 0
    options: list[str] = PField(default_factory=list)
    readOnly: bool = False
    pickerOnly: bool = False
    blockIndex: int = -1
    value: str = ""


class PlanIn(BaseModel):
    fields: list[FieldIn]
    site: str = ""
    url: str = ""
    signature: str = ""
    jobDescription: str = ""
    allowAI: bool = True


class CapturedIn(BaseModel):
    label: str = ""
    value: str = ""
    id: str = ""
    pointer: str = ""


class LearnIn(BaseModel):
    site: str = ""
    url: str = ""
    signature: str = ""
    jobTitle: str = ""
    company: str = ""
    jobDescription: str = ""
    allowAI: bool = True
    items: list[CapturedIn] = PField(default_factory=list)


class ResumeIn(BaseModel):
    text: str
    name: str = "resume"


class ApplyIn(BaseModel):
    data: dict[str, Any]


class RecordIn(BaseModel):
    type: str
    title: str = ""
    description: str = ""
    resource: str = ""
    tags: list[str] = PField(default_factory=list)
    fields: dict[str, Any] = PField(default_factory=dict)
    aliases: list[str] = PField(default_factory=list)
    body: str = ""


# -- endpoints ------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    store = get_store()
    p = planner()
    return {
        "ok": True,
        "root": str(store.root),
        "records": len(store.records),
        "pointers": len(p.retriever.catalog),
        "semantic": p.retriever.semantic,
        "embedder": p.retriever.embedder.backend,
        "hasKey": settings.has_key,
        "models": [m.as_dict() for m in router.ladder[: settings.ladder_depth]],
        "modelError": router.last_discovery_error,
        "authError": router.auth_error,
    }


@app.get("/models")
async def models(refresh: bool = False) -> dict[str, Any]:
    if refresh:
        router.fetched_at = 0.0
        await router.ensure_ladder(client())
    return {"count": len(router.ladder), "ladder": [m.as_dict() for m in router.ladder]}


@app.post("/plan")
async def plan(body: PlanIn) -> dict[str, Any]:
    fields = [Field(**f.model_dump()) for f in body.fields]
    return await planner().plan(
        client(), fields,
        site=body.site, signature=body.signature,
        job_description=body.jobDescription,
        allow_ai=body.allowAI and settings.has_key,
    )


@app.post("/learn")
async def learn_endpoint(body: LearnIn) -> dict[str, Any]:
    sub = Submission(
        site=body.site, url=body.url, signature=body.signature,
        job_title=body.jobTitle, company=body.company,
        job_description=body.jobDescription,
        items=[CapturedField(**i.model_dump()) for i in body.items],
    )
    result = await learn(client(), get_store(), sub, allow_ai=body.allowAI and settings.has_key)
    planner().rebuild()   # new answers must be routable immediately
    return result


@app.post("/ingest/resume")
async def ingest_resume(body: ResumeIn) -> dict[str, Any]:
    rec = ingest.save_resume(get_store(), body.text, body.name)
    planner().rebuild()
    return {"path": rec.path, "chars": len(rec.body)}


@app.post("/ingest/extract")
async def ingest_extract() -> dict[str, Any]:
    store = get_store()
    text = store.resume_text()
    if not text:
        raise HTTPException(400, "no resume stored — POST /ingest/resume first")
    try:
        out = await ingest.extract(client(), text)
    except ModelError as exc:
        raise HTTPException(502, str(exc)) from exc
    out["report"] = ingest.plan_apply(store, out["data"])
    return out


@app.post("/ingest/apply")
async def ingest_apply(body: ApplyIn) -> dict[str, Any]:
    out = ingest.apply(get_store(), body.data)
    planner().rebuild()
    return out


@app.get("/records")
async def list_records(type: str = "") -> dict[str, Any]:
    recs = get_store().all()
    if type:
        recs = [r for r in recs if r.type == type]
    return {"count": len(recs), "records": [
        {"path": r.path, "type": r.type, "title": r.title,
         "tags": r.tags, "timestamp": r.timestamp}
        for r in sorted(recs, key=lambda r: r.path)
    ]}


@app.get("/records/{path:path}")
async def get_record(path: str) -> dict[str, Any]:
    rec = get_store().get(path)
    if rec is None:
        raise HTTPException(404, f"no record at {path}")
    return {
        "path": rec.path, "type": rec.type, "title": rec.title,
        "description": rec.description, "resource": rec.resource, "tags": rec.tags,
        "timestamp": rec.timestamp, "fields": rec.fields, "aliases": rec.aliases,
        "seen_on": rec.seen_on, "body": rec.body,
    }


@app.put("/records/{path:path}")
async def put_record(path: str, body: RecordIn) -> dict[str, Any]:
    if not path.endswith(".md") or ".." in path:
        raise HTTPException(400, "path must be a .md file inside the bundle")
    store = get_store()
    rec = Record(path=path.lstrip("/"), **body.model_dump())
    store.save(rec)
    store.refresh_indexes()
    planner().rebuild()
    return {"path": rec.path}


@app.delete("/records/{path:path}")
async def delete_record(path: str) -> dict[str, Any]:
    store = get_store()
    if not store.delete(path):
        raise HTTPException(404, f"no record at {path}")
    store.refresh_indexes()
    planner().rebuild()
    return {"deleted": path}


class ResumeFileIn(BaseModel):
    filename: str
    mime: str = "application/pdf"
    base64: str


@app.post("/ingest/resume-file")
async def ingest_resume_file(body: ResumeFileIn) -> dict[str, Any]:
    """Store the original document, so applications can attach the real thing."""
    import base64 as b64

    try:
        data = b64.b64decode(body.base64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"base64 did not decode: {exc}") from exc
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, "file is larger than 15 MB")

    rec = ingest.save_resume_file(get_store(), data, body.filename, body.mime)
    planner().rebuild()
    return {"path": rec.path, "file": rec.fields["file"], "bytes": rec.fields["bytes"]}


@app.get("/resume/file")
async def get_resume_file() -> dict[str, Any]:
    """The stored document, base64'd, for the extension to attach to a form."""
    import base64 as b64

    found = ingest.read_resume_file(get_store())
    if found is None:
        raise HTTPException(404, "no resume file stored - POST /ingest/resume-file first")
    data, filename, mime = found
    return {
        "filename": filename, "mime": mime, "bytes": len(data),
        "base64": b64.b64encode(data).decode("ascii"),
    }


@app.get("/resume/status")
async def resume_status() -> dict[str, Any]:
    """What the service currently holds, for the Options page to show."""
    store = get_store()
    found = ingest.read_resume_file(store)
    text = store.resume_text()
    return {
        "text": {"stored": bool(text), "chars": len(text)},
        "file": {"stored": found is not None,
                 "filename": found[1] if found else "",
                 "bytes": len(found[0]) if found else 0},
        "envText": str(settings.resume_path or ""),
        "envFile": str(settings.resume_file_path or ""),
    }


class AnswerIn(BaseModel):
    question: str
    answer: str
    site: str = ""
    tags: list[str] = PField(default_factory=list)


@app.post("/answer")
async def save_answer(body: AnswerIn) -> dict[str, Any]:
    """One question, one answer, straight from the user.

    This is what the in-page prompt posts when it asks you something the store
    could not answer ("how much experience with Apigee?"). From here on it is an
    ordinary Answer record, so the next site that asks it — however it is worded
    — resolves locally with no model call.
    """
    store = get_store()
    rec = store.upsert_answer(body.question, body.answer, tags=[*body.tags, "user"])
    if body.site and not any(s.get("site") == body.site for s in rec.seen_on):
        rec.seen_on.append({"site": body.site})
        store.save(rec)
    store.refresh_indexes()
    planner().rebuild()
    return {"path": rec.path, "pointer": rec.pointer(), "aliases": rec.aliases}


@app.post("/reindex")
async def reindex() -> dict[str, Any]:
    store = get_store()
    store.load()
    store.refresh_indexes()
    planner().rebuild()
    return {"records": len(store.records), "pointers": len(planner().retriever.catalog)}


@app.post("/resolve")
async def resolve(pointer: str = Body(..., embed=True)) -> dict[str, Any]:
    value = get_store().resolve(pointer)
    return {"pointer": pointer, "value": value,
            "label": get_store().describe(pointer), "resolved": value is not None}
