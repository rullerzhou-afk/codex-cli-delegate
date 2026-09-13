#!/usr/bin/env python3
"""Local stdio MCP server. No listener, credentials, or model-driven polling."""
import argparse
import asyncio
import json
from functools import wraps

from mcp.server import MCPServer
from mcp.types import ToolAnnotations, CallToolResult, TextContent

import claude_task as ct
from delegate_service import DelegateService


def build_server(service):
    server = MCPServer("claude-delegate", version="1.0.0",
                       instructions="Delegate authorized work; preserve job_id and request_id. Await and independently review before accept. Owner is the calling Codex task ID.")

    def expose(read_only=False):
        def decorate(func):
            @wraps(func)
            async def wrapped(*args, **kwargs):
                try:
                    result = await func(*args, **kwargs)
                    data = result if isinstance(result, dict) else {"jobs": result}
                    return CallToolResult(structured_content=data,
                                          content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))])
                except ct.CliError as exc:
                    error = dict(ok=False, error=exc.code, message=exc.message, **exc.extra)
                    return CallToolResult(is_error=True, structured_content=error,
                                          content=[TextContent(type="text", text=json.dumps(error, ensure_ascii=False))])
            return server.tool(annotations=ToolAnnotations(read_only_hint=read_only, open_world_hint=not read_only))(wrapped)
        return decorate

    @expose()
    async def delegate_start(owner: str, request_id: str, cwd: str, task: str, backend: str = "claude",
                             allow_tools: list[str] | None = None, read_dirs: list[str] | None = None,
                             required_files: list[str] | None = None, timeout: int | None = None,
                             kimi_tools: list[str] | None = None, opencode_tools: list[str] | None = None) -> dict:
        """Start authorized work. Include scope and acceptance in task. Reuse request_id when retrying an uncertain call. Claude uses Agent SDK; Kimi/OpenCode retain native adapters. timeout=null means no wall-clock kill (default); only set seconds for an explicit limit."""
        return await asyncio.to_thread(service.start, owner, request_id, cwd, task, backend, allow_tools,
                                       read_dirs, required_files, timeout, None, kimi_tools, opencode_tools)

    @expose()
    async def delegate_revise(owner: str, request_id: str, job_id: str, expected_round: int,
                              task: str, recover: bool = False, timeout: int | str | None = "inherit",
                              allow_tools: list[str] | None = None, read_dirs: list[str] | None = None,
                              required_files: list[str] | None = None) -> dict:
        """Continue the same native session, including accepted jobs. Authorized command/reference additions are applied automatically; an idle connection may refresh while retaining the session. recover acknowledges inspected failure/interruption; it never authorizes duplicate active work. timeout defaults to inherit; null removes an old limit, integer sets seconds."""
        return await asyncio.to_thread(service.revise, owner, request_id, job_id, expected_round, task, recover, timeout,
                                       allow_tools, read_dirs, required_files)

    @expose(True)
    async def delegate_status(owner: str, job_id: str, details: bool = False) -> dict:
        """Read compact status; request details only to diagnose. No model call."""
        return await asyncio.to_thread(service.read, owner, job_id, details)

    @expose(True)
    async def delegate_list(owner: str) -> dict:
        """Find the calling task's existing jobs after interruption; do not redispatch blindly."""
        return {"jobs": await asyncio.to_thread(service.list, owner)}

    @expose(True)
    async def delegate_wait(owner: str, job_id: str, cursor: str = "-1:0", timeout: int = 600) -> dict:
        """Quietly await completion, an incident or a checkpoint. Carry returned cursor forward; waiting makes no model calls and cancellation does not stop the job."""
        return await service.wait(owner, job_id, cursor, timeout)

    @expose()
    async def delegate_notify(owner: str, job_id: str, expected_round: int, enabled: bool = True) -> dict:
        """Arm macOS notifications for this round, or disable them. Confirm watching before ending the Codex turn; no automatic Codex wakeup. Re-arm after each revision."""
        from delegate_notify import arm
        return await asyncio.to_thread(arm, service.context(owner), job_id, enabled, expected_round)

    @expose()
    async def delegate_stop(owner: str, job_id: str) -> dict:
        """Stop only verified processes for this job, retaining files and native session for recovery."""
        return await asyncio.to_thread(service.stop, owner, job_id)

    @expose()
    async def delegate_accept(owner: str, job_id: str, expected_round: int, notes: str,
                              evidence_dir: str | None = None) -> dict:
        """Record independent review, close the idle SDK connection and release the checkout. No push, merge or publication."""
        return await asyncio.to_thread(service.accept, owner, job_id, expected_round, notes, evidence_dir)

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--claude-bin", default=None)
    args = parser.parse_args()
    build_server(DelegateService(args.state_dir, args.claude_bin)).run(transport="stdio")
