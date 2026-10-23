# M6: share a guide as one file

## Outcome

From a finished review you save one `.html` file and send it to a colleague. Opened in any browser, it shows the guide you read: summary, tiles, key facts, diagrams, risks, reading steps, findings with their fact-check status, the file tree and the diffs. Two people discussing the stack talk about the same "step 3" and the same diagram, and the colleague does not wait 5 to 30 minutes or pay for a run that would come out different.

The colleague can also review from it. How much works depends on what they have:

| The colleague has | Read the guide | Comment | Post to GitHub | Know if the PR moved | Ask the agent |
| --- | --- | --- | --- | --- | --- |
| A browser only | yes | yes, kept in the file | one command to paste in a terminal (`gh`) | a link that lists the commits since | no |
| The plugin | yes | yes | from the page, as today | checked, with Rerun | yes |

## Why a file alone cannot post or sync by itself

- **GitHub needs the colleague's identity.** A page opened from disk can call GitHub only with a token typed into it. A personal token pasted into an HTML file that came over Slack is the kind of habit we should not teach, and Doctolib tokens need SSO authorization on top. So a file alone does not post.
- **The repositories are private.** The same applies to checking a PR's current head: without a login, GitHub answers 404.
- **What does work without a token:**
  - the colleague's browser is already logged in to GitHub, so a link opens the right page;
  - their terminal usually has `gh` logged in;
  - if they have the plugin, their local server has both, so the file hands itself over to it.

## The experience

**Saving** (the sender)
- "Share as a file…" in the "⋯" menu, on any built round.
- A short dialog:
  - **What it holds:** the guide, the findings, and the diff of N files, with its size (about 0.5 to 1.5 MB).
  - **What it leaves out:** your drafts, your verdicts, what you marked viewed or reviewed, and your feedback to the agent.
  - **Options**, all off by default: "Include my comments" (shown as yours), "Include questions and answers" (the M5 threads, once M5 is merged), "Hide refuted findings".
  - **Where it can go:** "This file contains source code from `<repo>`. Share it inside Doctolib only." In a self-review, it adds "It includes code that was never pushed."
  - A "Save file" button.
- The browser downloads `review-<repo>-<PRs or branch>-round<n>.html`.

**Opening without the plugin**
- A band at the top: "Shared review guide · built 22 Oct 18:40 from `<tip sha>`", with three actions: "Check for new commits", "Open in my review-guide" (shown only when a local server answers, see below), and "Download with my comments".
- **Check for new commits** opens GitHub's compare view from the commit the guide was built on to each PR's branch (`compare/<review_sha>...<head branch>`). The colleague's browser login does the rest. When it lists commits, the guide describes an older state, and the band says so in the same words.
- **Commenting works as in the plugin:**
  - Add the suggested findings, write your own on a line, and pick a verdict per PR.
  - Drafts are kept in the browser for that file (keyed by review and tip), so a reload does not lose them. This is the one place the plugin uses browser storage, because a file has no server. The dialog and the band say drafts live in this browser only.
  - "Download with my comments" saves a new copy of the file with the drafts inside, to keep them or to send back to the author.
- **Posting:** "Your review" works as in the plugin, except that "Post" becomes "Copy the post command".
  - For each PR with a verdict, it gives one `gh api` command that posts the review with its inline comments on the commit the guide was built on, the same payload `serve.py` sends today. It is shown in full before copying, so the colleague can read it, and pasted in a terminal where `gh` is logged in.
  - If the PR moved, the band's warning repeats there: the comments still land on the reviewed commit, which GitHub shows as outdated where the lines changed.
- **What is gone without the plugin:** Ask, Rerun and the run panel.

**Opening with the plugin**
- When a review-guide server runs on the machine (port 7357 or the port in its `server.json`), the band shows "Open in my review-guide". One click imports the file as a review in the colleague's own inbox:
  - **Their drafts** from the file come along.
  - **Posting** goes from the page through their `gh` login, with the usual guards.
  - **Heads** are checked: a PR that moved shows the stale warning, and Rerun builds a new round from their machine, carrying their drafts over.
  - **Ask** works once their server has a checkout of the repository: it fetches the reviewed commit into its own clone cache, as `fetch.py` does today.
