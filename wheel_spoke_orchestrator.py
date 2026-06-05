#!/usr/bin/env python3
"""
Wheel-and-Spoke Agent Orchestrator
Uses the Google Antigravity SDK and urllib OpenAI fallbacks.
Coordinates specialized agents (Code Builder, Test Builder, Test Runner, PR Builder, Code Reviewer)
to complete plan tasks autonomously with bi-directional recovery loops.
"""

import os
import sys
import re
import json
import time
import argparse
import subprocess
import urllib.request
import urllib.error
import webbrowser
import asyncio

# --- Bootstrap API keys BEFORE importing the Antigravity SDK ---
# The SDK reads GEMINI_API_KEY at session creation time, so it must be set
# in os.environ before the first Agent() call. We pull it from config.json
# and any local .env file as early as possible.
def _bootstrap_api_keys():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # 1. Load .env from script directory
    for env_path in [os.path.join(script_dir, ".env")]:
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    # 2. Load from config.json
    cfg_path = os.path.join(script_dir, "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r") as f:
                cfg = json.load(f)
            if cfg.get("gemini_api_key"):
                os.environ["GEMINI_API_KEY"] = cfg["gemini_api_key"]
            if cfg.get("openai_api_key"):
                os.environ["OPENAI_API_KEY"] = cfg["openai_api_key"]
        except Exception:
            pass

_bootstrap_api_keys()

from google.antigravity import Agent, LocalAgentConfig

# --- Configuration ---
POLL_INTERVAL = 60  # seconds
DEFAULT_CONFIG = {
    "code_builder": {
        "model": "gemini-2.5-flash",
        "provider": "google",
        "fallback": {"model": "gemini-2.5-flash", "provider": "google"}
    },
    "test_builder": {"model": "jules", "provider": "google"},
    "code_reviewer": {"model": "gemini-2.5-flash", "provider": "google"}
}

def load_config(config_path="config.json"):
    """Loads configuration from config.json, falling back to default config if missing."""
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Failed to parse {config_path}: {e}. Using defaults.")
    return DEFAULT_CONFIG

# --- Helper Functions ---
def send_notification(title, message):
    """Sends a macOS desktop notification using osascript."""
    print(f"📣 [NOTIFICATION] {title}: {message}")
    try:
        escaped_title = title.replace('"', '\\"')
        escaped_message = message.replace('"', '\\"')
        apple_script = f'display notification "{escaped_message}" with title "{escaped_title}"'
        subprocess.run(["osascript", "-e", apple_script], capture_output=True)
    except:
        pass

def run_cmd(args, stdin_input=None, cwd=None, env=None, timeout=None):
    """Runs a shell command and returns (stdout, stderr, returncode)."""
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    run_env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"

    try:
        res = subprocess.run(
            args,
            input=stdin_input,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=run_env,
            timeout=timeout
        )
        return res.stdout, res.stderr, res.returncode
    except subprocess.TimeoutExpired:
        return "", f"Command timed out after {timeout} seconds", -2
    except Exception as e:
        return "", str(e), -1

def strip_ansi(text):
    """Strips ANSI escape sequences from a string."""
    ansi_escape = re.compile(r'(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]')
    return ansi_escape.sub('', text)

def get_git_repo(plan_dir):
    """Detects the GitHub repository name (owner/repo) relative to the plan file directory."""
    stdout, _, code = run_cmd(["git", "remote", "get-url", "origin"], cwd=plan_dir)
    if code != 0 or not stdout.strip():
        return None
    url = stdout.strip()
    match = re.search(r'github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$', url)
    if match:
        return match.group(1)
    return None

# ─── AI Provider Abstraction ─────────────────────────────────────────────────
# Add a new provider by subclassing AIProvider and registering it in AIProvider.create().
# No other code changes needed — just update config.json.

class AIProvider:
    """Abstract base for all AI completion providers."""

    def __init__(self, config: dict):
        self.model       = config.get("model", "gemini-2.5-flash")
        self.temperature = float(config.get("temperature", 0.2))
        self.max_tokens  = int(config.get("max_tokens", 8192))
        self.timeout     = int(config.get("timeout", 120))

    async def complete(self, prompt: str,
                       system_instruction: str = None,
                       cwd: str = None) -> str:
        """Returns completion text. Raises on failure (caught by retry wrapper)."""
        raise NotImplementedError

    @staticmethod
    def create(config: dict) -> "AIProvider":
        """Factory — pick the right provider from a config dict."""
        p = config.get("provider", "google")
        if p == "google":
            return GoogleSDKProvider(config)
        if p == "openai":
            return OpenAIRESTProvider(config)
        if p in ("openai_compatible", "deepseek"):
            return OpenAICompatibleProvider(config)
        if p == "ollama":
            return OllamaProvider(config)
        raise ValueError(
            f"Unknown provider: '{p}'. Supported: google, openai, openai_compatible, ollama"
        )


class GoogleSDKProvider(AIProvider):
    """Gemini via the Antigravity Agent SDK.
    The Agent has built-in filesystem tools; os.chdir(cwd) makes them resolve
    paths against the target project rather than the orchestrator directory.
    """

    async def complete(self, prompt: str,
                       system_instruction: str = None,
                       cwd: str = None) -> str:
        old_cwd = os.getcwd()
        if cwd and os.path.isdir(cwd):
            os.chdir(cwd)
        try:
            cfg = LocalAgentConfig(
                model=self.model,
                system_instructions=system_instruction,
                generation_config={"temperature": self.temperature},
            )
            async with Agent(cfg) as agent:
                res = await agent.chat(prompt)
                return await res.text()
        finally:
            os.chdir(old_cwd)


