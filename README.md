# review-guide

A Claude Code plugin for reviewing pull requests, and stacks of them, without reading every line of every diff.

`/review-guide:review-guide <PR links or a Slack thread>` opens a local page within seconds, showing the changed files and highlighted diffs. A guide fills in a few minutes later:

- what a newcomer must know first;
- how a call travels through the changed components, drawn as sequence diagrams;
- a short reading order anchored on the lines that matter, with the test that covers each step;
- a role (read, skim, skip) for every file;
- suggested comments, each checked against the code by a second agent.

A stack is reviewed as its final state, so a file edited by three PRs appears once. Each comment you keep is still posted on the PR that wrote its line, as one GitHub review per PR, through your own `gh` login. Nothing is posted until you press Post.

## Install

In Claude Code, from the Doctolib marketplace:

```
/plugin marketplace add doctolib/prompts
/plugin install review-guide@doctolib
```

The marketplace entry points at this repository, so an update lands on the next `/reload-plugins` or launch once auto-update is on for the marketplace (`/plugin`, Marketplaces, doctolib). Without the marketplace, `/plugin marketplace add doctolib/review-guide` then `/plugin install review-guide@review-guide` installs it from here. Both need `gh` as the git credential helper for private repositories (`gh auth login`, then `gh auth setup-git`).

It requires `gh` (logged in), `git` and Python 3.9 or later, with no packages to install. Run it from a clone of the reviewed repository so the agents can search code locally; otherwise the plugin keeps a lightweight clone in `~/.cache/review-guide/`.

## Release

Commits follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/): `feat(cheat-123): …`, `fix: …`, `docs: …`. On each merge to `main`, release-please opens or updates a release PR that bumps `version` in `plugins/review-guide/.claude-plugin/plugin.json`, writes `plugins/review-guide/CHANGELOG.md` and, once merged, tags the release. Teammates receive the update when the version changes. Run the checks in `AGENTS.md` before merging.

## Documentation

- `docs/spec.md`: what the plugin does and why.
- `docs/status.md`: what is built, what was measured, and what comes next.
- `AGENTS.md`: how to work on the plugin.
