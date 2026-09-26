#!/usr/bin/env python3
"""Publish Home Assistant App metadata into a thin catalog repository."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

FILES_TO_COPY = (
    "config.yaml",
    "README.md",
    "DOCS.md",
    "icon.png",
    "logo.png",
    "apparmor.txt",
)
DIRS_TO_COPY = ("translations",)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--image", required=True)
    return parser.parse_args()


def validate_scalar(name: str, value: str) -> None:
    if not value or "\n" in value or "\r" in value:
        raise SystemExit(f"Invalid {name}")


def yaml_scalar(text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*['\"]?([^'\"\n#]+)['\"]?\s*(?:#.*)?$", text)
    return match.group(1).strip() if match else None


def replace_top_level(text: str, key: str, value: str, *, required: bool) -> str:
    pattern = re.compile(rf"(?m)^{re.escape(key)}:\s*.*$")
    replacement = f'{key}: "{value}"'
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    if required:
        raise SystemExit(f"Source config.yaml does not contain top-level '{key}'")
    if not text.endswith("\n"):
        text += "\n"
    return text + replacement + "\n"


def git_output(source: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def generate_changelog(
    source: Path, destination: Path, repository: str, source_sha: str, version: str
) -> str:
    changelog = destination / "CHANGELOG.md"
    previous_changelog = changelog.is_file()
    if not previous_changelog:
        changelog = source / "CHANGELOG.md"
    history = changelog.read_text(encoding="utf-8") if changelog.is_file() else ""

    previous_config = destination / "config.yaml"
    previous_version = (
        yaml_scalar(previous_config.read_text(encoding="utf-8"), "version")
        if previous_config.is_file()
        else None
    )
    publication = destination / ".publication.json"
    previous_sha = None
    if publication.is_file():
        previous = json.loads(publication.read_text(encoding="utf-8"))
        if previous["source_repository"] == repository:
            previous_sha = previous["source_sha"]
            if (
                previous_sha == source_sha
                and previous_version == version
                and previous_changelog
            ):
                return history
    elif previous_version and re.fullmatch(r"[0-9a-f]{7,40}", previous_version):
        # Existing edge entries use a short source SHA as their version.
        previous_sha = previous_version

    if previous_sha:
        resolved = subprocess.run(
            [
                "git", "-C", str(source), "rev-parse", "--verify", "--quiet",
                "--end-of-options", f"{previous_sha}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        previous_sha = resolved.stdout.strip() if resolved.returncode == 0 else None
    if previous_sha:
        ancestor = subprocess.run(
            [
                "git", "-C", str(source), "merge-base", "--is-ancestor",
                previous_sha, source_sha,
            ],
            capture_output=True,
        )
        if ancestor.returncode != 0:
            previous_sha = None

    url = f"https://github.com/{repository}"
    lines = [f"## {version}", ""]
    if previous_sha == source_sha:
        lines.append(
            f"- No source changes; published [{source_sha[:7]}]({url}/commit/{source_sha})."
        )
    else:
        revision = f"{previous_sha}..{source_sha}" if previous_sha else source_sha
        log_args = [] if previous_sha else ["-1"]
        commits = git_output(source, "log", "--format=%H%x09%s", *log_args, revision, "--")
        if not previous_sha:
            reason = (
                "Previous publication cannot be compared with this source history"
                if previous_version
                else "Initial publication"
            )
            lines.extend([f"{reason}; showing the published commit only.", ""])
        for commit in commits.splitlines():
            sha, _, subject = commit.partition("\t")
            subject = subject or "No commit message"
            subject = re.sub(r"([\\`*_{}\[\]<>()!#|])", r"\\\1", subject)
            lines.append(f"- {subject} ([{sha[:7]}]({url}/commit/{sha}))")
        if previous_sha:
            lines.extend(
                ["", f"[Full changelog]({url}/compare/{previous_sha}...{source_sha})"]
            )

    title = "# Changelog"
    if history.startswith("# "):
        title, _, history = history.partition("\n")
    entry = "\n".join(lines)
    return f"{title}\n\n{entry}\n" + (f"\n{history.strip()}\n" if history.strip() else "")


def main() -> None:
    args = parse_args()
    validate_scalar("slug", args.slug)
    validate_scalar("version", args.version)
    validate_scalar("image", args.image)

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.slug):
        raise SystemExit("Invalid app slug")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.source_repository):
        raise SystemExit("Invalid source repository")

    source = args.source.resolve()
    catalog = args.catalog.resolve()
    config = source / "config.yaml"
    if not source.is_dir() or not config.is_file():
        raise SystemExit(f"App source directory is invalid: {source}")

    source_config = config.read_text(encoding="utf-8")
    source_slug = yaml_scalar(source_config, "slug")
    if source_slug != args.slug:
        raise SystemExit(
            f"Payload slug '{args.slug}' does not match source config slug '{source_slug}'"
        )

    destination = catalog / args.slug
    source_sha = git_output(source, "rev-parse", "HEAD")
    changelog = generate_changelog(
        source, destination, args.source_repository, source_sha, args.version
    )
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    for name in FILES_TO_COPY:
        src = source / name
        if name == "README.md" and not src.is_file():
            for parent in (source, *source.parents):
                if (parent / ".git").exists():
                    src = parent / name
                    break
        if src.is_file():
            shutil.copy2(src, destination / name)

    for name in DIRS_TO_COPY:
        src = source / name
        if src.is_dir():
            shutil.copytree(src, destination / name)

    published_config = (destination / "config.yaml").read_text(encoding="utf-8")
    published_config = replace_top_level(
        published_config, "version", args.version, required=True
    )
    published_config = replace_top_level(
        published_config, "image", args.image, required=False
    )
    (destination / "config.yaml").write_text(published_config, encoding="utf-8")
    (destination / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    publication = {"source_repository": args.source_repository, "source_sha": source_sha}
    (destination / ".publication.json").write_text(
        json.dumps(publication, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
