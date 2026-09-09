"""Run a pack's `examples/cases.yaml` offline, before it is ever enabled (EX03/EX12).

Validating a manifest proves the pack is *well formed*. It does not prove the
operation behaves: that a missing required field is refused rather than filled
in, that a model returning the wrong shape fails instead of being persisted,
that an operation declaring no tools cannot reach one anyway. Those are the
claims `examples/cases.yaml` makes, and until now nothing executed them.

The harness is isolated by construction (EX12):

* **No model.** Each case supplies `mocked_output` or `mocked_error`; the real
  `ModelGateway` runs with an injected fake chat model, so the code path is the
  genuine agent runner and the only thing replaced is the model itself.
* **No network, vault or credentials.** Tools declared by the operation are
  bound to recording stubs. A case that reaches a tool it did not declare in
  `allowed_tools` fails; a case cannot reach the owner's real environment at
  all, because none is wired in.
* **No persistence.** The invoker runs with `persist=False` and an in-memory
  database, so conformance never writes an artifact into the owner's store.

A case passes only when the *observed* status matches `expected_status` and the
tools actually called are a subset of `allowed_tools`. Both halves matter: an
operation that succeeds by calling a tool it should not have is not conformant,
and reporting it as passing is exactly the silent namespace takeover EX03
exists to prevent.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from loop.ai.budget import BudgetLimits, RootBudget
from loop.ai.model_gateway import ModelGateway
from loop.capabilities.registry import CapabilityRegistry, load_manifest
from loop.capabilities.runners import CapabilityInvoker, RegisteredHandler
from loop.core.errors import (
    ApprovalRequired,
    InvalidInput,
    LoopError,
    PrivacyBlocked,
    Timeout,
    Unavailable,
    ValidationFailed,
)
from loop.core.privacy import ModelScope, PrivacyLabel
from loop.core.settings import Settings
from loop.runtime.authority import AuthorityContext

#: Statuses a case may expect. Kept closed: a typo in `expected_status` that
#: silently matched nothing would turn a red case green.
STATUSES = ("succeeded", "invalid_input", "validation_failed", "timeout",
            "unavailable", "approval_required", "privacy_blocked")

CASES_FILE = "examples/cases.yaml"


@dataclass(frozen=True)
class CaseResult:
    id: str
    expected: str
    observed: str
    tools_called: tuple[str, ...]
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.expected == self.observed and not self.detail


@dataclass
class ConformanceReport:
    package: Path
    operation: str = ""
    results: list[CaseResult] = field(default_factory=list)
    #: Reasons the suite could not run at all (bad manifest, unreadable cases).
    problems: list[str] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        return not self.problems

    @property
    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]

    @property
    def ok(self) -> bool:
        return self.ran and not self.failures


def has_cases(package: Path) -> bool:
    return (package / CASES_FILE).is_file()


def run_conformance(package: Path) -> ConformanceReport:
    """Execute every case in `package`, returning what happened.

    Never raises for a failing case — a conformance run reports, and the
    caller decides whether a failure is fatal. It *does* report a bad manifest
    as a problem rather than a failing case, because a pack that does not load
    has no operation to test.
    """
    # Absolute: JSON-Schema `$ref` resolution builds a file URI from this path,
    # and a relative one raises where the caller cannot see why.
    package = Path(package).resolve()
    report = ConformanceReport(package=package)

    manifest, problems = load_manifest(package)
    if manifest is None:
        report.problems = list(problems)
        return report

    cases_path = package / CASES_FILE
    if not cases_path.is_file():
        report.problems = [f"no {CASES_FILE} in {package}"]
        return report

    try:
        document = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        report.problems = [f"{CASES_FILE} is not valid YAML: {exc}"]
        return report
    if not isinstance(document, dict):
        report.problems = [f"{CASES_FILE} must be a mapping"]
        return report

    operation_name = str(document.get("operation") or "")
    if operation_name not in manifest.operations:
        report.problems = [
            f"{CASES_FILE} names operation {operation_name!r}, which this pack "
            f"does not define ({', '.join(sorted(manifest.operations))})"]
        return report
    report.operation = operation_name

    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        report.problems = [f"{CASES_FILE} declares no cases"]
        return report

    operation = manifest.operations[operation_name]
    for index, case in enumerate(cases):
        report.results.append(
            _run_case(package, manifest, operation_name, operation,
                      case if isinstance(case, dict) else {}, index))
    return report


def _run_case(package: Path, manifest: Any, operation_name: str,
              operation: Any, case: dict[str, Any], index: int) -> CaseResult:
    case_id = str(case.get("id") or case.get("name") or f"case-{index + 1}")
    expected = str(case.get("expected_status") or "succeeded")
    allowed = {str(t) for t in (case.get("allowed_tools") or [])}

    if expected not in STATUSES:
        return CaseResult(id=case_id, expected=expected, observed="unrun",
                          tools_called=(),
                          detail=f"unknown expected_status {expected!r}")

    called: list[str] = []
    # Every tool the *operation* declares is bound, not only the ones this case
    # allows — otherwise an operation reaching an undeclared tool would fail as
    # "unavailable" and look like a different problem than the one it is.
    handlers = {name: _recording_stub(name, called)
                for name in getattr(operation, "tools", []) or []}

    # Discovery over the package itself, so the pack under test reaches the
    # invoker by exactly the path a configured root would use. The declared
    # tools are `provided` because their stubs are the ones bound below —
    # otherwise the pack sits in `missing_dependencies` and every case reports
    # `unavailable` for a reason that has nothing to do with the case.
    registry = CapabilityRegistry(roots=[package],
                                  provided=set(handlers) | set(
                                      getattr(manifest, "required_dependencies", [])))
    registry.register_all(registry.discover())
    registry.enable(manifest.id, manifest.version)

    gateway = ModelGateway(settings=_offline_settings(),
                           local_model=_scripted_model(case))
    invoker = CapabilityInvoker(registry=registry, gateway=gateway,
                                artifact_store=None, handlers=handlers)

    arguments = dict(case.get("input") or {})
    # The invoker raises the same `ValidationFailed` whether the *input* or the
    # *output* failed its schema, and a case needs to tell those apart: one is
    # a caller mistake, the other is a model that did not honour the contract.
    # Deciding the phase here — by checking the input against the very same
    # schema before invoking — keeps that distinction off a message substring.
    # The invocation still runs either way, so the invoker's own refusal is
    # what is observed; this only names which refusal it was.
    input_was_valid = _input_is_valid(package, manifest, operation, arguments)

    observed, detail = "succeeded", ""
    try:
        invoker.invoke(operation_name, arguments,
                       context=_offline_context(), persist=False)
    except InvalidInput:
        observed = "invalid_input"
    except ValidationFailed:
        observed = "validation_failed" if input_was_valid else "invalid_input"
    except Timeout:
        observed = "timeout"
    except ApprovalRequired:
        observed = "approval_required"
    except PrivacyBlocked as exc:
        # The gateway deliberately converts a local model failure on private
        # input into PrivacyBlocked. Conformance still needs to distinguish a
        # scripted typed timeout from generic unavailability, so inspect the
        # preserved exception chain rather than parsing error text.
        cause = exc.__cause__
        while cause is not None and not isinstance(cause, Timeout):
            cause = cause.__cause__
        observed = "timeout" if isinstance(cause, Timeout) else "privacy_blocked"
    except Unavailable:
        observed = "unavailable"
    except LoopError as exc:            # A typed failure nobody declared.
        observed, detail = "error", f"{type(exc).__name__}: {exc}"
    except Exception as exc:            # noqa: BLE001 - a case must not abort the suite
        observed, detail = "error", f"{type(exc).__name__}: {exc}"

    unexpected = sorted(set(called) - allowed)
    if unexpected:
        detail = (detail + "; " if detail else "") + \
            "called tools it did not declare: " + ", ".join(unexpected)
    return CaseResult(id=case_id, expected=expected, observed=observed,
                      tools_called=tuple(called), detail=detail)


def _input_is_valid(package: Path, manifest: Any, operation: Any,
                    arguments: dict[str, Any]) -> bool:
    """True when `arguments` satisfy the operation's declared input schema."""
    from loop.capabilities.runners import _schema_path, _validate_schema
    defaults = getattr(operation, "default_arguments", None) or {}
    try:
        _validate_schema(_schema_path(manifest, operation.input_schema),
                         {**defaults, **arguments}, "input")
    except Exception:      # noqa: BLE001 - any rejection means "not valid"
        return False
    return True