class OpenAIRESTProvider(AIProvider):
    """OpenAI REST API — GPT-4o, GPT-4o-mini, o1, o3, etc."""

    def __init__(self, config: dict):
        super().__init__(config)
        key_env = config.get("api_key_env", "OPENAI_API_KEY")
        self.api_key  = os.environ.get(key_env, "")
        self.base_url = config.get("base_url", "https://api.openai.com/v1").rstrip("/")

    async def _post_chat(self, url: str, api_key: str,
                         prompt: str, system_instruction: str) -> str:
        messages = []
        if system_instruction:
            sys_role = "developer" if ("o1" in self.model or "o3" in self.model) else "system"
            messages.append({"role": sys_role, "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        data    = {"model": self.model, "messages": messages}
        if "o1" not in self.model and "o3" not in self.model:
            data["temperature"] = self.temperature
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {api_key}"}
        req     = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers)
        loop    = asyncio.get_running_loop()
        def _do():
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        res = await loop.run_in_executor(None, _do)
        return res["choices"][0]["message"]["content"].strip()

    async def complete(self, prompt: str,
                       system_instruction: str = None,
                       cwd: str = None) -> str:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY not set (or api_key_env misconfigured)")
        return await self._post_chat(
            f"{self.base_url}/chat/completions",
            self.api_key, prompt, system_instruction
        )


class OpenAICompatibleProvider(OpenAIRESTProvider):
    """Any OpenAI-compatible REST endpoint.
    Examples: Together AI, Fireworks, DeepSeek, LiteLLM proxy, Azure OpenAI.

    config example:
      { "provider": "openai_compatible",
        "model":    "deepseek-coder-v2",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY" }
    """

    async def complete(self, prompt: str,
                       system_instruction: str = None,
                       cwd: str = None) -> str:
        if not self.base_url:
            raise ValueError("base_url is required for openai_compatible provider")
        return await self._post_chat(
            f"{self.base_url}/chat/completions",
            self.api_key, prompt, system_instruction
        )


class OllamaProvider(AIProvider):
    """Local Ollama models — no API key required.

    Recommended models from the user's install:
      Fast  : qwen2.5-coder:7b            (4.7 GB — best for quick tasks)
      Medium: qwen3-coder:30b / gemma4:26b (18 GB — better quality)
      Heavy : qwen2.5-coder:32b-instruct  (19 GB — highest quality, slowest)
              deepseek-r1:32b             (19 GB — strong reasoning)

    config example:
      { "provider": "ollama",
        "model":    "qwen2.5-coder:7b",
        "base_url": "http://localhost:11434",
        "timeout":  300 }
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.base_url = config.get("base_url", "http://localhost:11434").rstrip("/")
        # Ollama can be slow on large models — use a longer default timeout
        self.timeout  = int(config.get("timeout", 300))

    async def complete(self, prompt: str,
                       system_instruction: str = None,
                       cwd: str = None) -> str:
        url      = f"{self.base_url}/api/chat"
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        data = {
            "model":    self.model,
            "messages": messages,
            "stream":   False,
            "options":  {"temperature": self.temperature},
        }
        req  = urllib.request.Request(
            url, data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}
        )
        loop = asyncio.get_running_loop()
        def _do():
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        res = await loop.run_in_executor(None, _do)
        return res["message"]["content"].strip()


# ─── Retry wrapper ────────────────────────────────────────────────────────────
# Transient error signals that warrant a retry
_TRANSIENT_ERROR_PATTERNS = [
    "ws close code 1006", "context canceled", "harness process exited",
    "connection reset", "eof", "timeout", "503", "502", "429",
    "rate limit", "service unavailable", "temporary", "try again",
]

async def call_model_api(provider, model, prompt, system_instruction=None,
                         temperature=0.2, cwd=None, max_retries=3):
    """Thin retry wrapper around AIProvider.create().complete().

    Automatically retries transient network/API failures with exponential backoff.
    All spoke classes call this function — they don't need to know about AIProvider.
    """
    last_error = None
    for attempt in range(max_retries):
        result, last_error = await _call_model_api_once(
            provider, model, prompt, system_instruction, temperature, cwd
        )
        if result is not None:
            return result
        err_lower = str(last_error).lower()
        is_transient = any(p in err_lower for p in _TRANSIENT_ERROR_PATTERNS)
        if not is_transient or attempt == max_retries - 1:
            break
        wait = 5 * (2 ** attempt)  # 5s → 10s → 20s
        print(f"⚠️ Transient API error (attempt {attempt+1}/{max_retries}). Retrying in {wait}s...")
        await asyncio.sleep(wait)
    print(f"❌ API call failed after {max_retries} attempts: {last_error}")
    return None


async def _call_model_api_once(provider, model, prompt, system_instruction, temperature, cwd):
    """Single attempt via AIProvider. Returns (result_text, None) or (None, exception)."""
    try:
        cfg      = {"provider": provider, "model": model, "temperature": temperature}
        p        = AIProvider.create(cfg)
        result   = await p.complete(prompt, system_instruction=system_instruction, cwd=cwd)
        return result, None
    except Exception as e:
        return None, e


# --- Spoke: PR Builder (Git / PR Operations) ---
# --- Spoke: PR Builder (Git / PR Operations) ---
class PRBuilderSpoke:
    def __init__(self, repo, plan_dir, dry_run=False):
        self.repo = repo
        self.plan_dir = plan_dir
        self.dry_run = dry_run

    def get_default_branch(self):
        default_branch = "main"
        stdout_ref, _, _ = run_cmd(["git", "show-ref", "--verify", "refs/heads/master"], cwd=self.plan_dir)
        if stdout_ref.strip():
            default_branch = "master"
        return default_branch

    def checkout_branch(self, branch_name, create=False):
        if self.dry_run:
            print(f"[DRY-RUN] Git: Checking out branch '{branch_name}' (create={create}).")
            return
        print(f"🔄 Git: Checking out branch '{branch_name}'...")
        if create:
            # Get latest from default branch
            default_branch = self.get_default_branch()
            run_cmd(["git", "checkout", default_branch], cwd=self.plan_dir)
            run_cmd(["git", "pull", "origin", default_branch], cwd=self.plan_dir)
            run_cmd(["git", "checkout", "-b", branch_name], cwd=self.plan_dir)
        else:
            run_cmd(["git", "checkout", branch_name], cwd=self.plan_dir)
            run_cmd(["git", "pull", "origin", branch_name], cwd=self.plan_dir)

    def find_pr_for_task(self, task_number):
        """Searches GitHub for the open PR corresponding to this task number."""
        if self.dry_run:
            print(f"[DRY-RUN] GitHub: Searching for open PR for Task {task_number}.")
            return None, None
        stdout, _, code = run_cmd([
            "gh", "pr", "list", "--repo", self.repo, "--state", "open", "--json", "number,title,headRefName"
        ], cwd=self.plan_dir)
        if code == 0:
            try:
                prs = json.loads(stdout)
                for pr in prs:
                    title = pr.get("title", "")
                    if re.search(rf'\bTask\s+0*{task_number}\b', title, re.IGNORECASE):
                        print(f"📦 Found PR #{pr['number']} for Task {task_number}")
                        return pr["number"], pr["headRefName"]
            except Exception as e:
                print(f"⚠️ Error parsing PR JSON: {e}")
        return None, None

    def apply_patch_and_push(self, session_id, pr_branch, commit_msg):
        """Applies Jules session patch, commits with no-verify, and pushes with no-verify."""
        if self.dry_run:
            print(f"[DRY-RUN] Git: Applying patch from session {session_id} to branch '{pr_branch}' and pushing with message '{commit_msg}'.")
            return True
        print(f"📥 Pulling and applying patch from Jules session {session_id}...")
        stdout_pull, stderr_pull, code_pull = run_cmd([
            "jules", "remote", "pull", "--session", session_id, "--apply"
        ], cwd=self.plan_dir)
        
        combined_pull = (stdout_pull + "\n" + stderr_pull).lower()
        patch_failed = "failed to apply patch" in combined_pull or "patch does not apply" in combined_pull
        
        force_push = False
        
        if code_pull != 0 or patch_failed:
            print(f"⚠️ Direct patch application failed. Resetting branch '{pr_branch}' to main for fallback...")
            run_cmd(["git", "reset", "--hard"], cwd=self.plan_dir)
            default_branch = self.get_default_branch()
            run_cmd(["git", "reset", "--hard", f"origin/{default_branch}"], cwd=self.plan_dir)
            
            stdout_pull, stderr_pull, code_pull = run_cmd([
                "jules", "remote", "pull", "--session", session_id, "--apply"
            ], cwd=self.plan_dir)
            
            combined_pull = (stdout_pull + "\n" + stderr_pull).lower()
            if code_pull != 0 or "failed to apply patch" in combined_pull or "patch does not apply" in combined_pull:
                print(f"❌ Failed to apply patch after clean reset: {stderr_pull or stdout_pull}")
                return False
            force_push = True

        stdout_status, _, _ = run_cmd(["git", "status", "--porcelain"], cwd=self.plan_dir)
        if stdout_status.strip():
            print("💾 Committing changes locally...")
            run_cmd(["git", "add", "."], cwd=self.plan_dir)
            run_cmd(["git", "commit", "--no-verify", "-m", commit_msg], cwd=self.plan_dir)
            
            print(f"🚀 Pushing branch '{pr_branch}' to remote origin (force={force_push})...")
            push_args = ["git", "push", "origin", pr_branch, "--no-verify"]
            if force_push:
                push_args.append("--force")
            run_cmd(push_args, cwd=self.plan_dir)
            return True
        else:
            print("ℹ️ No new changes detected in patch.")
            return True

    def create_pr(self, task_number, task_title, pr_branch, session_link):
        if self.dry_run:
            mock_pr = 900 + task_number
            print(f"[DRY-RUN] GitHub: Creating PR for branch '{pr_branch}' (simulating PR #{mock_pr})")
            return mock_pr
        print("📦 Creating Pull Request on GitHub...")
        stdout_pr, stderr_pr, code_pr = run_cmd([
            "gh", "pr", "create", 
            "--title", f"Auto PR: Task {task_number}: {task_title}",
            "--body", f"PR created automatically by Wheel-Spoke Orchestrator for Jules session [{session_link}]({session_link}).",
            "--head", pr_branch
        ], cwd=self.plan_dir)
        
        if code_pr == 0:
            match_pr = re.search(r'/pull/(\d+)', stdout_pr)
            if match_pr:
                pr_number = int(match_pr.group(1))
                print(f"🎉 Created PR #{pr_number}!")
                return pr_number
            else:
                print(f"⚠️ Could not parse PR number from output: {stdout_pr}")
        else:
            # Check if PR already exists by parsing the error message
            match_err = re.search(r'/pull/(\d+)', stderr_pr)
            if match_err:
                pr_number = int(match_err.group(1))
                print(f"ℹ️ PR #{pr_number} already exists.")
                return pr_number
            print(f"❌ Failed to create PR: {stderr_pr}")
        return None

    def get_pr_diff(self, pr_number):
        if self.dry_run:
            print(f"[DRY-RUN] GitHub: Getting diff for PR #{pr_number}")
            return f"""diff --git a/mock_file.ts b/mock_file.ts
new file mode 100644
--- /dev/null
+++ b/mock_file.ts
@@ -0,0 +1,5 @@
+// Mock implementation for Task {pr_number - 900}
+console.log("Mock implementation");
+"""
        stdout, stderr, code = run_cmd(["gh", "pr", "diff", str(pr_number), "--repo", self.repo], cwd=self.plan_dir)
        if code != 0:
            print(f"❌ Failed to fetch diff for PR #{pr_number}: {stderr}")
            return None
        return stdout

    def merge_pr(self, pr_number, merge_method="merge"):
        if self.dry_run:
            print(f"[DRY-RUN] GitHub: Merging PR #{pr_number} using --{merge_method}")
            return True
        print(f"🔀 Merging PR #{pr_number} using --{merge_method}...")
        stdout, stderr, code = run_cmd([
            "gh", "pr", "merge", str(pr_number), "--repo", self.repo, f"--{merge_method}", "--delete-branch"
        ], cwd=self.plan_dir)
        if code == 0:
            print(f"🎉 PR #{pr_number} merged successfully!")
            return True
        else:
            print(f"❌ Failed to merge PR #{pr_number}: {stderr}")
            return False

# --- Spoke: Test Runner (Local Validation) ---
# Environment failure patterns that should NOT be sent to the code builder.
# These are infrastructure/toolchain problems, not code bugs.
_ENV_ERROR_PATTERNS = [
    "cannot find module @rollup",
    "rollup-darwin-arm64",
    "npm has a bug related to optional dependencies",
    "please try `npm i` again after removing",
    "enoent",
    "node_modules/.bin",
    "npm install",
    "npm ci",
    "gyp err!",
    "node-pre-gyp err!",
    "prebuild-install",
    "command not found",
    "not recognized as an internal or external command",
]


def auto_remediate_environment(plan_dir: str, error_output: str) -> bool:
    """Detects known fixable environment issues and runs remediation commands.
    Returns True if a fix was applied (caller should re-run tests).
    """
    err = error_output.lower()
    remediated = False

    # Broken rollup native binding (npm optional deps bug)
    if ("@rollup/rollup-darwin" in error_output or
            "npm has a bug related to optional dependencies" in error_output or
            "rollup-darwin" in error_output):
        print("🔧 Detected broken rollup native binding. Wiping node_modules and reinstalling...")
        run_cmd(["rm", "-rf", "node_modules", "package-lock.json"], cwd=plan_dir)
        run_cmd(["npm", "install"], cwd=plan_dir)
        run_cmd(["npm", "rebuild"], cwd=plan_dir)
        remediated = True

    # General missing node_modules
    elif "cannot find module" in err and "node_modules" in err:
        print("🔧 Detected missing node_modules. Running npm install...")
        run_cmd(["npm", "install"], cwd=plan_dir)
        remediated = True

    # Lockfile / peer dep conflict
    elif "peer dep" in err or "lockfile" in err or "npm ci" in err:
        print("🔧 Detected dependency conflict. Running npm ci...")
        run_cmd(["npm", "ci"], cwd=plan_dir)
        remediated = True

    if remediated:
        print("\u2705 Environment remediation applied.")
    return remediated


class ResolverSpoke:
    def __init__(self, config, plan_dir, dry_run=False):
        self.config = config.get("resolver", {})
        self.plan_dir = plan_dir
        self.dry_run = dry_run
        self.require_approval = self.config.get("require_approval", False)

    async def resolve(self, error_output: str) -> bool:
        if self.dry_run:
            print("[DRY-RUN] ResolverSpoke: Simulating resolution.")
            return True

        api_cfg = self.config
        provider = api_cfg.get("provider", "google")
        model = api_cfg.get("model", "gemini-2.5-flash")
        
        feedback = ""
        for attempt in range(2):
            prompt = f"""An infrastructure or environment error occurred while running tests/lint. 
Diagnose the error and provide the exact bash commands to fix it.

ERROR OUTPUT:
{error_output}
{feedback}
Output ONLY a single ```bash block containing the necessary commands. Do not write anything else."""
            
            print(f"🤖 ResolverSpoke analyzing environment error (attempt {attempt+1}/2, using {model})...")
            res = await call_model_api(
                provider=provider,
                model=model,
                prompt=prompt,
                system_instruction="You are a DevOps troubleshooting agent. Output only bash commands inside a ```bash block.",
                cwd=self.plan_dir
            )
            
            if not res and "fallback" in api_cfg:
                fb = api_cfg["fallback"]
                print(f"🔄 Resolver API failed. Falling back to {fb.get('model')}...")
                res = await call_model_api(
                    provider=fb.get("provider", "google"),
                    model=fb.get("model", "gemini-2.5-flash"),
                    prompt=prompt,
                    system_instruction="You are a DevOps troubleshooting agent. Output only bash commands inside a ```bash block.",
                    cwd=self.plan_dir
                )
                
            if not res:
                return False
                
            match = re.search(r'```[a-zA-Z]*\s*\n(.*?)```', res, re.DOTALL)
            if not match:
                print("⚠️ ResolverSpoke returned no bash block.")
                return False
                
            bash_script = match.group(1).strip()
            if not bash_script:
                return False
                
            print(f"\n--- RESOLVER AGENT PROPOSED FIX ---\n{bash_script}\n-----------------------------------\n")
            
            if self.require_approval:
                ans = input("❓ Do you approve running these commands? [Y/n] ").strip().lower()
                if ans == 'n':
                    print("❌ Resolver fix rejected.")
                    return False
                    
            print("🚀 Executing Resolver commands...")
            script_path = os.path.join(self.plan_dir, ".resolver_fix.sh")
            with open(script_path, "w") as f:
                f.write(bash_script)
            
            stdout, stderr, code = run_cmd(["bash", ".resolver_fix.sh"], cwd=self.plan_dir)
            os.remove(script_path)
            
            if code == 0:
                print("✅ Resolver successfully executed the fix.")
                return True
            else:
                print(f"❌ Resolver fix failed with exit code {code}:\n{stderr}")
                feedback = f"\n\nPREVIOUS FIX ATTEMPT FAILED:\nYou ran:\n```bash\n{bash_script}\n```\nIt failed with exit code {code} and this output:\n{stderr}\nPlease provide an updated bash script to fix this."
                
        return False