- The import also works without the band: drop the file on the inbox page.
- An imported review carries "shared by `<author>`" in the inbox and in the page header.

**Self-reviews**
- Sending feedback to an agent from someone else's machine makes no sense. In a shared self-review, Send becomes "Download feedback for the author": a small file the author drops on their inbox, which delivers it to their waiting session as if they had sent it.
- This lets a colleague pair on an unpushed stack.

## How it works

**One file, nothing loaded from outside**
- Today the page loads Figtree and JetBrains Mono from Google Fonts and highlight.js from cdnjs. A shared file must work offline, and must make no request when it opens.
- The export inlines:
  - **highlight.js:** vendored in the plugin at the pinned version (11.9.0, as today), about 120 KB.
  - **Fonts:** Figtree and JetBrains Mono as base64 `@font-face`, in the Latin subset and the weights the page uses, about 150 KB. Vendored, with their open licenses (OFL).
- The live page keeps loading them as today.

**What the file carries**
- **The page data:** without what the options leave out.
- **The import bundle:** a gzip and base64 copy of the round's data files, which the colleague's server needs to make the review live: `run.json` without local paths, `guide.json`, the attribution maps, and the reviewers' and verifiers' files. `tip/`, `brief.md` and logs stay out; the importing server rebuilds or fetches them. The bundle adds about 20 to 40% to the file.
- **BOOT:** `{shared: {built, tip, repo, prs, author, options}, live: false}`, with no token and no API base.

**The exported page, without a server**
- **No fetch at all:** no state polling, no heads check, no saving, no prefs. One guard in the fetch helper refuses every request in shared mode.
- **The two exceptions** are the explicit, user-triggered calls:
  - the probe of the local server, a `GET http://127.0.0.1:<port>/api/ping` made only when the page opens, and only to the loopback address;
  - the import.
- **The `gh` command** is built from the same routing the page already uses for posting. Each comment goes to the PR that wrote its line, with `commit_id` set to that PR's reviewed commit and `line`/`start_line` on that commit. The command is a `gh api --method POST repos/<o>/<r>/pulls/<n>/reviews --input -` with the JSON in a quoted heredoc, so the shell does not expand anything in the comment text.

**Import (`serve.py`)**
- **CORS:** `/api/ping` answers with `Access-Control-Allow-Origin: null` for the file page's probe. Nothing else does.
- **Receiving:** "Open in my review-guide" submits a regular form `POST /import` (a top-level navigation, so no CORS is involved) carrying the bundle. The server stores it in `imports/<id>/` and answers with its own confirmation page: "Import the shared review of `<repo>` `<PRs>` from `<author>`, built `<date>`?". That page holds the server token.
- **Confirming:** the review is created only when the colleague confirms on the server's page. A page from anywhere else cannot create a review by posting to the server, because it never sees the token.
- **Creating:** confirmation creates a review with one round from the bundle. The colleague's checkout is resolved (their clone, or the clone cache with the reviewed commit fetched), the round is built with `build.py`, and the page opens. Drafts from the file become the round's `ui.json`.
- **Limits:** the bundle's size is capped at 25 MB, and its JSON is validated against the same checks `build.py --check` runs before anything is written.

**Before saving: size and a secret check**
- **Size:** over 5 MB, the dialog shows the size and the biggest files. Nothing is cut silently.
- **Secret check:** a quick check of the diff text for strings that look like secrets and personal data:
  - secrets: private keys, `AKIA…`, `ghp_…`, JWTs, `password=` in config;
  - personal data: emails, French phone numbers, NIR.

  Hits are listed with file and line, and the button becomes "Save anyway". This is a reminder, not a guarantee, and the dialog says so. It runs on the server with stdlib `re`, about 15 patterns.

## Tests

