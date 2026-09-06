"""Generic capability execution for agent, workflow and adapter packs.

The runners know only the three manifest modes.  Domain behavior stays in pack
data or in explicitly registered trusted handlers, so installing an ordinary
pack never adds a coordinator branch.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, TypedDict

import yaml
from jsonschema import Draft202012Validator
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from pydantic import ConfigDict, create_model
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from sqlalchemy.orm import Session, sessionmaker

from loop.ai.model_gateway import ModelGateway, gateway_chat_model
from loop.capabilities.registry import CapabilityRegistry, Manifest, OperationDef
from loop.core.clock import Clock, SystemClock, to_micros
from loop.core.errors import ApprovalRequired, InvalidInput, Unavailable, ValidationFailed
from loop.core.ids import content_hash, new_id
from loop.runtime.authority import AuthorityContext, ToolWrapper


@dataclass
class InvocationResult:
    """Validated output plus the immutable artifact that records it."""

    operation: str
    output: dict[str, Any]
    evidence: list[Any] = field(default_factory=list)
    artifact_id: str | None = None
    child_results: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class RegisteredHandler:
    """A trusted application port exposed to packs under a stable name."""

    handler: Callable[..., Any]
    description: str = "Registered Loop operation"
    input_schema: dict[str, Any] = field(default_factory=lambda: {
        "type": "object", "additionalProperties": True,
    })
    required_scope: str | None = None


class ArtifactStore:
    """Persists immutable capability results in the existing artifacts table."""

    def __init__(self, *, sessions: sessionmaker[Session],
                 clock: Clock | None = None) -> None:
        self._sessions = sessions
        self._clock = clock or SystemClock()

    def save(self, *, kind: str, payload: dict[str, Any], evidence: list[Any],
             context: AuthorityContext, plan_id: str | None = None) -> str:
        from loop.db.models import Artifact

        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), default=str)
        artifact_id = new_id()
        now = to_micros(self._clock.now())
        row = Artifact(
            id=artifact_id, version=1, created_at=now, updated_at=now,
            plan_id=plan_id, kind=kind, payload_json=encoded,
            content_path=None, content_hash=content_hash(encoded),
            evidence_json=json.dumps(evidence, sort_keys=True,
                                     ensure_ascii=False, default=str),
            privacy=json.dumps(context.privacy.to_json(), sort_keys=True),
        )
        with self._sessions() as session:
            session.add(row)
            session.commit()
        return artifact_id


def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}


def _merge_lists(left: list[Any], right: list[Any]) -> list[Any]:
    return [*left, *right]


class _WorkflowState(TypedDict):
    arguments: dict[str, Any]
    results: Annotated[dict[str, dict[str, Any]], _merge_dicts]
    evidence: Annotated[list[Any], _merge_lists]


class CapabilityInvoker:
    """Resolve a pinned operation and dispatch it through its generic runner."""

    def __init__(self, *, registry: CapabilityRegistry,
                 gateway: ModelGateway | None = None,
                 artifact_store: ArtifactStore | None = None,
                 handlers: Mapping[str, RegisteredHandler | Callable[..., Any]] | None = None
                 ) -> None:
        self.registry = registry
        self.gateway = gateway
        self.artifact_store = artifact_store
        self.handlers: dict[str, RegisteredHandler] = {}
        for name, binding in (handlers or {}).items():
            self.handlers[name] = (binding if isinstance(binding, RegisteredHandler)
                                   else RegisteredHandler(binding))

    def invoke(self, operation_name: str, arguments: dict[str, Any], *,
               context: AuthorityContext, plan_id: str | None = None,
               persist: bool = True) -> InvocationResult:
        resolved = self.registry.resolve_operation(operation_name)
        if resolved is None:
            output = self._invoke_handler(operation_name, arguments, context)
            return InvocationResult(operation=operation_name, output=output,
                                    evidence=_evidence_from(output))

        manifest, operation = resolved
        if operation.needs_approval:
            raise ApprovalRequired(
                "This operation requires bound approval before execution.",
                details={"operations": [operation_name]})
        merged_arguments = {**operation.default_arguments, **arguments}
        _validate_schema(_schema_path(manifest, operation.input_schema),
                         merged_arguments, f"{operation_name} input")

        if operation.mode == "agent":
            output, evidence, children = self._run_agent(
                manifest, operation, merged_arguments, context)
        elif operation.mode == "workflow":
            output, evidence, children = self._run_workflow(
                manifest, operation, merged_arguments, context)
        elif operation.mode == "adapter":
            output = self._invoke_handler(
                operation.handler or "", merged_arguments, context)
            evidence, children = _evidence_from(output), {}
        else:  # Manifest validation makes this unreachable; keep failure typed.
            raise ValidationFailed(f"Unsupported capability mode {operation.mode!r}")

        _validate_schema(_schema_path(manifest, operation.output_schema), output,
                         f"{operation_name} output")
        artifact_id = None
        if persist and self.artifact_store is not None:
            artifact_id = self.artifact_store.save(
                kind=operation.result_artifact_kind, payload=output,
                evidence=evidence, context=context, plan_id=plan_id)
        return InvocationResult(operation=operation_name, output=output,
                                evidence=evidence, artifact_id=artifact_id,
                                child_results=children)

    def _invoke_handler(self, name: str, arguments: dict[str, Any],
                        context: AuthorityContext) -> dict[str, Any]:
        binding = self.handlers.get(name)
        if binding is None:
            raise Unavailable(f"Required operation {name!r} is unavailable.",
                              details={"operation": name})
        _validate_inline_schema(binding.input_schema, arguments, f"{name} input")
        wrapper = ToolWrapper(name, binding.handler,
                              required_scope=binding.required_scope)
        result = wrapper(arguments, context=context)
        if not isinstance(result, dict):
            raise ValidationFailed(
                f"Registered operation {name!r} returned a non-object result.")
        return result

    def _run_agent(self, manifest: Manifest, operation: OperationDef,
                   arguments: dict[str, Any], context: AuthorityContext
                   ) -> tuple[dict[str, Any], list[Any], dict[str, dict[str, Any]]]:
        if self.gateway is None:
            raise Unavailable("Agent capability execution needs a ModelGateway.")
        package = _package(manifest)
        instructions = (package / str(operation.instructions)).read_text(encoding="utf-8")
        child_results: dict[str, dict[str, Any]] = {}
        tools = [self._langchain_tool(name, context, child_results)
                 for name in operation.tools]
        model = gateway_chat_model(self.gateway, labels=[context.privacy],
                                   budget=context.budget, purpose="agent")
        graph = create_agent(model=model, tools=tools, system_prompt=instructions,
                             name=_safe_name(operation.name))
        state = graph.invoke({"messages": [{
            "role": "user",
            "content": json.dumps(arguments, ensure_ascii=False, sort_keys=True),
        }]})
        output = _last_json_message(state.get("messages", []))
        evidence = [item for result in child_results.values()
                    for item in _evidence_from(result)]
        return output, evidence, child_results

    def _langchain_tool(self, operation_name: str, context: AuthorityContext,
                        child_results: dict[str, dict[str, Any]]) -> StructuredTool:
        binding = self.handlers.get(operation_name)
        if binding is None:
            raise Unavailable(f"Required tool {operation_name!r} is unavailable.")
        args_model = _model_from_schema(operation_name, binding.input_schema)
        tool_name = _safe_name(operation_name)

        def call_tool(**kwargs: Any) -> str:
            output = self._invoke_handler(operation_name, kwargs, context)
            result_key = operation_name
            suffix = 2
            while result_key in child_results:
                result_key = f"{operation_name}#{suffix}"
                suffix += 1
            child_results[result_key] = output
            return json.dumps(output, ensure_ascii=False, sort_keys=True)

        return StructuredTool.from_function(
            func=call_tool, name=tool_name, description=binding.description,
            args_schema=args_model)

    def _run_workflow(self, manifest: Manifest, operation: OperationDef,
                      arguments: dict[str, Any], context: AuthorityContext
                      ) -> tuple[dict[str, Any], list[Any], dict[str, dict[str, Any]]]:
        workflow_path = _package(manifest) / str(operation.workflow)
        raw = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
        steps = _validate_workflow(raw, allowed=set(operation.tools))
        graph = StateGraph(_WorkflowState)
        dependencies: dict[str, list[str]] = {}

        for step in steps:
            step_id = step["id"]
            dependencies[step_id] = step["depends_on"]

            def run_step(state: _WorkflowState, *, spec: dict[str, Any] = step
                         ) -> dict[str, Any]:
                resolved = _resolve_values(spec["arguments"], state["arguments"],
                                           state.get("results", {}))
                result = self.invoke(spec["operation"], resolved, context=context,
                                     persist=False)
                return {"results": {spec["id"]: result.output},
                        "evidence": result.evidence}

            graph.add_node(step_id, run_step)

        step_ids = set(dependencies)
        for step_id, needs in dependencies.items():
            if needs:
                graph.add_edge(needs, step_id)
            else:
                graph.add_edge(START, step_id)
        for terminal in sorted(step_ids - {d for values in dependencies.values()
                                           for d in values}):
            graph.add_edge(terminal, END)

        final = graph.compile().invoke({"arguments": arguments,
                                        "results": {}, "evidence": []})
        results = final["results"]
        output_mapping = raw.get("output")
        if output_mapping is not None:
            output = _resolve_values(output_mapping, arguments, results)
        else:
            terminal_ids = [s["id"] for s in steps
                            if s["id"] not in {d for values in dependencies.values()
                                              for d in values}]
            output = results[terminal_ids[-1]]
        if not isinstance(output, dict):
            raise ValidationFailed("Workflow output must resolve to an object.")
        return output, list(final.get("evidence", [])), results


def _package(manifest: Manifest) -> Path:
    if manifest.package_path is None:
        raise ValidationFailed(f"Pack {manifest.pack_key} has no package path.")
    return manifest.package_path


def _schema_path(manifest: Manifest, relative: str) -> Path:
    return _package(manifest) / relative


def _validate_schema(path: Path, value: Any, label: str) -> None:
    schema = json.loads(path.read_text(encoding="utf-8"))
    package = next((parent for parent in path.parents
                    if (parent / "capability.yaml").is_file()), path.parent)
    registry = Registry()
    for candidate in package.rglob("*.json"):
        contents = json.loads(candidate.read_text(encoding="utf-8"))
        resource = Resource.from_contents(
            contents, default_specification=DRAFT202012)
        registry = registry.with_resource(candidate.as_uri(), resource)
    effective_schema = dict(schema)
    effective_schema.setdefault("$id", path.as_uri())
    try:
        Draft202012Validator(effective_schema, registry=registry).validate(value)
    except Exception as exc:
        raise ValidationFailed(f"{label} failed schema validation.",
                               details={"reason": str(exc)}) from exc


def _validate_inline_schema(schema: dict[str, Any], value: Any, label: str) -> None:
    try:
        Draft202012Validator(schema).validate(value)
    except Exception as exc:
        raise ValidationFailed(f"{label} failed schema validation.",
                               details={"reason": str(exc)}) from exc


def _json_type(spec: dict[str, Any]) -> Any:
    kind = spec.get("type")
    if kind == "string":
        return str
    if kind == "integer":
        return int
    if kind == "number":
        return float
    if kind == "boolean":
        return bool
    if kind == "array":
        return list[Any]
    if kind == "object":
        return dict[str, Any]
    return Any


def _model_from_schema(name: str, schema: dict[str, Any]) -> type[Any]:
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for field_name, spec in (schema.get("properties") or {}).items():
        annotation = _json_type(spec)
        fields[field_name] = (annotation, ... if field_name in required else None)
    config = ConfigDict(extra=("forbid" if schema.get("additionalProperties") is False
                               else "allow"))
    return create_model(f"{_safe_name(name)}Arguments", __config__=config, **fields)


def _safe_name(operation_name: str) -> str:
    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", operation_name)[:48]
    return f"{stem}_{content_hash(operation_name)[:10]}"


def _last_json_message(messages: list[Any]) -> dict[str, Any]:
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        content = message.content
        if isinstance(content, list):
            content = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content)
        try:
            parsed = json.loads(str(content))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValidationFailed("Agent did not produce a JSON object result.")


def _evidence_from(output: dict[str, Any]) -> list[Any]:
    evidence = output.get("evidence")
    if isinstance(evidence, list):
        return list(evidence)
    sources = output.get("sources")
    return list(sources) if isinstance(sources, list) else []


def _validate_workflow(raw: Any, *, allowed: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValidationFailed("Workflow schema_version must be 1.")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValidationFailed("Workflow requires a non-empty steps list.")
    steps: list[dict[str, Any]] = []
    ids: set[str] = set()
    for item in raw_steps:
        if not isinstance(item, dict):
            raise ValidationFailed("Every workflow step must be an object.")
        step_id = str(item.get("id") or "")
        operation = str(item.get("operation") or "")
        if not step_id or step_id in ids:
            raise ValidationFailed(f"Invalid or duplicate workflow step {step_id!r}.")
        if operation not in allowed:
            raise ValidationFailed(
                f"Workflow step {step_id!r} invokes undeclared tool {operation!r}.")
        ids.add(step_id)
        steps.append({"id": step_id, "operation": operation,
                      "arguments": dict(item.get("arguments") or {}),
                      "depends_on": [str(v) for v in item.get("depends_on") or []]})
    graph = {step["id"]: step["depends_on"] for step in steps}
    for step_id, needs in graph.items():
        unknown = set(needs) - ids
        if unknown:
            raise ValidationFailed(
                f"Workflow step {step_id!r} has unknown dependencies {sorted(unknown)}.")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValidationFailed("Workflow dependency graph contains a cycle.")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for step_id in graph:
        visit(step_id)
    return steps


def _resolve_values(value: Any, arguments: dict[str, Any],
                    results: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, list):
        return [_resolve_values(item, arguments, results) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$input"}:
        return _select(arguments, str(value["$input"]))
    if set(value) == {"$step", "path"}:
        step_id = str(value["$step"])
        if step_id not in results:
            raise ValidationFailed(f"Workflow references unfinished step {step_id!r}.")
        return _select(results[step_id], str(value["path"]))
    return {key: _resolve_values(item, arguments, results)
            for key, item in value.items()}


def _select(value: Any, path: str) -> Any:
    current = value
    for part in filter(None, path.split(".")):
        if not isinstance(current, dict) or part not in current:
            raise InvalidInput(f"Workflow field path {path!r} does not exist.")
        current = current[part]
    return current