class TestRunnerSpoke:
    def __init__(self, plan_dir, dry_run=False):
        self.plan_dir = plan_dir
        self.dry_run = dry_run
        self.call_count = 0

    def _classify_failure(self, stdout: str, stderr: str) -> str:
        """Returns 'env_error' if this is an infrastructure issue, 'code_error' otherwise."""
        combined = (stdout + stderr).lower()
        for pat in _ENV_ERROR_PATTERNS:
            if pat in combined:
                return "env_error"
        return "code_error"

    def run_checks(self):
        """Runs npm test, lint, etc. Returns dict: {code_failures, env_failures}."""
        if self.dry_run:
            self.call_count += 1
            print(f"[DRY-RUN] TestRunner: Running checks (attempt {self.call_count}).")
            if self.call_count == 1:
                print("[DRY-RUN] TestRunner: Simulating FAILURE on first attempt.")
                return {"code_failures": [{"name": "Mock Lint", "command": "npm run lint",
                                           "stdout": "", "stderr": "Mock: unused import"}],
                        "env_failures": []}
            print("[DRY-RUN] TestRunner: Simulating SUCCESS.")
            return {"code_failures": [], "env_failures": []}

        print("🧪 Running local validation tests (lint and unit tests)...")
        verifications = []

        pkg_path = os.path.join(self.plan_dir, "package.json")
        has_nexus = os.path.exists(os.path.join(self.plan_dir, "packages/nexus"))

        if os.path.exists(pkg_path):
            try:
                with open(pkg_path, "r", encoding="utf-8") as f:
                    pkg = json.load(f)
                scripts = pkg.get("scripts", {})
                if has_nexus:
                    verifications.append(("Lint Check", ["npm", "run", "lint", "--workspace=packages/nexus"]))
                    verifications.append(("Unit Tests", ["npm", "test", "--workspace=packages/nexus"]))
                else:
                    if "lint" in scripts:
                        verifications.append(("Lint Check", ["npm", "run", "lint"]))
                    if "test" in scripts:
                        if "no test specified" not in scripts["test"]:
                            verifications.append(("Unit Tests", ["npm", "test"]))
            except Exception as e:
                print(f"\u26a0\ufe0f Failed to parse package.json: {e}")

        if not verifications and not has_nexus:
            print("\u2139\ufe0f No lint or test scripts found. Skipping local verification.")
            return {"code_failures": [], "env_failures": []}

        code_failures = []
        env_failures = []
        for name, cmd in verifications:
            print(f"\u8dd1 Running {name}: {' '.join(cmd)}...")
            stdout, stderr, code = run_cmd(cmd, cwd=self.plan_dir)
            if code != 0:
                failure = {"name": name, "command": " ".join(cmd),
                           "stdout": stdout, "stderr": stderr}
                kind = self._classify_failure(stdout, stderr)
                if kind == "env_error":
                    env_failures.append(failure)
                    print(f"\u26a0\ufe0f {name} failed due to environment/toolchain issue (not a code bug).")
                else:
                    code_failures.append(failure)
                    print(f"\u274c {name} failed.")
            else:
                print(f"\u2705 {name} passed.")

        return {"code_failures": code_failures, "env_failures": env_failures}

    def format_failures(self, failures):
        body = "\u274c Local verification tests failed:\n\n"
        for f in failures:
            body += f"### {f['name']} (`{f['command']}`)\n"
            body += "```\n"
            output = (f['stdout'].strip() + "\n" + f['stderr'].strip()).strip()
            lines = output.strip().split("\n")
            # Increase limit dramatically so we don't truncate the actual test failures
            if len(lines) > 300:
                output = "\n".join(lines[:50]) + "\n... [TRUNCATED] ...\n" + "\n".join(lines[-250:])
            body += output
            body += "\n```\n\n"
        return body


