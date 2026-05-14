"""Command line utilities for managing OpenReward environments."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from string import Template
from typing import Callable, Mapping
from urllib.parse import quote, urlencode, urlparse, urlunparse

import yaml


CPU_MEMORY_CHOICES = [
    "0.5:0.5", "1:1", "2:2", "4:4",
    "0.5:1", "1:2", "2:4", "4:8",
    "0.5:2", "1:4", "2:8", "4:16",
]

OUTPUT_FORMATS = ["table", "json", "yaml", "jsonl"]


# ---------------------------------------------------------------------------
# Structured output helpers
# ---------------------------------------------------------------------------


def _output_object(data: dict, fmt: str) -> None:
    """Print a single object in the requested format."""
    if fmt == "json":
        print(json.dumps(data))
    elif fmt == "yaml":
        yaml.dump(data, sys.stdout, default_flow_style=False, sort_keys=False)
    elif fmt == "jsonl":
        print(json.dumps(data))


def _output_rows(rows: list[dict], fmt: str) -> None:
    """Print a list of objects in the requested format."""
    if fmt == "json":
        print(json.dumps(rows))
    elif fmt == "yaml":
        yaml.dump(rows, sys.stdout, default_flow_style=False, sort_keys=False)
    elif fmt == "jsonl":
        for row in rows:
            print(json.dumps(row))


@dataclass(frozen=True)
class TemplateFiles:
    """Collection of scaffold files for an environment."""

    dockerfile: str
    server_py: str
    extra_files: Mapping[str, str] = field(default_factory=dict)


def _pascal_case(name: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", name)
    cleaned = "".join(part.capitalize() for part in parts if part)
    if not cleaned:
        cleaned = "Environment"
    if cleaned[0].isdigit():
        cleaned = f"Env{cleaned}"
    return cleaned


def _load_template_file(template_name: str, filename: str) -> str:
    package = f"openreward.templates.{template_name}"
    file = resources.files(package).joinpath(filename)
    return file.read_text(encoding="utf-8")


_README_TEMPLATE = """\
# {env_name}

