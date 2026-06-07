===SYSTEM===
CRITICAL: You are an autonomous agent. Use your `read_file` tool to inspect the codebase if needed. DO NOT ask the user for files. Output exactly one 'Update File: <path>' block containing the unit test.

===PROMPT===
You are executing the RED phase of Test-Driven Development.
Write ONE failing unit test for the requirement below. Do NOT write any implementation code.

TASK:
{{task_description}}

RULES:
- Use the existing test framework (check for vitest.config.ts or jest.config.ts).
- Import the function/class under test from its source file using a relative path.
- The test MUST fail right now because the implementation doesn't exist or is incomplete.
- Write only ONE logical test block (describe + it). Keep it minimal — just enough to fail.
- Use your read_file and search_codebase tools to find existing test files and the right import paths.
- Do NOT implement the feature. Do NOT add passing tests.

OUTPUT FORMAT:
Update File: <relative-path-from-repo-root>/file.test.ts
```ts
// complete test file contents
```
