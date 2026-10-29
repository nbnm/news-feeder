"""One scheduled run, sharing the installed app's server when it is open."""

import asyncio
import fcntl
from contextlib import contextmanager, suppress
from pathlib import Path

import httpx

from .config import Settings
from .database import database
from .models import Preference, WorkerState
from .service import Service


@contextmanager
def service_lock(directory=None):
    path = Path(directory or Path.cwd()) / ".collector.lock"
    with path.open("a") as lock:
        path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


async def request_running_service(base, instance_id, runner_key):
    headers = {"X-NewsFeeder-Runner-Token": runner_key}
    async with httpx.AsyncClient(timeout=660) as client:
        try:
            response = await client.get(base + "/status", headers=headers, timeout=2)
        except httpx.ConnectError:
            return None
        if response.status_code != 200 or response.json().get("instance_id") != instance_id:
            raise RuntimeError("Another service is using the local port; scheduled delivery was not sent")
        response = await client.post(base + "/telegram/daily", headers=headers, json={"scheduled": True})
        if response.status_code != 200:
            raise RuntimeError("The local service rejected scheduled delivery")
        return response.json()


async def run_scheduled(port=8000):
    settings = Settings()
    engine, sessions = database(settings.database_url)
    try:
        with sessions() as session:
            if session.get(Preference, "owner") is None:
                return {"state": "skipped", "reason": "unconfigured"}
            service = Service(settings, sessions)
            delivery, reason = service._delivery_due(session)
            if reason:
                return service.delivery_result(delivery, reason)
            instance = session.get(WorkerState, "service:instance_id")
            runner = session.get(WorkerState, "service:runner_key")
            if not instance or not runner:
                raise RuntimeError("Open the installed app once to finish setting up scheduled delivery")
            instance_id, runner_key = instance.value, runner.value
    finally:
        engine.dispose()
    base = f"http://127.0.0.1:{port}"
    result = await request_running_service(base, instance_id, runner_key)
    if result is not None:
        return result
    try:
        with service_lock():
            import uvicorn

            from .main import create_app

            # Serve the local API during preparation: the native app can open
            # normally while a background digest is collecting and publishing.
            app = create_app(settings.model_copy(update={"worker_enabled": False}))
            server = uvicorn.Server(
                uvicorn.Config(
                    app, host="127.0.0.1", port=port, loop="asyncio", http="h11", ws="none", access_log=False
                )
            )
            serving = asyncio.create_task(server.serve())
            publishing = None
            try:
                for _ in range(100):
                    if serving.done():
                        await serving
                        raise RuntimeError("The scheduled local service could not start")
                    if server.started:
                        publishing = asyncio.create_task(app.state.service.publish_daily())
                        done, _ = await asyncio.wait(
                            [serving, publishing], return_when=asyncio.FIRST_COMPLETED
                        )
                        if publishing in done:
                            return await publishing
                        await serving
                        return {"state": "skipped", "reason": "stopped"}
                    await asyncio.sleep(0.1)
                raise RuntimeError("The scheduled local service did not become ready")
            finally:
                if publishing and not publishing.done():
                    publishing.cancel()
                    with suppress(asyncio.CancelledError):
                        await publishing
                server.should_exit = True
                await serving
    except BlockingIOError:
        # The app won a simultaneous launch. Adopt its server after startup.
        for _ in range(40):
            await asyncio.sleep(0.25)
            result = await request_running_service(base, instance_id, runner_key)
            if result is not None:
                return result
        raise RuntimeError("The local service is busy starting; delivery will be checked again") from None
