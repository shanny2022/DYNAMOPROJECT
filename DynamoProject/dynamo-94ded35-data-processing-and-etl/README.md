# Dynamo Task Submission

Implement a hard, Terminal-Bench-style task in this repository (Harbor task format).
**The entire task lives in the [`task/`](task/) directory** — a single, self-contained
Harbor task. Everything outside `task/` is review infrastructure you don't need to touch:
`.github/` holds the provided pointer workflows (the actual checks are maintained centrally
by the Dynamo team, not in this repo), and [`references/`](references/) holds read-only
copies of the rubric, the diversity taxonomy, and the approved base-image list for you to
consult. When you open a pull request, automated checks review your task and post feedback
as PR comments. **Iterate on the feedback until every check passes.**

You **fork** this repo, build your task, and open a pull request back (you don't have
write access to the base).

## How to submit (fork → PR)

Requires the [GitHub CLI](https://cli.github.com/) (`gh`). Replace `dynamo-94ded35-data-processing-and-etl`
with the repository you were assigned:

```bash
gh repo fork handshake-project-dynamo/dynamo-94ded35-data-processing-and-etl --clone
cd dynamo-94ded35-data-processing-and-etl
git checkout -b submission
#  ... build the task (see below) ...
git add -A && git commit -m "Task submission"
git push -u origin submission
gh pr create --repo handshake-project-dynamo/dynamo-94ded35-data-processing-and-etl --fill
```

To iterate after reading the feedback: edit, then `git commit` and `git push`.

## What to build (Harbor task format)

Everything you edit is inside the **`task/`** directory. Fill in:

| File | What it is |
|------|------------|
| `task/instruction.md` | The prompt the agent receives — **write it yourself, as a human (no LLM-generated text)**. Use **absolute paths** in backticks (`` `/app/data.csv` ``, never `data.csv`), name every expected output file, describe the "what" not the "how", and keep it **under 1500 tokens**. |
| `task/task.toml` | Metadata + config (the file has inline comments for each field). Fill in `difficulty_explanation`, `solution_explanation`, `verification_explanation`, `task_objective`, `artifact_type` (which must use values from [`diversity-taxonomy.toml`](references/diversity-taxonomy.toml)), and `expert_time_estimate_hours`, and **adjust the verifier and agent `timeout_sec`** to fit your task (raise the agent timeout for long-running tasks). |
| `task/environment/Dockerfile` | The **single** container image — it builds the environment for **both the agent and the verifier**. Start `FROM` one of the pre-approved, digest-pinned base images (see Environment below; another base is allowed only if none fit your dependencies, and is flagged); **bake your verifier deps (e.g. `pytest`) here**; and don't COPY `solution/` or `tests/` into it. |
| `task/solution/solve.sh` (+ helpers) | Your reference solution. It must solve the task by genuine computation. |
| `task/tests/test.sh`, `task/tests/test_outputs.py` | Verifiers — check the agent's actual outputs (no string/source matching); 1:1 with `instruction.md`. Set appropriate tolerances so that every valid approach passes and only genuinely incorrect solutions fail. `test.sh` must **install nothing at verify time** (its deps are baked into `environment/Dockerfile`). |

Pin dependency versions, and make sure each expected output file is named in
`task/instruction.md`.

> The exact criteria your task is graded on are in
> [`dynamo-rubric.toml`](references/dynamo-rubric.toml) — read it to see what
> "good" looks like.

Each scaffold file has inline comments to guide you. More detail on the three folders:

### Environment (`task/environment/`)

`task/environment/Dockerfile` defines the isolated container the agent works in — install
every system package and dependency the task needs there. Tips:

- Start `FROM` one of the **pre-approved base images** (pinned by digest):
  `golang`, `python`, `debian`, `rust`, `node`, `ubuntu`, `eclipse-temurin`, `ruby`,
  `maven`, `gcc` (under `public.ecr.aws/docker/library/`). The exact pinned digests to use
  are in [`check-base-image.sh`](references/check-base-image.sh). If one
  of these fits your task, you **must** use its pre-approved digest. Only if none support
  your dependencies may you use another base — that's allowed but **flagged as a
  (non-blocking) warning**.
- This is **also the verifier's image** — bake your test dependencies here (e.g.
  `pytest`, pinned) so `tests/test.sh` installs nothing at verify time.
- Don't pin apt package versions (they go stale); run `apt-get update` before
  `apt-get install`, and clean the cache with `rm -rf /var/lib/apt/lists/*` in the
  same layer.
- Never COPY `solution/` or `tests/` into this image.
- For input data, put files in `environment/data/` and copy them in (e.g.
  `COPY data /app/data`). Don't put solution or ground-truth data here — the agent
  can read whatever is in its container.
- Avoid committing files over ~100 MB; download them at build time or ask a maintainer.
- Tasks run with **open internet** available (`allow_internet = true` in `task.toml`). Don't
  rely on the network being disabled, and make sure open internet doesn't let the agent simply
  look up the intended answer.

### Solution (`task/solution/`)

