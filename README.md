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

```bash
claude plugin marketplace add <path or repository of this marketplace>
claude plugin install review-guide@review-guide
```

It requires `gh` (logged in), `git` and `python3`. Run it from a clone of the reviewed repository so the agents can search code locally; otherwise the plugin keeps a lightweight clone in `~/.cache/review-guide/`.

## Documentation

- `docs/spec.md`: what the plugin does and why.
- `docs/status.md`: what is built, what was measured, and what comes next.
- `AGENTS.md`: how to work on the plugin.
