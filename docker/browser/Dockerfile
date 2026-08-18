# Rendering service. Replaces browserless, which could not do what we now need:
# Chrome extension support is a paid feature there, and the open-source image we
# were running does not include it. It was also SSPL-licensed; Playwright is
# Apache 2.0, so this swap settles a licensing question as well as a capability
# one.
#
# Base image ships Chromium plus the system libraries it needs, already matched
# to the Playwright version — that pairing is the thing most likely to break, so
# letting Microsoft pin it is worth the image size.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

# The base image ships the BROWSERS (under /ms-playwright) but not the Python
# client, so it has to be installed — and pinned to exactly the base image tag.
# A client newer than its browsers looks for a chromium-<build> directory that
# is not there and fails at launch, so these two versions move together or not
# at all.
RUN pip install --no-cache-dir --break-system-packages \
        "playwright==1.62.0" \
        "fastapi==0.121.2" \
        "uvicorn==0.41.0"

COPY app.py ./

# Chromium will not start as root with a sandbox, and the base image ships a
# pwuser for exactly this. The profile and extension mounts must be readable by
# it — see the compose file.
ENV PROFILE_DIR=/profile \
    EXTENSIONS_DIR=/extensions

EXPOSE 3010

# Shell form so PORT is honoured — compose sets it, and 3000 is already taken on
# this host.
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-3010}
