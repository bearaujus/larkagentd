# Open a pull request

1. Fetch `origin` and start from current `origin/main`. Choose the next free numbered feature branch for your handle, for example `your-handle/patch-1`.
2. Make one focused change. For local development, run `make setup` and `npm --prefix frontend ci`. Point `TEST_DATABASE_URL` at disposable PostgreSQL. Run `make check` and, for UI changes, `make ui-check ui-build ui-test`.
3. Review `git diff --cached` before committing. Include synthetic fixtures only. Start commit and PR titles with `feat:` for a feature, `fix:` for a bug, `docs:` for documentation, `refactor:` for restructuring, `test:` for coverage or `chore:` for upkeep.
4. Push the feature branch and open a GitHub PR against `main`. Describe the change, the checks you ran, and any live verification still needed.

```sh
git fetch origin
git switch -c your-handle/patch-1 origin/main
# Edit, test, stage and commit the intended files.
git push -u origin HEAD
gh pr create --base main --head "$(git branch --show-current)"
```

Use the PR review and merge flow for all changes to `main`.
