from __future__ import annotations

from .stages import IntentionalFailureDemoAgent

AGENT_NAME = "intentional_failure_demo"

intentional_failure_demo_agent = IntentionalFailureDemoAgent(
    name=AGENT_NAME,
    description=(
        "Deterministically emits a failed ADK workflow result for kanban "
        "screenshot/testing. Demo infrastructure only."
    ),
)

root_agent = intentional_failure_demo_agent
