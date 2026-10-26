# Working on review-guide

A Claude Code plugin that turns GitHub PRs and stacks into a guided local review page. Read `docs/spec.md` for what it must do, and `docs/status.md` for where the work stands and what comes next.

## Layout

```
.claude-plugin/marketplace.json            marketplace with one plugin
plugins/review-guide/
  .claude-plugin/plugin.json
  agents/guide-author.md                   writes <stack>/guide/NN-<ref>.html, figures/<ref>.json and files.json (balanced, deep): the guide as a walk through the change
  agents/figure-artist.md                  writes <stack>/figures/<ref>.svg: one picture of the guide, from its spec, in parallel with the prose
  agents/guide-writer.md                   writes <stack>/guide.json (quick): the structured guide
  agents/director.md                       writes <stack>/plan.json: which reviewers the stack needs
  agents/reviewer.md                       writes <stack>/review-<reviewer>.json, from a charge in plan.json or a built-in axis
  agents/comment-verifier.md               writes <stack>/verify.json
  agents/answerer.md                       answers a question asked from the page; read-only, proposes an action block
  skills/review-guide/
    SKILL.md                               PR review: sends PRs to the server, reports
    references/guide-schema.md             contract between agents and scripts
    scripts/fetch.py                       GitHub and git data, stacks, attribution, brief
    scripts/build.py                       validation and rendering
    scripts/guidehtml.py                   sanitizer for the HTML guide: the only tags, classes and colors the page inserts
    scripts/figures.py                     the guide's pictures: placeholders, specs, the required map and timeline
    scripts/serve.py                       the one server: inbox, rounds, saved page state, posting
    scripts/ask.py                         questions from the page: threads, the answering agent, proposed actions
    scripts/rerun.py                       one round: fetch, then the agents headless
    scripts/share.py                       share a guide as one file: export, secret check, import bundle checks, checkout for an import
    scripts/test_scripts.py                offline test
    ui/template.html                       the review page (data injected at /*DATA*/null)
    ui/inbox.html                          the server's home: every review
    ui/vendor/                             Figtree, JetBrains Mono, highlight.js 11.9.0 and their licenses, inlined into shared files
  skills/self-review/SKILL.md              self-review of local work; feedback goes back to the session
docs/spec.md, docs/status.md
CODEOWNERS, release-please-config.json, .release-please-manifest.json, .github/   the prompts marketplace scaffolding: ownership, releases from conventional commits, CI
```

## Rules

- **Model-written HTML:** the guide is inserted into the page, next to the posting token. Everything goes through `guidehtml.py` on the server and `ghParse` in the page; keep both allowlists in step, and add a hostile case to `check_sanitizer` for anything new.
- **Contract changes:** a change to `guide.json`, `guide.html`, `plan.json` or `verify.json` touches `references/guide-schema.md`, `build.py` (validation and rendering), the agent prompts, `template.html`, and `test_scripts.py`. Update them together.
- **Speed is a requirement.** Keep the reviewers', verifier's and director's budgets. The guide author has a time target instead of a read budget: the guide is only useful when it explains the design, and that needs the code. Anything every agent would explore for still belongs in `brief.md`, computed by `fetch.py`.
- **Writing in prompts and page:** no AI tells (praise, "Consider…", contrasts with nobody, dashes everywhere). The structured guide shows short text and `build.py` enforces its limits; the HTML guide is as long as its ideas need.
- **Design:** Doctolib brand. Figtree, Nightshift, Oxylove, Skyjoy, Honeywell, Berryblush, and a neutral near-black dark theme (never navy). Do not add libraries beyond Figtree and highlight.js without a reason.
- **Posting:** the agents never post. Posting happens only from the page, through `serve.py` and its guards.

## Checks

Commit messages follow Conventional Commits (`feat(cheat-123): …`, `fix: …`, `docs: …`); release-please writes the changelog and bumps the version from them, so a commit that changes behavior is a `feat` or a `fix`.

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
