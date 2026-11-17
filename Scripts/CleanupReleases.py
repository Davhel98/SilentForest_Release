#!/usr/bin/env python3
"""Remove other patch releases in a promoted Stable's major/minor series; keep Git tags."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

VERSION = re.compile(r"v?((?:0|[1-9][0-9]*))\.((?:0|[1-9][0-9]*))\.((?:0|[1-9][0-9]*))(?:\.((?:0|[1-9][0-9]*)))?\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+\Z")


def version_parts(tag):
    match = VERSION.fullmatch(tag) if isinstance(tag, str) else None
    if not match:
        return None
    parts = tuple(int(part or 0) for part in match.groups())
    return parts if all(part <= 2147483647 for part in parts) else None


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError("A positive numeric release ID is required.")
    return value


def stable(release):
    return isinstance(release, dict) and release.get("draft") is False and release.get("prerelease") is False


def updated_at(release):
    text = release.get("updated_at")
    if not isinstance(text, str):
        raise ValueError("Release updated_at is missing; cleanup aborted.")
    timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Release updated_at must include its timezone.")
    return timestamp


def other_patch(release, anchor):
    candidate_version = version_parts(release.get("tag_name"))
    anchor_version = version_parts(anchor.get("tag_name"))
    return (anchor_version is not None and candidate_version is not None
            and release.get("id") != anchor.get("id")
            and candidate_version[:2] == anchor_version[:2]
            and candidate_version[2:] != anchor_version[2:])


def promotion_event(event, repository, release_id):
    """Reject note/title edits and stale/mismatched events before any API calls."""
    event_repository = event.get("repository", {}).get("full_name", "")
    if not isinstance(event_repository, str) or event_repository.lower() != repository.lower():
        raise ValueError("Event repository does not match the cleanup repository.")
    release = event.get("release", {})
    if release.get("id") != release_id:
        raise ValueError("Event release ID does not match the cleanup target.")
    if not stable(release):
        return None
    action = event.get("action")
    # released covers promotion from Pre-release; published covers a new full release.
    # Accept an explicit flag transition as well, never an ordinary metadata edit.
    transition = event.get("changes", {}).get("prerelease", {}).get("from") is True
    if action not in ("published", "released") and not (action == "edited" and transition):
        return None
    return release


class GitHubAPIError(RuntimeError):
    def __init__(self, status, method, endpoint):
        self.status = status
        super().__init__(f"GitHub {method} {endpoint} failed (HTTP {status or 'unknown'}).")


class GitHubReleases:
    def __init__(self, repository):
        if not REPOSITORY.fullmatch(repository) or repository.split("/")[1] in (".", ".."):
            raise ValueError("Repository must be owner/name.")
        self.repository = repository
        self.prefix = "repos/" + repository + "/releases"

    def _request(self, method, endpoint):
        result = subprocess.run(["gh", "api", "--method", method,
                                 "--header", "Accept: application/vnd.github+json",
                                 "--header", "X-GitHub-Api-Version: 2022-11-28", endpoint],
                                text=True, capture_output=True)
        if result.returncode:
            status = re.search(r"\(HTTP ([0-9]{3})\)", result.stderr)
            raise GitHubAPIError(int(status[1]) if status else None, method, endpoint)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def get_release(self, release_id):
        endpoint = self.prefix + "/" + str(positive_id(release_id))
        try:
            release = self._request("GET", endpoint)
        except GitHubAPIError as error:
            if error.status == 404:
                return None
            raise
        if not isinstance(release, dict) or release.get("id") != release_id:
            raise ValueError("GitHub returned an unexpected release ID.")
        return release

    def list_releases(self):
        # Complete pagination before deleting: mutation must not shift the pages.
        releases = {}
        page = 1
        while True:
            batch = self._request("GET", self.prefix + f"?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ValueError("GitHub returned an invalid release list.")
            for release in batch:
                releases[positive_id(release["id"])] = release
            if len(batch) < 100:
                return list(releases.values())
            page += 1

    def delete_release(self, release_id):
        # This endpoint deletes the Release object and its assets, never git/refs.
        endpoint = self.prefix + "/" + str(positive_id(release_id))
        try:
            self._request("DELETE", endpoint)
        except GitHubAPIError as error:
            if error.status == 404:  # Already removed by a previous/parallel attempt.
                return False
            raise
        return True


def cleanup(client, release_id, *, dry_run=False, event_release=None):
    positive_id(release_id)
    report = {"status": "skipped", "retained": None, "planned": [], "deleted": [], "skipped": [], "errors": []}
    anchor = client.get_release(release_id)
    if not stable(anchor) or version_parts(anchor.get("tag_name")) is None:
        report["reason"] = "Target is absent, Non-Stable, a draft, or has an unsupported version tag."
        return report
    report["retained"] = anchor["tag_name"]
    if event_release and (event_release.get("tag_name") != anchor["tag_name"] or updated_at(event_release) != updated_at(anchor)):
        report["reason"] = "Target changed after the promotion event; stale cleanup skipped."
        return report
    anchor_time = updated_at(anchor)
    snapshot = client.list_releases()
    candidates = sorted((item for item in snapshot if other_patch(item, anchor)), key=lambda item: positive_id(item["id"]))
    report["planned"] = [{"id": item["id"], "tag": item["tag_name"]} for item in candidates]
    def newer_stable(item):
        return stable(item) and (updated_at(item) > anchor_time or (event_release is not None and updated_at(item) == anchor_time))
    if any(newer_stable(item) for item in candidates):
        # GitHub timestamps have limited precision. A tie between Stable versions
        # is ambiguous: an explicit manual retry may select which one to retain.
        report["reason"] = "Another Stable in this series was updated at the same time or later; stale cleanup skipped."
        return report
    if dry_run:
        report["status"] = "planned"
        return report
    report["status"] = "completed"
    for planned in candidates:
        try:
            current_anchor = client.get_release(release_id)
            if not stable(current_anchor) or current_anchor.get("tag_name") != anchor["tag_name"] or updated_at(current_anchor) != anchor_time:
                report.update(status="stopped", reason="Target was changed, demoted or deleted during cleanup.")
                break
            candidate = client.get_release(planned["id"])
            if candidate is None or candidate.get("tag_name") != planned["tag_name"] or not other_patch(candidate, anchor):
                report["skipped"].append(planned["tag_name"])
                continue
            if newer_stable(candidate):
                report.update(status="stopped", reason="Another release was promoted or updated during cleanup.")
                break
            if client.delete_release(candidate["id"]):
                report["deleted"].append(candidate["tag_name"])
            else:
                report["skipped"].append(candidate["tag_name"])
        except (RuntimeError, ValueError) as error:
            report["status"] = "failed"
            report["errors"].append(str(error))
            break
    return report


def write_summary(report):
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if not destination:
        return
    lines = ["## Stable release cleanup", "", f"Status: **{report['status']}**.", "",
             "Retained release: `" + str(report["retained"]) + "`.", "",
             "Git tags are preserved; only Release objects/assets are deletion targets.", ""]
    if report.get("reason"):
        lines += [report["reason"], ""]
    for item in report["planned"]:
        result = "deleted" if item["tag"] in report["deleted"] else "not deleted"
        lines.append(f"- `{item['tag']}` (release ID {item['id']}): {result}")
    for error in report["errors"]:
        lines.append("- Error: " + error)
    with Path(destination).open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-id", required=True, type=int)
    parser.add_argument("--event-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    client = GitHubReleases(args.repository)
    event_release = None
    if args.event_file:
        event_release = promotion_event(json.loads(args.event_file.read_text(encoding="utf-8")), args.repository, args.release_id)
        if event_release is None:
            print("Not a Stable promotion/publication; no cleanup.")
            return 0
    report = cleanup(client, args.release_id, dry_run=args.dry_run, event_release=event_release)
    print(json.dumps(report, indent=2))
    write_summary(report)
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError) as error:
        print("RELEASE CLEANUP FAILED: " + str(error), file=sys.stderr)
        sys.exit(1)