# --- Spoke: Code Reviewer (LLM Review) ---
class CodeReviewerSpoke:
    def __init__(self, config, plan_dir=None, dry_run=False):
        self.config = config
        self.plan_dir = plan_dir
        self.dry_run = dry_run
        self.call_count = 0

    async def review_diff(self, task_description, pr_diff):
        if self.dry_run:
            self.call_count += 1
            print(f"[DRY-RUN] CodeReviewer: Reviewing diff (attempt {self.call_count}).")
            if self.call_count == 1:
                print("[DRY-RUN] CodeReviewer: Simulating Code Review REJECTION to verify builder fix loop.")
                return "The code looks good, but please add comments explaining the logic."
            else:
                print("[DRY-RUN] CodeReviewer: Simulating Code Review APPROVED.")
                return "APPROVED"
        review_prompt = f"""You are an expert software engineer and code reviewer.
Review the code changes provided in the diff below.
The changes are intended to address this task description:

TASK:
---
{task_description}
---

DIFF:
---
{pr_diff}
---

Verify correctness, clean styles, performance, security, and adherence to the task.

If the diff correctly and completely implements the task specifications and has no issues, reply with exactly the word "APPROVED" (case-insensitive).
If there are issues, list them clearly as bullet points so they can be fed back into an automated coder agent to fix them. Be specific about what needs to be changed. Do not output "APPROVED" if there are any pending issues.
"""
        print("🕵️ Invoking Code Reviewer Agent...")
        res = await call_model_api(
            provider=self.config["provider"],
            model=self.config["model"],
            prompt=review_prompt,
            system_instruction="You are a strict code reviewer. Return APPROVED or point out precise bugs/improvements.",
            cwd=self.plan_dir
        )
        return res

# --- Spoke: Test Builder (Jules primary, LLM API fallback) ---
class TestBuilderSpoke:
    def __init__(self, config, plan_dir, repo=None, plan_path=None, jules_handle="@jules", dry_run=False):
        self.config = config
        self.plan_dir = plan_dir
        self.repo = repo
        self.plan_path = plan_path
        self.jules_handle = jules_handle
        self.dry_run = dry_run

    async def generate_tests(self, task_description, code_diff, pr_branch=None):
        if self.dry_run:
            print("[DRY-RUN] TestBuilder: Simulating test generation.")
            return "mock_test.spec.ts"

        # --- Jules path: ask Jules to write tests on the same branch ---
        if self.config.get("model") == "jules" and self.repo:
            prompt = (
                f"Write comprehensive unit tests for the following task.\n"
                f"Plan file: {self.plan_path}\n\n"
                f"Task: {task_description}\n\n"
                f"Implementation diff (for context):\n{code_diff[:3000]}\n\n"
                f"Please checkout branch '{pr_branch}' and add unit tests that verify all "
                f"acceptance criteria. Only write test files — do not modify existing implementation files."
            )
            print("🧪 Submitting test-writing task to Jules...")
            stdout, stderr, code = run_cmd(
                ["jules", "remote", "new", "--repo", self.repo, "--session", prompt],
                cwd=self.plan_dir
            )
            combined = strip_ansi(stdout + "\n" + stderr)
            match = re.search(r'\b\d{18,20}\b', combined)
            if not match:
                print("⚠️ Could not start Jules test session. Falling back to LLM API.")
            else:
                session_id = match.group(0)
                print(f"➡️ Jules Test Builder Session: {session_id}")
                print(f"⏳ Monitoring Jules test session {session_id}...")
                while True:
                    status_out, _, _ = run_cmd(["jules", "remote", "list", "--session"], cwd=self.plan_dir)
                    status = "NOT_FOUND"
                    for line in status_out.strip().split("\n"):
                        cleaned = strip_ansi(line).strip()
                        if session_id in cleaned:
                            parts = re.split(r'\s{2,}', cleaned)
                            if len(parts) >= 5:
                                status = parts[4].strip()
                            elif len(parts) == 4:
                                status = "In Progress"
                            break
                    print(f"🔄 Jules Test Status: {status}")
                    if status == "Completed":
                        stdout_p, stderr_p, code_p = run_cmd(
                            ["jules", "remote", "pull", "--session", session_id, "--apply"],
                            cwd=self.plan_dir
                        )
                        if code_p == 0:
                            git_st, _, _ = run_cmd(["git", "status", "--porcelain"], cwd=self.plan_dir)
                            if git_st.strip():
                                run_cmd(["git", "add", "."], cwd=self.plan_dir)
                                run_cmd(["git", "commit", "--no-verify", "-m",
                                         f"test: add unit tests via Jules session {session_id}"], cwd=self.plan_dir)
                                run_cmd(["git", "push", "origin", pr_branch, "--no-verify"], cwd=self.plan_dir)
                                print("✅ Jules test files committed and pushed.")
                            else:
                                print("ℹ️ Jules test session completed but produced no new test files.")
                            return session_id
                        else:
                            print(f"⚠️ Jules test patch failed: {stderr_p}. Falling back to LLM API.")
                            break
                    elif "Awaiting Plan" in status:
                        url = f"https://jules.google.com/task/{session_id}"
                        print(f"\n🔔 Jules test builder awaiting plan approval: {url}\n")
                        send_notification("Jules Test Builder", "Review and approve the test plan in the browser.")
                        try:
                            webbrowser.open(url)
                        except Exception:
                            pass
                    elif status in ["Failed", "Paused", "Error", "NOT_FOUND", "Awaiting User F"]:
                        print(f"⚠️ Jules test session ended with: {status}. Falling back to LLM API.")
                        break
                    await asyncio.sleep(POLL_INTERVAL)

        # --- LLM API path (fallback or when model != jules) ---
        fallback_cfg = self.config.get("fallback", {})
        if self.config.get("model") == "jules":
            api_model = fallback_cfg.get("model", "gemini-1.5-flash")
            api_provider = fallback_cfg.get("provider", "google")
        else:
            api_model = self.config.get("model")
            api_provider = self.config.get("provider", "google")
        prompt = f"""Given the following task description and the implementation diff, write comprehensive unit or integration tests.

TASK DESCRIPTION:
{task_description}

CODE CHANGES DIFF:
{code_diff}

Generate the complete test file. Respond with the file path followed by a markdown code block:
File: packages/<package>/tests/my-test.spec.ts
```ts
// test code here
```
"""
        print("🧪 Invoking Test Builder (LLM API)...")
        res = await call_model_api(
            provider=api_provider,
            model=api_model,
            prompt=prompt,
            system_instruction="You are a test engineer. Output complete test files matching task specifications.",
            cwd=self.plan_dir
        )
        if res:
            match = re.search(r'File:\s*([^\n]+)\n+```[a-zA-Z]*\n([\s\S]*?)```', res)
            if match:
                file_path = match.group(1).strip()
                test_code = match.group(2)
                abs_path = os.path.join(self.plan_dir, file_path)
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(test_code)
                print(f"✅ Generated test file: {file_path}")
                return file_path
        return None


# --- Spoke: Router (LLM State Machine) ---
class RouterSpoke:
    def __init__(self, plan_dir, dry_run=False):
        self.plan_dir = plan_dir
        self.dry_run = dry_run
        
    async def decide_next_step(self, task_description, state_summary, last_action_result):
        if self.dry_run:
            print("[DRY-RUN] RouterSpoke simulating next step...")
            return "CodeBuilder"
            
        system_instruction = """You are the Router Agent for the Wheel-and-Spoke Orchestrator.
Your job is to read the current state of a software engineering task and decide which specialized agent (Spoke) to invoke next.

Available Spokes:
1. CodeBuilder: Writes application code based on task requirements and feedback. Use this when code needs to be written or fixed.
2. TestRunner: Runs local linters and unit tests (npm test, npm run lint). Use this AFTER code is written to verify it.
3. Resolver: Fixes infrastructure/environment errors (e.g. missing node modules, missing commands). Use this ONLY when TestRunner reports an environment error.
4. CodeReviewer: Reviews the final code diff against the task description. Use this ONLY after TestRunner confirms all tests pass.
5. Exit: Use this if the task is impossible, or if the loop is stuck in an infinite failure cycle.

You MUST respond with exactly one line containing the next spoke to invoke. For example:
NEXT: CodeBuilder
NEXT: TestRunner
NEXT: Resolver
NEXT: CodeReviewer
NEXT: Exit
"""
        prompt = f"""TASK: {task_description}

STATE SUMMARY:
{state_summary}

LAST ACTION RESULT:
{last_action_result}

What is the next Spoke to invoke?"""
        
        print("🧠 RouterSpoke analyzing state to decide next step...")
        # Hardcoding openai/gpt-4o-mini as per user approval for speed/cost.
        res = await call_model_api(
            provider="openai",
            model="gpt-4o-mini",
            prompt=prompt,
            system_instruction=system_instruction,
            cwd=self.plan_dir
        )
        
        if res:
            match = re.search(r'NEXT:\s*([A-Za-z]+)', res)
            if match:
                decision = match.group(1).strip()
                print(f"🔀 Router decision: {decision}")
                return decision
        
        print(f"⚠️ Router failed to return a valid spoke. Raw response: {res}")
        return "Exit"


