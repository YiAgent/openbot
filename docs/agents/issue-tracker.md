# Issue tracker: GitHub

Issues and PRDs for this repo live as **GitHub issues**. Use the `gh` CLI for all operations.

> **Note**: At the time this file was written, the repo had not been `git init`-ed yet and had no GitHub remote. Before any `gh` command will work, the repo must be:
> 1. `git init`-ed locally
> 2. pushed to a GitHub repo named `openbot` (PRD §13 #1 locks the name)
>
> Skills that try to write to the tracker before this is done should pause and surface the prerequisite.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v` — `gh` does this automatically when run inside a clone.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.
