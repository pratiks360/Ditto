"""Commits the knowledge base to its own private repository.

The knowledge base is a directory of Markdown files that the service rewrites
as you fill forms. That history is worth keeping -- it is how you see what an
answer used to say and get it back -- but it holds a name, email, phone,
employment history and a resume, so it lives in its own PRIVATE repository
rather than in the public one.

Run from the host, not from inside the container. The container image has no
git, and giving it push credentials would bake a secret into something that may
be published to a registry. The knowledge base is a bind mount, so the host can
see every change the service makes.

    python kb_autocommit.py                  commit and push if anything changed
    python kb_autocommit.py --no-push        commit only
    python kb_autocommit.py --watch 300      loop, checking every 5 minutes

Safe to run when nothing has changed: it exits without making an empty commit.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_KB = Path(__file__).resolve().parent.parent / "kb"

# Written into kb/.gitignore on init. The index is derived from the Markdown and
# is rebuilt on every start, so committing it only produces noise.
KB_GITIGNORE = ".index/\n"


class GitError(RuntimeError):
    pass


def token_from_env() -> str:
    return (os.environ.get("GIT_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()


def base_config(kb: Path) -> list[str]:
    """`-c` settings applied to every call, so nothing is written to disk.

    Two things a container needs that a laptop does not:

    `safe.directory` — the knowledge base is a bind mount, so its files are
    owned by the host user while git runs as root inside. Without this git
    refuses the repository as "dubious ownership" and every command fails.

    `credential.helper` — Windows Credential Manager is not reachable from a
    container. This helper answers from the environment instead, so the token
    lives in the process and never reaches .git/config, where it would sit in
    plain text inside the very repository being pushed.
    """
    cfg = [f"safe.directory={kb.as_posix()}"]
    if token_from_env():
        cfg.append(
            "credential.helper=!f() { echo username=x-access-token; "
            'echo "password=$GIT_TOKEN"; }; f'
        )
    return cfg


def identity(kb: Path) -> list[str]:
    """A committer, but only when the environment has not named one.

    A fresh container has no git identity and commits fail outright. Supplying
    one unconditionally would rewrite the author on a laptop that already has a
    real name configured, so this only fills a gap.
    """
    have = subprocess.run(
        ["git", "config", "user.email"], cwd=str(kb),
        capture_output=True, text=True,
    )
    if have.returncode == 0 and have.stdout.strip():
        return []
    return ["user.name=Ditto", "user.email=ditto@localhost"]


def git(*args: str, cwd: Path, check: bool = True, config: list[str] | None = None,
        strip_output: bool = True) -> str:
    cmd = ["git"]
    for setting in (config if config is not None else base_config(cwd)):
        cmd += ["-c", setting]
    cmd += list(args)

    env = dict(os.environ)
    token = token_from_env()
    if token:
        env["GIT_TOKEN"] = token          # what the helper above reads
        env["GIT_TERMINAL_PROMPT"] = "0"  # fail loudly rather than hang on a prompt

    proc = subprocess.run(
        cmd, cwd=str(cwd), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip()).replace(token, "***") if token \
            else (proc.stderr.strip() or proc.stdout.strip())
        raise GitError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout.strip() if strip_output else proc.stdout


def is_repo(kb: Path) -> bool:
    return (kb / ".git").exists()


def dirty(kb: Path) -> list[str]:
    """Paths git considers changed. Empty means there is nothing to commit.

    `--untracked-files=all` matters: by default git collapses a wholly untracked
    directory into a single entry, so the first commit of an existing knowledge
    base reports `1 answer` for thirty of them.

    The output must not be stripped. A modified-but-unstaged file is reported as
    " M path", and trimming that leading space shifts every column left, so the
    path is read one character in — which turned `answers` into `nswers`, and
    then into `1 nswer` once the plural was trimmed.
    """
    out = git("status", "--porcelain", "--untracked-files=all", cwd=kb, strip_output=False)
    return [line for line in out.splitlines() if line.strip()]


def summarize(changes: list[str]) -> str:
    """A subject line that says what actually moved.

    `3 answers, 1 profile` beats `Update knowledge base` when you are scrolling
    back through months of commits looking for the day an answer changed.
    """
    buckets: dict[str, int] = {}
    for line in changes:
        path = line[3:].strip().strip('"')
        # A rename reads "old -> new". The destination is what now exists.
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip().strip('"')
        top = path.split("/")[0] if "/" in path else "root"
        buckets[top] = buckets.get(top, 0) + 1

    parts = []
    for name in sorted(buckets):
        count = buckets[name]
        # The buckets are directory names and those are plural: "answers",
        # "applications". One of them is an answer, not an answers.
        label = name[:-1] if count == 1 and name.endswith("s") else name
        parts.append(f"{count} {label}")
    return ", ".join(parts) or "no changes"


def commit(kb: Path, push: bool, remote: str = "origin") -> str | None:
    """Returns the commit subject, or None when there was nothing to do."""
    changes = dirty(kb)
    if not changes:
        return None

    git("add", "-A", cwd=kb)
    # Re-check: `add` can resolve to nothing when every change was ignored.
    if not git("diff", "--cached", "--name-only", cwd=kb):
        return None

    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    subject = f"{summarize(changes)} — {stamp}"
    git("commit", "-q", "-m", subject, cwd=kb,
        config=base_config(kb) + identity(kb))

    if push:
        has_remote = remote in git("remote", cwd=kb).split()
        if not has_remote:
            print(f"  committed, not pushed: no '{remote}' remote configured")
            return subject
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=kb)

        # An empty remote has no branch to rebase onto, and asking for one fails
        # with "couldn't find remote ref" -- which would strand the very first
        # push, the one that matters most.
        upstream_exists = bool(git("ls-remote", "--heads", remote, branch, cwd=kb, check=False))

        if upstream_exists:
            # Rebase first so a knowledge base written from two machines
            # converges instead of rejecting the push. Markdown conflicts still
            # need hands.
            try:
                git("pull", "--rebase", "--autostash", remote, branch, cwd=kb)
            except GitError as e:
                print(f"  pull --rebase failed, not pushing: {e}", file=sys.stderr)
                return subject
            git("push", remote, branch, cwd=kb)
        else:
            git("push", "-u", remote, branch, cwd=kb)

    return subject


def init(kb: Path, remote_url: str | None) -> None:
    """Turns an existing knowledge base directory into a repository."""
    if is_repo(kb):
        print(f"{kb} is already a git repository")
    else:
        git("init", "-q", "-b", "main", cwd=kb)
        print(f"initialised {kb}")

    ignore = kb / ".gitignore"
    if not ignore.exists():
        ignore.write_text(KB_GITIGNORE, encoding="utf-8", newline="\n")
        print("  wrote .gitignore (.index/)")

    if remote_url:
        remotes = git("remote", cwd=kb).split()
        if "origin" in remotes:
            git("remote", "set-url", "origin", remote_url, cwd=kb)
        else:
            git("remote", "add", "origin", remote_url, cwd=kb)
        print(f"  origin -> {remote_url}")
        print("\n  Make sure that repository is PRIVATE before pushing. It will")
        print("  contain your name, email, phone, employment history and resume.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kb", type=Path, default=DEFAULT_KB, help="knowledge base directory")
    ap.add_argument("--no-push", action="store_true", help="commit locally only")
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="keep running, checking this often")
    ap.add_argument("--init", action="store_true", help="make the directory a repository")
    ap.add_argument("--remote", metavar="URL", help="with --init: set origin")
    args = ap.parse_args()

    kb: Path = args.kb.resolve()
    if not kb.is_dir():
        print(f"no such directory: {kb}", file=sys.stderr)
        return 2

    if args.init:
        init(kb, args.remote)
        return 0

    if not is_repo(kb):
        print(f"{kb} is not a git repository. Run with --init first.", file=sys.stderr)
        return 2

    def once() -> None:
        try:
            subject = commit(kb, push=not args.no_push)
            print(f"committed: {subject}" if subject else "nothing to commit")
        except GitError as e:
            print(f"error: {e}", file=sys.stderr)

    if args.watch:
        print(f"watching {kb} every {args.watch}s. Ctrl+C to stop.")
        while True:
            once()
            time.sleep(args.watch)

    once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