# --- Spoke: Code Builder (Jules & Direct LLM Fallbacks) ---
class CodeBuilderSpoke:
    def __init__(self, config, repo, plan_path, plan_dir, jules_handle, dry_run=False):
        self.config = config
        self.repo = repo
        self.plan_path = plan_path
        self.plan_dir = plan_dir
        self.jules_handle = jules_handle
        self.dry_run = dry_run

    def start_jules_session(self, prompt):
        """Kicks off a Jules session and extracts the session ID."""
        print(f"🤖 Submitting coding task to Jules...")
        stdout, stderr, code = run_cmd([
            "jules", "remote", "new", "--repo", self.repo, "--session", prompt
        ], cwd=self.plan_dir)
        
        combined_output = strip_ansi(stdout + "\n" + stderr).lower()
        if code != 0:
            if any(kw in combined_output for kw in ["quota", "limit", "rate limit", "exhausted", "insufficient", "run out"]):
                print("⚠️ Jules daily task limit or quota limit reached!")
                return "QUOTA_EXHAUSTED"
            print(f"❌ Failed to submit task to Jules: {stderr}")
            return None
            
        cleaned = strip_ansi(stdout)
        match = re.search(r'\b\d{18,20}\b', cleaned)
        if match:
            session_id = match.group(0)
            print(f"➡️ Created Jules Session ID: {session_id}")
            return session_id
        return None

    def get_jules_session_status(self, session_id):
        stdout, _, code = run_cmd(["jules", "remote", "list", "--session"], cwd=self.plan_dir)
        if code != 0:
            return "ERROR"
        lines = stdout.strip().split("\n")
        for line in lines:
            cleaned_line = strip_ansi(line).strip()
            if cleaned_line.startswith("Warning:") or "color support" in cleaned_line or cleaned_line.startswith("ID"):
                continue
            if session_id in cleaned_line:
                parts = re.split(r'\s{2,}', cleaned_line)
                if len(parts) >= 5:
                    return parts[4].strip()
                elif len(parts) == 4:
                    return "In Progress"
        return "NOT_FOUND"

    async def monitor_jules_session(self, session_id):
        """Polls Jules session status until complete or failed."""
        print(f"⏳ Monitoring Jules Session {session_id}...")
        notified_plan_approval = False
        notified_user_feedback = False
        while True:
            status = self.get_jules_session_status(session_id)
            print(f"🔄 Current Jules Status: {status}")
            
            if status == "Completed":
                return "SUCCESS"
            elif "Awaiting Plan" in status or status == "Awaiting Plan A":
                if not notified_plan_approval:
                    url = f"https://jules.google.com/task/{session_id}"
                    msg = f"Jules is awaiting plan approval. Opening browser: {url}"
                    print(f"\n🔔 {msg}\n")
                    send_notification("Jules Plan Approval Required", "Review and approve the task plan in the browser.")
                    try:
                        webbrowser.open(url)
                    except Exception as e:
                        print(f"⚠️ Failed to open browser: {e}")
                    notified_plan_approval = True
            elif "Awaiting User" in status or status == "Awaiting User F":
                print("\n⚠️ Jules is awaiting user feedback (asking a question in the browser).")
                print("🔄 Since we cannot reply to Jules programmatically, we will treat this as a failure and fall back to the Code Builder agent on the wheel to implement the changes autonomously.\n")
                return "FAILED"
            elif status in ["Failed", "Paused", "Error", "NOT_FOUND"]:
                return "FAILED"
                
            # Reset notifications if status goes back to In Progress
            if status == "In Progress":
                notified_user_feedback = False
                notified_plan_approval = False
                
            await asyncio.sleep(POLL_INTERVAL)

    def _gather_context(self, task_description, revision_feedback=None, max_files=6, max_chars_per_file=1500):
        """Reads relevant source files to give the LLM grounding in the actual codebase."""
        context = ""
        found = []

        search_text = task_description + "\n" + str(revision_feedback or "")
        # 1. Find specific filenames with extensions mentioned in task
        file_mentions = re.findall(r'[\w\-./]+\.(?:ts|tsx|js|jsx|py|json|md)', search_text)
        for fname in file_mentions[:max_files]:
            basename = os.path.basename(fname)
            stdout, _, code = run_cmd(
                ["find", ".", "-name", basename,
                 "-not", "-path", "*/node_modules/*",
                 "-not", "-path", "*/.git/*",
                 "-not", "-path", "*/dist/*"],
                cwd=self.plan_dir
            )
            if code == 0:
                for p in stdout.strip().split("\n")[:2]:
                    if p.strip() and p.strip() not in found:
                        found.append(p.strip())

        # 2. Find directory globs (e.g. packages/nexus/src/renderer/day-trader/**)
        dir_mentions = re.findall(r'`([\w\-./]+)/\*\*?`', task_description)
        dir_mentions += re.findall(r'([\w\-]+/(?:[\w\-]+/){1,4}[\w\-]+)(?:/\*\*?)?', task_description)
        for dir_path in dir_mentions[:3]:
            abs_dir = os.path.join(self.plan_dir, dir_path)
            if os.path.isdir(abs_dir):
                stdout, _, code = run_cmd(
                    ["find", abs_dir, "-name", "*.ts", "-o", "-name", "*.tsx"],
                    cwd=self.plan_dir
                )
                if code == 0:
                    for p in stdout.strip().split("\n")[:4]:
                        rel = os.path.relpath(p.strip(), self.plan_dir) if p.strip() else ""
                        if rel and rel not in found:
                            found.append(f"./{rel}")

        # 3. Read found files
        for path in found[:max_files]:
            abs_path = os.path.join(self.plan_dir, path.lstrip("./"))
            if os.path.exists(abs_path) and os.path.isfile(abs_path):
                try:
                    with open(abs_path, "r", encoding="utf-8") as fh:
                        content = fh.read(max_chars_per_file)
                    context += f"\n\n=== {path} ===\n{content}"
                except Exception:
                    pass

        if not context:
            # Last resort: list the top-level package dirs so Gemini knows the structure
            stdout, _, _ = run_cmd(
                ["find", ".", "-maxdepth", "4", "-name", "*.ts",
                 "-not", "-path", "*/node_modules/*",
                 "-not", "-path", "*/.git/*",
                 "-not", "-path", "*/dist/*"],
                cwd=self.plan_dir
            )
            file_list = "\n".join(stdout.strip().split("\n")[:40])
            context = f"(no specific files matched — here are available TypeScript files)\n{file_list}"

        return context

    async def implement_task(self, task_description, revision_feedback=None, pr_branch=None, pr_number=None):
        """Generates code changes: Jules (if configured) with Gemini/OpenAI as primary or fallback."""
        if self.dry_run:
            print(f"[DRY-RUN] CodeBuilder: Simulating code changes. Feedback: {revision_feedback}")
            return {"mode": "jules", "session_id": "9999999999999999999"}

        prompt = f"Implement changes for Task. Plan: {self.plan_path}\n\nTask: {task_description}"
        if revision_feedback:
            prompt += f"\n\nAddress these code review issues on branch '{pr_branch}' (PR #{pr_number}):\n{revision_feedback}"
            if pr_branch:
                prompt += f"\nPlease checkout branch '{pr_branch}' first."

        # --- Jules path (only when explicitly configured as primary) ---
        if self.config.get("model") == "jules":
            session_id = self.start_jules_session(prompt)
            if session_id == "QUOTA_EXHAUSTED":
                print("🔄 Jules quota exhausted, falling back to API...")
            elif session_id:
                status = await self.monitor_jules_session(session_id)
                if status == "SUCCESS":
                    return {"session_id": session_id, "mode": "jules"}
                print(f"❌ Jules coding session failed ({status}). Falling back to API...")
            else:
                print("🔄 Jules initialization failed, falling back to API...")
            # Jules failed — use fallback sub-config
            api_cfg = self.config.get("fallback", DEFAULT_CONFIG["code_builder"]["fallback"])
        else:
            # Direct API mode: Gemini/OpenAI as primary (no Jules)
            api_cfg = self.config

        # --- LLM API path ---
        print(f"🚀 Code Builder using '{api_cfg.get('model')}' ({api_cfg.get('provider')})...")

        # Gather relevant file context so the LLM has grounding in the real codebase
        file_context = self._gather_context(task_description, revision_feedback)

        api_prompt = f"""You are an expert software engineer implementing a task in a real TypeScript/Electron codebase.

TASK:
{task_description}

REVISION FEEDBACK (address these if present):
{revision_feedback or 'None'}

RELEVANT FILES FROM CODEBASE:
{file_context}

You MUST output your changes using EXACTLY this format for each file. No exceptions:

Update File: packages/nexus/src/some/path/file.ts
```ts
// complete file contents here
```

Update File: packages/nexus/src/another/file.ts
```ts
// complete file contents here
```

Rules:
- Output EVERY file that needs to be created or modified.
- Include the COMPLETE file contents, not just diffs or snippets.
- If a file does not need changes, do NOT include it.
- Use the relative path from the repository root.
- If you are not sure what to change, make your best attempt based on the task description and file context above.
"""
        res = await call_model_api(
            provider=api_cfg.get("provider", "google"),
            model=api_cfg.get("model", "gemini-2.5-flash"),
            prompt=api_prompt,
            system_instruction="You are a senior software engineer. You MUST respond with file changes using the 'Update File: <path>' format followed by a code block. Do not respond with only prose.",
            cwd=self.plan_dir
        )

        if not res and "fallback" in api_cfg:
            fb = api_cfg["fallback"]
            print(f"🔄 Primary API failed or rate limited. Falling back to {fb.get('model')}...")
            res = await call_model_api(
                provider=fb.get("provider", "google"),
                model=fb.get("model", "gemini-2.5-flash"),
                prompt=api_prompt,
                system_instruction="You are a senior software engineer. You MUST respond with file changes using the 'Update File: <path>' format followed by a code block. Do not respond with only prose.",
                cwd=self.plan_dir
            )

        if res:
            matches = list(re.finditer(r'Update File:\s*([^\n]+)\n+```[a-zA-Z]*\n([\s\S]*?)```', res))
            if not matches:
                print(f"\n⚠️ Gemini returned a response but no 'Update File:' blocks were found.")
                print(f"   First 600 chars of response:\n---\n{res[:600]}\n---")
                print("   Tip: The task may need more context or a more specific prompt.")
            updated_files = []
            for match in matches:
                file_path = match.group(1).strip()
                file_code = match.group(2)
                abs_path = os.path.join(self.plan_dir, file_path)
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(file_code)
                updated_files.append(file_path)
                print(f"📝 Updated: {file_path}")
            if updated_files:
                return {"mode": "fallback", "files": updated_files}

        return {"mode": "error", "reason": "API code generation produced no file changes"}

