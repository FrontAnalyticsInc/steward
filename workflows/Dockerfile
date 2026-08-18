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

# Pinned by digest, not by tag: python:3.12-slim would resolve to a different
# image on a later rebuild, and the brief requires reproducible rebuilds.
# Digest below is python:3.12-slim as of 2026-08-05.
FROM python@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b

RUN pip install --no-cache-dir uv==0.8.13

WORKDIR /code

COPY ./pyproject.toml ./README.md ./uv.lock* ./

COPY ./app ./app

# scripts/ ships in the image because the deterministic Gmail fetch is meant to
# be runnable inside the container — for a credential check, a dry run of a
# query, or generating an eval fixture from a real inbox — without a checkout.
COPY ./scripts ./scripts

# tests/ ships in the image on purpose. The brief requires `agents-cli eval` to
# pass INSIDE the container, not just on a developer machine, and the eval
# dataset, config and deterministic grader all live under tests/.
COPY ./tests ./tests

# --frozen: install exactly what uv.lock pins. A rebuild that silently resolves
# different versions is the thing this whole file exists to prevent.
RUN uv sync --frozen

# agents-cli is the eval runner, pinned to the version this project was built
# and verified against.
RUN uv pip install --system google-agents-cli==1.3.1

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION} \
    PYTHONUNBUFFERED=1 \
    # ADK's Cloud Trace path calls google.auth.default() and hard-fails without
    # Application Default Credentials, which this project must not configure.
    OTEL_TO_CLOUD=false

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "app.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080"]
