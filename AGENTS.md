# Working on LarkAgentD

- Read [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md). Develop on a feature branch and open a PR against `main`. Use a title such as `feat: add group replies` or `chore: refresh tooling`.
- Keep public code and fixtures tenant-neutral. Private adapters, deployment settings and credentials belong outside this repo. Preserve unrelated work.
- The Gateway owns Lark events, admission and thread delivery. DMs handle deterministic commands. Bind model tools to the live caller, group, thread and credential version. Check current roles, source visibility and file grants at use time.
- Keep external calls outside DB transactions. Preserve queue leases, retry deduplication and content-free diagnostics. Build notification targets from verified Lark IDs.
- When tool definitions change, regenerate `hermes_plugin/larkagentd_tools/tools.json` from `larkagentd.toolservice.tools.specs()`. Run the checks in the contribution guide with a disposable database and mocked external services.
- Commit, push, deploy or send live Lark messages only with task authorization.
