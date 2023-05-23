"""Diffs generated files before and after the current commit."""

# Note, this is implemented to be run with "bazel run".


from __future__ import annotations

import abc
import argparse
import contextlib
import logging
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import typing
from collections.abc import Iterator, Sequence, Set
from typing import Any


# Bazel targets that we do not bother diffing.
IGNORED_TARGETS: Set[str] = {
    # Note, keep sorted and leave a comment explaining why a target is
    # excluded.
    #
    # This is not generated code, but instead a downloaded data set.
    "//:download_mnist",
}


def main(argv: list[str]) -> None:
    """Entry point of program."""
    parser = argparse.ArgumentParser(prog=argv[0], description=__doc__)
    parser.add_argument(
        "--debug", action="store_true", help="If true, sets log level to DEBUG."
    )
    parser.add_argument(
        "--keep-files",
        action="store_true",
        help="If true, do not cleanup generated files.",
    )

    args = parser.parse_args(argv[1:])

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    os.chdir(pathlib.Path(os.environ["BUILD_WORKING_DIRECTORY"]))
    with contextlib.ExitStack() as stack:
        if args.keep_files:
            temp_dir = tempfile.mkdtemp()
        else:
            temp_dir = stack.enter_context(tempfile.TemporaryDirectory())

        run(Sapling(), pathlib.Path(temp_dir))


def run(repo: Repository, temp_dir: pathlib.Path) -> None:
    """Runs the generation and diffing, after all the setup."""
    with parent_commit(repo):
        get_generated_files(temp_dir / "before/")
    get_generated_files(temp_dir / "after/")

    # TODO compare if any codegen targets added or removed.

    # Run a recursive diff between the directory before and after.
    subprocess.run(
        ["diff", "--color", "--recursive", temp_dir / "before", temp_dir / "after"],
        check=True,
    )


@contextlib.contextmanager
def parent_commit(repo: Repository, /) -> Iterator[None]:
    """Context manager that allows for working with the parent commit."""
    commit = repo.whereami()
    repo.goto(CommitId("prev"))
    try:
        yield None
    finally:
        repo.goto(commit)


def get_generated_files(temp_dir: pathlib.Path) -> None:
    """Generates files and replicates their tree under temp_dir."""

    # The basic strategy is to query for all the genrules, filter out
    # the blocklisted targets, build them all, then copy each targets
    # outputs to the output directory.

    bazel = Bazel()

    # TODO Add custom rule targets as well. For example, we have a
    # cmake_configure_rule that is a rule and not a macro, hence it is
    # not caught by this query.

    genrule_targets = set(bazel.query("kind(genrule, //...)"))
    genrule_targets -= IGNORED_TARGETS

    bazel.build(*genrule_targets)

    queries = []
    for target in genrule_targets:
        queries.append(f'labels("out", {target})')
        queries.append(f'labels("outs", {target})')

    outs = bazel.query(" union ".join(queries))
    for out in outs:
        path = label_to_path(out)
        src = "bazel-bin" / path
        dest = temp_dir / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(temp_dir / path)


# Identifies a commit in a version control system.
#
# Need not be an absolute identifier, for example, HEAD~ or .^ are
# valid relative values in Git and Sapling respectively.
CommitId = typing.NewType("CommitId", str)


class Repository(abc.ABC):
    """Subset of repository functionality needed for @parent_commit."""

    @abc.abstractmethod
    def whereami(self, /) -> CommitId:
        """Gets the current commit id."""

    @abc.abstractmethod
    def goto(self, commit: CommitId, /) -> None:
        """Moves the repo to the given commit."""


class Sapling(Repository):
    """Implements Repository for the Sapling version control system."""

    def whereami(self, /) -> CommitId:
        return CommitId(self._run("whereami", stdout=subprocess.PIPE).stdout)

    def goto(self, commit: CommitId, /) -> None:
        self._run("goto", commit)

    def _run(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd = ["sl"]
        cmd.extend(args)
        return subprocess.run(cmd, check=True, **kwargs)


def label_to_path(label: str) -> pathlib.Path:
    """Converts a Bazel label to its workspace relative path."""
    match = _LABEL.fullmatch(label)
    assert match is not None
    return pathlib.Path(match.group(1)) / match.group(2)


class Bazel:
    def __init__(self, path: pathlib.Path = pathlib.Path("bazelisk")) -> None:
        self.path = path

    def build(self, /, *targets: str) -> None:
        subprocess.run(self._make_cmd("build", *targets), check=True)

    def query(self, /, query: str) -> list[str]:
        return subprocess.run(
            self._make_cmd("query", query), stdout=subprocess.PIPE, text=True
        ).stdout.splitlines()

    def _make_cmd(self, /, *args: str) -> Sequence[str]:
        """Creates and logs the command to execute."""
        cmd = [os.fspath(self.path)]
        cmd.extend(args)
        _logger.debug("%s", shlex.join(cmd))
        return cmd


_logger = logging.getLogger(__name__)


# Regular expression pattern matching a Bazel label.
_LABEL = re.compile("//([^:]*):(.*)")


if __name__ == "__main__":
    main(sys.argv)
