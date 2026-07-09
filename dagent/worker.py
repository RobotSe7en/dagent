"""Worker process entrypoint for executing RuntimeRunSpec payloads."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from typing import Protocol, TextIO

from pydantic import ValidationError

from dagent import __version__
from dagent.agent import AutoAgent, DagAgent, ToolAgent
from dagent.config import UserPythonToolConfig
from dagent.providers.openai_compatible import OpenAICompatibleProvider
from dagent.result import RunStreamEvent
from dagent.runner import Runner
from dagent.schemas import RuntimeAgentSpec, RuntimeFrame, RuntimeRunSpec, RuntimeRunTarget

UNIX_SOCKET_CONNECT_ATTEMPTS = 6
UNIX_SOCKET_CONNECT_RETRY_DELAY_SECONDS = 0.05


class RuntimeFrameTransport(Protocol):
    def read_frame(self) -> RuntimeFrame:
        """Read one JSONL runtime frame."""

    def write_frame(self, frame: RuntimeFrame) -> None:
        """Write one JSONL runtime frame."""

    def write_event(self, event: RunStreamEvent) -> None:
        """Write one typed event frame without revalidating the already-typed event."""

    def close(self) -> None:
        """Close transport resources."""


class StdioJsonlTransport:
    """JSONL transport over caller-provided text streams."""

    def __init__(
        self,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ) -> None:
        self._input = input_stream or sys.stdin
        self._output = output_stream or sys.stdout

    def read_frame(self) -> RuntimeFrame:
        line = self._input.readline()
        if not line:
            raise EOFError("worker control channel closed before a frame was received.")
        return RuntimeFrame.model_validate_json(line)

    def write_frame(self, frame: RuntimeFrame) -> None:
        self._output.write(frame.model_dump_json() + "\n")
        self._output.flush()

    def write_event(self, event: RunStreamEvent) -> None:
        self._output.write(_event_frame_json(event) + "\n")
        self._output.flush()

    def close(self) -> None:
        return None


class UnixSocketJsonlTransport:
    """JSONL transport over a connected Unix domain socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._socket = sock
        self._reader = sock.makefile("r", encoding="utf-8")
        self._writer = sock.makefile("w", encoding="utf-8")

    @classmethod
    def connect(cls, path: str | Path) -> "UnixSocketJsonlTransport":
        socket_path = str(path)
        for attempt in range(UNIX_SOCKET_CONNECT_ATTEMPTS):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(socket_path)
                return cls(sock)
            except (ConnectionRefusedError, FileNotFoundError):
                sock.close()
                if attempt + 1 == UNIX_SOCKET_CONNECT_ATTEMPTS:
                    raise
                time.sleep(UNIX_SOCKET_CONNECT_RETRY_DELAY_SECONDS)
            except Exception:
                sock.close()
                raise
        raise RuntimeError("unreachable unix socket connect retry state.")

    @classmethod
    def from_socket(cls, sock: socket.socket) -> "UnixSocketJsonlTransport":
        return cls(sock)

    def read_frame(self) -> RuntimeFrame:
        line = self._reader.readline()
        if not line:
            raise EOFError("worker control socket closed before a frame was received.")
        return RuntimeFrame.model_validate_json(line)

    def write_frame(self, frame: RuntimeFrame) -> None:
        self._writer.write(frame.model_dump_json() + "\n")
        self._writer.flush()

    def write_event(self, event: RunStreamEvent) -> None:
        self._writer.write(_event_frame_json(event) + "\n")
        self._writer.flush()

    def close(self) -> None:
        self._reader.close()
        self._writer.close()
        self._socket.close()


def _transport_from_args(args: argparse.Namespace) -> RuntimeFrameTransport:
    if args.transport == "stdio":
        return StdioJsonlTransport()
    if args.transport == "unix-socket":
        if not args.socket_path:
            raise ValueError("--socket-path is required for unix-socket transport.")
        return UnixSocketJsonlTransport.connect(args.socket_path)
    raise ValueError(f"Unsupported worker transport: {args.transport}")


def _read_spec(transport: RuntimeFrameTransport) -> RuntimeRunSpec:
    try:
        frame = transport.read_frame()
    except EOFError as exc:
        raise ValueError("first frame must be spec.") from exc
    if frame.type != "spec":
        raise ValueError("first frame must be spec.")
    return frame.spec_payload()


def _event_frame_json(event: RunStreamEvent) -> str:
    return json.dumps(
        {"type": "event", "payload": event.model_dump(mode="json")},
        separators=(",", ":"),
    )


