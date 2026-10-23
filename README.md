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

One-time setup, if `gh` is not already your git credential helper (the repository is private):

```bash
gh auth login
gh auth setup-git
```

Then, in Claude Code:

```
/plugin marketplace add dogganidhal/review-guide
/plugin install review-guide@review-guide
```

To get updates without doing anything, open `/plugin`, pick Marketplaces, review-guide, and enable auto-update. Updates apply after `/reload-plugins` or the next launch.

It requires `gh` (logged in), `git` and Python 3.9 or later, with no packages to install. Run it from a clone of the reviewed repository so the agents can search code locally; otherwise the plugin keeps a lightweight clone in `~/.cache/review-guide/`.

## Release

Teammates only receive an update when `version` in `plugins/review-guide/.claude-plugin/plugin.json` changes. To release:

1. Run the checks in `AGENTS.md`.
2. Bump `version` (semver), commit, and merge to `main`.
3. Tag it: `git tag v<version> && git push --tags`.

## Documentation

- `docs/spec.md`: what the plugin does and why.
- `docs/status.md`: what is built, what was measured, and what comes next.
- `AGENTS.md`: how to work on the plugin.
