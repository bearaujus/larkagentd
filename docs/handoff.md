# Runtime verification

The Gateway receives Lark events, enforces group admission, serves the dashboard and delivers responses to the originating thread. The tool service validates the active turn and group grants before invoking an adapter. Optional account providers translate upstream account choices into public IDs and labels.

Run the checks in [Deployment and acceptance](run.md) against dedicated test groups. Verify group membership, file grants for each supported file type, source excerpts and links, configured MCP transports, and provider model settings. Use disposable PostgreSQL for migration and queue tests.