`task/solution/solve.sh` is your reference (Oracle) solution — it must complete the task
correctly, which proves the task is solvable. Harbor mounts `solution/` at
`/solution/` and runs `solve.sh`. Keep the real logic in helpers (e.g.
`task/solution/solve.py`) that `solve.sh` calls, and write outputs to the absolute paths
your `instruction.md` names.

### Tests (`task/tests/`)

The verifier runs in the **same environment image** (built from `task/environment/Dockerfile`)
after the agent's run finishes — canonical TB2 has **no separate `tests/Dockerfile`**. Harbor
overlays your `tests/` directory at `/tests` only at verify time. `task/tests/test.sh` is the
entry point — typically it runs your `pytest` files (e.g. `task/tests/test_outputs.py`) with the
`pytest-json-ctrf` plugin and writes `1`/`0` to `/logs/verifier/reward.txt`.

Because the verifier shares the agent's image, **bake every verifier dependency into
`environment/Dockerfile`** (pinned) — `test.sh` must install or download **nothing** at verify
time. Keep ground truth / expected outputs in `tests/` (overlaid at verify time); never `COPY`
`tests/` or `solution/` into the image, or the agent could read the answer. The verifier reads
the files listed in `artifacts = [...]` in `task.toml`, anything baked into the image, and any
sidecar services declared in `environment/docker-compose.yaml`; pre-create the parent dir of each
declared artifact (`RUN mkdir -p /app`).

Write good tests: cover every behavior the instruction describes, give each test an
informative docstring, reference every expected output file in `instruction.md`, make
it hard for the agent to cheat, and keep ground-truth / oracle data in `tests/` (baked
into the verifier image) — never in `environment/`, where the agent could read it.

## Test it locally first

With [Harbor](https://github.com/laude-institute/harbor) and Docker installed, run:

```bash
harbor run -p task --agent oracle   # your solution should score reward 1.0
harbor run -p task --agent nop      # doing nothing should score reward < 1.0
```

## Before you submit — checklist

Your task must meet all of the following criteria before you open a PR:

- [ ] All behavior checked in `task/tests/` is described in `task/instruction.md`.
- [ ] All behavior described in `task/instruction.md` is checked in `task/tests/`.
- [ ] My `task/tests/` have informative docstrings that describe which behavior they check.
- [ ] My `task/instruction.md` was written by a human.
- [ ] My `task/solution/` was written by a human (with minimal help from a language model).
- [ ] It is hard for the agent to cheat on my task.
- [ ] My task is **novel** — not a reworded or reskinned version of an existing
      Terminal-Bench 2 or 3 task.

## What happens on your PR

On every push, automated checks run and post their results as comments:

- **Static checks** — validate your task's structure and formatting (absolute paths,
  pre-approved base image, Dockerfile hygiene, dependency pinning, diversity labels,
  instruction length, and no verify-time installs in `test.sh`).
- **Rubric review** — grades your task against the Dynamo rubric and posts a
  **PASS / FAIL verdict** with per-criterion feedback.
- **Duplicate check** — compares your `instruction.md` against the existing
  Terminal-Bench 2 & 3 task sets. Your task must be **novel**: a reworded, renamed,
  or reskinned version of an existing benchmark task is rejected. (Sharing a domain or
  technique with an existing task is fine — only solving the same underlying problem is
  a duplicate.)
- **Validation** — builds your environment and confirms your reference solution
  passes the tests while doing nothing fails them.

- **Timeout pre-check (pass@2)** — runs the agent twice under a hard 3600-second (1 hour) budget on
  the agent's run (excludes environment startup) to catch timeout/infra problems and confirm
  the task is plausibly hard before the full trials. **This pass@2 is the gate** — it
  proceeds only if **at least one run is a genuine agent failure** (the agent really tried and
  didn't solve it), and is **blocked** if the agent solved both runs (too easy → make it harder),
  both runs errored out (agent timeout → make the task faster; env/infra timeout → flag an admin
  in Slack), or a failure looks like a task/verifier bug (→ fix the task or tests).
- **Automated Review (blocking)** — after pass@2 passes, an automated deep review checks what the
  earlier stages don't: that every instruction requirement is actually tested, that the deciding
  rules/values your verifier enforces are discoverable from what the agent can see, that the spec
  is internally consistent, and that the agent traces are clean. Its PR comment lists **Blocking
  Issues** (each with the concrete fix inline — the check stays red until you address them and
  push) and **Advisory Notes** (optional improvements that never block). Passing it does not guarantee acceptance — **human reviewers are stricter
  than this check**.
- **Agent trials (pass@5)** — the agent (the same model as the pass@2 gate above)
  attempts your task **5 times**. Your task must be **hard enough that at least 3 of the
  5 attempts are genuine, valid agent failures** — the agent really tried and lost (reward 0, ran
  to completion, sound approach), not a crash, setup/infra error, broken task, or a timeout where
  the agent was still making progress (→ raise the agent timeout). If it's too easy (fewer than 3
  valid fails), the task is rejected. The average score across the 5 runs (`avg@5`) is recorded on
  your task.

When a check fails, its comment tells you what to fix. **Read the feedback, address
it, and push again** — the checks re-run automatically. Your task is ready when the
checks are green and the rubric verdict is ✅ PASS.

When your task is complete, replace this README with a short description of your task
(overview, approach, environment, and how verification works).
