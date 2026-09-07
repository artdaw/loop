"""The vNext HTTP surface (interfaces §4), sharing the same `Application`.

One `Application` is built at process startup and stored on `app.state`; every
route reads it from there — the same constraint the CLI observes via
`build_application()`, so a task completed through the API and one completed
through `loop-next task complete` are the same task table, not two.

**Scope of this pass.** interfaces §4 names a large target surface (messages,
reminders, captures, search, questions, research, compile, weather, trips,
routines, approvals, feedback, memory, activity, why, policy). This module
wires the subset backed by services that already exist end-to-end today —
tasks, capability discovery/enablement/invocation, and health/status — using
the required envelope, versioning, and auth contract so every later addition
follows the same shape. The remaining routes are not yet implemented; they are
not stubbed either, since a stub that returns 200 would be worse than a 404.

**Envelope (interfaces §4).** Success: ``{data, request_id, warnings}``.
Error: ``{error: {code, message, details}, request_id}`` — produced by
`LoopError.to_envelope`, the same structured taxonomy the CLI's exit codes use
(core/errors.py), so the API and CLI cannot drift on what a failure means.

**Auth.** A bearer token is required on every ``/api/v1/*`` request, even from
localhost (interfaces §4). ``Settings.api_bearer_token`` ships blank, so a
fresh install fails closed (401 on everything) rather than opening itself;
the operator must set ``API_BEARER_TOKEN`` before the API is usable.
``/health/live`` and ``/health/ready`` stay open, since a process-alive check
that itself requires a live process's secret is not useful to a health probe.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from html import escape
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Form, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import text as sql_text

from loop.ai.budget import RootBudget
from loop.api.service import (
    POST_REDIRECT_STATUS,
    CsrfError,
    DurableIdempotencyStore,
    issue_csrf_token,
    verify_csrf_token,
)
from loop.app import Application, build_application
from loop.core.clock import to_micros
from loop.core.errors import (
    AuthRequired,
    InvalidInput,
    LoopError,
    Unavailable,
)
from loop.core.ids import new_id
from loop.core.privacy import PrivacyLabel
from loop.runtime.authority import AuthorityContext
from loop.runtime.routine_dispatch import ROUTINE_SUBJECT, next_local_fire
from loop.services.knowledge import KnowledgeService

SCHEMA_VERSION = 1


def _application() -> Application:
    return build_application()


app = FastAPI(title="Loop vNext API")
app.state.application = None  # set on first request or by tests via override


def get_application(request: Request) -> Application:
    if request.app.state.application is None:
        request.app.state.application = _application()
    return request.app.state.application


def require_bearer_token(application: Application = Depends(get_application),
                         authorization: str | None = Header(default=None)
                         ) -> None:
    configured = application.settings.api_bearer_token
    if not configured:
        raise AuthRequired(
            "No API_BEARER_TOKEN is configured; the API refuses every "
            "request until an operator sets one.")
    if authorization != f"Bearer {configured}":
        raise AuthRequired("Missing or invalid bearer token.")


@app.exception_handler(LoopError)
def _handle_loop_error(request: Request, exc: LoopError) -> JSONResponse:
    del request
    return JSONResponse(status_code=exc.http_status,
                        content=exc.to_envelope(new_id()))


def _ok(data: Any, *, warnings: list[str] | None = None,
       status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code,
                        content={"data": data, "request_id": new_id(),
                                "warnings": warnings or []})


def idempotent(application: Application, key: str | None,
               request: dict[str, Any],
               act: Callable[[], tuple[int, dict[str, Any]]]) -> JSONResponse:
    """Run a mutation at most once per `Idempotency-Key` (T15, O08).

    Three outcomes, and the middle one is the whole point:

    * no key — the caller opted out of the guarantee, so just act;
    * a key seen with **this** body — replay the stored response without
      touching anything, because the client is retrying a request whose reply
      it never saw, and the mutation already happened;
    * a key seen with a **different** body — 409. Returning the first result
      would silently discard the second request, and applying it under a used
      key would defeat the retry guarantee for both.

    The store is durable, not in-process: a client retries precisely when it
    got no answer, which includes the server dying between committing and
    replying — the one case an in-memory record would have forgotten.
    """
    if not key:
        status_code, body = act()
        return _ok(body, status_code=status_code)

    store = DurableIdempotencyStore(sessions=application.sessions)
    replay = store.lookup(key, request=request)
    if replay is not None:
        status_code, body = replay
        return _ok(body, status_code=status_code,
                   warnings=["replayed: this idempotency key was already used "
                             "for this exact request"])

    status_code, body = act()
    store.remember(key, request=request, status_code=status_code,
                   response=body, now=to_micros(application.clock.now()))
    return _ok(body, status_code=status_code)


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready")
def health_ready(application: Application = Depends(get_application)
                 ) -> JSONResponse:
    try:
        with application.sessions() as session:
            session.execute(sql_text("SELECT 1"))
    except Exception:  # noqa: BLE001 — readiness must report, never crash
        return JSONResponse(status_code=503,
                            content={"status": "not_ready"})
    return JSONResponse(status_code=200, content={"status": "ready"})


router_dependencies = [Depends(require_bearer_token)]


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
@app.get("/api/v1/status", dependencies=router_dependencies)
def status(application: Application = Depends(get_application)) -> JSONResponse:
    pending = application.service.pending_summary()
    return _ok({
        "schema_version": SCHEMA_VERSION,
        "triggers_enabled": pending["triggers"],
        "jobs_pending": pending["jobs"],
        "outbox_pending": pending["outbox"],
        "outbox_unknown": pending["unknown"],
        "enabled_capabilities": sorted(application.registry.enabled_operations()),
        "limitations": application.model_gateway.describe_limitations(),
    })


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
class CreateTask(BaseModel):
    title: str
    due_date: str | None = None


class CompleteTask(BaseModel):
    expected_version: int


def _task_body(task: Any) -> dict[str, Any]:
    return {"id": task.id, "title": task.title, "status": task.status,
           "version": task.version, "due_date": task.due_date}


@app.get("/api/v1/tasks", dependencies=router_dependencies)
def list_tasks(status: str | None = None,
               application: Application = Depends(get_application)
               ) -> JSONResponse:
    tasks = [_task_body(t) for t in application.tasks.list(status=status)]
    return _ok({"items": tasks, "next_cursor": None})


@app.post("/api/v1/tasks", dependencies=router_dependencies, status_code=201)
def create_task(payload: CreateTask,
                application: Application = Depends(get_application),
                idempotency_key: str | None = Header(
                    default=None, alias="Idempotency-Key")) -> JSONResponse:
    def act() -> tuple[int, dict[str, Any]]:
        task = application.tasks.create(payload.title, due_date=payload.due_date)
        return 201, _task_body(task)

    return idempotent(application, idempotency_key, payload.model_dump(), act)


@app.post("/api/v1/tasks/{task_id}/complete", dependencies=router_dependencies)
def complete_task(task_id: str, payload: CompleteTask,
                  application: Application = Depends(get_application),
                  idempotency_key: str | None = Header(
                      default=None, alias="Idempotency-Key")) -> JSONResponse:
    def act() -> tuple[int, dict[str, Any]]:
        task = application.tasks.complete(
            task_id, expected_version=payload.expected_version)
        return 200, _task_body(task)

    return idempotent(application, idempotency_key,
                      {"task_id": task_id, **payload.model_dump()}, act)


# --------------------------------------------------------------------------- #
# Captures, questions and routines (M5)
# --------------------------------------------------------------------------- #
def _require_vault(application: Application) -> KnowledgeService:
    """A missing vault is `unavailable`, not an empty result set.

    Returning `{"items": []}` for a question when no vault is configured would
    be indistinguishable from a vault that genuinely holds nothing about it.
    """
    if application.knowledge is None:
        raise Unavailable("No vault is configured on this instance.")
    return application.knowledge


class CreateCapture(BaseModel):
    body: str
    title: str | None = None
    source_kind: str = "note"
    origin: str = "owner"


@app.post("/api/v1/captures", dependencies=router_dependencies, status_code=201)
def create_capture(payload: CreateCapture,
                   application: Application = Depends(get_application)
                   ) -> JSONResponse:
    knowledge = _require_vault(application)
    result = knowledge.capture(payload.body, title=payload.title,
                               source_kind=payload.source_kind,
                               origin=payload.origin)
    # `status` and `message` are the result, not decoration: a client that
    # showed "saved" for a queued capture would be reporting a vault write
    # that has not happened (vault §5).
    return _ok({"capture_id": result.capture_id, "path": result.path,
                "status": result.status, "registered": result.registered,
                "message": result.message}, status_code=201)


class CompileRequest(BaseModel):
    source: str | None = None
    limit: int = 10


@app.post("/api/v1/compile", dependencies=router_dependencies)
def compile_sources(payload: CompileRequest,
                    application: Application = Depends(get_application)
                    ) -> JSONResponse:
    knowledge = _require_vault(application)
    reports = ([knowledge.compile_source(payload.source)] if payload.source
               else knowledge.compile_pending(limit=payload.limit))
    return _ok({"items": [{
        "source": report.source_path, "outcome": report.outcome.value,
        "pages": report.pages, "ledger_status": report.ledger_status,
        "claims_written": report.claims_written,
        "claims_rejected": report.claims_rejected,
        "contradictions": report.contradictions, "reason": report.reason,
    } for report in reports]})


@app.get("/api/v1/questions", dependencies=router_dependencies)
def ask_question(q: str, application: Application = Depends(get_application)
                 ) -> JSONResponse:
    knowledge = _require_vault(application)
    answer = knowledge.answer(q)
    return _ok({
        "question": answer.question, "text": answer.text,
        "knowledge_gap": answer.knowledge_gap,
        "citations": [{"path": c.path, "title": c.title, "layer": c.layer,
                       "snippet": c.snippet, "sources": c.sources,
                       "uncompiled": c.uncompiled} for c in answer.citations],
    })


def _routine_body(application: Application, routine: Any) -> dict[str, Any]:
    triggers = application.triggers.for_subject(ROUTINE_SUBJECT, routine.slug)
    when = next_local_fire(triggers[0]) if triggers else None
    return {"slug": routine.slug, "title": routine.title,
            "status": routine.status.value,
            "next_run": when.isoformat() if when else None,
            "questions": routine.blocking_questions}


@app.get("/api/v1/routines", dependencies=router_dependencies)
def list_routines(application: Application = Depends(get_application)
                  ) -> JSONResponse:
    return _ok({"items": [_routine_body(application, r)
                          for r in application.routines.list_routines()]})


class ActivateRoutine(BaseModel):
    #: Required, with no default: activation is authority (P01), and a routine
    #: activated by an anonymous request has no record of who asked for it.
    activation_event_id: str


@app.post("/api/v1/routines/{slug}/activate", dependencies=router_dependencies)
def activate_routine(slug: str, payload: ActivateRoutine,
                     application: Application = Depends(get_application)
                     ) -> JSONResponse:
    routine = application.routine_scheduler.activate(
        slug, activation_event_id=payload.activation_event_id)
    return _ok(_routine_body(application, routine))


@app.post("/api/v1/routines/{slug}/pause", dependencies=router_dependencies)
def pause_routine(slug: str, application: Application = Depends(get_application)
                  ) -> JSONResponse:
    routine = application.routine_scheduler.pause(slug)
    return _ok(_routine_body(application, routine))


# --------------------------------------------------------------------------- #
# Capabilities (agent-stack §2: generic invocation, no route per pack)
# --------------------------------------------------------------------------- #
def _entry_body(entry: Any) -> dict[str, Any]:
    return {"pack_key": entry.manifest.pack_key, "id": entry.manifest.id,
           "version": entry.manifest.version, "title": entry.manifest.title,
           "enabled": entry.enabled, "availability": entry.availability.value}


@app.get("/api/v1/capabilities", dependencies=router_dependencies)
def list_capabilities(application: Application = Depends(get_application)
                      ) -> JSONResponse:
    items = [_entry_body(e) for e in application.registry.entries()]
    return _ok({"items": items, "next_cursor": None})


@app.get("/api/v1/capabilities/{pack_id}", dependencies=router_dependencies)
def get_capability(pack_id: str,
                   application: Application = Depends(get_application)
                   ) -> JSONResponse:
    """`pack_id` names a pack across all its versions (`registry.enable`'s own
    unit); the registry itself keys entries by `id@version`, so this picks the
    enabled version if there is one, else the first discovered version."""
    matches = [e for e in application.registry.entries()
              if e.manifest.id == pack_id]
    if not matches:
        raise InvalidInput(f"No such capability {pack_id!r}")
    entry = next((e for e in matches if e.enabled), matches[0])
    return _ok(_entry_body(entry))


class EnablePack(BaseModel):
    version: str


@app.post("/api/v1/capabilities/{pack_id}/enable", dependencies=router_dependencies)
def enable_capability(pack_id: str, payload: EnablePack,
                      application: Application = Depends(get_application)
                      ) -> JSONResponse:
    entry = application.registry.enable(pack_id, payload.version)
    return _ok(_entry_body(entry))


@app.post("/api/v1/capabilities/{pack_id}/disable", dependencies=router_dependencies)
def disable_capability(pack_id: str,
                       application: Application = Depends(get_application)
                       ) -> JSONResponse:
    affected = application.registry.disable(pack_id)
    return _ok({"disabled": affected})


class InvokeOperation(BaseModel):
    arguments: dict[str, Any] = {}


@app.post("/api/v1/capabilities/{operation}/invoke", dependencies=router_dependencies)
def invoke_capability(operation: str, payload: InvokeOperation,
                      application: Application = Depends(get_application)
                      ) -> JSONResponse:
    """Invoke any enabled operation by name — the same route serves every
    pack, present or future (agent-stack §2)."""
    # An operation nobody has enabled is `unavailable`, not a validation
    # failure: the request was well formed and the operation may exist once it
    # is enabled. Checked here so the API agrees with the CLI and the bot,
    # which reach the same verdict through the invoker.
    if operation not in application.known_capabilities():
        raise Unavailable(f"Required operation {operation!r} is unavailable.",
                          details={"operation": operation})

    context = AuthorityContext(owner="owner", root_id="http", privacy=PrivacyLabel(),
                               budget=RootBudget())
    result = application.coordinator.invoke(
        objective=operation, operation=operation, arguments=payload.arguments,
        context=context)
    return _ok(result.response)


# --------------------------------------------------------------------------- #
# HTML forms (O07)
# --------------------------------------------------------------------------- #
# A browser cannot set an Authorization header on a plain form POST, so the UI
# authenticates with the same token carried as a cookie, and CSRF is bound to
# that cookie's value. Two separate checks doing two separate jobs: the cookie
# says *who*, the token says *this form came from a page we served*. A local
# port is not authentication — anything on the machine, including a page open
# in the browser, can reach it (interfaces §5).
UI_COOKIE = "loop_session"


def _ui_session(application: Application, session: str | None) -> str:
    configured = application.settings.api_bearer_token
    if not configured:
        raise AuthRequired("No API_BEARER_TOKEN is configured; the UI refuses "
                           "every request until an operator sets one.")
    if not session or not hmac.compare_digest(session, configured):
        raise AuthRequired("Sign in before using the web interface.")
    return session


def _page(body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>Loop</title>"
        "<style>body{font:14px system-ui;margin:2rem;max-width:40rem}"
        "li{margin:.4rem 0}</style>" + body)


@app.get("/ui/tasks", response_class=HTMLResponse)
def ui_tasks(application: Application = Depends(get_application),
             loop_session: str | None = Cookie(default=None)) -> HTMLResponse:
    session = _ui_session(application, loop_session)
    token = issue_csrf_token(session, secret=application.settings.api_bearer_token)

    rows = []
    for task in application.tasks.list(status="ready"):
        rows.append(
            f"<li>{escape(task.title)} "
            f"<form method='post' action='/ui/tasks/{escape(task.id)}/complete' "
            "style='display:inline'>"
            f"<input type='hidden' name='csrf_token' value='{escape(token)}'>"
            f"<input type='hidden' name='expected_version' value='{task.version}'>"
            "<button type='submit'>Done</button></form></li>")
    listing = "".join(rows) or "<li>Nothing open.</li>"
    return _page(f"<h1>Tasks</h1><ul>{listing}</ul>")


@app.post("/ui/tasks/{task_id}/complete")
def ui_complete_task(task_id: str,
                     csrf_token: str = Form(default=""),
                     expected_version: int = Form(...),
                     application: Application = Depends(get_application),
                     loop_session: str | None = Cookie(default=None)
                     ) -> RedirectResponse:
    """Apply, then redirect. 303 so a refresh does not repeat the mutation."""
    session = _ui_session(application, loop_session)
    try:
        verify_csrf_token(csrf_token, session_id=session,
                          secret=application.settings.api_bearer_token)
    except CsrfError as exc:
        raise AuthRequired(str(exc)) from exc

    application.tasks.complete(task_id, expected_version=expected_version)
    return RedirectResponse("/ui/tasks", status_code=POST_REDIRECT_STATUS)


def main() -> None:
    import uvicorn
    settings = _application().settings
    uvicorn.run(app, host=settings.http_bind, port=settings.http_port)