# --- Task Plan Parser & State Manager ---
def parse_plan_tasks(plan_path):
    if not os.path.exists(plan_path):
        print(f"❌ Plan file not found: {plan_path}")
        return []
    with open(plan_path, 'r', encoding='utf-8') as f:
        content = f.read()

    task_list_marker = re.search(r'(?i)##\s+.*Task\s+List', content)
    search_content = content[task_list_marker.start():] if task_list_marker else content

    header_pattern = re.compile(r'^(#{2,4})\s+Task\s+(\d+)[:\s]*(.*)$', re.MULTILINE | re.IGNORECASE)
    matches = list(header_pattern.finditer(search_content))
    tasks = []
    
    for i, match in enumerate(matches):
        _, task_num, title = match.groups()
        start_idx = match.end()
        end_idx = matches[i+1].start() if i + 1 < len(matches) else len(search_content)
        
        task_body = search_content[start_idx:end_idx].strip()
        task_title = title.strip()
        is_completed = "[COMPLETED]" in task_title.upper()
        
        tasks.append({
            "number": int(task_num),
            "title": task_title,
            "description": f"Task {task_num}: {task_title}\n\n{task_body}",
            "completed": is_completed
        })
    return tasks

def get_state_file_path(repo, dry_run=False):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    states_dir = os.path.join(script_dir, "states")
    os.makedirs(states_dir, exist_ok=True)
    project_name = repo.split("/")[-1].replace(".git", "")
    suffix = ".dry_run" if dry_run else ""
    return os.path.join(states_dir, f"{project_name}_state.json{suffix}")

def load_state(state_file):
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                return json.load(f)
        except:
            pass
    return None

def save_state(state, state_file):
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)

