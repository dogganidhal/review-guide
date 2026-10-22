# Working on review-guide

A Claude Code plugin that turns GitHub PRs and stacks into a guided local review page. Read `docs/spec.md` for what it must do, and `docs/status.md` for where the work stands and what comes next.

## Layout

```
.claude-plugin/marketplace.json            marketplace with one plugin
plugins/review-guide/
  .claude-plugin/plugin.json
  agents/guide-writer.md                   writes <stack>/guide.json
  agents/comment-verifier.md               writes <stack>/verify.json
  agents/answerer.md                       answers a question asked from the page; read-only, proposes an action block
  skills/review-guide/
    SKILL.md                               PR review: sends PRs to the server, reports
    references/guide-schema.md             contract between agents and scripts
    scripts/fetch.py                       GitHub and git data, stacks, attribution, brief
    scripts/build.py                       validation and rendering
    scripts/serve.py                       the one server: inbox, rounds, saved page state, posting
    scripts/ask.py                         questions from the page: threads, the answering agent, proposed actions
    scripts/rerun.py                       one round: fetch, then the agents headless
    scripts/test_scripts.py                offline test
    ui/template.html                       the review page (data injected at /*DATA*/null)
    ui/inbox.html                          the server's home: every review
  skills/self-review/SKILL.md              self-review of local work; feedback goes back to the session
docs/spec.md, docs/status.md
```

## Rules

- **Contract changes:** a change to `guide.json` or `verify.json` touches `references/guide-schema.md`, `build.py` (validation and rendering), the agent prompts, `template.html`, and `test_scripts.py`. Update them together.
- **Speed is a requirement.** Keep the agents' tool budgets. Anything an agent would explore for belongs in `brief.md`, computed by `fetch.py`.
- **Writing in prompts and page:** no AI tells (praise, "Consider…", contrasts with nobody, dashes everywhere). The page shows short text; `build.py` enforces the limits.
- **Design:** Doctolib brand. Figtree, Nightshift, Oxylove, Skyjoy, Honeywell, Berryblush, and a neutral near-black dark theme (never navy). Do not add libraries beyond Figtree and highlight.js without a reason.
- **Posting:** the agents never post. Posting happens only from the page, through `serve.py` and its guards.

## Checks

```bash
python3 -I plugins/review-guide/skills/review-guide/scripts/test_scripts.py
claude plugin validate ./ --strict
claude plugin validate ./plugins/review-guide --strict
```

To work on the UI without agents:

```bash
python3 -I plugins/review-guide/skills/review-guide/scripts/test_scripts.py --keep /tmp/rg-home
python3 plugins/review-guide/skills/review-guide/scripts/serve.py --home /tmp/rg-home --port 0
```

For a real end-to-end run, follow `SKILL.md`. Running it from a clone of the reviewed repository lets the agents search code locally.