def _runner_from_spec(spec: RuntimeRunSpec) -> Runner:
    validator = spec.validation.validator if spec.validation.enabled else None
    runner = Runner(
        workspace=spec.workspace.workspace_root or ".dagent",
        provider=OpenAICompatibleProvider(spec.provider),
        validator=validator,
        skill_roots=[Path(root) for root in spec.skill_roots],
        profile_root=None if spec.profile_root is None else Path(spec.profile_root),
        sandbox=spec.sandbox,
    )
    try:
        runner.enable_validation = spec.validation.enabled
        if spec.validation.max_retries is not None:
            runner.runtime.max_validation_retries = spec.validation.max_retries
        snapshots = {snapshot.name: snapshot for snapshot in spec.mcp_snapshots}
        for name, config in spec.mcp_servers.items():
            runner.add_mcp_server(
                name,
                config,
                snapshot=snapshots.get(name),
                lazy_connect=spec.lazy_mcp,
            )
        if spec.python_tools:
            if spec.python_tool_user_config_dir is None:
                raise ValueError("python_tools require python_tool_user_config_dir.")
            result = runner.reload_python_tool_sources(
                [UserPythonToolConfig.model_validate(item) for item in spec.python_tools],
                user_config_dir=Path(spec.python_tool_user_config_dir),
                managed_root=None if spec.python_tool_managed_root is None else Path(spec.python_tool_managed_root),
                replace_ids=set(),
            )
            if result.errors:
                raise ValueError(f"Python tool registration failed: {result.errors}")
        for agent in spec.registered_agents:
            runner.add_agent(_tool_agent_from_runtime_agent(agent))
    except Exception:
        runner.close()
        raise
    return runner


def _tool_agent_from_runtime_agent(agent: RuntimeAgentSpec) -> ToolAgent:
    return ToolAgent(
        profile=agent.profile,
        name=agent.name,
        max_steps=agent.max_steps,
        capabilities=agent.capabilities,
        skills=agent.skills,
        agents=agent.agents,
        review=agent.review,
        description=agent.description,
    )


def _target_from_spec(target: RuntimeRunTarget):
    if target.type == "auto_agent":
        return AutoAgent(
            profile=target.profile,
            planner_profile=target.planner_profile,
            name=target.name,
            max_steps=target.max_steps,
            max_cycles=target.max_cycles,
            capabilities=target.capabilities,
            skills=target.skills,
            agents=target.agents,
            review=target.review,
            dynamic_adjust=target.dynamic_adjust,
        )
    if target.type == "tool_agent":
        return ToolAgent(
            profile=target.profile,
            name=target.name,
            max_steps=target.max_steps,
            capabilities=target.capabilities,
            skills=target.skills,
            agents=target.agents,
            review=target.review,
        )
    if target.type == "dag_agent":
        return DagAgent(
            planner_profile=target.planner_profile,
            name=target.name,
            max_cycles=target.max_cycles,
            capabilities=target.capabilities,
            skills=target.skills,
            agents=target.agents,
            review=target.review,
            dynamic_adjust=target.dynamic_adjust,
        )
    if target.type == "dag_spec":
        if target.dag_spec is None:
            raise ValueError("dag_spec target requires dag_spec.")
        return target.dag_spec
    raise ValueError(f"Unsupported runtime target type: {target.type}")


async def _run_spec(spec: RuntimeRunSpec, transport: RuntimeFrameTransport) -> int:
    runner: Runner | None = None
    final_run_status = None
    failure: Exception | None = None
    try:
        runner = _runner_from_spec(spec)
        transport.write_frame(RuntimeFrame(type="hello", payload={"sdk_version": __version__}))
        if spec.action == "resume":
            if spec.review_decision is None:
                raise ValueError("resume specs require review_decision.")
            events = runner.resume_stream(
                spec.review_decision.to_review_decision(),
                state=spec.state,
            )
        else:
            if spec.target is None:
                raise ValueError("run specs require target.")
            events = runner.stream(
                _target_from_spec(spec.target),
                messages=spec.target.messages,
                graph_input=spec.target.graph_input,
                state=spec.state,
                workspace_root=spec.workspace.run_workspace_root or "runs",
                workspace_path=spec.workspace.workspace_path,
                run_id=spec.run_id,
            )
        async for event in events:
            transport.write_event(event)
            if event.type == "run.finished":
                result = event.data.result
                final_run_status = result.state.status
                transport.write_frame(RuntimeFrame(
                    type="state_snapshot",
                    payload=result.state.model_dump(mode="json"),
                ))
            elif event.type == "run.failed":
                final_run_status = "failed"
    except Exception as exc:
        failure = exc
    if runner is not None:
        try:
            runner.close()
        except Exception as exc:
            if failure is None:
                failure = exc
    if failure is None:
        transport.write_frame(RuntimeFrame(
            type="bye",
            payload={"process_status": "completed", "run_status": final_run_status, "exit_code": 0},
        ))
        return 0
    transport.write_frame(RuntimeFrame(
        type="bye",
        payload={
            "process_status": "failed",
            "run_status": final_run_status,
            "exit_code": 1,
            "error_type": type(failure).__name__,
            "error": str(failure),
        },
    ))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a dagent worker over a JSONL control channel.")
    parser.add_argument("--transport", choices=["stdio", "unix-socket"], default="stdio")
    parser.add_argument("--socket-path")
    args = parser.parse_args(argv)
    transport: RuntimeFrameTransport | None = None
    try:
        transport = _transport_from_args(args)
        spec = _read_spec(transport)
    except (EOFError, ValidationError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run_spec(spec, transport))
    except (EOFError, ValidationError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        if transport is not None:
            transport.write_frame(RuntimeFrame(
                type="bye",
                payload={
                    "process_status": "failed",
                    "run_status": None,
                    "exit_code": 1,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            ))
        return 1
    finally:
        if transport is not None:
            transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
