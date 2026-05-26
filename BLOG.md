# The graph was already there

*On building a code context engine for AI agents, and why the most interesting piece of it ended up living inside a git commit message.*

---

For the last few months I've been building a thing called **Mimir**. It's a code context engine for LLMs: you point it at one or more repositories, it parses them with tree-sitter, stitches the results into a typed graph of calls, imports, inheritance, API calls and cross-repo links, and then — when you or an agent asks "what do I need to know to touch this function?" — it walks that graph to assemble a minimal, token-budgeted context bundle. Think of it as the piece that sits between your code and whichever LLM you're handing code to.

If you stopped reading there, that would sound like yet another RAG-over-code tool. And for the first couple of weeks, honestly, that's what it was. You parse, you embed, you search. Cosine similarity finds your thing. Ship it. Stop typing.

The reason I'm writing this post is that one night, a few weeks into the project, I had the kind of realisation that makes you sit still for a minute before touching the keyboard again. I had already built a code graph. I was already walking it to assemble context. And I was staring at a completely different problem — architectural review for AI-generated code — wondering how to build it, as if it were a new system.

It wasn't a new system. The graph was already there.

---

## What the graph unlocks that RAG doesn't

Let me back up. The thing that makes Mimir different from "vector search over your repo" is that it doesn't treat code as text. It treats code as a graph, and the graph has types.

Nodes are what you'd expect — files, classes, functions, endpoints, config blocks. Edges are the interesting part: `IMPORTS`, `CALLS`, `INHERITS`, `IMPLEMENTS`, `USES_TYPE`, `READS_CONFIG`, `EXPOSES_API`, `API_CALLS` (cross-repo), `SHARED_LIB`, `PROTO_DEFINES`. When you query for context, Mimir does a hybrid search (semantic + BM25 + name/path), seeds a handful of starting nodes, then expands along those typed edges with a beam search, re-ranks with quality and recency signals, and only at the very end crunches everything into your token budget and topologically orders it so dependencies come before dependents.

That last step is the thing RAG can't do. Vector search over files gives you *chunks that mention the same words*. The graph gives you *the set of things that have to be true for this change to make sense*: the interfaces it implements, the things that call it, the config it reads, the types it flows through. That's not a retrieval problem. That's a structural one. And you can't solve structural problems with cosine similarity, no matter how clever your embedding model is.

Fine. Great. I had the graph. I had context assembly. I shipped an MCP server. Developers (and agents) could ask "show me what I need to touch `ApprovalService`" and get back a coherent, ordered bundle that fit in 4000 tokens. I was pretty pleased with myself.

Then I started thinking about what comes after the LLM writes the change.

---

## The unspoken promise of the graph

Here is something that happens when you give a capable coding agent a coding task. It does the task. The code compiles. The tests, if you have them, pass. The review comment is "nice, shipping it." And then three weeks later, someone notices that a domain model now imports from the infrastructure layer, a module that was supposed to be a stable port has grown an outbound dependency on a concrete adapter, and the "small" change to a shared type is actually being consumed by forty-three other call sites that nobody thought to check.

The agent did nothing wrong. It did the task. But the code doesn't know what the architecture of the project is supposed to look like, and neither does the LLM, and neither does the review system, and the combination of those three things is how architectures quietly die.

When I noticed this, my first instinct was "oh, this is a new product I should build" — an architectural linter of some kind. Maybe wrap Semgrep. Maybe write rules. And then I remembered the graph.

I already had a typed graph of every dependency in the repo. If you wanted to ask "is this change introducing a dependency from `*/domain/*` to `*/infra/*`?", that wasn't a new problem. That was `graph.new_edges.filter(src matches pattern and dst matches pattern)`. If you wanted to ask "does this change introduce a cycle?" — same graph, `nx.simple_cycles` on the scoped subgraph, keep the ones that contain at least one new edge. "What's the blast radius of this change?" Run impact analysis from the modified nodes along the reverse edges, bound by a hop count, count the touched nodes.

