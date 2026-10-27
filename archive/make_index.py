#!/usr/bin/env python3
"""Writes a navigable index of every branch in the archive.

For each branch: when it was last touched, who touched it, what it was for,
and how far it has drifted from main. With 114 branches and no tags, an index
is the difference between a preserved archive and a pile.

Usage:
    python make_index.py [--repo <path>] [--out <file>]
"""

from __future__ import annotations

import argparse
import datetime
import subprocess
from pathlib import Path

DEFAULT_REPO = Path(r"D:\Code\Projects\X4-Multiplayer-Mod")
DEFAULT_OUT = Path(__file__).resolve().parent / "BRANCHES.md"

SEPARATOR = "|"

# Branches that are worth calling out in prose rather than only in the table.
NOTABLE = {
    "main": "The integration branch. What the mod actually ships.",
    "src-layout-main": "Sources moved to src/ and tools to tools/.",
    "contrib/ferrite-rust-relay": (
        "An earlier Rust port of the relay and the native module, with a "
        "differential parity harness."
    ),
    "contrib/ferrite-rust-core": (
        "A Rust core that binds X4's own FFI at runtime, pinned to a specific "
        "game build. The game-side half, in Rust."
    ),
    "contrib/ferrite-rewrite-base": "The base the rewrite branches build on.",
    "contrib/kimi-dedicated-server": "An earlier dedicated-server experiment.",
    "contrib/ferrite-linux-server": "The server work, for Linux.",
    "contrib/astra-7-5-delivery": "The 7.5.0 delivery the source snapshot was cut from.",
}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout


def collect(repo: Path) -> list[dict]:
    raw = git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)|%(objectname)|%(committerdate:short)|%(authorname)|%(subject)",
        "refs/heads",
    )
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        name, rest = line.split(SEPARATOR, 1)
        if name.startswith("archive/"):
            continue
        sha, rest = rest.split(SEPARATOR, 1)
        date, rest = rest.split(SEPARATOR, 1)
        author, subject = rest.split(SEPARATOR, 1)
        behind, ahead = 0, 0
        counts = git(repo, "rev-list", "--left-right", "--count", f"main...{name}").split()
        if len(counts) == 2:
            behind, ahead = int(counts[0]), int(counts[1])
        rows.append(
            {
                "name": name,
                "sha": sha[:8],
                "date": date,
                "author": author,
                "subject": subject,
                "behind": behind,
                "ahead": ahead,
            }
        )
    rows.sort(key=lambda row: row["date"], reverse=True)
    return rows


def render(rows: list[dict], main_sha: str) -> str:
    taken = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [
        "# X4 Multiplayer - branch archive",
        "",
        "Archive of <https://github.com/Lith7ium/X4-Multiplayer> - the working mod repository.",
        f"Taken {taken} with `main` at `{main_sha[:8]}`.",
        "",
        "The upstream repository states that only the owner and invited collaborators can push",
        "to it. These branches are the project's entire development history, and nothing on",
        "GitHub guarantees a branch stays where it is - a force-push or a delete on the original",
        "would erase it. This archive keeps a copy.",
        "",
        "## Restoring",
        "",
        "```sh",
        "git clone x4-multiplayer-all-refs.bundle restored",
        "cd restored",
        "git branch -a                 # every branch is present",
        "git checkout contrib/ferrite-rust-relay",
        "```",
        "",
        "A copy of every branch is also pushed under `archive/<name>` in",
        "<https://github.com/ComfyChloe/X4-Multiplayer>, so the branches survive even if the",
        "originals are deleted upstream.",
        "",
        "## The branches worth knowing about",
        "",
    ]
    for row in rows:
        if row["name"] in NOTABLE:
            out.append(f"- **`{row['name']}`** - {NOTABLE[row['name']]}")
    out += [
        "",
        "## Every branch",
        "",
        "`behind` and `ahead` are commit counts against `main`: how much of `main` the branch is",
        "missing, and how many commits it has of its own. A branch that is 0 ahead of main has",
        "nothing of its own.",
        "",
        "| Branch | Last commit | Author | behind | ahead | Subject |",
        "|---|---|---|---:|---:|---|",
    ]
    for row in rows:
        subject = row["subject"].replace("|", "/")
        if len(subject) > 88:
            subject = subject[:85] + "..."
        out.append(
            f"| `{row['name']}` | {row['date']} | {row['author']} | "
            f"{row['behind']} | {row['ahead']} | {subject} |"
        )
    out += ["", f"{len(rows)} branches.", ""]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    arguments = parser.parse_args()

    rows = collect(arguments.repo)
    main_sha = git(arguments.repo, "rev-parse", "main").strip()
    arguments.out.write_text(render(rows, main_sha), encoding="utf-8")
    print(f"wrote {arguments.out} ({len(rows)} branches)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