[![OpenReward Environment](https://img.shields.io/badge/%E2%AD%90%20OpenReward-Environment-f7e6cc)](https://openreward.ai/YourOrg/{env_name})

## Description

<!-- 2-4 sentences: what does this environment evaluate? What domain does it cover?
     Mention the verification method and the source dataset. -->

## Capabilities

<!-- Bulleted list of skills or capabilities the environment tests. -->

- Capability 1
- Capability 2
- Capability 3

## Compute Requirements

<!-- Does the environment need a sandbox? GPU? Extra memory?
     If it runs without a sandbox, say so. -->

## License

<!-- License that covers the use of the environment, as well as any underlying data or code it depends on.
     Link to the license text. If the environment and its dependencies have different licenses, list both. -->

## Tasks

<!-- How many tasks are there? What splits (train/test)?
     Briefly describe the structure of each task. -->

## Reward Structure

<!-- How are rewards computed?
     Sparse or dense? Binary or continuous?
     Is verification programmatic or does it use an LLM grader? Does it use rubrics?
     If there are multiple validation steps, list them in order. -->

## Data

<!-- Where does the task data come from? How is it stored?
     Link to the source dataset if applicable. -->

## Tools

<!-- Explain the tools available to the agent. -->

## Time Horizon

<!-- Single-turn or multi-turn?
     If multi-turn, roughly how many tool calls does a typical task require? -->

## Environment Difficulty

<!-- Solve rates, baseline model performance, or other difficulty statistics. -->

## Other Environment Requirements

<!-- Any external dependencies: API keys, secrets, third-party services. -->

## Safety

<!-- Does the agent have access to external systems (network, file system, APIs)?
     Are there dual-use risks in the domain (e.g. chemistry, cybersecurity)?
     Is there a possibility of goal misspecification?
     What mitigations are in place? -->

## Citations

<!-- BibTeX entries for the environment itself and any underlying datasets or papers. -->

```bibtex
@dataset{{YourCitation,
  author    = {{Your Name or Team}},
  title     = {{{env_name}}},
  year      = {{2026}},
  publisher = {{OpenReward}},
  url       = {{https://openreward.ai/YourOrg/{env_name}}}
}}
```
"""


def _render_readme(env_name: str) -> str:
    return _README_TEMPLATE.format(env_name=env_name)


def _basic_template(env_name: str) -> TemplateFiles:
    class_name = _pascal_case(env_name)
    dockerfile = _load_template_file("basic", "Dockerfile")
    server_template = Template(_load_template_file("basic", "server.py.tmpl"))
    server_py = server_template.substitute(CLASS_NAME=class_name)
    extra_files = {
        "requirements.txt": _load_template_file("basic", "requirements.txt"),
        "README.md": _render_readme(env_name),
    }
    return TemplateFiles(dockerfile=dockerfile, server_py=server_py, extra_files=extra_files)


def _sandbox_template(env_name: str) -> TemplateFiles:
    """Create a sandbox template with Docker-in-Docker support."""
    dockerfile = _load_template_file("sandbox", "Dockerfile")
    server_py = _load_template_file("sandbox", "server.py")
    extra_files = {
        "requirements.txt": _load_template_file("sandbox", "requirements.txt"),
        "sandbox_env.py": _load_template_file("sandbox", "sandbox_env.py"),
        "README.md": _render_readme(env_name),
    }
    return TemplateFiles(dockerfile=dockerfile, server_py=server_py, extra_files=extra_files)


TemplateFactory = Callable[[str], TemplateFiles]


TEMPLATES: Mapping[str, TemplateFactory] = {
    "basic": _basic_template,
    "sandbox": _sandbox_template,
}


def _render_template(template_name: str, env_name: str) -> TemplateFiles:
    generator = TEMPLATES.get(template_name)
    if generator is None:
        raise ValueError(f"Unknown template '{template_name}'. Available templates: {', '.join(TEMPLATES)}")
    return generator(env_name)


def _write_file(path: Path, contents: str) -> None:
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    path.write_text(contents, encoding="utf-8")


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _require_api_key() -> str:
    api_key = os.environ.get("OPENREWARD_API_KEY")
    if not api_key:
        raise SystemExit("Error: OPENREWARD_API_KEY environment variable is not set")
    return api_key


def _parse_env_ref(ref: str) -> tuple[str, str]:
    parts = ref.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise SystemExit(f"Error: expected <owner/name>, got {ref!r}")
    return quote(parts[0], safe=""), quote(parts[1], safe="")


def _url_path(*segments: str) -> str:
    """Build a URL path with properly encoded segments."""
    return "/" + "/".join(quote(s, safe="") for s in segments)


def _api_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    return str(urlunparse(parsed._replace(netloc=f"api.{parsed.netloc}")))


def _api_request(
    path: str,
    api_key: str,
    method: str = "GET",
    body: dict | None = None,
    query: dict | None = None,
) -> dict:
    base_url = os.environ.get("OPENREWARD_URL", "https://openreward.ai")
    api_url = (os.environ.get("OPENREWARD_API_URL") or _api_base_url(base_url)).rstrip("/")
    if query:
        path = f"{path}?{urlencode(query)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers: dict[str, str] = {"X-Api-Key": api_key}

    if data is not None:
        headers["Content-Type"] = "application/json"
    url = f"{api_url}{path}"
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
    except (ValueError, Exception) as e:
        raise SystemExit(f"Error: invalid request URL: {e}")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8")
        try:
            msg = json.loads(body_text).get("message", body_text)
        except Exception:
            msg = body_text
        raise SystemExit(f"Error {e.code}: {msg}")


def _resolve_namespace(api_key: str) -> str:
    return _api_request("/v1/namespace/current?type=user", api_key)["name"]


# ---------------------------------------------------------------------------
# whoami / namespaces commands
# ---------------------------------------------------------------------------


def command_whoami(fmt: str) -> None:
    api_key = _require_api_key()

    # Org-scoped API keys have an orgId in their auth context, so try org
    # first; fall back to user for personal keys.
    try:
        result = _api_request("/v1/namespace/current", api_key, query={"type": "org"})
    except SystemExit:
        result = _api_request("/v1/namespace/current", api_key, query={"type": "user"})

    if fmt != "table":
        _output_object(result, fmt)
        return

    print(f"Namespace: {result['name']}")
    if result.get("id"):
        print(f"ID:        {result['id']}")
    if result.get("type"):
        print(f"Type:      {result['type']}")


# ---------------------------------------------------------------------------
# GitHub auth helper
# ---------------------------------------------------------------------------


def _ensure_github_auth(api_key: str) -> None:
    """Check GitHub auth and guide the user through OAuth if needed."""
    result = _api_request("/v1/github/auth-status", api_key)
    status = result["status"]

    if status == "success":
        return

    base_url = os.environ.get("OPENREWARD_URL", "https://openreward.ai")
    redirect_url = base_url

    if status == "noAuth":
        url_result = _api_request(
            "/v1/github/install-url",
            api_key,
            query={"redirectUrl": redirect_url},
        )
        action = "install the OpenReward GitHub App"
    else:  # requiresReauth
        url_result = _api_request(
            "/v1/github/reauth-url",
            api_key,
            query={"redirectUrl": redirect_url},
        )
        action = "reauthorize GitHub access"

    url = url_result["url"]
    print(f"You need to {action}.")
    print(f"Opening browser: {url}")
    webbrowser.open(url)
    input("\nAfter completing authorization in the browser, press Enter to continue...")

    recheck = _api_request("/v1/github/auth-status", api_key)
    if recheck["status"] != "success":
        raise SystemExit(
            "GitHub authorization not detected. Please try again or visit "
            f"{base_url}/settings/github to connect your account."
        )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def command_init(environment: str, template: str, directory: str | None) -> None:
    target_dir = Path.cwd() / (directory or environment)
    target_dir.mkdir(parents=True, exist_ok=False)

    files = _render_template(template, environment)

    _write_file(target_dir / "Dockerfile", files.dockerfile)
    _write_file(target_dir / "server.py", files.server_py)
    for relative, contents in files.extra_files.items():
        _write_file(target_dir / relative, contents)

    print(f"Created environment '{environment}' in {target_dir}")
    print(f"  Template: {template}")
    print(f"  Files: Dockerfile, server.py, {', '.join(files.extra_files)}")


def command_create(name: str, namespace: str | None, description: str, private: bool, harbor: bool, fmt: str) -> None:
    api_key = _require_api_key()

    if namespace is None:
        namespace = _resolve_namespace(api_key)

    body: dict = {"name": name, "namespace": namespace, "description": description, "isPrivate": private}
    if harbor:
        body["isHarbor"] = True

    result = _api_request(
        "/v1/environments",
        api_key,
        method="POST",
        body=body,
    )
    if fmt != "table":
        _output_object(result, fmt)
    else:
        print(f"Created environment: {result['owner']}/{result['name']} (id: {result['id']})")


def command_list(owner: str | None, mine: bool, search: str | None, limit: int | None, offset: int, fmt: str) -> None:
    api_key = _require_api_key()

    if mine:
        owner = _resolve_namespace(api_key)

    base_query: dict[str, str | int] = {}
    if owner:
        base_query["owner"] = owner
    if search:
        base_query["search"] = search

    page_size = min(limit, 100) if limit else 100
    envs: list[dict] = []
    total = 0
    current_offset = offset

    while True:
        query = {**base_query, "limit": page_size, "offset": current_offset}
        result = _api_request("/v1/environments", api_key, query=query)
        batch = result["environments"]
        total = result["total"]
        envs.extend(batch)
        if not batch or len(batch) < page_size:
            break
        if limit and len(envs) >= limit:
            envs = envs[:limit]
            break
        current_offset += len(batch)

    if fmt != "table":
        _output_rows(envs, fmt)
        return

    if not envs:
        print("No environments found.")
        return

    print(f"{'OWNER/NAME':<40} {'CONNECTED':<12} DESCRIPTION")
    for e in envs:
        full = f"{e.get('owner', '?')}/{e['name']}"
        connected = "yes" if e.get("has_github_connection") else "no"
        desc = (e.get("description") or "")[:60]
        print(f"{full:<40} {connected:<12} {desc}")
    print(f"\nShowing {len(envs)} of {total} environments")


def command_get(env_ref: str, fmt: str) -> None:
    api_key = _require_api_key()
    owner, name = _parse_env_ref(env_ref)

    env = _api_request(f"/v1/environments/{owner}/{name}", api_key)

    if fmt != "table":
        _output_object(env, fmt)
        return

    print(f"Name:        {env.get('owner', owner)}/{env['name']}")
    print(f"ID:          {env['id']}")
    print(f"Description: {env.get('description', '')}")
    print(f"Private:     {env.get('is_private', False)}")
    print(f"GitHub:      {'connected' if env.get('has_github_connection') else 'not connected'}")
    if env.get("original_github_url"):
        print(f"Repo:        {env['original_github_url']}")
    if env.get("compute_config"):
        cc = env["compute_config"]
        print(f"Compute:     {cc.get('cpu_count', '?')} CPU, {cc.get('mem_gb', '?')} GB")
    if env.get("max_concurrency") is not None:
        print(f"Concurrency: {env['max_concurrency']}")
    if env.get("max_scale") is not None:
        print(f"Max Scale:   {env['max_scale']}")
    print(f"Created:     {env.get('created_at', '?')}")
    print(f"Updated:     {env.get('updated_at', '?')}")


def command_deployments(env_ref: str, fmt: str) -> None:
    api_key = _require_api_key()
    owner, name = _parse_env_ref(env_ref)

    env = _api_request(f"/v1/environments/{owner}/{name}", api_key)
    env_id = env["id"]

    deployments: list[dict] = _api_request(f"/v1/github/environments/{env_id}/deployments", api_key)  # type: ignore[assignment]

    if fmt != "table":
        _output_rows(deployments, fmt)
        return

    if not deployments:
        print("No deployments found. Has this environment been linked to GitHub?")
        print(f"  Run: orwd link {owner}/{name} <github-repo>")
        return

    print(f"{'ID':<10} {'STATUS':<12} {'BRANCH':<20} {'COMMIT':<10} CREATED")
    for d in deployments:
        did = d["id"][:8]
        status = d.get("status", "?")
        branch = (d.get("gitBranch") or "")[:20]
        commit = (d.get("commitSha") or "")[:8]
        created = d.get("created_at", "?")
        print(f"{did:<10} {status:<12} {branch:<20} {commit:<10} {created}")


def command_logs(env_ref: str, build: bool, deployment_id: str | None, limit: int, fmt: str) -> None:
    api_key = _require_api_key()

    if deployment_id is None:
        owner, name = _parse_env_ref(env_ref)
        env = _api_request(f"/v1/environments/{owner}/{name}", api_key)
        env_id = env["id"]
        deployments = _api_request(f"/v1/github/environments/{env_id}/deployments", api_key)
        if not deployments:
            if fmt != "table":
                _output_rows([], fmt)
            else:
                print("No deployments found. Has this environment been linked to GitHub?")
                print(f"  Run: orwd link {owner}/{name} <github-repo>")
            return
        deployment_id = str(deployments[0]["id"])
        if fmt == "table":
            print(f"Using latest deployment: {deployment_id[:8]} ({deployments[0].get('status', '?')})\n")

    if build:
        result = _api_request(f"/v1/github/deployments/{deployment_id}/logs", api_key)
        if fmt != "table":
            _output_object(result, fmt)
            return
        logs = result.get("logs")
        if logs:
            print(logs)
        else:
            print(f"No build logs available (status: {result.get('status', '?')})")
    else:
        result = _api_request(
            f"/v1/github/deployments/{deployment_id}/runtime-logs",
            api_key,
            query={"limit": limit},
        )
        entries = result.get("entries", [])
        if fmt != "table":
            _output_rows(entries, fmt)
            return
        if not entries:
            print(f"No runtime logs available (status: {result.get('status', '?')})")
            return
        for entry in entries:
            ts = entry.get("timestamp", "")
            sev = entry.get("severity", "")
            msg = entry.get("message", "")
            print(f"[{ts}] [{sev}] {msg}")


def command_task_builds(env_ref: str, deployment_id: str | None, fmt: str) -> None:
    api_key = _require_api_key()

    if deployment_id is None:
        owner, name = _parse_env_ref(env_ref)
        env = _api_request(f"/v1/environments/{owner}/{name}", api_key)
        env_id = env["id"]
        deployments = _api_request(f"/v1/github/environments/{env_id}/deployments", api_key)
        if not deployments:
            if fmt != "table":
                _output_rows([], fmt)
            else:
                print("No deployments found.")
            return
        deployment_id = str(deployments[0]["id"])
        if fmt == "table":
            print(f"Using latest deployment: {deployment_id[:8]} ({deployments[0].get('status', '?')})\n")

    result = _api_request(f"/v1/github/deployments/{deployment_id}/task-builds", api_key)
    builds = result.get("taskBuilds", [])

    if fmt != "table":
        _output_rows(builds, fmt)
        return

    if not builds:
        print("No task builds found.")
        return

    print(f"{'TASK':<30} {'STATUS':<10} {'ID':<10} UPDATED")
    for b in builds:
        task = b.get("taskName", "?")[:30]
        status = b.get("status", "?")
        bid = b["id"][:8]
        updated = b.get("updated_at", "?")
        error = b.get("errorMessage")
        line = f"{task:<30} {status:<10} {bid:<10} {updated}"
        if error:
            line += f"\n{'':>30} error: {error}"
        print(line)


def command_task_build_logs(task_build_id: str, fmt: str) -> None:
    api_key = _require_api_key()

    result = _api_request(f"/v1/github/task-builds/{quote(task_build_id, safe='')}/logs", api_key)

    if fmt != "table":
        _output_object(result, fmt)
        return

    print(f"Task: {result.get('taskName', '?')} (status: {result.get('status', '?')})\n")
    logs = result.get("logs")
    if logs:
        print(logs)
    else:
        print("No build logs available yet.")


def command_link(
    env_ref: str,
    github_repo: str,
    cpu_memory: str,
    concurrency: int,
    max_scale: int,
    subdirectory: str | None,
    fmt: str,
) -> None:
    api_key = _require_api_key()
    owner, name = _parse_env_ref(env_ref)

    # Ensure the user has GitHub auth
    _ensure_github_auth(api_key)

    # List repos and find the target
    repos_result = _api_request("/v1/github/repos", api_key)
    if repos_result["status"] != "success":
        raise SystemExit(f"Error: unexpected GitHub repos status: {repos_result['status']}")

    repos = repos_result["repos"]
    target = github_repo.lower()
    match = None
    for repo in repos:
        if repo["fullName"].lower() == target:
            match = repo
            break

    if match is None:
        print(f"Error: repo '{github_repo}' not found in your accessible repositories.")
        print("Ensure you have admin access to the repo and the OpenReward GitHub App is installed on it.\n")
        if repos:
            print("Available repos:")
            for r in repos[:20]:
                print(f"  {r['fullName']}")
            if len(repos) > 20:
                print(f"  ... and {len(repos) - 20} more")
        raise SystemExit(1)

    # Convert cpu:memory format to API format
    api_cpu_memory = cpu_memory.replace(":", "-")

    connect_body = {
        "githubRepoId": match["id"],
        "githubInstallationId": match["installationId"],
        "settings": {
            "cpuMemory": api_cpu_memory,
            "minScale": 0,
            "maxScale": max_scale,
            "concurrency": concurrency,
        },
    }
    if subdirectory:
        connect_body["subdirectory"] = subdirectory

    result = _api_request(
        f"/v1/github/environments/{owner}/{name}/connect",
        api_key,
        method="POST",
        body=connect_body,
    )
    if fmt != "table":
        _output_object(result, fmt)
    else:
        print(f"Linked {owner}/{name} to {match['fullName']} (environment id: {result['environmentId']})")
        print(f"A deployment has been triggered. Run `orwd deployments {owner}/{name}` to check status.")


def command_unlink(env_ref: str, fmt: str) -> None:
    api_key = _require_api_key()
    owner, name = _parse_env_ref(env_ref)

    env = _api_request(f"/v1/environments/{owner}/{name}", api_key)
    env_id = env["id"]

    if not env.get("has_github_connection"):
        print(f"{owner}/{name} is not linked to GitHub.")
        return

    result = _api_request(f"/v1/github/environments/{env_id}", api_key, method="DELETE")
    if fmt != "table":
        _output_object(result, fmt)
    else:
        print(f"Unlinked {owner}/{name} from GitHub.")


def command_update(
    env_ref: str,
    new_name: str | None,
    description: str | None,
    private: bool | None,
    harbor: bool | None,
    arxiv_url: str | None,
    github_url: str | None,
    hf_url: str | None,
    max_concurrency: int | None,
    max_scale: int | None,
    fmt: str,
) -> None:
    api_key = _require_api_key()
    owner, name = _parse_env_ref(env_ref)

    body: dict = {}
    if new_name is not None:
        body["new_name"] = new_name
    if description is not None:
        body["new_description"] = description
    if private is not None:
        body["new_is_private"] = private
    if harbor is not None:
        body["new_is_harbor"] = harbor
    if arxiv_url is not None:
        body["new_arxiv_url"] = arxiv_url or None
    if github_url is not None:
        body["new_external_github_url"] = github_url or None
    if hf_url is not None:
        body["new_external_hf_url"] = hf_url or None

    has_link_update = max_concurrency is not None or max_scale is not None

    if not body and not has_link_update:
        raise SystemExit("Error: no updates specified. Use --name, --description, --private, etc.")

    if body:
        result = _api_request(f"/v1/environments/{owner}/{name}/details", api_key, method="PUT", body=body)
        if fmt != "table":
            _output_object(result, fmt)
        else:
            print(f"Updated environment: {result.get('owner', owner)}/{result['name']}")

    if has_link_update:
        env = _api_request(f"/v1/environments/{owner}/{name}", api_key)
        env_id = env["id"]
        if not env.get("has_github_connection"):
            raise SystemExit(f"Error: {owner}/{name} is not linked to GitHub. Use 'update-link' after linking.")
        # The settings endpoint requires all fields, so fetch current values and merge
        cc = env.get("compute_config") or {}
        cpu = cc.get("cpu_count", 1)
        mem = cc.get("mem_gb", 4)
        cpu_s = str(int(cpu)) if cpu == int(cpu) else str(cpu)
        mem_s = str(int(mem)) if mem == int(mem) else str(mem)
        settings: dict = {
            "cpuMemory": f"{cpu_s}-{mem_s}",
            "minScale": 0,
            "maxScale": env.get("max_scale") or 10,
            "concurrency": env.get("max_concurrency") or 500,
        }
        if max_concurrency is not None:
            settings["concurrency"] = max_concurrency
        if max_scale is not None:
            settings["maxScale"] = max_scale
        _api_request(f"/v1/github/environments/{env_id}", api_key, method="PUT", body={"settings": settings})
        print(f"Updated deployment settings for {owner}/{name}.")



def command_update_link(
    env_ref: str,
    cpu_memory: str | None,
    concurrency: int | None,
    max_scale: int | None,
    subdirectory: str | None,
    fmt: str,
) -> None:
    api_key = _require_api_key()
    owner, name = _parse_env_ref(env_ref)

    env = _api_request(f"/v1/environments/{owner}/{name}", api_key)
    env_id = env["id"]

    if not env.get("has_github_connection"):
        raise SystemExit(f"Error: {owner}/{name} is not linked to GitHub.")

    body: dict = {}
    if cpu_memory is not None or concurrency is not None or max_scale is not None:
        settings: dict = {}
        if cpu_memory is not None:
            settings["cpuMemory"] = cpu_memory.replace(":", "-")
        if concurrency is not None:
            settings["concurrency"] = concurrency
        if max_scale is not None:
            settings["maxScale"] = max_scale
            settings["minScale"] = 0
        body["settings"] = settings
    if subdirectory is not None:
        body["subdirectory"] = subdirectory or None

    if not body:
        raise SystemExit("Error: no updates specified. Use --cpu-memory, --concurrency, --max-scale, or --subdirectory.")

    result = _api_request(f"/v1/github/environments/{env_id}", api_key, method="PUT", body=body)
    if fmt != "table":
        _output_object(result, fmt)
    else:
        print(f"Updated link settings for {owner}/{name}.")


def command_runs(search: str | None, limit: int, offset: int, fmt: str) -> None:
    api_key = _require_api_key()

    query: dict[str, str | int] = {"limit": limit, "offset": offset}
    if search:
        query["search"] = search

    result = _api_request("/v1/runs", api_key, query=query)
    runs = result["runs"]
    total = result["total"]

    if fmt != "table":
        _output_rows(runs, fmt)
        return

    if not runs:
        print("No runs found.")
        return

    print(f"{'NAME':<40} {'ROLLOUTS':<10} {'AVG REWARD':<12} CREATED")
    for r in runs:
        name = r["name"][:40]
        num = r.get("num_rollouts") or 0
        avg = r.get("avg_rollout_reward")
        avg_str = f"{avg:.3f}" if avg is not None else "-"
        created = r.get("created_at", "?")
        print(f"{name:<40} {num:<10} {avg_str:<12} {created}")
    print(f"\nShowing {len(runs)} of {total} runs")


def command_run(run_id: str, fmt: str) -> None:
    api_key = _require_api_key()

    run = _api_request(f"/v1/runs/{quote(run_id, safe='')}", api_key)

    if fmt != "table":
        _output_object(run, fmt)
        return

    print(f"Name:           {run['name']}")
    print(f"ID:             {run['id']}")
    print(f"Rollouts:       {run.get('num_rollouts', 0)}")
    avg = run.get("avg_rollout_reward")
    print(f"Avg reward:     {f'{avg:.3f}' if avg is not None else '-'}")
    avg_len = run.get("avg_rollout_length")
    print(f"Avg length:     {f'{avg_len:.1f}' if avg_len is not None else '-'}")
    print(f"Created:        {run.get('created_at', '?')}")
    print(f"Updated:        {run.get('updated_at', '?')}")


def command_rollouts(run_id: str, limit: int, offset: int, fmt: str) -> None:
    api_key = _require_api_key()

    query: dict[str, str | int] = {"limit": limit, "offset": offset}
    result = _api_request(f"/v1/runs/{quote(run_id, safe='')}/rollouts", api_key, query=query)
    rollouts = result["rollouts"]
    total = result["total"]

    if fmt != "table":
        _output_rows(rollouts, fmt)
        return

    if not rollouts:
        print("No rollouts found.")
        return

    print(f"{'NAME':<30} {'ENVIRONMENT':<25} {'REWARD':<10} {'MSGS':<6} CREATED")
    for r in rollouts:
        name = (r.get("rolloutName") or r["id"][:8])[:30]
        env = (r.get("environment") or "-")[:25]
        reward = r.get("max_reward")
        reward_str = f"{reward:.3f}" if reward is not None else "-"
        msgs = r.get("num_messages", 0)
        created = r.get("created_at", "?")
        print(f"{name:<30} {env:<25} {reward_str:<10} {msgs:<6} {created}")
    print(f"\nShowing {len(rollouts)} of {total} rollouts")


def _collect_files(paths: list[str], dest: str | None) -> list[tuple[Path, str]]:
    """Collect (local_path, remote_name) pairs from the given file/dir paths.

    *remote_name* is the path used in the environment file store.  When *dest*
    is ``None`` each file keeps its path relative to cwd.  When *dest* is
    given it replaces the leading component (for a single file) or is
    prepended as a prefix (for directories / multiple files).
    """
    cwd = Path.cwd()
    collected: list[tuple[Path, str]] = []

    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            raise SystemExit(f"Error: {raw} does not exist")

        if p.is_file():
            try:
                rel = str(p.resolve().relative_to(cwd))
            except ValueError:
                rel = p.name
            if dest is not None:
                # If dest looks like a directory (ends with /) put file inside
                if dest.endswith("/"):
                    remote = dest + p.name
                else:
                    remote = dest
            else:
                remote = rel
            collected.append((p, remote))
        elif p.is_dir():
            found = False
            for child in sorted(p.rglob("*")):
                if not child.is_file():
                    continue
                found = True
                try:
                    rel = str(child.resolve().relative_to(cwd))
                except ValueError:
                    rel = str(child.relative_to(p.parent))
                if dest is not None:
                    child_rel = str(child.relative_to(p))
                    remote = f"{dest.rstrip('/')}/{child_rel}"
                else:
                    remote = rel
                collected.append((child, remote))
            if not found:
                raise SystemExit(f"Error: directory {raw} contains no files")
        else:
            raise SystemExit(f"Error: {raw} is not a file or directory")

    return collected


def _upload_to_signed_url(url: str, file_path: Path, content_type: str) -> None:
    """PUT file contents to a GCS signed URL."""
    data = file_path.read_bytes()
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={"Content-Type": content_type},
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()


def command_upload(env_ref: str, files: list[str], dest: str | None, concurrency: int, fmt: str) -> None:
    api_key = _require_api_key()
    owner, name = _parse_env_ref(env_ref)

    # Resolve environment ID
    env = _api_request(f"/v1/environments/{owner}/{name}", api_key)
    env_id = env["id"]

    # Collect files
    file_pairs = _collect_files(files, dest)
    if not file_pairs:
        raise SystemExit("Error: no files to upload")

    total = len(file_pairs)
    print(f"Uploading {total} file{'s' if total != 1 else ''} to {owner}/{name}...")

    # Process in batches of 50 (API limit)
    uploaded = 0
    failed = 0
    batch_size = 50

    for batch_start in range(0, total, batch_size):
        batch = file_pairs[batch_start : batch_start + batch_size]

        # Get signed URLs for this batch
        files_body = []
        for _, remote_name in batch:
            ct = mimetypes.guess_type(remote_name)[0] or "application/octet-stream"
            files_body.append({"fileName": remote_name, "contentType": ct})

        result = _api_request(
            "/v1/files/upload-urls",
            api_key,
            method="POST",
            body={"environmentId": env_id, "files": files_body},
        )

        url_map: dict[str, str] = {}
        for entry in result["urls"]:
            url_map[entry["fileName"]] = entry["uploadUrl"]

        # Upload files concurrently
        def _do_upload(item: tuple[Path, str]) -> tuple[str, str | None]:
            local_path, remote_name = item
            ct = mimetypes.guess_type(remote_name)[0] or "application/octet-stream"
            upload_url = url_map.get(remote_name)
            if not upload_url:
                return remote_name, "no signed URL returned"
            try:
                _upload_to_signed_url(upload_url, local_path, ct)
                return remote_name, None
            except Exception as e:
                return remote_name, str(e)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(_do_upload, item): item for item in batch}
            for future in as_completed(futures):
                remote_name, error = future.result()
                if error:
                    failed += 1
                    if fmt == "table":
                        print(f"  FAIL {remote_name}: {error}")
                else:
                    uploaded += 1
                    if fmt == "table":
                        print(f"  {remote_name}")

    if fmt != "table":
        _output_object({"uploaded": uploaded, "failed": failed, "total": total}, fmt)
    else:
        if failed:
            print(f"\nUploaded {uploaded}/{total} files ({failed} failed)")
        else:
            print(f"\nUploaded {uploaded} file{'s' if uploaded != 1 else ''}")


def command_files(env_ref: str, prefix: str | None, fmt: str) -> None:
    api_key = _require_api_key()
    owner, name = _parse_env_ref(env_ref)

    env = _api_request(f"/v1/environments/{owner}/{name}", api_key)
    env_id = env["id"]

    query: dict[str, str | int] = {"limit": 200}
    if prefix:
        query["prefix"] = prefix

    all_files: list[dict] = []
    all_folders: list[str] = []

    while True:
        result = _api_request(f"/v1/files/{quote(env_id, safe='')}", api_key, query=query)
        all_files.extend(result.get("files", []))
        all_folders.extend(result.get("folders", []))
        token = result.get("nextPageToken")
        if not token:
            break
        query["pageToken"] = token

    if fmt != "table":
        _output_object({"files": all_files, "folders": all_folders}, fmt)
        return

    if not all_files and not all_folders:
        print("No files found.")
        return

    if all_folders:
        for f in all_folders:
            print(f"  {f}/")
    if all_files:
        for f in all_files:
            size = f.get("size", 0)
            if size >= 1_048_576:
                size_str = f"{size / 1_048_576:.1f} MB"
            elif size >= 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size} B"
            print(f"  {f['path']:<60} {size_str:>10}")
    print(f"\n{len(all_files)} file{'s' if len(all_files) != 1 else ''}, {len(all_folders)} folder{'s' if len(all_folders) != 1 else ''}")


def command_delete_file(env_ref: str, file_path: str, folder: bool, fmt: str) -> None:
    api_key = _require_api_key()
    owner, name = _parse_env_ref(env_ref)

    env = _api_request(f"/v1/environments/{owner}/{name}", api_key)
    env_id = env["id"]

    encoded_path = quote(file_path, safe="")
    if folder:
        result = _api_request(
            f"/v1/files/{quote(env_id, safe='')}/folder/{encoded_path}",
            api_key,
            method="DELETE",
        )
        if fmt != "table":
            _output_object(result, fmt)
        else:
            count = result.get("deletedCount", 0)
            print(f"Deleted folder '{file_path}' ({count} file{'s' if count != 1 else ''})")
    else:
        result = _api_request(
            f"/v1/files/{quote(env_id, safe='')}/{encoded_path}",
            api_key,
            method="DELETE",
        )
        if fmt != "table":
            _output_object(result, fmt)
        else:
            print(f"Deleted '{file_path}'")


# ---------------------------------------------------------------------------
# Interactive prompts (styled via click)
# ---------------------------------------------------------------------------

try:
    import click
    _styled = True
except ImportError:
    _styled = False


def _dim(text: str) -> str:
    return click.style(text, dim=True) if _styled else text


def _bold(text: str) -> str:
    return click.style(text, bold=True) if _styled else text


def _green(text: str) -> str:
    return click.style(text, fg="green") if _styled else text


def _yellow(text: str) -> str:
    return click.style(text, fg="yellow") if _styled else text


def _red(text: str) -> str:
    return click.style(text, fg="red") if _styled else text


def _cyan(text: str) -> str:
    return click.style(text, fg="cyan") if _styled else text


def _prompt(label: str, default: str | None = None) -> str:
    hint = _dim(f" ({default})") if default else ""
    while True:
        value = input(f"  {_cyan('>')} {label}{hint}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print(f"    {_red('Value required.')}")


def _prompt_bool(label: str, default: bool = False) -> bool:
    hint = _dim(" (Y/n)") if default else _dim(" (y/N)")
    value = input(f"  {_cyan('>')} {label}{hint}: ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def _prompt_choice(label: str, choices: list[str], default: str) -> str:
    print(f"  {_cyan('>')} {label}")
    for i, c in enumerate(choices, 1):
        if c == default:
            print(f"    {_green(f'{i}.')} {_bold(c)} {_dim('(default)')}")
        else:
            print(f"    {_dim(f'{i}.')} {c}")
    value = input(f"    {_dim('Choice')}: ").strip()
    if not value:
        return default
    if value.isdigit() and 1 <= int(value) <= len(choices):
        return choices[int(value) - 1]
    if value in choices:
        return value
    return default


def _step(num: int, total: int, msg: str) -> None:
    print(f"\n  {_bold(f'[{num}/{total}]')} {msg}")


def _success(msg: str) -> None:
    print(f"  {_green('✓')} {msg}")


def _warn(msg: str) -> None:
    print(f"  {_yellow('!')} {msg}")


def _fail(msg: str) -> None:
    print(f"  {_red('✗')} {msg}")


def _run_cmd(args: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=capture, text=True)


# ---------------------------------------------------------------------------
# New environment wizard
# ---------------------------------------------------------------------------


def command_new(
    env_name: str | None,
    template: str | None,
    description: str | None,
    is_private: bool | None,
    is_harbor: bool | None,
    namespace: str | None,
    directory: str | None,
    github_repo: str | None,
    no_github: bool,
    yes: bool,
) -> None:
    print(f"\n  {_bold('OpenReward')} {_dim('— new environment')}\n")

    # Check prerequisites
    api_key = os.environ.get("OPENREWARD_API_KEY")
    if not api_key:
        _fail("OPENREWARD_API_KEY is not set")
        print(f"    Get your key at {_cyan('https://openreward.ai/settings/api-keys')}")
        raise SystemExit(1)

    has_gh = shutil.which("gh") is not None
    if not has_gh and not no_github:
        _warn(f"GitHub CLI not found. Install with: {_bold('brew install gh')}")
        _warn("Repo creation and linking will be skipped.\n")
        no_github = True

    # Gather inputs (use args or prompt)
    if env_name is None:
        env_name = _prompt("Environment name")
    if template is None:
        template = "basic" if yes else _prompt_choice("Template", sorted(TEMPLATES.keys()), default="basic")
    if description is None:
        description = _prompt("Description")
    if is_private is None:
        is_private = False if yes else _prompt_bool("Private", default=False)
    if is_harbor is None:
        is_harbor = False if yes else _prompt_bool("Harbor (sandbox) mode", default=False)

    if namespace is None:
        default_ns = _resolve_namespace(api_key)
        namespace = default_ns if yes else _prompt("Namespace", default=default_ns)

    if directory is None:
        directory = env_name if yes else _prompt("Directory", default=env_name)
    target_dir = Path.cwd() / directory

    if target_dir.exists():
        _fail(f"{target_dir} already exists")
        raise SystemExit(1)

    repo_private = is_private
    if not no_github and github_repo is None:
        if yes:
            github_repo = f"{namespace}/{env_name}"
        else:
            want_repo = _prompt_bool("Create a GitHub repo", default=True)
            if want_repo:
                github_repo = _prompt("GitHub repo (owner/repo)", default=f"{namespace}/{env_name}")
                repo_private = _prompt_bool("Private repo", default=is_private)
            else:
                no_github = True

    # Summary
    total_steps = 4 if github_repo else 2
    print(f"\n  {_bold('Summary')}")
    print(f"    Name        {_bold(f'{namespace}/{env_name}')}")
    print(f"    Template    {template}")
    print(f"    Description {description}")
    print(f"    Private     {is_private}")
    if is_harbor:
        print(f"    Harbor      {_yellow('yes')}")
    print(f"    Directory   {target_dir}")
    if github_repo:
        print(f"    GitHub      {github_repo} {_dim('(private)' if repo_private else '(public)')}")
    print()

    if not yes:
        if not _prompt_bool("Proceed", default=True):
            print(f"\n  {_dim('Aborted.')}")
            raise SystemExit(0)

    # Step 1: Scaffold
    _step(1, total_steps, "Scaffolding files...")
    command_init(env_name, template, directory)
    _success(f"Created {directory}/")

    # Step 2: Create on OpenReward
    _step(2, total_steps, "Creating environment on OpenReward...")
    body: dict = {
        "name": env_name,
        "namespace": namespace,
        "description": description,
        "isPrivate": is_private,
    }
    if is_harbor:
        body["isHarbor"] = True
    result = _api_request("/v1/environments", api_key, method="POST", body=body)
    env_id = result["id"]
    _success(f"{result['owner']}/{result['name']} {_dim(f'(id: {env_id})')}")

    if not github_repo:
        base_url = os.environ.get("OPENREWARD_URL", "https://openreward.ai")
        print(f"\n  {_green('Done!')} {_dim('Next steps:')}")
        print(f"    cd {directory}")
        print(f"    {_dim(base_url + '/' + namespace + '/' + env_name)}")
        return

    # Step 3: Create GitHub repo
    _step(3, total_steps, "Creating GitHub repo...")
    _run_cmd(["git", "init", str(target_dir)], capture=True)
    _run_cmd(["git", "-C", str(target_dir), "add", "."], capture=True)
    _run_cmd(["git", "-C", str(target_dir), "commit", "-m", "Initial scaffold from orwd new"], capture=True)

    gh_args = ["gh", "repo", "create", github_repo, "--source", str(target_dir)]
    gh_args.append("--private" if repo_private else "--public")
    gh_args.append("--push")

    try:
        _run_cmd(gh_args, capture=True)
        _success(f"Created and pushed to {github_repo}")
    except subprocess.CalledProcessError as e:
        _fail("Failed to create GitHub repo")
        if e.stderr:
            print(f"    {_dim(e.stderr.strip())}")
        print(f"    Run manually:")
        print(f"      gh repo create {github_repo} --source {target_dir} --push")
        print(f"      orwd link {namespace}/{env_name} {github_repo}")
        return

    # Step 4: Link
    _step(4, total_steps, "Linking to GitHub...")
    _ensure_github_auth(api_key)

    repos_result = _api_request("/v1/github/repos", api_key)
    if repos_result["status"] != "success":
        _fail(f"Unexpected GitHub repos status: {repos_result['status']}")
        return

    target = github_repo.lower()
    match = None
    for repo in repos_result["repos"]:
        if repo["fullName"].lower() == target:
            match = repo
            break

    if match is None:
        _warn(f"Repo not found in accessible repos — the GitHub App may need to be installed on it.")
        print(f"    Run manually: orwd link {namespace}/{env_name} {github_repo}")
        return

    connect_body = {
        "githubRepoId": match["id"],
        "githubInstallationId": match["installationId"],
        "settings": {
            "cpuMemory": "1-4",
            "minScale": 0,
            "maxScale": 10,
            "concurrency": 500,
        },
    }
    owner_enc = quote(namespace, safe="")
    name_enc = quote(env_name, safe="")
    link_result = _api_request(
        f"/v1/github/environments/{owner_enc}/{name_enc}/connect",
        api_key,
        method="POST",
        body=connect_body,
    )
    _success(f"Linked to {match['fullName']}")

    base_url = os.environ.get("OPENREWARD_URL", "https://openreward.ai")
    print(f"\n  {_green('Done!')} Your environment is deploying.")
    print(f"    cd {directory}")
    print(f"    orwd deployments {namespace}/{env_name}")
    print(f"    {_dim(base_url + '/' + namespace + '/' + env_name)}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orwd", description="OpenReward CLI utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- new ---
    new_parser = subparsers.add_parser(
        "new",
        help="Create a new environment (interactive wizard or one-shot with flags)",
    )
    new_parser.add_argument("name", nargs="?", default=None, help="Environment name")
    new_parser.add_argument("--template", default=None, choices=sorted(TEMPLATES.keys()), help="Scaffold template")
    new_parser.add_argument("--description", default=None, help="Environment description")
    new_parser.add_argument("--private", action="store_true", default=None, dest="is_private", help="Make private")
    new_parser.add_argument("--harbor", action="store_true", default=None, dest="is_harbor", help="Enable harbor mode")
    new_parser.add_argument("--namespace", default=None, help="Owner namespace")
    new_parser.add_argument("--dir", default=None, dest="directory", help="Local directory name")
    new_parser.add_argument("--repo", default=None, dest="github_repo", help="GitHub repo to create (owner/repo)")
    new_parser.add_argument("--no-github", action="store_true", default=False, help="Skip GitHub repo creation")
    new_parser.add_argument("-y", "--yes", action="store_true", default=False, help="Accept all defaults, no prompts")
    new_parser.set_defaults(
        func=lambda args: command_new(
            args.name, args.template, args.description, args.is_private, args.is_harbor,
            args.namespace, args.directory, args.github_repo, args.no_github, args.yes,
        )
    )

    # Shared parent parser for -o/--output flag
    _output_parent = argparse.ArgumentParser(add_help=False)
    _output_parent.add_argument(
        "-o", "--output",
        default="table",
        choices=OUTPUT_FORMATS,
        help="Output format (default: table)",
    )

    # --- whoami ---
    whoami_parser = subparsers.add_parser(
        "whoami",
        parents=[_output_parent],
        help="Show the current authenticated user",
    )
    whoami_parser.set_defaults(func=lambda args: command_whoami(args.output))

    # --- init ---
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize a new environment template",
    )
    init_parser.add_argument("environment", help="Name of the environment to scaffold")
    init_parser.add_argument(
        "--template",
        default="basic",
        choices=sorted(TEMPLATES.keys()),
        help="Scaffold template to use (default: %(default)s)",
    )
    init_parser.add_argument(
        "--dir",
        default=None,
        dest="directory",
        help="Directory to create (default: same as environment name)",
    )
    init_parser.set_defaults(func=lambda args: command_init(args.environment, args.template, args.directory))

    # --- create ---
    create_parser = subparsers.add_parser(
        "create",
        parents=[_output_parent],
        help="Create a new environment on OpenReward (requires OPENREWARD_API_KEY)",
    )
    create_parser.add_argument("name", help="Environment name (3-64 chars, alphanumeric/hyphens/underscores)")
    create_parser.add_argument(
        "--namespace",
        default=None,
        help=(
            "Owner namespace (username or org slug). Defaults to your personal namespace. "
            "Pass your organisation slug here if you want the environment to belong to an org you are a member of."
        ),
    )
    create_parser.add_argument("--description", required=True, help="Short description of the environment")
    create_parser.add_argument("--private", action="store_true", default=False, help="Make the environment private")
    create_parser.add_argument("--harbor", action="store_true", default=False, help="Enable harbor (sandbox) mode")
    create_parser.set_defaults(
        func=lambda args: command_create(args.name, args.namespace, args.description, args.private, args.harbor, args.output)
    )

    # --- list ---
    list_parser = subparsers.add_parser(
        "list",
        parents=[_output_parent],
        help="List environments on OpenReward",
    )
    list_parser.add_argument("--owner", default=None, help="Filter by owner namespace")
    list_parser.add_argument("--mine", action="store_true", default=False, help="List only your own environments")
    list_parser.add_argument("--search", default=None, help="Search by name or description")
    list_parser.add_argument("--limit", type=int, default=None, help="Max results (default: all)")
    list_parser.add_argument("--offset", type=int, default=0, help="Result offset for pagination")
    list_parser.set_defaults(
        func=lambda args: command_list(args.owner, args.mine, args.search, args.limit, args.offset, args.output)
    )

    # --- get ---
    get_parser = subparsers.add_parser(
        "get",
        parents=[_output_parent],
        help="Get details of an environment",
    )
    get_parser.add_argument("env", help="Environment reference (owner/name)")
    get_parser.set_defaults(func=lambda args: command_get(args.env, args.output))

    # --- deployments ---
    deploy_parser = subparsers.add_parser(
        "deployments",
        parents=[_output_parent],
        help="List deployments for an environment",
    )
    deploy_parser.add_argument("env", help="Environment reference (owner/name)")
    deploy_parser.set_defaults(func=lambda args: command_deployments(args.env, args.output))

    # --- logs ---
    logs_parser = subparsers.add_parser(
        "logs",
        parents=[_output_parent],
        help="View logs for an environment deployment",
    )
    logs_parser.add_argument("env", help="Environment reference (owner/name)")
    logs_parser.add_argument("--build", action="store_true", default=False, help="Show build logs instead of runtime logs")
    logs_parser.add_argument("--deployment-id", default=None, help="Specific deployment ID (default: latest)")
    logs_parser.add_argument("--limit", type=int, default=50, help="Number of runtime log entries (default: 50)")
    logs_parser.set_defaults(
        func=lambda args: command_logs(args.env, args.build, args.deployment_id, args.limit, args.output)
    )

    # --- task-builds ---
    task_builds_parser = subparsers.add_parser(
        "task-builds",
        parents=[_output_parent],
        help="List harbor task image builds for a deployment",
    )
    task_builds_parser.add_argument("env", help="Environment reference (owner/name)")
    task_builds_parser.add_argument("--deployment-id", default=None, help="Specific deployment ID (default: latest)")
    task_builds_parser.set_defaults(
        func=lambda args: command_task_builds(args.env, args.deployment_id, args.output)
    )

    # --- task-build-logs ---
    task_build_logs_parser = subparsers.add_parser(
        "task-build-logs",
        parents=[_output_parent],
        help="View build logs for a harbor task image build",
    )
    task_build_logs_parser.add_argument("task_build_id", help="Task build ID")
    task_build_logs_parser.set_defaults(
        func=lambda args: command_task_build_logs(args.task_build_id, args.output)
    )

    # --- link ---
    link_parser = subparsers.add_parser(
        "link",
        parents=[_output_parent],
        help="Link an environment to a GitHub repository",
    )
    link_parser.add_argument("env", help="Environment reference (owner/name)")
    link_parser.add_argument("github_repo", help="GitHub repository (owner/repo)")
    link_parser.add_argument(
        "--cpu-memory",
        default="1:4",
        choices=CPU_MEMORY_CHOICES,
        help="CPU:Memory allocation (default: %(default)s)",
    )
    link_parser.add_argument(
        "--concurrency",
        type=int,
        default=500,
        help="Max concurrent requests (1-10000, default: %(default)s)",
    )
    link_parser.add_argument(
        "--max-scale",
        type=int,
        default=10,
        help="Max instances (0-10, default: %(default)s)",
    )
    link_parser.add_argument(
        "--subdirectory",
        default=None,
        help="Subdirectory in the repo containing the environment",
    )
    link_parser.set_defaults(
        func=lambda args: command_link(
            args.env, args.github_repo, args.cpu_memory, args.concurrency, args.max_scale, args.subdirectory, args.output
        )
    )

    # --- unlink ---
    unlink_parser = subparsers.add_parser(
        "unlink",
        parents=[_output_parent],
        help="Disconnect an environment from its GitHub repository",
    )
    unlink_parser.add_argument("env", help="Environment reference (owner/name)")
    unlink_parser.set_defaults(func=lambda args: command_unlink(args.env, args.output))

    # --- update ---
    update_parser = subparsers.add_parser(
        "update",
        parents=[_output_parent],
        help="Update environment details (name, description, privacy, URLs)",
    )
    update_parser.add_argument("env", help="Environment reference (owner/name)")
    update_parser.add_argument("--name", default=None, dest="new_name", help="New environment name")
    update_parser.add_argument("--description", default=None, help="New description")
    update_parser.add_argument("--private", action="store_true", default=None, dest="set_private", help="Make private")
    update_parser.add_argument("--public", action="store_false", default=None, dest="set_private", help="Make public")
    update_parser.add_argument("--harbor", action="store_true", default=None, dest="set_harbor", help="Enable harbor mode")
    update_parser.add_argument("--no-harbor", action="store_false", default=None, dest="set_harbor", help="Disable harbor mode")
    update_parser.add_argument("--arxiv-url", default=None, help="ArXiv URL (empty string to clear)")
    update_parser.add_argument("--github-url", default=None, help="External GitHub URL (empty string to clear)")
    update_parser.add_argument("--hf-url", default=None, help="HuggingFace URL (empty string to clear)")
    update_parser.add_argument("--max-concurrency", type=int, default=None, help="Max concurrent requests (1-10000)")
    update_parser.add_argument("--max-scale", type=int, default=None, help="Max instances (0-100)")
    update_parser.set_defaults(
        func=lambda args: command_update(
            args.env, args.new_name, args.description, args.set_private, args.set_harbor,
            args.arxiv_url, args.github_url, args.hf_url, args.max_concurrency, args.max_scale, args.output,
        )
    )

    # --- update-link ---
    update_link_parser = subparsers.add_parser(
        "update-link",
        parents=[_output_parent],
        help="Update compute settings for a GitHub-linked environment",
    )
    update_link_parser.add_argument("env", help="Environment reference (owner/name)")
    update_link_parser.add_argument(
        "--cpu-memory",
        default=None,
        choices=CPU_MEMORY_CHOICES,
        help="CPU:Memory allocation",
    )
    update_link_parser.add_argument("--concurrency", type=int, default=None, help="Max concurrent requests (1-10000)")
    update_link_parser.add_argument("--max-scale", type=int, default=None, help="Max instances (0-100)")
    update_link_parser.add_argument("--subdirectory", default=None, help="Subdirectory in the repo (empty string to clear)")
    update_link_parser.set_defaults(
        func=lambda args: command_update_link(
            args.env, args.cpu_memory, args.concurrency, args.max_scale, args.subdirectory, args.output
        )
    )

    # --- upload ---
    upload_parser = subparsers.add_parser(
        "upload",
        parents=[_output_parent],
        help="Upload local files to an environment's file store",
    )
    upload_parser.add_argument("env", help="Environment reference (owner/name)")
    upload_parser.add_argument("files", nargs="+", help="Files or directories to upload")
    upload_parser.add_argument(
        "--dest",
        default=None,
        help="Destination path in the environment (default: relative path from cwd)",
    )
    upload_parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Max parallel uploads (default: %(default)s)",
    )
    upload_parser.set_defaults(
        func=lambda args: command_upload(args.env, args.files, args.dest, args.concurrency, args.output)
    )

    # --- files ---
    files_parser = subparsers.add_parser(
        "files",
        parents=[_output_parent],
        help="List files in an environment's file store",
    )
    files_parser.add_argument("env", help="Environment reference (owner/name)")
    files_parser.add_argument("--prefix", default=None, help="Filter by path prefix")
    files_parser.set_defaults(func=lambda args: command_files(args.env, args.prefix, args.output))

    # --- delete-file ---
    delete_file_parser = subparsers.add_parser(
        "delete-file",
        parents=[_output_parent],
        help="Delete a file or folder from an environment's file store",
    )
    delete_file_parser.add_argument("env", help="Environment reference (owner/name)")
    delete_file_parser.add_argument("path", help="File or folder path to delete")
    delete_file_parser.add_argument("--folder", action="store_true", default=False, help="Delete a folder and all its contents")
    delete_file_parser.set_defaults(
        func=lambda args: command_delete_file(args.env, args.path, args.folder, args.output)
    )

    # --- runs ---
    runs_parser = subparsers.add_parser(
        "runs",
        parents=[_output_parent],
        help="List runs",
    )
    runs_parser.add_argument("--search", default=None, help="Search by name")
    runs_parser.add_argument("--limit", type=int, default=20, help="Max results")
    runs_parser.add_argument("--offset", type=int, default=0, help="Result offset for pagination")
    runs_parser.set_defaults(func=lambda args: command_runs(args.search, args.limit, args.offset, args.output))

    # --- run ---
    run_parser = subparsers.add_parser(
        "run",
        parents=[_output_parent],
        help="Get details of a run",
    )
    run_parser.add_argument("run_id", help="Run ID")
    run_parser.set_defaults(func=lambda args: command_run(args.run_id, args.output))

    # --- rollouts ---
    rollouts_parser = subparsers.add_parser(
        "rollouts",
        parents=[_output_parent],
        help="List rollouts in a run",
    )
    rollouts_parser.add_argument("run_id", help="Run ID")
    rollouts_parser.add_argument("--limit", type=int, default=20, help="Max results")
    rollouts_parser.add_argument("--offset", type=int, default=0, help="Result offset for pagination")
    rollouts_parser.set_defaults(func=lambda args: command_rollouts(args.run_id, args.limit, args.offset, args.output))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print(f"\n  {_dim('Interrupted.')}")
        return 130
    except FileExistsError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