- **`test_scripts.py`:**
  - **Export:** of the fixture review.
    - It has no loading `src` or `href` outside the file, and no token.
    - It has no drafts by default; with "mine" it has the picked comments; with "refuted=0" it has no refuted findings.
    - The secret check finds a planted `AKIA…` key and an email.
  - **The `gh` payload:** for a comment on each PR, it has the routed line and commit, and its heredoc survives a comment containing `$(...)` and backticks.
  - **Import:** a POST without confirmation creates nothing. Confirmation with the token creates a review that builds, carries the file's drafts and posts through the stub `gh`. A bundle over the cap or failing the checks is refused.
- **`dev/check_page.mjs`:** opens the exported file from disk (`file://`).
  - **With the network blocked:** there is no request except the loopback probe, and no page error. Diagrams render, a box opens its file and Back returns, a draft survives a reload, and "Download with my comments" produces a file that reopens with the draft.
  - **With a fixture server running:** the band offers the import, and the import ends on the new review.
- **Live:**
  - export a real multi-PR review and open it in Chrome and Safari from Downloads;
  - draft two comments and copy the `gh` command for a PR in a scratch repository you own, then run it to see the review land;
  - import the file into a second server home.

## Risks

- **Code leaves the machine in a file.** That is the point of the feature, and also the risk.
  - The dialog says what is in the file and where it may go.
  - The secret and personal data check lists what it finds, and refuses nothing.
  - The plugin uploads nothing.
- **A pasted command posts in the colleague's name.** It is shown in full before copying, it only calls `gh api` on the PR's own repository, and the heredoc is quoted.
- **The local server takes input from a web page.**
  - Only a form post that lands on a confirmation page; nothing is created without the server's token.
  - The import is validated before it is written, and capped in size.
  - The CORS opening is limited to `/api/ping`.
- **Drafts in browser storage, for files only.** They can be lost to cleared site data; "Download with my comments" is the durable way. The live plugin page stays on server storage.
- **Fonts and highlight.js vendored.** About 300 KB in the plugin; updating them becomes a manual step.

## Order

M6 does not depend on M5 to start. Merging after M5 adds the "questions and answers" option and Ask in imported reviews.

1. **Export:** vendored fonts and highlight.js, the export route with its options and checks, and the file's shared mode for reading. Tested.
2. **Commenting in the file:** browser drafts, "Download with my comments", the `gh` command and "Check for new commits".
3. **Import:** probe, form post, confirmation page, review creation, the checkout, and dropping a file on the inbox. Then "Download feedback for the author" for self-reviews.
4. Page check from `file://`, then the live check.

## State