Every single architectural question I cared about was **already expressible as a query on the graph I had built for context retrieval.**

That's the moment Mimir stopped being "a context engine" in my head and became "an engine that answers structural questions about code, one of which happens to be 'what's the minimal context for this query'". Retrieval and guardrails aren't two products. They're the same product asking the graph two different questions.

I wrote a rule engine on top of the existing graph in about a day. Five rule types: `dependency_ban`, `cycle_detection`, `metric_threshold` (afferent/efferent coupling, instability), `impact_threshold` (bounded blast radius), and `file_scope_ban` (protect certain paths). All five share the same underlying machinery. The rule configuration is a YAML file, severities are `warning`, `error`, `block`, and the CLI command is `mimir guardrail check`, which takes a diff, analyses it against the loaded graph, and tells you what's wrong.

And then I hit the actually interesting part.

---

## The part I got wrong three times

`block`-severity rules were supposed to be the escape valve. The idea was: some files are so load-bearing — a DI container, a port interface, a config schema — that even a legitimate change requires a human to explicitly sign off. Warnings and errors can't express that. You need a third severity that says "this is going to fail CI until a human tells me, in writing, that it's fine."

I called this "approvals". And I built it the way you'd build it if you were sketching a backend feature on a whiteboard.

**Version one** was a YAML file in a directory called `.mimir/approvals/`, checked into git. Running `mimir guardrail request --rules <rule-id>` created a file named something like `apr-6c971b3c.yaml`. Running `mimir guardrail approve <id> --reason "..."` mutated the `status: pending` field to `status: approved` and recorded the approver's git identity. An `ApprovalService` in the service layer handled CRUD. There was a TTL. There was a `revoke` command. There was a `clean` command for expired entries. There was a `status` command that printed a nicely formatted table. It all worked. I was proud of it.

It was also, I gradually realised, awful to use.

Three specific things broke down:

1. **`mimir guardrail check` auto-created approval requests on top of whatever `mimir guardrail request` had already made.** So there were two creation paths, and if you ran them in the wrong order you got ghost files. The CLI was technically correct and humanly confusing.
2. **The matching logic used `(rule_id, branch, not-expired)` only.** It stored a `diff_hash` in the YAML, for audit, but never actually checked it. So once you'd been approved for `protect-container` on `feature/foo`, you could push another five-hundred-line rewrite of `container.py` to the same branch and the approval would still apply, because neither the rule id nor the branch name had changed.
3. **CI could read approvals but not grant them.** The approval command had to run locally, which meant an approver had to clone the branch, run the command, commit the new YAML file, and push. The PR comment from CI could tell you "this is pending approval", but it couldn't give you anything to click, and the request ID existed only in the local terminal of whoever had last run `check`. Nobody on the PR could see it.

I lived with this for a couple of weeks. Each time I ran into one of those three problems, I would think "I should fix that", and then mentally draw the fix, and each fix added more machinery to something that was already too much machinery. Diff-hash enforcement. A per-rule approver allowlist. Exposing the request ID in the GitHub PR comment formatter. A bot to listen for comments. A bot to dispatch re-runs. You know how this story goes.

I eventually did what I should have done on day one, which is sit down with a blank file and write a design document without looking at the code. The brief was one sentence: *"If I were designing this from scratch, knowing what I know now, what would it look like?"*

The answer, after about two hours of squinting at it, was: **delete the entire approval database.**

---

## The commit message was the approval

Here is the shape the new design landed on.

There is no persisted approval object. No YAML directory. No registry. No TTL. No `request`, `revoke`, `status`, or `clean` subcommand. The only thing that exists is a `git commit` trailer on the HEAD commit of the branch being checked:

```
approval: protect-container

Mimir-Approved: protect-container
Mimir-Approved-Reason: legal signoff for new DI wiring
```

