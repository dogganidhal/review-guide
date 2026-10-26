## What

<!-- Two or three lines: the intent and what changes for the reviewer using the page. -->

## Why

<!-- The problem this solves. Link the GitHub issue or the Jira ticket if the title does not carry it. -->

## Checks

- [ ] `python3 -I plugins/review-guide/skills/review-guide/scripts/test_scripts.py` passes
- [ ] `claude plugin validate ./ --strict` and `claude plugin validate ./plugins/review-guide --strict` pass
- [ ] A contract change (`guide.json`, `guide.html`, `plan.json`, `verify.json`) updates the schema doc, `build.py`, the prompts, `template.html` and the test together
- [ ] The commit messages follow Conventional Commits, so release-please can write the changelog