def sync_and_tag_plan(plan_path, task_number, dry_run=False, tag="COMPLETED"):
    if dry_run:
        print(f"[DRY-RUN] Plan Sync: Mark Task {task_number} as [{tag}] in plan (no git push/pull occurred).")
        return True
    plan_dir = os.path.dirname(os.path.abspath(plan_path))
    print(f"📝 Appending [{tag}] to Task {task_number} in plan file...")

    stdout, _, _ = run_cmd(["git", "branch", "--show-current"], cwd=plan_dir)
    active_branch = stdout.strip()

    default_branch = "main"
    stdout, _, _ = run_cmd(["git", "show-ref", "--verify", "refs/heads/master"], cwd=plan_dir)
    if stdout.strip():
        default_branch = "master"

    print(f"🔄 Switching to default branch '{default_branch}' and pulling updates...")
    run_cmd(["git", "checkout", default_branch], cwd=plan_dir)
    run_cmd(["git", "pull", "origin", default_branch], cwd=plan_dir)

    abs_plan_path = os.path.abspath(plan_path)
    with open(abs_plan_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    updated = False
    for idx, line in enumerate(lines):
        match = re.match(rf'^(\s*#+\s+Task\s+{task_number}\b.*)$', line, re.IGNORECASE)
        if match:
            header_line = match.group(1).rstrip()
            tag_upper = tag.upper()
            if f"[{tag_upper}]" not in header_line.upper():
                lines[idx] = f"{header_line} [{tag}]\n"
                updated = True
                break

    if updated:
        try:
            with open(abs_plan_path, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            print(f"✅ Successfully marked Task {task_number} as [{tag}] in plan file.")
            run_cmd(["git", "add", abs_plan_path], cwd=plan_dir)
            run_cmd(["git", "commit", "--no-verify", "-m",
                     f"chore: mark Task {task_number} as {tag.lower()} in plan"], cwd=plan_dir)
            run_cmd(["git", "push", "origin", default_branch, "--no-verify"], cwd=plan_dir)
            print("🚀 Pushed progress update to remote repository.")
            return True
        except Exception as e:
            print(f"⚠️ Git operations failed: {e}")
            return False
    return False

def load_env_and_keys(config, plan_dir):
    # 1. Load from local .env in script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_env = os.path.join(script_dir, ".env")
    if os.path.exists(local_env):
        try:
            with open(local_env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ[k.strip()] = v.strip().strip("'\"")
        except:
            pass

    # 2. Load from plan directory .env
    plan_env = os.path.join(plan_dir, ".env")
    if os.path.exists(plan_env):
        try:
            with open(plan_env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ[k.strip()] = v.strip().strip("'\"")
        except:
            pass

    # 3. Load from config.json keys (overwrites .env if present)
    if "gemini_api_key" in config and config["gemini_api_key"]:
        os.environ["GEMINI_API_KEY"] = config["gemini_api_key"]
    if "openai_api_key" in config and config["openai_api_key"]:
        os.environ["OPENAI_API_KEY"] = config["openai_api_key"]

# --- Orchestrator Hub ---
async def run_orchestrator(args):
    config = load_config()
    plan_dir = os.path.dirname(os.path.abspath(args.plan))
    load_env_and_keys(config, plan_dir)
    repo = args.repo or get_git_repo(plan_dir)
    if not repo:
        if args.dry_run:
            repo = "mock-owner/mock-repo"
        else:
            print("❌ Error: Could not auto-detect GitHub repo name.")
            sys.exit(1)

    print(f"📋 Plan File: {args.plan}")
    print(f"🐙 Git Repository: {repo}")
    print(f"🔀 Merge Method: {args.merge_method}")
    if args.dry_run:
        print("🧪 RUNNING IN DRY-RUN MODE (No real mutations or API calls will occur)")

    all_tasks = parse_plan_tasks(args.plan)
    tasks = [t for t in all_tasks if not t["completed"]]
    completed_count = len(all_tasks) - len(tasks)
    print(f"📊 Plan Summary: {len(all_tasks)} total tasks, {completed_count} completed. {len(tasks)} remain.")

    if not tasks:
        print("🏁 All tasks in this plan are already completed!")
        return

    state_file = get_state_file_path(repo, dry_run=args.dry_run)
    state = load_state(state_file)
    if not state or state.get("plan_path") != args.plan:
        state = {
            "plan_path": args.plan,
            "repo": repo,
            "current_task_idx": 0,
            "current_task_number": tasks[0]["number"] if tasks else None,
            "completed_tasks": [],
            "jules_session_id": None,
            "pr_number": None,
            "pr_branch": None,
            "loop_count": 0,
            "tests_generated": False
        }
    else:
        # Align current_task_idx with current_task_number in the updated tasks list
        if state.get("current_task_number"):
            found_idx = -1
            for i, t in enumerate(tasks):
                if t["number"] == state["current_task_number"]:
                    found_idx = i
                    break
            if found_idx != -1:
                state["current_task_idx"] = found_idx
            else:
                state["current_task_idx"] = 0
    
    # Initialize spokes
    pr_builder = PRBuilderSpoke(repo, plan_dir, dry_run=args.dry_run)
    test_runner = TestRunnerSpoke(plan_dir, dry_run=args.dry_run)
    code_reviewer = CodeReviewerSpoke(config["code_reviewer"], plan_dir=plan_dir, dry_run=args.dry_run)
    resolver_spoke = ResolverSpoke(config, plan_dir, dry_run=args.dry_run)
    test_builder = TestBuilderSpoke(
        config["test_builder"], plan_dir,
        repo=repo, plan_path=args.plan, jules_handle=args.jules_handle,
        dry_run=args.dry_run
    )
    code_builder = CodeBuilderSpoke(config["code_builder"], repo, args.plan, plan_dir, args.jules_handle, dry_run=args.dry_run)

    async def run_router_loop(task, state, state_file, plan_dir, repo, args,
                              code_builder, pr_builder, test_runner, resolver_spoke, test_builder, code_reviewer):
        router_spoke = RouterSpoke(plan_dir, dry_run=args.dry_run)
        state_summary = f"Task {task['number']} started. Code has not been written yet. Start by using CodeBuilder."
        last_action_result = "No actions taken yet."
    
        router_iterations = 0
        max_iterations = 20
    
        while router_iterations < max_iterations:
            router_iterations += 1
            print(f"\\n--- ROUTER ITERATION {router_iterations}/{max_iterations} ---")
        
            decision = await router_spoke.decide_next_step(task["description"], state_summary, last_action_result)
        
            if decision == "CodeBuilder":
                feedback = None
                if "failed" in last_action_result.lower() or "error" in last_action_result.lower() or "rejected" in last_action_result.lower():
                    feedback = last_action_result

                build_res = await code_builder.implement_task(
                    task["description"],
                    revision_feedback=feedback,
                    pr_branch=state["pr_branch"],
                    pr_number=state.get("pr_number")
                )
            
                if build_res.get("mode") == "fallback":
                    print("💾 Committing API changes locally...")
                    if not args.dry_run:
                        run_cmd(["git", "add", "."], cwd=plan_dir)
                        run_cmd(["git", "commit", "--no-verify", "-m", f"feat: implement task {task['number']} via Router fallback"], cwd=plan_dir)
                        run_cmd(["git", "push", "origin", state["pr_branch"], "--no-verify"], cwd=plan_dir)
                
                    if not state.get("pr_number"):
                        pr_num = pr_builder.create_pr(task["number"], task["title"], state["pr_branch"], "Gemini API")
                        if pr_num:
                            state["pr_number"] = pr_num
                            save_state(state, state_file)
                
                    state_summary = "Code was written and pushed to the branch. You should now use TestRunner to verify."
                    last_action_result = "Code successfully built and pushed."
                else:
                    state_summary = "CodeBuilder failed to write changes."
                    last_action_result = f"CodeBuilder error: {build_res.get('reason', 'Unknown')}"
                
            elif decision == "TestRunner":
                test_result = test_runner.run_checks()
                env_failures = test_result.get("env_failures", [])
                code_failures = test_result.get("code_failures", [])
            
                if env_failures:
                    last_action_result = test_runner.format_failures(env_failures)
                    state_summary = "Tests failed due to an environment/toolchain error. You MUST use Resolver next."
                elif code_failures:
                    last_action_result = test_runner.format_failures(code_failures)
                    state_summary = "Tests failed due to code bugs. You MUST use CodeBuilder next to fix them."
                    state["pr_number"] = None
                    save_state(state, state_file)
                else:
                    last_action_result = "All lint checks and unit tests passed successfully."
                    state_summary = "Tests passed. You should now use CodeReviewer to review the PR diff."
                
            elif decision == "Resolver":
                fixed = auto_remediate_environment(plan_dir, last_action_result)
                if not fixed:
                    fixed = await resolver_spoke.resolve(last_action_result)
            
                if fixed:
                    last_action_result = "Environment was successfully remediated."
                    state_summary = "Environment fixed. You MUST use TestRunner next to re-run the tests."
                else:
                    last_action_result = "Resolver failed to fix the environment."
                    state_summary = "Environment is permanently broken. Cannot proceed."
                
            elif decision == "CodeReviewer":
                if not state.get("pr_number"):
                    last_action_result = "No PR exists to review."
                    state_summary = "Cannot review because PR does not exist."
                    continue
                
                diff = pr_builder.get_pr_diff(state["pr_number"])
                if not diff:
                    last_action_result = "Failed to get PR diff."
                    continue
                
                review_res = await code_reviewer.review_diff(task["description"], diff)
                last_action_result = review_res
            
                if "APPROVED" in review_res.upper() and "NOT APPROVED" not in review_res.upper() and "REJECTED" not in review_res.upper():
                    print(f"✅ Code review APPROVED for Task {task['number']}!")
                    if pr_builder.merge_pr(state["pr_number"], args.merge_method):
                        sync_and_tag_plan(args.plan, task["number"], dry_run=args.dry_run)
                        send_notification("Task Completed", f"Task {task['number']} merged!")
                    
                        state["completed_tasks"].append(task["number"])
                        state["current_task_idx"] += 1
                        state["pr_branch"] = None
                        state["pr_number"] = None
                        save_state(state, state_file)
                        return True
                    else:
                        last_action_result = "Failed to merge PR."
                        state_summary = "PR Merge failed."
                else:
                    state_summary = "Code review rejected. You MUST use CodeBuilder to address the feedback."
                    state["pr_number"] = None
                    save_state(state, state_file)
                
            elif decision == "Exit":
                print(f"\\n\u26a0\ufe0f Router elected to Exit for Task {task['number']}.")
                break
            
            else:
                print(f"⚠️ Unknown Router decision: {decision}")
                break

        print(f"\\n\u26a0\ufe0f Router hit loop limit or elected to exit for Task {task['number']}.")
        print("Select recovery action:")
        print("  [A] Approve and merge PR manually")
        print("  [S] Skip this task and continue to next")
        print("  [E] Exit orchestrator")
        choice = input("Choice: ").strip().lower()
        if choice == "a":
            pr_number, _ = pr_builder.find_pr_for_task(task["number"])
            if pr_number and pr_builder.merge_pr(pr_number, args.merge_method):
                sync_and_tag_plan(args.plan, task["number"], dry_run=args.dry_run)
                state["completed_tasks"].append(task["number"])
                state["current_task_idx"] += 1
                state["pr_branch"] = None
                state["pr_number"] = None
                save_state(state, state_file)
            else:
                print("\u274c Failed to resolve PR manually. Exiting.")
                sys.exit(1)
        elif choice == "s":
            sync_and_tag_plan(args.plan, task["number"], tag="SKIPPED", dry_run=args.dry_run)
            state["completed_tasks"].append(task["number"])
            state["current_task_idx"] += 1
            state["pr_branch"] = None
            state["pr_number"] = None
            save_state(state, state_file)
        else:
            sys.exit(1)
    
        return False

    while state["current_task_idx"] < len(tasks):
        idx = state["current_task_idx"]
        task = tasks[idx]
        state["current_task_number"] = task["number"]
        save_state(state, state_file)

        print("\n" + "="*60)
        print(f"📋 WHEEL-SPOKE: PROCESSING TASK {task['number']}: {task['title']}")
        print("="*60)

        # 1. Branch setup
        if not state.get("pr_branch"):
            state["pr_branch"] = f"jules-task-{task['number']}-{int(time.time())}"
            pr_builder.checkout_branch(state["pr_branch"], create=True)
            save_state(state, state_file)

        if args.mode == "router":
            await run_router_loop(task, state, state_file, plan_dir, repo, args,
                                  code_builder, pr_builder, test_runner, resolver_spoke, test_builder, code_reviewer)
            continue
            
        # 2. Outer loop: Retrying Code Builder + Verifications (Deterministic Mode)
        retry_count = 0
        feedback = None
        while retry_count < 5:
            # 2a. Code Builder
            if not state.get("jules_session_id") and not state.get("pr_number"):
                build_res = await code_builder.implement_task(
                    task["description"],
                    revision_feedback=feedback,
                    pr_branch=state["pr_branch"],
                    pr_number=state.get("pr_number")
                )
                
                if build_res.get("mode") == "jules":
                    state["jules_session_id"] = build_res["session_id"]
                    save_state(state, state_file)
                elif build_res.get("mode") == "fallback":
                    print("💾 Committing API changes locally...")
                    if not args.dry_run:
                        run_cmd(["git", "add", "."], cwd=plan_dir)
                        run_cmd(["git", "commit", "--no-verify", "-m",
                                 f"feat: implement task {task['number']} via API fallback"], cwd=plan_dir)
                        run_cmd(["git", "push", "origin", state["pr_branch"], "--no-verify"], cwd=plan_dir)
                    else:
                        print("[DRY-RUN] Committing and pushing API fallback changes")
                else:
                    # Code builder produced no changes — count this as a retry, not a hard crash
                    retry_count += 1
                    reason = build_res.get('reason', 'unknown')
                    print(f"\u26a0\ufe0f Code builder produced no changes: {reason} "
                          f"(retry {retry_count}/5 — will try again with broader context)")
                    if retry_count >= 5:
                        break  # fall through to skip logic below
                    await asyncio.sleep(3)
                    continue

            # 2b. Pull and apply patch (if Jules mode was used)
            if state.get("jules_session_id"):
                session_id = state["jules_session_id"]
                print(f"⏳ Resuming monitoring of Jules Session {session_id}...")
                status = await code_builder.monitor_jules_session(session_id)
                if status != "SUCCESS":
                    print(f"❌ Jules session failed during resumed monitoring ({status}). Falling back to Code Builder agent fallback...")
                    state["jules_session_id"] = None
                    save_state(state, state_file)
                    continue
                
                commit_msg = f"feat: implement task {task['number']} changes from jules session {session_id}"
                success = pr_builder.apply_patch_and_push(session_id, state["pr_branch"], commit_msg)
                state["jules_session_id"] = None
                save_state(state, state_file)
                if not success:
                    print("❌ Failed to apply patch.")
                    sys.exit(1)

            # 2c. Create PR if missing
            if not state.get("pr_number"):
                session_link = f"https://jules.google.com/task/{state.get('jules_session_id', 'unknown')}" if state.get("jules_session_id") else "Gemini API"
                pr_number = pr_builder.create_pr(task["number"], task["title"], state["pr_branch"], session_link)
                if pr_number:
                    state["pr_number"] = pr_number
                    save_state(state, state_file)
                else:
                    print("❌ Failed to create PR.")
                    sys.exit(1)

            # 2c.5. Generate tests via Test Builder (Jules or LLM) — runs once per task
            if not state.get("tests_generated"):
                print("\n🔬 Invoking Test Builder spoke...")
                pr_diff_for_tests = pr_builder.get_pr_diff(state["pr_number"])
                if pr_diff_for_tests:
                    test_result = await test_builder.generate_tests(
                        task["description"], pr_diff_for_tests, state["pr_branch"]
                    )
                    # Mark as generated/attempted so we don't endlessly retry if the API is down
                    state["tests_generated"] = True
                    save_state(state, state_file)
                else:
                    print("⚠️ Could not fetch PR diff for test generation. Skipping.")
                    state["tests_generated"] = True
                    save_state(state, state_file)

            # 2d. Run local tests / lint
            test_result = test_runner.run_checks()
            env_failures  = test_result.get("env_failures", [])
            code_failures = test_result.get("code_failures", [])

            # --- Environment issues: auto-remediate, don't send to code builder ---
            if env_failures:
                all_err_text = test_runner.format_failures(env_failures)
                print(f"\\n🔧 Environment issue detected — attempting auto-remediation...")
                
                if state.get("env_remediations", 0) >= 2:
                    print("⚠️ Max environment remediations (2) reached. Treating as code failure.")
                    fixed = False
                else:
                    fixed = auto_remediate_environment(plan_dir, all_err_text)
                    if not fixed:
                        fixed = await resolver_spoke.resolve(all_err_text)
                    
                if fixed:
                    state["env_remediations"] = state.get("env_remediations", 0) + 1
                    print("✅ Remediation applied. Re-running tests (code builder NOT invoked).")
                    # Don't reset pr_number or tests_generated — just retry the test step
                    save_state(state, state_file)
                    continue  # loop back — code builder skipped (pr_number still set)
                else:
                    print("⚠️ Could not auto-remediate. Treating as code failure for retry.")
                    code_failures.extend(env_failures)  # fall through to normal retry

            if code_failures:
                retry_count += 1
                feedback = test_runner.format_failures(code_failures)
                print(f"\n\u26a0\ufe0f Verification failed (Retry {retry_count}/5):\n{feedback}")
                state["pr_number"] = None  # force new commits
                save_state(state, state_file)
                continue
            
            # 2e. Run Code Reviewer
            diff = pr_builder.get_pr_diff(state["pr_number"])
            if not diff:
                print("❌ Failed to get PR diff.")
                sys.exit(1)

            review_res = await code_reviewer.review_diff(task["description"], diff)
            if not review_res:
                print("❌ Code Reviewer failed to return a response.")
                sys.exit(1)
            print(f"\n--- CODE REVIEW OUTPUT ---\n{review_res}\n--------------------------\n")

            if "APPROVED" in review_res.upper() and "NOT APPROVED" not in review_res.upper() and "REJECTED" not in review_res.upper():
                print(f"✅ Code review APPROVED for Task {task['number']}!")
                # Merge PR automatically
                if pr_builder.merge_pr(state["pr_number"], args.merge_method):
                    sync_and_tag_plan(args.plan, task["number"], dry_run=args.dry_run)
                    send_notification("Task Completed", f"Task {task['number']} merged!")
                    
                    state["completed_tasks"].append(task["number"])
                    state["current_task_idx"] += 1
                    state["pr_branch"] = None
                    state["pr_number"] = None
                    state["loop_count"] = 0
                    state["env_remediations"] = 0
                    state["tests_generated"] = False
                    save_state(state, state_file)
                    break
                else:
                    print("❌ Failed to merge PR.")
                    sys.exit(1)
            else:
                retry_count += 1
                feedback = review_res
                print(f"❌ Review rejected (Retry {retry_count}/5). Requesting revision from builder...")
                # Comment on the PR
                if not args.dry_run:
                    comment_body = f"@jules please address code review comments:\n\n{review_res}"
                    run_cmd(["gh", "pr", "comment", str(state["pr_number"]), "--repo", repo, "--body", comment_body], cwd=plan_dir)
                else:
                    print(f"[DRY-RUN] Commenting code reviewer feedback on PR #{state['pr_number']}: {review_res[:50]}...")
                
                # Reset pr_number to force building next revision
                state["pr_number"] = None
                save_state(state, state_file)
                continue

        if retry_count >= 5:
            send_notification("Retry Limit Exceeded", f"Task {task['number']} hit maximum fix loops (5/5).")
            print(f"\n\u26a0\ufe0f Retry limit reached for Task {task['number']}.")
            print("Select recovery action:")
            print("  [A] Approve and merge PR manually")
            print("  [S] Skip this task and continue to next")
            print("  [E] Exit orchestrator")
            choice = input("Choice: ").strip().lower()
            if choice == "a":
                print("🔀 Merging PR...")
                pr_number, _ = pr_builder.find_pr_for_task(task["number"])
                if pr_number and pr_builder.merge_pr(pr_number, args.merge_method):
                    sync_and_tag_plan(args.plan, task["number"], dry_run=args.dry_run)
                    state["completed_tasks"].append(task["number"])
                    state["current_task_idx"] += 1
                    state["pr_branch"] = None
                    state["pr_number"] = None
                    state["loop_count"] = 0
                    state["env_remediations"] = 0
                    state["tests_generated"] = False
                    save_state(state, state_file)
                else:
                    print("\u274c Failed to resolve PR manually. Exiting.")
                    sys.exit(1)
            elif choice == "s":
                print(f"\u23ed\ufe0f Skipping Task {task['number']} and marking as [SKIPPED] in plan...")
                sync_and_tag_plan(args.plan, task["number"], tag="SKIPPED", dry_run=args.dry_run)
                send_notification("Task Skipped", f"Task {task['number']} skipped after 5 failed retries.")
                state["completed_tasks"].append(task["number"])
                state["current_task_idx"] += 1
                state["pr_branch"] = None
                state["pr_number"] = None
                state["loop_count"] = 0
                state["env_remediations"] = 0
                state["tests_generated"] = False
                save_state(state, state_file)
            else:
                print("\u274c Exiting orchestrator.")
                sys.exit(1)

    print("\n🏁 ALL TASKS COMPLETED SUCCESSFULLY! Wheel-Spoke Orchestrator finished.")
    send_notification("Orchestrator Finished", "All tasks in the plan are completed!")
    if os.path.exists(state_file):
        os.remove(state_file)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wheel-and-Spoke Agent Orchestrator")
    parser.add_argument("--plan", required=True, help="Path to markdown plan file")
    parser.add_argument("--repo", help="GitHub repo (auto-detected if omitted)")
    parser.add_argument("--merge-method", choices=["merge", "squash", "rebase"], default="merge", help="PR merge method")
    parser.add_argument("--start-task", type=int, help="Task number to start/resume from")
    parser.add_argument("--jules-handle", default="@jules", help="Jules bot handle")
    parser.add_argument("--dry-run", action="store_true", help="Run the orchestrator loop with mocked spoke responses for verification")
    parser.add_argument("--mode", choices=["deterministic", "router"], default="router", help="Execution mode (router uses LLM for state transitions)")

    args = parser.parse_args()
    asyncio.run(run_orchestrator(args))
