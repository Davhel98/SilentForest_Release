from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("sf_cleanup", Path(__file__).resolve().parents[1] / "CleanupReleases.py")
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)

OLDER = "2026-11-16T10:00:00Z"
PROMOTION = "2026-11-17T10:00:00Z"
NEWER = "2026-11-17T10:01:00Z"


def release(release_id, tag, *, stable=False, draft=False, updated=OLDER, immutable=False):
    return {"id": release_id, "tag_name": tag, "prerelease": not stable, "draft": draft,
            "updated_at": updated, "immutable": immutable}


class FakeGitHub:
    def __init__(self, releases):
        self.releases = {item["id"]: deepcopy(item) for item in releases}
        self.tags = {item["tag_name"]: "commit-" + str(item["id"]) for item in releases}
        self.deleted = []
        self.list_calls = 0
        self.on_get = None
        self.on_delete = None

    def get_release(self, release_id):
        if self.on_get:
            self.on_get(self, release_id)
        return deepcopy(self.releases.get(release_id))

    def list_releases(self):
        self.list_calls += 1
        return deepcopy(list(self.releases.values()))

    def delete_release(self, release_id):
        if self.on_delete:
            return self.on_delete(self, release_id)
        self.deleted.append(release_id)
        return self.releases.pop(release_id, None) is not None


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.anchor = release(13, "v1.2.13", stable=True, updated=PROMOTION)

    def client(self, *others):
        return FakeGitHub([self.anchor, *others])

    def event(self, action="released", **fields):
        return {"action": action, "repository": {"full_name": "owner/game"}, "release": deepcopy(self.anchor), **fields}

    def test_exact_requested_example_deletes_other_patches_and_preserves_every_tag(self):
        client = self.client(release(1, "v1.2.0"), release(2, "v1.2.12", stable=True, immutable=True),
                             release(3, "v1.2.99", updated=NEWER), release(4, "v1.3.12"),
                             release(5, "v2.2.12"), release(6, "v1.20.12"), release(7, "nightly"))
        tags_before = deepcopy(client.tags)
        report = cleanup.cleanup(client, 13, event_release=self.anchor)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(set(client.deleted), {1, 2, 3})
        self.assertEqual(set(client.releases), {4, 5, 6, 7, 13})
        self.assertEqual(client.tags, tags_before)

    def test_other_patch_drafts_are_included(self):
        client = self.client(release(1, "v1.2.12", draft=True))
        cleanup.cleanup(client, 13)
        self.assertEqual(client.deleted, [1])

    def test_four_component_versions_remove_other_patch_or_revision(self):
        self.anchor["tag_name"] = "v1.2.13.0"
        client = self.client(release(1, "1.2.12.7"), release(2, "v1.2.13.9"), release(3, "1.2.13"))
        cleanup.cleanup(client, 13)
        self.assertEqual(client.deleted, [1, 2])
        self.assertIn(3, client.releases)

    def test_version_tags_are_parsed_numerically_with_optional_v(self):
        for tag in ("1.2.13", "v1.2.13", "v1.2.13.0"):
            self.assertEqual(cleanup.version_parts(tag), (1, 2, 13, 0))
        for tag in ("1.2", "1.2.13-rc.1", "v1.2.13\n", "01.2.13", "nightly", "v1.2.13/other", "v1.2.2147483648"):
            with self.subTest(tag=tag):
                self.assertIsNone(cleanup.version_parts(tag))

    def test_missing_draft_and_nonstable_targets_never_start_cleanup(self):
        for anchor in (None, release(13, "v1.2.13"), release(13, "v1.2.13", stable=True, draft=True)):
            client = FakeGitHub([anchor] if anchor else [])
            report = cleanup.cleanup(client, 13)
            self.assertEqual(report["status"], "skipped")
            self.assertEqual(client.deleted, [])
            self.assertEqual(client.list_calls, 0)

    def test_unsupported_stable_tag_does_not_delete_anything(self):
        self.anchor["tag_name"] = "nightly"
        client = self.client(release(1, "v1.2.12"))
        self.assertEqual(cleanup.cleanup(client, 13)["status"], "skipped")
        self.assertEqual(client.deleted, [])

    def test_dry_run_reports_candidates_without_deletion(self):
        client = self.client(release(1, "v1.2.12"))
        report = cleanup.cleanup(client, 13, dry_run=True)
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["planned"], [{"id": 1, "tag": "v1.2.12"}])
        self.assertEqual(client.deleted, [])

    def test_duplicate_promotion_events_are_idempotent(self):
        client = self.client(release(1, "v1.2.12"))
        cleanup.cleanup(client, 13, event_release=self.anchor)
        report = cleanup.cleanup(client, 13, event_release=self.anchor)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["planned"], [])
        self.assertEqual(client.deleted, [1])

    def test_newer_stable_stops_an_old_queued_event_without_deletions(self):
        client = self.client(release(1, "v1.2.12"), release(14, "v1.2.14", stable=True, updated=NEWER))
        report = cleanup.cleanup(client, 13, event_release=self.anchor)
        self.assertEqual(report["status"], "skipped")
        self.assertEqual(client.deleted, [])
        self.assertIn(14, client.releases)

    def test_same_second_stable_promotions_require_explicit_manual_selection(self):
        client = self.client(release(12, "v1.2.12", stable=True, updated=PROMOTION))
        self.assertEqual(cleanup.cleanup(client, 13, event_release=self.anchor)["status"], "skipped")
        self.assertEqual(client.deleted, [])
        self.assertEqual(cleanup.cleanup(client, 13)["status"], "completed")
        self.assertEqual(client.deleted, [12])

    def test_anchor_changed_after_event_is_not_used_as_cleanup_authority(self):
        client = self.client(release(1, "v1.2.12"))
        client.releases[13]["updated_at"] = NEWER
        report = cleanup.cleanup(client, 13, event_release=self.anchor)
        self.assertEqual(report["status"], "skipped")
        self.assertEqual(client.list_calls, 0)

    def test_demotion_before_delete_stops_cleanup(self):
        client = self.client(release(1, "v1.2.12"))
        calls = []
        def demote(server, release_id):
            if release_id == 13:
                calls.append(1)
                if len(calls) == 2:
                    server.releases[13]["prerelease"] = True
        client.on_get = demote
        report = cleanup.cleanup(client, 13)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(client.deleted, [])

    def test_candidate_promoted_during_cleanup_is_retained(self):
        client = self.client(release(1, "v1.2.12"))
        def promote(server, release_id):
            if release_id == 1:
                server.releases[1].update(prerelease=False, updated_at=NEWER)
        client.on_get = promote
        report = cleanup.cleanup(client, 13)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(client.deleted, [])

    def test_candidate_renamed_outside_the_series_is_retained(self):
        client = self.client(release(1, "v1.2.12"))
        def rename(server, release_id):
            if release_id == 1:
                server.releases[1]["tag_name"] = "v1.3.12"
        client.on_get = rename
        report = cleanup.cleanup(client, 13)
        self.assertEqual(report["skipped"], ["v1.2.12"])
        self.assertEqual(client.deleted, [])

    def test_candidate_already_deleted_is_an_idempotent_skip(self):
        client = self.client(release(1, "v1.2.12"))
        def remove(server, release_id):
            if release_id == 1:
                server.releases.pop(1, None)
        client.on_get = remove
        report = cleanup.cleanup(client, 13)
        self.assertEqual(report["skipped"], ["v1.2.12"])
        self.assertEqual(report["status"], "completed")

    def test_delete_failure_is_reported_and_does_not_delete_anchor_or_tags(self):
        client = self.client(release(1, "v1.2.11"), release(2, "v1.2.12"))
        tags_before = deepcopy(client.tags)
        def denied(server, release_id):
            raise cleanup.GitHubAPIError(403, "DELETE", "releases/" + str(release_id))
        client.on_delete = denied
        report = cleanup.cleanup(client, 13)
        self.assertEqual(report["status"], "failed")
        self.assertIn("403", report["errors"][0])
        self.assertIn(13, client.releases)
        self.assertEqual(client.tags, tags_before)

    def test_only_promotion_and_stable_publication_events_are_accepted(self):
        for action in ("published", "released"):
            self.assertEqual(cleanup.promotion_event(self.event(action), "owner/game", 13), self.anchor)
        explicit = self.event("edited", changes={"prerelease": {"from": True}})
        self.assertEqual(cleanup.promotion_event(explicit, "owner/game", 13), self.anchor)
        for action in ("edited", "deleted", "prereleased", "unpublished", "created"):
            self.assertIsNone(cleanup.promotion_event(self.event(action), "owner/game", 13))
        notes_edit = self.event("edited", changes={"body": {"from": "old notes"}})
        self.assertIsNone(cleanup.promotion_event(notes_edit, "owner/game", 13))

    def test_nonstable_events_and_wrong_repositories_cannot_authorize_cleanup(self):
        event = self.event("published")
        event["release"]["prerelease"] = True
        self.assertIsNone(cleanup.promotion_event(event, "owner/game", 13))
        with self.assertRaises(ValueError):
            cleanup.promotion_event(self.event(), "other/game", 13)
        with self.assertRaises(ValueError):
            cleanup.promotion_event(self.event(), "owner/game", 14)

    def test_cli_dry_run_writes_a_reviewable_actions_summary(self):
        client = self.client(release(1, "v1.2.12"))
        with tempfile.TemporaryDirectory() as temp:
            event = Path(temp) / "event.json"
            event.write_text(json.dumps(self.event()), encoding="utf-8")
            summary = Path(temp) / "summary.md"
            with patch.object(cleanup, "GitHubReleases", return_value=client), patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), redirect_stdout(io.StringIO()):
                result = cleanup.main(["--repository", "owner/game", "--release-id", "13", "--event-file", str(event), "--dry-run"])
            self.assertEqual(result, 0)
            self.assertIn("v1.2.12", summary.read_text(encoding="utf-8"))
            self.assertIn("Git tags are preserved", summary.read_text(encoding="utf-8"))
            self.assertEqual(client.deleted, [])

    def test_cli_ignores_title_edits_without_contacting_github(self):
        with tempfile.TemporaryDirectory() as temp:
            event = Path(temp) / "event.json"
            event.write_text(json.dumps(self.event("edited")), encoding="utf-8")
            with patch.object(cleanup.GitHubReleases, "get_release") as get, redirect_stdout(io.StringIO()):
                self.assertEqual(cleanup.main(["--repository", "owner/game", "--release-id", "13", "--event-file", str(event)]), 0)
            get.assert_not_called()