def _recording_stub(name: str, called: list[str]) -> RegisteredHandler:
    """A tool that records its use and reaches nothing.

    It returns a shape general enough for the schemas packs actually declare
    without pretending to have found anything: the point of a conformance run
    is the control flow, not the content.
    """
    def handler(arguments: dict[str, Any], context: AuthorityContext) -> dict[str, Any]:
        del arguments, context
        called.append(name)
        return {"matches": [], "sources": [], "answer": ""}

    return RegisteredHandler(
        handler, description=f"conformance stub for {name}",
        input_schema={"type": "object", "additionalProperties": True})


def _scripted_model(case: dict[str, Any]) -> Any:
    """LangChain's fake chat model, scripted from the case.

    `mocked_error: timeout` raises the real `Timeout` from inside the model
    call, so the runner's own error handling is what turns it into a status —
    rather than this function deciding the answer in advance.
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    error = str(case.get("mocked_error") or "")
    payload = case.get("mocked_output")
    content = json.dumps(payload if payload is not None else {},
                         ensure_ascii=False, sort_keys=True)

    class ConformanceModel(GenericFakeChatModel):
        def bind_tools(self, tools: Sequence[object], *,
                       tool_choice: str | None = None, **kwargs: object) -> Any:
            del tools, tool_choice, kwargs
            return self

        def _generate(self, messages: list[Any], stop: list[str] | None = None,
                      run_manager: Any = None, **kwargs: Any) -> Any:
            if error == "timeout":
                raise Timeout("The conformance case scripted a model timeout.")
            if error:
                raise Unavailable(f"The conformance case scripted {error!r}.")
            return super()._generate(messages, stop=stop,
                                     run_manager=run_manager, **kwargs)

    # Repeated rather than a single message: a repair attempt asks again, and a
    # StopIteration from an exhausted script is not the failure under test.
    return ConformanceModel(messages=iter([AIMessage(content=content)] * 8))


def _offline_settings() -> Settings:
    """In-memory, no vault, no cloud, a named local model that is a fake."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None, environment="test", database_url="sqlite://",
        obsidian_vault_path="", cloud_enabled=False,
        ollama_default_model="conformance-fake")


def _offline_context() -> AuthorityContext:
    return AuthorityContext(
        owner="conformance", root_id="conformance",
        privacy=PrivacyLabel(model_scope=ModelScope.LOCAL_ONLY,
                             origins=frozenset({"conformance-example"})),
        budget=RootBudget(limits=BudgetLimits(model_calls=4, tool_calls=8)))


def report_text(report: ConformanceReport) -> str:
    """Human-readable result, one line per case."""
    if not report.ran:
        return "\n".join([f"cannot run: {p}" for p in report.problems])
    lines = [f"{report.operation}: {len(report.results)} case(s)"]
    for result in report.results:
        mark = "ok  " if result.passed else "FAIL"
        line = f"  {mark} {result.id}: expected {result.expected}, got {result.observed}"
        if result.detail:
            line += f" — {result.detail}"
        lines.append(line)
    lines.append("all cases passed" if report.ok
                 else f"{len(report.failures)} case(s) failed")
    return "\n".join(lines)
