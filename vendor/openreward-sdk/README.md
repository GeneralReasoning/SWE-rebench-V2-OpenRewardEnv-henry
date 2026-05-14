# OpenReward Python SDK

[![PyPI version](https://img.shields.io/pypi/v/openreward)](https://pypi.org/project/openreward/)
[![Python 3.11+](https://img.shields.io/badge/python-%3E=3.11-green)](https://pypi.org/project/openreward/)
[![Docs](https://img.shields.io/badge/docs-openreward.ai-blue)](https://docs.openreward.ai)

The official Python SDK for [OpenReward](https://openreward.ai) — a platform for building, hosting, and training on RL environments for language models.

The SDK has two complementary roles:

- **Build environments** — define evaluation tasks, expose tools, and serve them via a standards-compliant API that can be deployed on the OpenReward platform.
- **Train agents** — connect to any environment (local or hosted), run agent loops, and log rollouts with rewards back to OpenReward.

## Installation

```bash
pip install openreward
```

For environments that process documents (PDF, DOCX, Excel, PowerPoint):

```bash
pip install "openreward[tools]"
```

Requires Python 3.11+.

## Core concepts

### Environment

An `Environment` subclass defines a benchmark or task distribution. Implement three required methods:

| Method | Purpose |
|---|---|
| `list_splits()` | Return split names, e.g. `["train", "test"]` |
| `list_tasks(split)` | Return a deterministically ordered list of task dicts |
| `get_prompt()` | Return the task instructions as a list of `TextBlock` / `ImageBlock` |

Actions are defined as `async` methods decorated with `@tool`. Each tool receives a Pydantic model as input and returns a `ToolOutput`.

### ToolOutput

Every tool returns a `ToolOutput` containing:

- `blocks` — a list of `TextBlock` or `ImageBlock` results
- `reward` — optional float reward signal
- `finished` — whether the episode is complete
- `metadata` — optional arbitrary metadata

### Server

`Server` wraps one or more `Environment` classes in a FastAPI app and exposes the [Open Reward Standard](https://docs.openreward.ai) API over HTTP with SSE streaming.

Key endpoints:

| Endpoint | Description |
|---|---|
| `POST /create` | Spawn a new environment session |
| `POST /{env}/call` | Execute a tool (streamed via SSE) |
| `GET /{env}/prompt` | Get the current task prompt |
| `GET /{env}/tools` | List available tools |
| `POST /{env}/tasks` | List all tasks for a split |

### Sandboxes

Environments that need isolated compute (e.g. code execution) can spin up Docker containers via the sandbox API using `SandboxSettings`. Containers are managed automatically — started in `setup()` and torn down in `teardown()`.

### Toolsets

Group reusable tools into `Toolset` classes and compose them across environments via the `toolsets` class attribute.

### Rollout logging

Log agent trajectories with reward signals back to OpenReward for analysis and training. The client's `rollout` API supports normalized message types as well as raw outputs from Anthropic, OpenAI, and Google GenAI SDKs.

## CLI

The `orwd` CLI helps you scaffold and create environments.

### Scaffold a new environment locally

```bash
# Minimal environment
orwd init my-env

# Environment with a Docker sandbox for code execution
orwd init my-env --template sandbox
```

### Create an environment on OpenReward

Registers a new environment under your account (requires `OPENREWARD_API_KEY`):

```bash
orwd create my-env --description "A short description of my environment"
```

By default the environment is created under your personal namespace. To create it under an organisation you are a member of, pass `--namespace`:

```bash
orwd create my-env --description "A short description" --namespace my-org
```

Pass `--private` to make the environment private:

```bash
orwd create my-env --description "A short description" --private
```

## Deploying to OpenReward

1. Push your environment to a GitHub repository.
2. Connect the repository in the [OpenReward dashboard](https://openreward.ai).
3. Configure compute resources (CPU, memory, scaling).
4. Every push to the connected branch triggers an automatic build and deployment.

Your environment is then accessible to any agent via the OpenReward API using the `username/environment-name` namespace.

## Environment variables

| Variable | Description |
|---|---|
| `OPENREWARD_API_KEY` | API key for authentication |
| `OPENREWARD_URL` | Override base URL (default: `https://openreward.ai`) |
| `OPENREWARD_USE_STRUCTURED_LOGS` | Set to `1` for JSON logging (recommended in production) |
| `OPENREWARD_ROLLOUT_LOGGING_FORMAT` | `pretty` or `structured` for rollout log output |

## Documentation

Full documentation, guides, and examples are at **[docs.openreward.ai](https://docs.openreward.ai)**.

## License

Apache 2.0
