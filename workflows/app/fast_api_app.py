# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import logging
import os
from collections.abc import AsyncIterator

import google.auth
from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from google.cloud import logging as google_cloud_logging

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.typing import Feedback

load_dotenv()


class _LocalLogger:
    """Stand-in for the Cloud Logging logger when running locally.

    The scaffold calls google.auth.default() and constructs a Cloud Logging
    client at import time, so importing this module fails outright without
    Application Default Credentials. This project is local-only and is required
    NOT to configure ADC, so both are made optional and the structured-log call
    degrades to stdlib logging. Set GOOGLE_CLOUD_PROJECT with real credentials
    present and the cloud path is used again automatically.
    """

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def log_struct(self, payload: dict, severity: str = "INFO") -> None:
        self._log.log(
            getattr(logging, severity.upper(), logging.INFO),
            "%s",
            payload,
        )


try:
    _, project_id = google.auth.default()
    logging_client = google_cloud_logging.Client()
    logger = logging_client.logger(__name__)
except Exception:  # no ADC / no GCP project — the expected local case
    project_id = None
    logger = _LocalLogger(__name__)
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    # Scaffold default is True, which makes ADK call google.auth.default() for
    # Cloud Trace/Logging and hard-fail without ADC. This project is local-only
    # and must not configure ADC, so default to off and let a real deployment
    # opt in with OTEL_TO_CLOUD=true.
    otel_to_cloud=os.getenv("OTEL_TO_CLOUD", "false").strip().lower() == "true",
    lifespan=lifespan,
)
app.title = "workflows"
app.description = "API for interacting with the Agent workflows"


@app.get("/integration-config")
def integration_config() -> dict:
    """What this service may reach, where its output goes, and as whom.

    Read-only, and secret values never leave this process — see
    app/integration_config.py. The dashboard's settings page renders this;
    it cannot derive it, because the credentials are in this container's
    environment and not in any file that container can see.

    Imported inside the handler so a mistake in the inventory degrades to a
    500 on one settings section rather than taking down the agent server at
    import time.
    """
    from app import integration_config as _cfg

    return _cfg.snapshot()


@app.get("/agent-load-errors")
def agent_load_errors() -> dict:
    """Custom agents that failed to load, and why. Empty is the good case.

    The counterpart to app/registry.py skipping a broken overlay agent instead
    of refusing to start. Skipping is only honest if the error is reachable
    without a shell on the box, and the operator who wrote the file is exactly
    the person least likely to have one — so it is served here, next to the
    workflows that did load.

    Imported inside the handler for the same reason /integration-config is:
    this endpoint must not be able to break startup.
    """
    from app import registry as _registry

    errors = _registry.load_errors()
    return {"count": len(errors), "errors": errors}


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    logger.log_struct(feedback.model_dump(), severity="INFO")
    return {"status": "success"}


@app.get("/cost")
def cost_position() -> dict:
    """Today's model spend, the cap, and which models the aliases point at.

    Served from here rather than read off disk by the caller because this is the
    process that owns both files: the usage ledger lives under this container's
    ADK_STATE_DIR and the alias map under its read-only /code/config mount. The
    invoker runs in the gateway container, where neither path is guaranteed to
    be the same one — asking the owner is the only answer that cannot drift.

    Read-only and never raises on the cap: the endpoint reports the position,
    and the *caller* decides whether to refuse. Returning 200 with `over: true`
    keeps "we are over budget" distinguishable from "the service is broken",
    which a 429 here would blur.
    """
    from app import cost_ledger, model_aliases

    spent = cost_ledger.spend_today()
    cap = cost_ledger.DAILY_CAP_USD
    return {
        "spent_today_usd": round(spent, 6),
        "cap_usd": cap,
        "enabled": cap > 0,
        "over": bool(cap > 0 and spent >= cap),
        "remaining_usd": round(cap - spent, 6) if cap > 0 else None,
        "aliases": model_aliases.load(),
    }


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
