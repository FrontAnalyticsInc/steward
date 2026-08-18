# ruff: noqa
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

"""Root agent — the entry point that exposes this project's workflows.

One agents-cli project, many agent modules under app/agents/. Add a workflow by
creating app/agents/<name>/ (schema.py, prompt.py, agent.py). It is registered
by existing — there is no list to edit.

The scheduler invokes a specific agent by name over HTTP rather than routing
through this root, so the root exists to make every agent reachable and
discoverable in one server.

This file deliberately names no agent. It used to hold the list, which made it
the one file every new workflow had to touch and therefore a standing merge
conflict — two agents added on two branches collide there every time. That does
not survive more than one author, let alone more than one deployment, so the
list became app/registry.py. A tenant deployment adds workflows by pointing
HERMES_AGENTS_PATH at its own directory, and turns shipped ones off with
HERMES_DISABLED_AGENTS, without editing anything here.
"""

from google.adk.agents import Agent
from google.adk.apps import App

from app.config import build_model, describe_model
from app.registry import load_agents

# Kept as a module-level name: the docs, the authoring skill and the dashboard's
# source parser all refer to WORKFLOW_AGENTS by name.
WORKFLOW_AGENTS = load_agents()

root_agent = Agent(
    name="root_agent",
    model=build_model(),
    instruction=(
        "You route to the workflow agents available in this project. "
        "Prefer delegating to a named workflow agent over answering directly. "
        f"Configured model: {describe_model()}."
    ),
    sub_agents=WORKFLOW_AGENTS,
)

app = App(
    root_agent=root_agent,
    name="app",
)