`mimir guardrail check` reads `git log -1 --format=%B HEAD`, parses trailers, and for every BLOCK violation in its output, checks whether its rule id is listed in `Mimir-Approved:` with a non-empty `Mimir-Approved-Reason:`. That's the entire algorithm. There is a `mimir guardrail approve <rule-ids...> --reason "..."` command — it's one call to `git commit --allow-empty` with the trailer pre-filled. The CLI shrunk from seven subcommands to four. The service layer lost an entire module.

The properties that fell out of this are the part I keep thinking about.

Approvals are **automatically invalidated the moment HEAD moves.** There is no `revoke` command because pushing any new commit without the trailer is the revoke. The branch author can't fake it, because trailers are on a specific commit, and the specific commit is what HEAD points to right now.

Approvals are **branch-local** without any branch-name tracking. There's no "which branch was this approved for" field anywhere. The approval lives on the commit, the commit lives on the branch, done.

Approvals are **the audit trail.** `git log --grep="Mimir-Approved"` is your compliance report. You don't need a JSONL file. You don't need a database. The thing you were going to use for audit (git) was already the thing storing the approval.

And it's **VCS-agnostic.** The original design was YAML-in-git, which is technically VCS-agnostic, but the UX was pushing me toward a GitHub-specific bot to actually make it tolerable. The commit-trailer design needs nothing beyond `git log` and a regex. It works identically on GitHub, GitLab, Gitea, or a server on your desk with no internet.

There are tradeoffs. Squash-merges will concatenate the commit body by default, which preserves the trailer, but a team that customises the squash message to strip them would lose the trailer on main — I decided this is fine, because the approval was enforced before merge, and the post-merge audit is informational. I initially added a "self-approval guard" that required the HEAD committer to not be the sole author of the branch, and then removed it after talking to the user I was building this for, who pointed out that self-approval is fine in a solo project and the audit trail is anyway in `git log`. The system is deliberately lightweight. If you want harder guarantees, you sign your commits.

Here's the part that ties back to the retrieval story: **the guardrail system didn't need a new data model.** It's the same graph. The rule engine is a pure function over a `ChangeSet`. The approval check is a pure function over a commit message. The CLI is 150 lines shorter. The hardest piece of the whole thing — figuring out whether a commit trailer should clear a BLOCK — is nine lines of code, and it's unit-tested in sub-millisecond time with no filesystem, no git, no mocks.

---

## What I learned

I'm going to try to say the thing without being too cute about it.

When you're building a system, you have two kinds of problems: things you don't yet know how to solve, and things you've *over-solved* because you wrote code before you understood the shape of the answer. The second kind is more dangerous, because the code works. The tests pass. It ships. And it's only months later, the third time you try to use it on a real change, that you realise you're paying a UX tax every single day for a data structure you didn't need to have.

The graph-for-retrieval / graph-for-guardrails unification was a case of the first kind. I didn't see it until I'd already built half of it twice, and then it was obvious.

The approval-in-a-commit-trailer was a case of the second. I had built something real, with tests and a service layer and a CLI surface, and the fix wasn't to iterate on it — the fix was to delete it and notice that the underlying need ("this branch, this rule, a human said yes") was already representable in a file I was already tracking in version control.

I keep coming back to the same loose heuristic: *before you add a new data store, check whether the semantics you want already exist somewhere in the system.* A lot of the time they do. A lot of the time the pieces you'd write are re-encoding a fact that git already knows, or the graph already knows, or the filesystem already knows. When that's true, the most principled move is to not write the pieces, and to let whatever was already there carry the meaning for you.

Mimir is open source. The code for the guardrail rewrite I described is in `mimir/services/guardrail_trailers.py` and `apply_approvals` in `mimir/services/guardrail.py`, and it's honestly smaller than the comment block at the top of the original `ApprovalService`. If you want to read something well-named that does nothing, you can still find the old commits in the history — they're a good before/after for the rest of this post.

I'm going to go delete more things now.
