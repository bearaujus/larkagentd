# Sample group walkthrough

Use synthetic groups and records. The main audience includes employees who do all their
work in Lark and people who occasionally need specialist data or engineering sources.

## Cross-functional rollout

1. Seed workspace Markdown with a generic rollout checklist and group general knowledge
   with a project brief for a fictional appointment-reminder launch. In the same test
   group's earlier thread, agree to pilot with 30 participants and record the decision
   source. Keep a similarly named fixture in a second group with a different target.
2. In Lark ask, “What did we agree to pilot and what still needs an owner?” Expect the
   shared checklist plus this group's decision and a link to the source message. Ask a
   second question that uses the learned decision. The other group's target stays out.
3. Share a Lark brief with the group and ask for a four-slide steering update. Open the
   editable presentation and confirm the title, cited facts, triggering-user ownership
   and group/bot access. Create a checklist Doc and a tracker Sheet from the same brief.
4. Request an edit while the group grant is View. The assistant asks for Edit. Give the
   group Edit and make the same request. Use the sheet tracker to show a matched-row
   preview and requester-only confirmation. Change a matched row before confirming
   to show stale approvals are rejected.
5. Ask for a topical update that requires new public facts. Show the evidence gap and
   approve the explicit public query. Reject or ignore the card for a second request.

## Governance and specialist examples

- Set a personal cap and a tier pool in the dashboard. Use a smaller model to answer a
  routine question and verify its recorded usage and estimate. Lower the pool to show
  a queued request being refused. Open the quota change audit.
- In an enabled group, ask when a particular MR was created. Use the GitLab
  `created_at` value and cite the record. From chat alone, report when it was shared.
- Ask for the number of unique participants who opted in by region in a synthetic
  dataset. Count unique fixture IDs and return only requested counts. Verify the query
  uses the requester’s data access and that a second requester lacking access gets a
  refusal. Ask for a data correction and verify write SQL is rejected by the connector.
- Measure request-to-reply latency and tokens for the same scripted sequence across models.
  Record any missing rates, permission failures and unverified links explicitly.

For the tenant run, use the acceptance checks in [deployment](run.md).