class APIBoundaryTests(unittest.TestCase):
    def test_release_list_is_fully_paginated_and_duplicate_ids_are_deduplicated(self):
        client = cleanup.GitHubReleases("owner/game")
        first = [release(i, f"v1.2.{i}") for i in range(1, 101)]
        second = [release(50, "v1.2.50"), release(101, "v1.2.101")]
        responses = [subprocess.CompletedProcess([], 0, json.dumps(page), "") for page in (first, second)]
        with patch.object(cleanup.subprocess, "run", side_effect=responses) as run:
            results = client.list_releases()
        self.assertEqual(len(results), 101)
        self.assertTrue(run.call_args_list[0].args[0][-1].endswith("per_page=100&page=1"))
        self.assertTrue(run.call_args_list[1].args[0][-1].endswith("per_page=100&page=2"))

    def test_delete_transport_uses_only_release_id_endpoint_and_never_git_refs(self):
        client = cleanup.GitHubReleases("owner/game")
        with patch.object(cleanup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertTrue(client.delete_release(123))
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ["gh", "api", "--method", "DELETE"])
        self.assertEqual(args[-1], "repos/owner/game/releases/123")
        self.assertNotIn("git/refs", " ".join(args))
        self.assertNotIn("--cleanup-tag", args)

    def test_only_404_is_an_idempotent_missing_release(self):
        client = cleanup.GitHubReleases("owner/game")
        with patch.object(cleanup.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "failed (HTTP 404)")):
            self.assertIsNone(client.get_release(123))
            self.assertFalse(client.delete_release(123))
        for status in (401, 403, 429, 500):
            with self.subTest(status=status), patch.object(cleanup.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", f"failed (HTTP {status})")), self.assertRaises(cleanup.GitHubAPIError):
                client.delete_release(123)

    def test_ids_cannot_be_used_to_target_tag_or_other_api_endpoints(self):
        client = cleanup.GitHubReleases("owner/game")
        for value in (True, -1, 0, "1/../../git/refs/tags/v1.2.1", "123"):
            with self.subTest(value=value), patch.object(cleanup.subprocess, "run") as run, self.assertRaises(ValueError):
                client.delete_release(value)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
