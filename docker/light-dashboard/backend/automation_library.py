"""The automation library: templates that cannot run until someone fills them in.

A fresh install ships four automations the client can actually use, and none of
them runs. That is not a disabled flag someone could flip by accident — a
template is not a job and the scheduler never reads this directory. It has no
job id, no `next_run_at`, no entry in `cron/jobs.json`, and required parameters
with no values. The only way one becomes a job is `render_job()`, which raises
unless every required parameter has been supplied. "Nothing acts without you"
is therefore a property of the data rather than a promise made by a boolean.

What a template is
------------------
A job with holes in it, plus the schema of those holes. One YAML file per
template under ``automations/library/`` — in this repo at
``hermes/automations/library/``, on a deployed box at
``<data dir>/automations/library/`` where seed.sh puts it copy-if-absent.
``hermes/automations/library/README.md`` is the author-facing description of
the format; this module is its enforcement.

The `job:` block is deliberately NOT a second job format. Its keys are the
argument names of Hermes's own ``cron.jobs.create_job`` — ``prompt``,
``schedule``, ``name``, ``deliver``, ``enabled_toolsets``, ``monitor_url``,
``monitor_script`` — and ``render_job`` returns exactly that kwargs dict. If
the scheduler's signature changes, this fails loudly rather than drifting into
a dialect of its own.

Note what ``render_job`` does NOT do: it does not create anything. Creation is
a human act performed elsewhere, and a caller that wants the job created but
switched off must follow ``create_job(**kwargs)`` with
``update_job(job["id"], {"enabled": False})`` — ``create_job`` has no
``enabled`` argument and always returns an enabled job.

Change detection is a required section
--------------------------------------
``docs/cookbook/competitor-watch.md`` carries the lesson this format exists to
encode: without "only tell me what's new" the reader gets the same summary
every week, stops opening it by week three, and is then worse off than with no
automation at all, because they believe they are covered. So every template
must declare a ``change_detection`` mechanism, and the two real ones are
enforced structurally:

* ``monitor`` — the scheduler fetches ``monitor_url`` (or runs
  ``monitor_script``) BEFORE any model call, hashes it, and suppresses the run
  entirely when the bytes are unchanged. The memory is ``monitor_state`` on the
  job plus the previous body on disk. Validation requires the rendered job to
  carry one of those two fields.
* ``prompt`` — the agent is told to report only what is new and to answer
  exactly ``[SILENT]`` when nothing is, which the scheduler honours by
  suppressing delivery. Validation requires the literal ``[SILENT]`` to appear
  in the prompt, because that string is the contract with the scheduler and a
  paraphrase of it does nothing.

``none`` is accepted and must be justified in the file, for the case where
every run is meant to produce output.

Standard library plus PyYAML, and no import from `main`: this runs from the CLI
(``python -m backend.automation_library --check <dir>``) on a box with no
dashboard as readily as it runs inside one.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

# Where the library sits, relative to a data directory or a repo's hermes/ dir.
LIBRARY_SUBDIR = os.path.join("automations", "library")

# The create_job arguments a template is allowed to fill. Anything else is
# either runtime state (`enabled`, `next_run_at`, `last_status`) or a facility
# this library deliberately does not hand to a template — `script`/`no_agent`
# would let a template ship code that runs unattended, and `workdir` points at
# the host filesystem.
ALLOWED_JOB_KEYS = frozenset(
    {"name", "prompt", "enabled_toolsets", "monitor_url", "monitor_script"}
)

# Fields that would make a template into a job. Their presence is the error the
# whole format exists to prevent, so they are named rather than merely unknown.
FORBIDDEN_JOB_KEYS = frozenset(
    {
        "id",
        "enabled",
        "state",
        "next_run_at",
        "last_run_at",
        "last_status",
        "repeat",
        "origin",
        "schedule",
        "deliver",
        "script",
        "no_agent",
        "workdir",
        "monitor_state",
    }
)

PARAM_TYPES = frozenset({"string", "url", "list", "integer", "boolean"})
MECHANISMS = frozenset({"monitor", "prompt", "none"})
# `adk-pipeline` is accepted so this format does not contradict the routing
# work in task 05, which owns the vocabulary. Nothing in the shipped library
# uses it: both ADK agents set `output_schema`, which disables tool calling, so
# an ADK pipeline currently cannot search or fetch.
TIERS = frozenset({"prompt-cron", "adk-pipeline"})

PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")
# Anything that opens a placeholder but is not one of the above — a typo, or a
# conditional/loop syntax someone assumed was supported. There is no template
# engine here: substitution of declared parameters, nothing else.
MALFORMED_PLACEHOLDER = re.compile(r"\{\{(?!\s*[a-z0-9_]+\s*\}\})")

REQUIRED_TOP_LEVEL = (
    "id",
    "version",
    "title",
    "summary",
    "tier",
    "parameters",
    "schedule",
    "delivery",
    "change_detection",
    "job",
)


class TemplateError(ValueError):
    """A template is invalid, or the values offered for it are.

    Carries every problem found, not the first: an operator filling a form
    should be told about all four empty fields at once.
    """

    def __init__(self, problems: Iterable[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------
#
# Mirrors the four forms cron.jobs.parse_schedule accepts, and deliberately
# nothing more. It is a pre-check so a bad default is caught in CI rather than
# by a ValueError inside the gateway; parse_schedule remains the authority.
_DURATION = re.compile(r"^\d+(\.\d+)?\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)$")
_CRON_FIELD = re.compile(r"^[\d*\-,/]+$")


def schedule_looks_valid(schedule: Any) -> bool:
    if not isinstance(schedule, str) or not schedule.strip():
        return False
    s = schedule.strip()
    if s.lower().startswith("every "):
        return bool(_DURATION.match(s[6:].strip().lower()))
    parts = s.split()
    if len(parts) >= 5 and all(_CRON_FIELD.match(p) for p in parts[:5]):
        return True
    if "T" in s or re.match(r"^\d{4}-\d{2}-\d{2}", s):
        # parse_schedule takes this branch on any string containing a "T" and
        # then calls fromisoformat, so "on Tuesdays" reaches it and raises.
        # Mirror that rather than waving the string through.
        try:
            datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    return bool(_DURATION.match(s.lower()))


# --------------------------------------------------------------------------
# loading and validation
# --------------------------------------------------------------------------


def load_template(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise TemplateError([f"{os.path.basename(path)}: not a YAML mapping"])
    return data


def _validate_parameters(template: Dict[str, Any], problems: List[str]) -> Dict[str, Dict[str, Any]]:
    params: Dict[str, Dict[str, Any]] = {}
    raw = template.get("parameters")
    if not isinstance(raw, list) or not raw:
        problems.append("parameters: must be a non-empty list")
        return params
    for i, p in enumerate(raw):
        where = f"parameters[{i}]"
        if not isinstance(p, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        name = p.get("name")
        if not isinstance(name, str) or not re.match(r"^[a-z][a-z0-9_]*$", name):
            problems.append(f"{where}: name {name!r} must be lower_snake_case")
            continue
        if name in params:
            problems.append(f"{where}: duplicate parameter {name!r}")
            continue
        if p.get("type") not in PARAM_TYPES:
            problems.append(f"{where} ({name}): type must be one of {sorted(PARAM_TYPES)}")
        if not isinstance(p.get("required"), bool):
            problems.append(f"{where} ({name}): required must be true or false")
        if not isinstance(p.get("ask"), str) or not p["ask"].strip():
            problems.append(
                f"{where} ({name}): needs an `ask` — the question an operator is "
                "shown. A parameter nobody knows how to answer is not configurable."
            )
        # A required parameter with a default is not required: the default
        # would silently fill the hole the format exists to keep open.
        if p.get("required") and "default" in p:
            problems.append(f"{where} ({name}): required parameters must not have a default")
        if p.get("required") is False and "default" not in p:
            problems.append(f"{where} ({name}): optional parameters need a default")
        params[name] = p
    return params


def validate_template(template: Dict[str, Any], *, filename: Optional[str] = None) -> List[str]:
    """Every problem with this template. Empty list means it is well-formed."""
    problems: List[str] = []

    for key in REQUIRED_TOP_LEVEL:
        if key not in template:
            problems.append(f"missing required key: {key}")

    tid = template.get("id")
    if not isinstance(tid, str) or not re.match(r"^[a-z][a-z0-9-]*$", tid or ""):
        problems.append(f"id {tid!r} must be lower-kebab-case")
    elif filename and os.path.splitext(os.path.basename(filename))[0] != tid:
        problems.append(f"id {tid!r} does not match filename {os.path.basename(filename)}")

    if not isinstance(template.get("version"), int):
        problems.append("version must be an integer")
    for key in ("title", "summary"):
        if not isinstance(template.get(key), str) or not str(template.get(key)).strip():
            problems.append(f"{key} must be a non-empty string")
    if template.get("tier") not in TIERS:
        problems.append(f"tier must be one of {sorted(TIERS)}")

    caps = template.get("requires_capabilities", [])
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        problems.append("requires_capabilities must be a list of strings")

    params = _validate_parameters(template, problems)

    schedule = template.get("schedule")
    if not isinstance(schedule, dict):
        problems.append("schedule must be a mapping with a `default`")
    else:
        if not schedule_looks_valid(schedule.get("default")):
            problems.append(
                f"schedule.default {schedule.get('default')!r} is not a schedule "
                "cron.jobs.parse_schedule accepts"
            )
        if not isinstance(schedule.get("editable"), bool):
            problems.append("schedule.editable must be true or false")

    delivery = template.get("delivery")
    if not isinstance(delivery, dict) or not isinstance(delivery.get("default"), str):
        problems.append("delivery must be a mapping with a string `default`")

    job = template.get("job")
    if not isinstance(job, dict):
        problems.append("job must be a mapping")
        job = {}
    else:
        for key in sorted(set(job) & FORBIDDEN_JOB_KEYS):
            problems.append(
                f"job.{key}: a template is not a job. Schedule and delivery are "
                "declared in their own sections; runtime state belongs to the "
                "scheduler."
            )
        for key in sorted(set(job) - ALLOWED_JOB_KEYS - FORBIDDEN_JOB_KEYS):
            problems.append(f"job.{key}: not an argument of cron.jobs.create_job")
        if not isinstance(job.get("prompt"), str) or not job.get("prompt", "").strip():
            problems.append("job.prompt must be a non-empty string")
        toolsets = job.get("enabled_toolsets", [])
        if not isinstance(toolsets, list) or not all(isinstance(t, str) for t in toolsets):
            problems.append("job.enabled_toolsets must be a list of strings")

    # Placeholders must name declared parameters, and every declared parameter
    # must be used — an unused parameter is a question asked for no reason.
    used = set()
    for key in ("prompt", "name", "monitor_url", "monitor_script"):
        text = job.get(key)
        if not isinstance(text, str):
            continue
        if MALFORMED_PLACEHOLDER.search(text):
            problems.append(
                f"job.{key}: malformed placeholder. The only substitution is "
                "{{parameter_name}} — there are no conditionals or loops."
            )
        for name in PLACEHOLDER.findall(text):
            used.add(name)
            if params and name not in params:
                problems.append(f"job.{key}: {{{{{name}}}}} is not a declared parameter")
    for name in sorted(set(params) - used):
        problems.append(f"parameters: {name!r} is declared but never used in the job")

    cd = template.get("change_detection")
    if not isinstance(cd, dict):
        problems.append("change_detection must be a mapping")
    else:
        mechanism = cd.get("mechanism")
        if mechanism not in MECHANISMS:
            problems.append(f"change_detection.mechanism must be one of {sorted(MECHANISMS)}")
        if mechanism == "monitor" and not (job.get("monitor_url") or job.get("monitor_script")):
            problems.append(
                "change_detection.mechanism is `monitor` but the job has neither "
                "monitor_url nor monitor_script, so nothing would be compared"
            )
        if mechanism == "prompt" and "[SILENT]" not in str(job.get("prompt", "")):
            problems.append(
                "change_detection.mechanism is `prompt` but the prompt never "
                "gives the agent the [SILENT] contract, so a run with nothing "
                "new still delivers a message"
            )
        if mechanism in ("monitor", "prompt") and not str(cd.get("verify", "")).strip():
            problems.append(
                "change_detection.verify is required: change detection that "
                "nobody checks is the failure this section exists to prevent"
            )
        if mechanism == "none" and not str(cd.get("why", "")).strip():
            problems.append("change_detection.mechanism is `none` — say why in `why`")

    return problems


def list_templates(root: str) -> List[Tuple[str, Dict[str, Any]]]:
    """(path, template) for every YAML file in a library directory, sorted."""
    out = []
    if not os.path.isdir(root):
        return out
    for entry in sorted(os.listdir(root)):
        if not entry.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(root, entry)
        out.append((path, load_template(path)))
    return out


# --------------------------------------------------------------------------
# filling
# --------------------------------------------------------------------------


def _coerce(param: Dict[str, Any], value: Any, problems: List[str]) -> Any:
    name, ptype = param.get("name"), param.get("type")
    if ptype == "list":
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if not isinstance(value, list) or not all(isinstance(v, (str, int, float)) for v in value):
            problems.append(f"{name}: expected a list")
            return None
        if not value:
            problems.append(f"{name}: expected a non-empty list")
            return None
        return [str(v).strip() for v in value]
    if ptype == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{name}: expected a whole number")
            return None
        return value
    if ptype == "boolean":
        if not isinstance(value, bool):
            problems.append(f"{name}: expected true or false")
            return None
        return value
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{name}: expected a non-empty string")
        return None
    value = value.strip()
    if ptype == "url" and not value.lower().startswith(("http://", "https://")):
        problems.append(f"{name}: expected an http(s) URL, got {value!r}")
        return None
    return value


def _render(text: str, rendered: Dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: rendered[m.group(1)], text)


def _as_text(param: Dict[str, Any], value: Any) -> str:
    if param.get("type") == "list":
        return ", ".join(value)
    if param.get("type") == "boolean":
        return "yes" if value else "no"
    return str(value)


def render_job(
    template: Dict[str, Any],
    values: Dict[str, Any],
    *,
    schedule: Optional[str] = None,
    deliver: Optional[str] = None,
) -> Dict[str, Any]:
    """Turn a template plus operator-supplied values into create_job kwargs.

    Raises TemplateError if the template is malformed, if a required parameter
    is missing or empty, if a value is the wrong type, or if an undeclared
    parameter was supplied. This is the only door from the library to the
    scheduler, which is what makes an unconfigured template unable to run.
    """
    problems = validate_template(template)
    if problems:
        raise TemplateError([f"template is invalid: {p}" for p in problems])

    params = {p["name"]: p for p in template["parameters"]}
    values = values or {}

    for name in sorted(set(values) - set(params)):
        problems.append(f"{name}: not a parameter of this template")

    resolved: Dict[str, Any] = {}
    for name, param in params.items():
        if name in values and values[name] is not None:
            resolved[name] = _coerce(param, values[name], problems)
        elif param.get("required"):
            problems.append(f"{name}: required — {param.get('ask', '').strip()}")
        else:
            resolved[name] = param.get("default")

    chosen_schedule = schedule or template["schedule"].get("default")
    if not schedule_looks_valid(chosen_schedule):
        problems.append(f"schedule: {chosen_schedule!r} is not a schedule the scheduler accepts")
    if schedule and not template["schedule"].get("editable", False):
        problems.append("schedule: this template's schedule is not editable")

    chosen_deliver = deliver or template["delivery"].get("default")
    if not isinstance(chosen_deliver, str) or not chosen_deliver.strip():
        problems.append("deliver: must be a non-empty string")

    if problems:
        raise TemplateError(problems)

    text = {name: _as_text(params[name], value) for name, value in resolved.items()}

    job = template["job"]
    kwargs: Dict[str, Any] = {
        "prompt": _render(job["prompt"], text).strip(),
        "schedule": chosen_schedule,
        "name": _render(job.get("name") or template["title"], text),
        "deliver": chosen_deliver.strip(),
    }
    if job.get("enabled_toolsets"):
        kwargs["enabled_toolsets"] = list(job["enabled_toolsets"])
    for key in ("monitor_url", "monitor_script"):
        if job.get(key):
            kwargs[key] = _render(job[key], text)
    return kwargs


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_check(root: str) -> int:
    templates = list_templates(root)
    if not templates:
        print(f"no templates found in {root}")
        return 1
    failed = 0
    for path, template in templates:
        problems = validate_template(template, filename=path)
        if problems:
            failed += 1
            print(f"FAIL  {os.path.basename(path)}")
            for p in problems:
                print(f"        {p}")
        else:
            print(f"ok    {os.path.basename(path)}")
    print(f"\n{len(templates)} template(s), {failed} invalid")
    return 1 if failed else 0


def _cmd_fill(path: str, values_json: str) -> int:
    template = load_template(path)
    try:
        kwargs = render_job(template, json.loads(values_json or "{}"))
    except TemplateError as exc:
        print(f"cannot fill {os.path.basename(path)}:")
        for p in exc.problems:
            print(f"  {p}")
        return 1
    print(json.dumps(kwargs, indent=2, ensure_ascii=False))
    return 0


def main(argv: List[str]) -> int:
    if len(argv) >= 3 and argv[1] == "--check":
        return _cmd_check(argv[2])
    if len(argv) >= 3 and argv[1] == "--fill":
        return _cmd_fill(argv[2], argv[3] if len(argv) > 3 else "{}")
    print(
        "usage:\n"
        "  automation_library.py --check <library dir>\n"
        "  automation_library.py --fill <template.yaml> '<json values>'",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