- Phase: built on 2026-10-23, all four steps, checked on two real reviews. Needs the live check by a person (Chrome and Safari from Downloads, a `gh` run on a scratch repository).
- Files: `scripts/share.py` (export, the secret check, bundle reading and staging, the clone-cache checkout), routes in `scripts/serve.py`, the `// ---- Shared file` blocks in `ui/template.html`, the drop zone in `ui/inbox.html`, `ui/vendor/` (fonts, highlight.js 11.9.0, their licenses), `dev/check_shared.mjs`.
- Decisions made while building, where the plan left room:
  - **Export is a POST, not a GET.** `POST /api/r/<id>/<n>/export` takes the token and `{mine, threads, refuted, dry}`; `dry` answers the report (size, hits, counts) for the save dialog, without building the file twice for the user. The file is built from the round's own `index.html` template, so it is as current as the scripts.
  - **Any built round exports**, not only the latest. The sender's drafts (`ui.json`) are the picked comments of that round.
  - **`mine` carries comments, not verdicts.** The seed holds the findings the sender added and the comments they wrote, as drafts of the reader. Verdicts, summaries, viewed marks stay out; the dialog says so.
  - **Questions and answers are in** (M5 merged first): `threads` adds finished threads without their session id, shown read-only in the file. No Ask and no "?" box in a file.
  - **The secret check scans the changed files at the tip and the lines the stack deleted.** Fourteen patterns: private keys, AWS, GitHub, JWT, Slack, Google and Stripe keys, bearer tokens, credentials in a URL, passwords and keys in config; emails, French phone numbers, NIR. It skips lines over 2,000 characters and emails that end in a file extension (`x@me.yaml` in OpenAPI refs). Hits show a masked match, never the secret. No IBAN rule: too many false hits in test data.
  - **Drafts in `localStorage` are keyed by review and tip** (`rg-draft:<key>`), the page state as a whole, wrapped in try/catch. A blocked storage shows in the band. A shared file is a different key when the sender exports a later round.
  - **Download with my comments** serialises the open document (the page rebuilds itself from script, so the rendered parts are emptied first) with the seed replaced by the reader's drafts. The bundle is unchanged.
  - **`gh` command**: `gh api --method POST repos/<o>/<r>/pulls/<n>/reviews --input - <<'REVIEW_GUIDE_JSON'`, the marker lengthened if the JSON contains it. The payload comes from one function on each side, `review_payload` in `serve.py` and `shPayload` in the page; the test and the real-stack check compare them. The repository and PR number are validated before a command is built. "Copy all" joins several.
  - **Check for new commits** uses the PR's head branch (`compare/<review sha>...<branch>`); where the file has no head branch, the PR's `/commits` page. Self-reviews have no such link.
  - **The probe is 7357 only.** A file cannot read `server.json`, so a server on another port is not found by the band; the drop zone on the inbox page covers it. `/api/ping` also answers the preflight Chrome sends for loopback requests from `file:` pages (`Access-Control-Allow-Private-Network`), for that route only.
  - **Import**: the form post (`POST /import`, no token) stages the bundle in `imports/<id>/work`, builds it with `build.py` (which runs the guide and finding checks; the size caps and reference lookups into a checkout are the two `--check` strictnesses it skips, as for any earlier round) and answers with the confirmation page holding the token. Only that page's `POST /api/import/<id>` creates the review. Staged imports older than an hour, and all but the last seven, are deleted.
  - **Review identity**: the same PRs are the same review (a second import adds a round; the confirmation page says so). A shared self-review gets `self:shared:<author>/<repo>@<branch>`, so it never collides with the reader's own branch review.
  - **Checkout**: after the review exists, a thread resolves the clone cache (`repos/<owner>__<repo>`, cloned with `gh repo clone` when missing), fetches the PR heads and the reviewed commits, and prefetches the tip tree. `run.json` gets the path, and the review's `imported.checkout` goes `pending`, then `ready` or `none`. A self-review's unpushed commits cannot be fetched: `none`, and the page says questions cannot read the code. The review reads fine without it.
  - **Imported reviews**: `review.json` carries `imported` (author, built, tip, source review id and round). The inbox row says "shared by"; the page shows a one-line band on the guide view. A shared self-review has no Rerun (its branches are not here), and its Send is "Download feedback for the author".
  - **Feedback file**: `{"review-guide-feedback": 1, review, round, from, layers}`. `from` is the name the reader types (remembered in `localStorage` as `rg-name`). The inbox accepts it as a drop or at `POST /api/feedback-file`, finds the self-review by id, and delivers it as the round's `feedback.json` with "Feedback from <name>." on top, so a waiting `--wait-feedback` prints it. The latest round takes it; a second one for the same round is a 409, as for Send.
  - **Size cap**: the form post accepts three times the bundle cap (form-encoding triples a base64 body at worst); the bundle itself is capped at 25 MB, and at 120 MB once inflated.
  - **The fetch guard** replaces `window.fetch` in a file: every call but the loopback ping is refused with a rejected promise. The one place that guards it is the first lines of the script, before anything can call it.
- Real-stack result (2026-10-23): the 3-layer self-review (3.2 MB of source on disk, 1.2 MB exported, 2 check hits) and the single-PR review (0.88 MB, 1 hit). Opened from disk with every network request blocked: no request but the loopback probe, no page error, at 1600 dark, 1440 light and 390. The `gh` command parsed back to the payload `serve.py` builds for the same drafts, and ran through a stub `gh` with the JSON arriving intact. Import into a second home built the review and kept the drafts.
