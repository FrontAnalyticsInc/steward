# Documentation site. Two stages so the runtime image carries nginx and ~3 MB of
# static HTML, not a Python toolchain.
#
# The build context is the repository ROOT, not this directory, because the docs
# source lives at docs/ alongside the code it describes. A .dockerignore at the
# root whitelists only docs/ and mkdocs.yml, so the context stays tiny.

FROM python:3.12-slim AS builder

WORKDIR /build

# Pinned deliberately. mkdocs is held below 2.0: that release removes the plugin
# system and rewrites theming with no migration path, so an unpinned rebuild
# would break this site rather than update it.
RUN pip install --no-cache-dir \
        "mkdocs==1.6.1" \
        "mkdocs-material==9.7.7" \
        "pymdown-extensions==11.0.1"

COPY mkdocs.yml ./
COPY docs/ ./docs/
# docs/legal/license.md includes this via a pymdownx snippet, and check_paths
# makes its absence a build failure rather than an empty page.
COPY LICENSE ./

# --strict turns a broken internal link into a failed build. A docs image that
# builds is therefore a docs image whose links resolve.
RUN mkdocs build --strict --site-dir /site


FROM nginx:alpine

COPY --from=builder /site /usr/share/nginx/html

# No custom nginx config: MkDocs emits directory-style URLs (/quickstart/ with an
# index.html inside), which nginx serves correctly by default.
EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost/ >/dev/null || exit 1
