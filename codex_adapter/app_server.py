"""
Minimal async JSON-RPC client for Codex App Server (stdio).

This is GX-side integration/reference code, NOT Codex upstream source.
Verify every method and parameter against the pinned Codex checkout and
generated app-server schemas before production use.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator


class CodexAppServerError(RuntimeError):
    pass


@dataclass
class RpcError:
    code: int
    message: str
    data: Any = None


class CodexAppServer:
    def __init__(
        self,
        command: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        request_timeout: float = 60.0,
    ) -> None:
        self.command = command or ["codex", "app-server", "--listen", "stdio://"]
        self.cwd = cwd
        self.env = {**os.environ, **(env or {})}
        self.request_timeout = request_timeout
        self.proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._closed = False

    async def start(self, client_name: str = "gx_science_harness",
                    client_version: str = "0.1.0") -> None:
        if self.proc is not None:
            return
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self.request("initialize", {
            "clientInfo": {
                "name": client_name,
                "title": "GX Science Harness",
                "version": client_version,
            }
        })
        await self.notify("initialized", {})

    async def _write(self, message: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise CodexAppServerError("App Server is not running")
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        async with self._write_lock:
            self.proc.stdin.write((payload + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any] | None = None,
                      timeout: float | None = None) -> dict[str, Any]:
        if self._closed:
            raise CodexAppServerError("App Server client is closed")
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._write(message)
            result = await asyncio.wait_for(
                future, timeout=timeout or self.request_timeout
            )
            return result
        except asyncio.TimeoutError as exc:
            # Outcome is unknown: caller must reconcile before retrying side effects.
            raise CodexAppServerError(
                f"Timeout waiting for {method}; outcome may be unknown"
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while line := await self.proc.stdout.readline():
                try:
                    message = json.loads(line.decode("utf-8"))
                except Exception:
                    await self._notifications.put({
                        "method": "_protocol/parse_error",
                        "params": {"raw": line.decode("utf-8", errors="replace")}
                    })
                    continue

                if "id" in message and (
                    "result" in message or "error" in message
                ):
                    future = self._pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            error = message["error"]
                            future.set_exception(CodexAppServerError(
                                f"RPC {error.get('code')}: {error.get('message')}"
                            ))
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    await self._notifications.put(message)
        except asyncio.CancelledError:
            raise
        finally:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(
                        CodexAppServerError("App Server stdout closed")
                    )

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while line := await self.proc.stderr.readline():
            # Replace with structured logging in GX. Never log secrets.
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                print(f"[codex-app-server] {text}")

    async def notifications(self) -> AsyncIterator[dict[str, Any]]:
        while not self._closed:
            yield await self._notifications.get()

    async def start_thread(self, *, model: str, cwd: str,
                           approval_policy: str = "on-request",
                           sandbox_policy: dict[str, Any] | None = None,
                           developer_instructions: str | None = None,
                           extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model,
            "cwd": cwd,
            "approvalPolicy": approval_policy,
        }
        if sandbox_policy is not None:
            params["sandboxPolicy"] = sandbox_policy
        if developer_instructions is not None:
            params["developerInstructions"] = developer_instructions
        if extra:
            params.update(extra)
        return await self.request("thread/start", params)

    async def resume_thread(self, thread_id: str,
                            exclude_turns: bool = True,
                            extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "excludeTurns": exclude_turns,
        }
        if extra:
            params.update(extra)
        return await self.request("thread/resume", params)

    async def start_turn(self, thread_id: str, text: str,
                         extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if extra:
            params.update(extra)
        return await self.request("turn/start", params)

    async def steer_turn(self, thread_id: str, expected_turn_id: str,
                         text: str) -> dict[str, Any]:
        return await self.request("turn/steer", {
            "threadId": thread_id,
            "expectedTurnId": expected_turn_id,
            "input": [{"type": "text", "text": text}],
        })

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return await self.request("turn/interrupt", {
            "threadId": thread_id,
            "turnId": turn_id,
        })

    async def set_goal(self, thread_id: str, objective: str,
                       token_budget: int, status: str = "active") -> dict[str, Any]:
        # Important: setting a new objective may replace the goal and reset usage.
        # Do not call for every small Intent change. Verify status enum in pinned source.
        return await self.request("thread/goal/set", {
            "threadId": thread_id,
            "objective": objective,
            "status": status,
            "tokenBudget": token_budget,
        })

    async def get_goal(self, thread_id: str) -> dict[str, Any]:
        return await self.request("thread/goal/get", {"threadId": thread_id})

    async def clear_goal(self, thread_id: str) -> dict[str, Any]:
        return await self.request("thread/goal/clear", {"threadId": thread_id})

    async def close(self, terminate: bool = True) -> None:
        self._closed = True
        if self.proc:
            if terminate and self.proc.returncode is None:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.proc.kill()
                    await self.proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
        self.proc = None
