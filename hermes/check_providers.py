"""Assert every model route in a Hermes config template goes through the gateway.

The templates carry roughly twenty provider/model quads across auxiliary roles,
MoA presets, delegation and the top-level model block. Repointing them by grep
is unreliable — `background_review: codex` and the MoA reference models both sit
outside the repeating `custom:local-ollama` shape and were missed by exactly that
approach while this change was being made. So walk the parsed YAML instead and
fail on anything that still names a provider or endpoint we no longer have
credentials for.

Usage:
    python hermes/check_providers.py hermes/config.yaml.template ...
"""

from __future__ import annotations

import sys

import yaml

# `auto` defers to the top-level model block, which is itself checked; empty
# string means "inherit", which is the documented way to leave a role unset.
ALLOWED_PROVIDERS = {"anthropic", "auto", ""}

# `provider` is not exclusively an LLM key: the speech sections use the same
# field name for an entirely separate registry (`tts.provider: edge`,
# `stt.provider: local`). They route audio, never a model call, so the provider
# rule does not apply to them and flagging them would train the reader to
# ignore this check's output.
SKIP_SUBTREES = {"tts", "stt"}

# Endpoints and model ids that no longer resolve in this deployment. A match
# here is a route that would fail at first use rather than at startup.
STALE_TOKENS = (
    "11434",
    "local-ollama",
    "gemma4",
    "openai-codex",
    "openrouter",
    "gpt-5",
    "deepseek",
    # Alias names from the proxy era. A model field holding one of these would
    # now be sent to Anthropic verbatim as a model id and 404 at first use.
    "custom:gateway",
)


def walk(node, path=""):
    """Yield (kind, dotted_path, value) for every stale reference found."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if not path and key in SKIP_SUBTREES:
                continue
            if key == "provider" and isinstance(value, str):
                if value not in ALLOWED_PROVIDERS:
                    yield ("provider", here, value)
            elif key in ("base_url", "model", "api_key") and isinstance(value, str):
                lowered = value.lower()
                if any(token in lowered for token in STALE_TOKENS):
                    yield (key, here, value)
            else:
                yield from walk(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


def main(paths: list[str]) -> int:
    exit_code = 0
    for path in paths:
        with open(path) as handle:
            document = yaml.safe_load(handle)
        findings = list(walk(document))
        print(f"\n{path}")
        if findings:
            exit_code = 1
            for kind, location, value in findings:
                print(f"  STALE {kind:9} {location} = {value!r}")
        else:
            print("  clean — every route names anthropic/auto, no stale endpoints")
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
