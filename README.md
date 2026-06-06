# 🚀 autopr-agent

> **Autonomous multi-agent orchestrator that reads your plan and ships code — writing, testing, reviewing, and merging PRs without you lifting a finger.**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python)](https://www.python.org/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Build Status](https://img.shields.io/github/actions/workflow/status/your-org/autopr-agent/ci.yml?branch=main)](https://github.com/your-org/autopr-agent/actions)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

---

## What is autopr-agent?

`autopr-agent` (also called the **Wheel-Spoke Orchestrator**) is a Python-based autonomous pipeline that takes a structured Markdown task plan and drives it to completion — fully unattended. Point it at a plan file, walk away, and come back to merged PRs.

It uses a **hub-and-spoke architecture** where a central orchestrator coordinates a team of specialized AI agents: one writes the code, one builds the tests, one reviews the diff, and one handles all the Git and GitHub operations. Each agent can be powered by any AI provider — Google Gemini, OpenAI, a local Ollama model, or any OpenAI-compatible endpoint.

---

## ✨ Key Features

| Feature | Details |
|---|---|
| **Fully autonomous** | Writes code → opens PR → runs tests → reviews → merges. Zero human steps. |
| **Any AI provider** | Google Gemini, OpenAI, Ollama (local/free), or any OpenAI-compatible endpoint |
| **Strict TDD Mode** | Optional `--tdd` flag enforces the Red-Green-Refactor loop natively |
| **Smart retry loops** | 5 retries per task; lint failures and review feedback auto-feed back into the code builder |
| **Human-in-the-loop** | Add `[BLOCKED]` to a task title to safely pause the orchestrator for manual QA or UI work |
| **Env vs. bug detection** | Distinguishes broken environment (missing `rollup`, bad `node_modules`) from real bugs; runs `npm install` automatically |
| **Transient fault tolerance** | WebSocket drops, 503s, rate-limit errors are retried with exponential backoff |
| **Resumable state** | JSON state file persisted after every action — crash and re-run to pick up exactly where you left off |
| **Skip-on-failure** | After 5 retries, interactively `[S]kip` a task and continue to the next |
| **Parallel execution** | Run two orchestrators in separate terminals targeting different repos simultaneously |
| **Jules integration** | Optionally delegates test generation to Google Jules AI (with LLM fallback) |

---

## Architecture: The Wheel and Spoke

```
                        ┌─────────────────────────────────┐
                        │        📋 PLAN.md               │
                        │  ### Task 1: Add feature X      │
                        │  ### Task 2: Refactor Y         │
                        └──────────────┬──────────────────┘
                                       │
                                       ▼
                        ┌─────────────────────────────────┐
                        │     🎯 Orchestrator Hub         │
                        │  - Reads & parses plan file     │
                        │  - Manages state (JSON)         │
                        │  - Drives the task loop         │
                        │  - Routes to spokes             │
                        └──┬──────┬──────┬──────┬─────────┘
                           │      │      │      │
             ┌─────────────┘      │      │      └─────────────┐
             │                    │      │                     │
             ▼                    ▼      ▼                     ▼
  ┌──────────────────┐  ┌────────────────────┐  ┌───────────────────┐  ┌──────────────────┐
  │  🧠 Code Builder │  │  🔬 Test Runner    │  │ 🛠️  PR Builder    │  │ 👁️ Code Reviewer │
  │                  │  │                    │  │                   │  │                  │
  │ LLM writes the   │  │ npm run lint       │  │ git branch        │  │ Reviews PR diff  │
  │ actual code diff │  │ npm test           │  │ git commit/push   │  │ against task spec│
  │                  │  │                    │  │ gh pr create      │  │ Returns APPROVED │
  │ Default:         │  │ Reports failures   │  │ gh pr merge       │  │ or feedback      │
  │ Gemini 2.5 Flash │  │ back to builder    │  │                   │  │                  │
  └──────────────────┘  └────────────────────┘  └───────────────────┘  └──────────────────┘
             │
             ▼
  ┌──────────────────┐
  │ 🧪 Test Builder  │
  │                  │
  │ Jules AI (opt.)  │
  │ or LLM fallback  │
  │ generates unit   │
  │ tests            │
  └──────────────────┘
```

### The Task Loop

For each task in your plan, the orchestrator runs this loop:

```
┌──────────────────────────────────────────────────────────────┐
│                         TASK LOOP                            │
│                                                              │
│  1. Code Builder  ──► writes code changes                    │
│         │                                                    │
│  2. Test Builder  ──► generates unit tests                   │
│         │                                                    │
│  3. PR Builder    ──► branch → commit → push → open PR       │
│         │                                                    │
│  4. Test Runner   ──► npm run lint && npm test               │
│         │                                                    │
│         ├── FAIL ──► feedback → Code Builder (retry)         │
│         │                                                    │
│  5. Code Reviewer ──► reviews diff vs. task spec             │
│         │                                                    │
│         ├── FEEDBACK ──► Code Builder (retry, max 5)         │
│         │                                                    │
│         └── APPROVED ──► PR Builder merges → mark COMPLETED  │
└──────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### 1. Prerequisites

```bash
# Python 3.10+
python3 --version

# GitHub CLI — authenticated
gh auth login

# git (obviously)
git --version

# npm (for Node.js/TypeScript projects)
npm --version
```

### 2. Install

```bash
git clone https://github.com/your-org/autopr-agent.git
cd autopr-agent

# Install Python dependencies
pip install -r requirements.txt

# For Google Gemini support
pip install google-antigravity
```

### 3. Configure

```bash
cp config.example.json config.json
```

Edit `config.json` and fill in your provider credentials (see [Provider Configuration](#provider-configuration) below).

You can also use a `.env` file or environment variables — they take precedence over `config.json`.

### 4. Write your plan

Create a Markdown plan file (see [Plan File Format](#plan-file-format) for the full spec):

```markdown
# My Feature Plan

### Task 1: Add input validation to the login form

Validate that the email field is a valid email address and the password field
is at least 8 characters. Show inline error messages. Add unit tests.

### Task 2: Refactor the auth service to use async/await

Replace Promise chains in `src/services/auth.ts` with async/await. Maintain
the same public API. Do not change any tests.
```

### 5. Run

```bash
python3 wheel_spoke_orchestrator.py --plan /path/to/PLAN.md
```

That's it. The orchestrator will now autonomously execute every task, creating branches, PRs, running tests, reviewing code, and merging — printing progress to your terminal as it goes.

---

## Provider Configuration

### Google Gemini (recommended)

Gemini 2.5 Flash/Pro via the Google Antigravity SDK. This provider can read real files from disk using built-in filesystem tools, which improves code quality.

```json
{
  "provider": "google",
  "model": "gemini-2.5-flash",
  "api_key": "YOUR_GEMINI_API_KEY"
}
```

Or set `GEMINI_API_KEY` in your environment / `.env` file.

---

### OpenAI

```json
{
  "provider": "openai",
  "model": "gpt-4o",
  "api_key": "YOUR_OPENAI_API_KEY"
}
```

Supported models: `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`, `o1-mini`, etc.

---

### Ollama (local — zero API cost)

No API key required. Requires [Ollama](https://ollama.ai) running locally.

```json
{
  "provider": "ollama",
  "model": "qwen2.5-coder:32b",
  "base_url": "http://localhost:11434"
}
```

Recommended local models for coding tasks:

| Model | Size | Notes |
|---|---|---|
| `qwen2.5-coder:32b` | ~20 GB | Best quality local coding model |
| `deepseek-r1:14b` | ~9 GB | Good reasoning, smaller footprint |
| `gemma3:27b` | ~17 GB | Google's open model |
| `codellama:13b` | ~8 GB | Lighter option for fast machines |

---

### OpenAI-Compatible Endpoints

Works with Together AI, Fireworks, DeepSeek, LiteLLM, and any server that implements the OpenAI REST API spec.

```json
{
  "provider": "openai_compatible",
  "model": "deepseek/deepseek-r1",
  "api_key": "YOUR_API_KEY",
  "base_url": "https://api.together.xyz/v1"
}
```

---

### Provider Comparison

| Provider | Cost | Latency | Filesystem Access | Requires API Key |
|---|---|---|---|---|
| `google` (Gemini) | ~$0.10–0.40/task | Fast | ✅ Yes | ✅ Yes |
| `openai` (GPT-4o) | ~$0.20–0.60/task | Fast | ❌ No | ✅ Yes |
| `ollama` (local) | Free | Slow (hardware-bound) | ❌ No | ❌ No |
| `openai_compatible` | Varies | Varies | ❌ No | Usually yes |

---

## Plan File Format

Plans are standard Markdown files. The orchestrator parses `### Task N: Title` headers.

```markdown
# Sprint Plan — Week 24

### Task 1: Add dark mode toggle

Add a dark mode toggle button to the navigation bar. Use CSS custom properties
for theming. Persist the user's preference in localStorage. Update all affected
component stylesheets.

Acceptance criteria:
- Toggle appears in the nav bar
- Theme persists across page reloads
- All existing tests pass

### Task 2: Fix pagination bug on the dashboard [COMPLETED]

<!-- Already done — orchestrator will skip this -->

### Task 3: Add rate limiting to the API

Implement per-user rate limiting on all POST endpoints. Use a sliding window
algorithm. Return 429 Too Many Requests with a Retry-After header.
```

**Rules:**
- Headers must follow the pattern `### Task N: Title` (N is an integer)
- Tasks marked `[COMPLETED]` or `[SKIPPED]` in the header are skipped automatically
- Tasks marked `[BLOCKED]` will safely halt the orchestrator so a human can intervene
- Tasks marked `[IN PROGRESS]` will seamlessly resume execution from the local `state.json`
- When a task finishes, the orchestrator appends `[COMPLETED]` to the header and commits the updated plan to GitHub
- Task body is passed verbatim as context to every spoke agent — be descriptive

---

## CLI Reference

```
python3 wheel_spoke_orchestrator.py [OPTIONS]

Required:
  --plan PATH              Path to your Markdown plan file

Optional:
  --repo OWNER/REPO        GitHub repository (auto-detected from git remote if omitted)
  --merge-method METHOD    PR merge strategy: merge | squash | rebase (default: merge)
  --start-task N           Skip to task number N (useful for resuming mid-plan)
  --jules-handle HANDLE    Jules bot GitHub handle (default: @jules)
  --tdd                    Enable strict Test-Driven Development mode (Red-Green-Refactor)
  --dry-run                Simulate the full pipeline without real API calls or git mutations

Examples:
  # Basic run
  python3 wheel_spoke_orchestrator.py --plan ~/plans/SPRINT-24.md

  # Start from task 3 (resume after a crash on task 3)
  python3 wheel_spoke_orchestrator.py --plan ~/plans/SPRINT-24.md --start-task 3

  # Use squash merges and specify repo explicitly
  python3 wheel_spoke_orchestrator.py \
    --plan ~/plans/SPRINT-24.md \
    --repo my-org/my-repo \
    --merge-method squash

  # Dry run — safe to test against your plan without side effects
  python3 wheel_spoke_orchestrator.py --plan ~/plans/SPRINT-24.md --dry-run
```

---

## How Resumability Works

The orchestrator writes a `state.json` file next to your plan after every significant action:

```
📁 ~/plans/
├── SPRINT-24.md          ← your plan (updated in place with [COMPLETED])
└── SPRINT-24.state.json  ← persisted state (current task, retry count, PR URLs, ...)
```

If the process crashes or you kill it, simply re-run the same command. The orchestrator reads `state.json`, determines which task was in progress, and resumes from the last safe checkpoint. Partially applied code changes are detected and the code builder is prompted to retry cleanly.

---

## Running Two Plans in Parallel

Each orchestrator instance is stateless with respect to other instances. You can run two plans simultaneously against different repos from different terminals:

```bash
# Terminal 1 — backend repo
python3 wheel_spoke_orchestrator.py --plan ~/plans/backend-sprint.md --repo my-org/backend

# Terminal 2 — frontend repo
python3 wheel_spoke_orchestrator.py --plan ~/plans/frontend-sprint.md --repo my-org/frontend
```

> ⚠️ Running two orchestrators against the **same repo** concurrently is not supported and will cause branch/PR conflicts.

---

## Output Guide

| Emoji | Meaning |
|---|---|
| `📋` | Reading / parsing plan file |
| `🎯` | Orchestrator starting a new task |
| `🧠` | Code Builder is generating code |
| `🧪` | Test Builder is generating tests |
| `🔬` | Test Runner is running lint / tests |
| `🛠️` | PR Builder performing git / GitHub operations |
| `👁️` | Code Reviewer is reviewing the PR diff |
| `✅` | Task completed successfully |
| `🔁` | Retry attempt (with retry number) |
| `⚠️` | Non-fatal warning (env issue, transient error) |
| `❌` | Fatal error or max retries exceeded |
| `⏭️` | Task skipped (`[SKIPPED]` or user skip) |
| `💤` | Waiting / exponential backoff before retry |
| `🎉` | All tasks complete — plan finished |

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `gh: command not found` | GitHub CLI not installed | Install from [cli.github.com](https://cli.github.com) and run `gh auth login` |
| `Error: authentication required` | `gh` not authenticated | Run `gh auth login` |
| `npm test` always fails | Broken `node_modules` | Run `npm install` in your project root manually, then re-run |
| `RollupError: Cannot find module` | Missing native rollup binding | Run `npm install` — the orchestrator may already do this automatically |
| API key errors (Gemini/OpenAI) | Missing or wrong key | Check `config.json` or set the appropriate environment variable |
| Code Builder produces empty diffs | Model doesn't understand the task | Add more detail / acceptance criteria to the task description in your plan |
| Orchestrator keeps retrying same error | Bug in feedback loop | Check `state.json` for the last error, then `[S]kip` the task manually |
| `Jules` times out on every task | Jules not set up or repo not linked | Set `"jules_enabled": false` in `config.json` to fall back to LLM test generation |
| PR merge fails with conflicts | Two concurrent tasks touched same file | Resolve manually, close the PR, and use `--start-task N` to retry |
| `ModuleNotFoundError: google.antigravity` | SDK not installed | Run `pip install google-antigravity` |
| Rate limit errors (429) | Quota exceeded | Switch provider/model, or add `"rate_limit_delay_seconds": 10` to `config.json` |

---

## Configuration Reference (`config.json`)

```jsonc
{
  // AI Provider: "google" | "openai" | "ollama" | "openai_compatible"
  "provider": "google",

  // Model name — provider-specific
  "model": "gemini-2.5-flash",

  // API key (can also be set via env var: GEMINI_API_KEY, OPENAI_API_KEY, etc.)
  "api_key": "YOUR_API_KEY",

  // Base URL — required for "ollama" and "openai_compatible"
  "base_url": "http://localhost:11434",

  // PR merge strategy: "merge" | "squash" | "rebase"
  "merge_method": "merge",

  // Maximum retries per task before giving up / prompting to skip
  "max_retries": 5,

  // Enable Google Jules AI for test generation (falls back to LLM if unavailable)
  "jules_enabled": true,
  "jules_handle": "@jules",

  // Delay (seconds) added between retries to respect rate limits
  "rate_limit_delay_seconds": 2,

  // Dry run: simulate without real API calls or git mutations
  "dry_run": false
}
```

---

## Why Build This?

Most AI coding tools are interactive — they wait for you to review and approve each step. That's great for exploratory work, but it doesn't scale when you have a backlog of 20 well-defined tasks and you'd rather not babysit a terminal all day.

More importantly, it solves the problem of **AI service outages and credit exhaustion**. We call this **"Tokenmaxxing"** (optimizing the yield of your AI credits). By abstracting the roles into a Wheel-and-Spoke model, the orchestrator routes tasks intelligently:
- Expensive/high-quota agents (like Google Jules or GPT-4o) are used *only* for the hardest tasks (like generating test coverage).
- Cheap/limitless models (like Gemini 2.5 Flash) handle the bulk code building.
- Free local models (like Ollama) act as fallback safety nets when cloud providers drop or rate limits are hit.

If an API drops, `autopr-agent` doesn't crash your sprint — it gracefully falls back to the next provider, ensuring your code gets shipped even during outages.

**This is different from interactive pair-programming tools.** It's closer to hiring a resilient contractor, handing them a spec, and checking back when they're done.

---

## Comparison with Similar Tools

| | autopr-agent | swarm-forge | Devin | GitHub Copilot Workspace |
|---|---|---|---|---|
| Fully autonomous (no human in loop) | ✅ | ❌ (needs watching) | ✅ | ❌ |
| Git/PR/merge automation | ✅ | ❌ | ✅ | Partial |
| Retry loop with lint/review feedback | ✅ | ❌ | ✅ | ❌ |
| Local/offline model support | ✅ (Ollama) | ❌ | ❌ | ❌ |
| Any OpenAI-compatible provider | ✅ | ❌ | ❌ | ❌ |
| Resumable after crash | ✅ | ❌ | ❌ | ❌ |
| Open source | ✅ | ✅ | ❌ | ❌ |
| Cost | API cost only | API cost only | Subscription | Subscription |

---

## Contributing

Contributions are very welcome! Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) before opening a PR.

- **Bug reports**: Open a GitHub Issue with the relevant section of your terminal output and `state.json`
- **Feature requests**: Open a Discussion first — describe the use case, not the implementation
- **New provider support**: Add a new provider class in `providers/` following the existing pattern and open a PR

---

## Roadmap

- [ ] Web UI dashboard to visualize plan progress in real time
- [ ] Support for Python projects (pytest runner)
- [ ] Multi-repo plans (one plan, multiple target repos)
- [ ] Webhook server mode — trigger from GitHub Issues or Linear
- [ ] Cost tracking and per-task token usage report
- [ ] Plan generation from GitHub Issues / Linear tickets

---

## License

Apache License 2.0 © autopr-agent contributors

See [`LICENSE`](LICENSE) for the full text.

---

<p align="center">
  Built with 🤖 by developers who got tired of reviewing their own PRs.
</p>
