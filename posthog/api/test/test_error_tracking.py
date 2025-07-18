import os
from boto3 import resource
from unittest.mock import patch
from rest_framework import status
from freezegun import freeze_time
from django.test import override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import ANY

from ee.models.rbac.role import Role
from posthog.test.base import APIBaseTest
from posthog.models import (
    ErrorTrackingSymbolSet,
    ErrorTrackingStackFrame,
    ErrorTrackingIssue,
    ErrorTrackingIssueAssignment,
    ErrorTrackingIssueFingerprintV2,
)
from posthog.models.utils import uuid7
from botocore.config import Config
from posthog.settings import (
    OBJECT_STORAGE_ENDPOINT,
    OBJECT_STORAGE_ACCESS_KEY_ID,
    OBJECT_STORAGE_SECRET_ACCESS_KEY,
    OBJECT_STORAGE_BUCKET,
)

TEST_BUCKET = "test_storage_bucket-TestErrorTracking"


def get_path_to(fixture_file: str) -> str:
    file_dir = os.path.dirname(__file__)
    return os.path.join(file_dir, "fixtures", fixture_file)


class TestErrorTracking(APIBaseTest):
    def create_issue(self, fingerprints=None) -> ErrorTrackingIssue:
        issue = ErrorTrackingIssue.objects.create(team=self.team)
        fingerprints = fingerprints if fingerprints else []
        for fingerprint in fingerprints:
            ErrorTrackingIssueFingerprintV2.objects.create(team=self.team, issue=issue, fingerprint=fingerprint)
        return issue

    def teardown_method(self, method) -> None:
        s3 = resource(
            "s3",
            endpoint_url=OBJECT_STORAGE_ENDPOINT,
            aws_access_key_id=OBJECT_STORAGE_ACCESS_KEY_ID,
            aws_secret_access_key=OBJECT_STORAGE_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4"),
            region_name="us-east-1",
        )
        bucket = s3.Bucket(OBJECT_STORAGE_BUCKET)
        bucket.objects.filter(Prefix=TEST_BUCKET).delete()

    def test_issue_not_found_fingerprint_redirect(self):
        deleted_issue_id = uuid7()
        merged_fingerprint = "merged_fingerprint"

        merged_issue = self.create_issue()
        ErrorTrackingIssueFingerprintV2.objects.create(
            team=self.team, issue=merged_issue, fingerprint=merged_fingerprint
        )

        # no fingerprint
        response = self.client.get(
            f"/api/environments/{self.team.id}/error_tracking/issues/{deleted_issue_id}",
        )
        assert response.status_code == 404

        # with fingerprint hint
        response = self.client.get(
            f"/api/environments/{self.team.id}/error_tracking/issues/{deleted_issue_id}?fingerprint={merged_fingerprint}",
        )
        assert response.status_code == 308
        assert response.json() == {"issue_id": str(merged_issue.id)}

    def test_issue_fingerprint_does_not_redirect_when_not_merged(self):
        issue = self.create_issue(fingerprints=["fingerprint"])

        # with fingerprint hint
        response = self.client.get(
            f"/api/environments/{self.team.id}/error_tracking/issues/{issue.id}?fingerprint=fingerprint",
        )
        assert response.status_code == 200
        assert response.json().get("id") == str(issue.id)

    @freeze_time("2025-01-01")
    def test_issue_fetch(self):
        issue = self.create_issue(["fingerprint"])

        response = self.client.get(f"/api/environments/{self.team.id}/error_tracking/issues/{issue.id}")

        assert response.status_code == 200
        assert response.json() == {
            "id": str(issue.id),
            "name": None,
            "description": None,
            "status": "active",
            "assignee": None,
            "first_seen": "2025-01-01T00:00:00Z",
            "external_issues": [],
        }

    @freeze_time("2025-01-01")
    def test_issue_update(self):
        issue = self.create_issue(["fingerprint"])

        response = self.client.patch(
            f"/api/environments/{self.team.id}/error_tracking/issues/{issue.id}", data={"status": "resolved"}
        )
        issue.refresh_from_db()

        assert response.status_code == 200
        assert response.json() == {
            "id": str(issue.id),
            "name": None,
            "description": None,
            "status": "resolved",
            "assignee": None,
            "first_seen": "2025-01-01T00:00:00Z",
            "external_issues": [],
        }
        assert issue.status == ErrorTrackingIssue.Status.RESOLVED

        self._assert_logs_the_activity(
            issue.id,
            [
                {
                    "activity": "updated",
                    "created_at": ANY,
                    "detail": {
                        "changes": [
                            {
                                "action": "changed",
                                "after": "resolved",
                                "before": "active",
                                "field": "status",
                                "type": "ErrorTrackingIssue",
                            }
                        ],
                        "name": issue.name,
                        "short_id": None,
                        "trigger": None,
                        "type": None,
                    },
                    "item_id": str(issue.id),
                    "scope": "ErrorTrackingIssue",
                    "user": {"email": "user1@posthog.com", "first_name": ""},
                }
            ],
        )

    def test_issue_merge(self):
        issue_one = self.create_issue(fingerprints=["fingerprint_one"])
        issue_two = self.create_issue(fingerprints=["fingerprint_two"])

        assert ErrorTrackingIssue.objects.count() == 2

        repsonse = self.client.post(
            f"/api/environments/{self.team.id}/error_tracking/issues/{issue_one.id}/merge", data={"ids": [issue_two.id]}
        )

        assert repsonse.status_code == 200
        assert ErrorTrackingIssueFingerprintV2.objects.filter(issue_id=issue_one.id).count() == 2
        assert ErrorTrackingIssueFingerprintV2.objects.filter(fingerprint="fingerprint_one", version=0).exists()
        assert ErrorTrackingIssueFingerprintV2.objects.filter(fingerprint="fingerprint_two", version=1).exists()
        assert ErrorTrackingIssue.objects.count() == 1

    def test_can_start_symbol_set_upload(self) -> None:
        chunk_id = uuid7()
        response = self.client.post(
            f"/api/environments/{self.team.id}/error_tracking/symbol_sets/start_upload?chunk_id={chunk_id}"
        )
        response_json = response.json()

        assert response_json["presigned_url"] is not None

        symbol_set = ErrorTrackingSymbolSet.objects.get(id=response_json["symbol_set_id"])
        assert symbol_set.content_hash is None

    def test_finish_upload_fails_if_file_not_found(self):
        symbol_set = ErrorTrackingSymbolSet.objects.create(
            team=self.team, ref=str(uuid7()), storage_ptr=f"symbolsets/{uuid7()}"
        )

        response = self.client.put(
            f"/api/environments/{self.team.id}/error_tracking/symbol_sets/{symbol_set.pk}/finish_upload",
            data={"content_hash": "this_is_a_content_hash"},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["code"] == "file_not_found"

    @patch("posthog.storage.object_storage._client")
    def test_finish_upload_fails_if_uploaded_file_is_too_large(self, patched_s3_client):
        patched_s3_client.head_object.return_value = {"ContentLength": 1073741824}  # 1GB
        symbol_set = ErrorTrackingSymbolSet.objects.create(
            team=self.team, ref=str(uuid7()), storage_ptr=f"symbolsets/{uuid7()}"
        )

        response = self.client.put(
            f"/api/environments/{self.team.id}/error_tracking/symbol_sets/{symbol_set.pk}/finish_upload",
            data={"content_hash": "this_is_a_content_hash"},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["code"] == "file_too_large"

    @patch("posthog.storage.object_storage._client")
    def test_finish_upload_updates_the_content_hash(self, patched_s3_client):
        patched_s3_client.head_object.return_value = {"ContentLength": 1048576}  # 1MB
        symbol_set = ErrorTrackingSymbolSet.objects.create(
            team=self.team, ref=str(uuid7()), storage_ptr=f"symbolsets/{uuid7()}"
        )

        response = self.client.put(
            f"/api/environments/{self.team.id}/error_tracking/symbol_sets/{symbol_set.pk}/finish_upload",
            data={"content_hash": "this_is_a_content_hash"},
        )

        symbol_set.refresh_from_db()

        assert response.status_code == status.HTTP_200_OK
        assert symbol_set.content_hash == "this_is_a_content_hash"

    def test_can_upload_a_source_map(self) -> None:
        with self.settings(OBJECT_STORAGE_ENABLED=True, OBJECT_STORAGE_ERROR_TRACKING_SOURCE_MAPS_FOLDER=TEST_BUCKET):
            symbol_set = ErrorTrackingSymbolSet.objects.create(
                ref="https://app-static-prod.posthog.com/static/chunk-BPTF6YBO.js", team=self.team, storage_ptr=None
            )

            with open(get_path_to("source.js.map"), "rb") as image:
                # Note - we just use the source map twice, because we don't expect the API to do
                # any validation here - cymbal does the parsing work.
                # TODO - we could have the api validate these contents before uploading, if we wanted
                data = {"source_map": image, "minified": image}
                response = self.client.patch(
                    f"/api/environments/{self.team.id}/error_tracking/symbol_sets/{symbol_set.id}",
                    data,
                    format="multipart",
                )
                self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_rejects_upload_when_object_storage_is_unavailable(self) -> None:
        symbol_set = ErrorTrackingSymbolSet.objects.create(
            ref="https://app-static-prod.posthog.com/static/chunk-BPTF6YBO.js", team=self.team, storage_ptr=None
        )
        with override_settings(OBJECT_STORAGE_ENABLED=False):
            fake_big_file = SimpleUploadedFile(name="large_source.js.map", content=b"", content_type="text/plain")
            data = {"source_map": fake_big_file, "minified": fake_big_file}
            response = self.client.put(
                f"/api/environments/{self.team.id}/error_tracking/symbol_sets/{symbol_set.id}",
                data,
                format="multipart",
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.json())
            self.assertEqual(
                response.json()["detail"],
                "Object storage must be available to allow source map uploads.",
            )

    def test_fetching_symbol_sets(self):
        other_team = self.create_team_with_organization(organization=self.organization)
        ErrorTrackingSymbolSet.objects.create(ref="source_1", team=self.team, storage_ptr=None)
        ErrorTrackingSymbolSet.objects.create(
            ref="source_2", team=self.team, storage_ptr="https://app-static-prod.posthog.com/static/chunk-BPTF6YBO.js"
        )
        ErrorTrackingSymbolSet.objects.create(
            ref="source_2", team=other_team, storage_ptr="https://app-static-prod.posthog.com/static/chunk-BPTF6YBO.js"
        )

        self.assertEqual(ErrorTrackingSymbolSet.objects.count(), 3)

        # it only fetches symbol sets for the specified team
        response = self.client.get(f"/api/environments/{self.team.id}/error_tracking/symbol_sets")
        self.assertEqual(len(response.json()["results"]), 2)

    def test_fetching_stack_frames(self):
        other_team = self.create_team_with_organization(organization=self.organization)
        symbol_set = ErrorTrackingSymbolSet.objects.create(ref="source_1", team=self.team, storage_ptr=None)
        other_symbol_set = ErrorTrackingSymbolSet.objects.create(ref="source_2", team=self.team, storage_ptr=None)
        ErrorTrackingStackFrame.objects.create(
            raw_id="raw_id", team=self.team, symbol_set=symbol_set, resolved=True, contents={}
        )
        ErrorTrackingStackFrame.objects.create(
            raw_id="other_raw_id", team=self.team, symbol_set=other_symbol_set, resolved=True, contents={}
        )
        ErrorTrackingStackFrame.objects.create(
            raw_id="raw_id", team=other_team, symbol_set=symbol_set, resolved=True, contents={}
        )

        self.assertEqual(ErrorTrackingStackFrame.objects.count(), 3)

        # it only fetches stack traces for the specified team
        response = self.client.post(f"/api/environments/{self.team.id}/error_tracking/stack_frames/batch_get")
        self.assertEqual(len(response.json()["results"]), 2)

        # fetching can be filtered by raw_ids
        data = {"raw_ids": ["raw_id"]}
        response = self.client.post(
            f"/api/environments/{self.team.id}/error_tracking/stack_frames/batch_get", data=data
        )
        self.assertEqual(len(response.json()["results"]), 1)

        # fetching can be filtered by symbol set
        data = {"symbol_set": symbol_set.id}
        response = self.client.post(
            f"/api/environments/{self.team.id}/error_tracking/stack_frames/batch_get", data=data
        )
        self.assertEqual(len(response.json()["results"]), 1)
        self.assertEqual(response.json()["results"][0]["symbol_set_ref"], symbol_set.ref)

    def test_assigning_issues(self):
        issue = self.create_issue()

        self.assertEqual(ErrorTrackingIssueAssignment.objects.count(), 0)
        self.client.patch(
            f"/api/environments/{self.team.id}/error_tracking/issues/{issue.id}/assign",
            data={"assignee": {"id": self.user.id, "type": "user"}},
        )
        # assigns the issue
        self.assertEqual(ErrorTrackingIssueAssignment.objects.count(), 1)
        self.assertEqual(ErrorTrackingIssueAssignment.objects.filter(issue=issue, user_id=self.user.id).count(), 1)

        self._assert_logs_the_activity(
            issue.id,
            [
                {
                    "activity": "assigned",
                    "created_at": ANY,
                    "detail": {
                        "changes": [
                            {
                                "action": "changed",
                                "after": {"id": self.user.id, "type": "user"},
                                "before": None,
                                "field": "assignee",
                                "type": "ErrorTrackingIssue",
                            }
                        ],
                        "name": issue.name,
                        "short_id": None,
                        "trigger": None,
                        "type": None,
                    },
                    "item_id": str(issue.id),
                    "scope": "ErrorTrackingIssue",
                    "user": {"email": "user1@posthog.com", "first_name": ""},
                }
            ],
        )

        self.client.patch(
            f"/api/environments/{self.team.id}/error_tracking/issues/{issue.id}/assign",
            data={"assignee": None},
        )
        # deletes the assignment
        self.assertEqual(ErrorTrackingIssueAssignment.objects.count(), 0)

        other_team = self.create_team_with_organization(organization=self.organization)
        response = self.client.patch(
            f"/api/environments/{other_team.id}/error_tracking/issues/{issue.id}/assign",
            data={"assignee": None},
        )
        # cannot assign issues from other teams
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_error_tracking_issue_bulk_resolve(self):
        issue_one = self.create_issue()
        issue_two = self.create_issue()

        self.assertEqual(issue_one.status, ErrorTrackingIssue.Status.ACTIVE)
        self.assertEqual(issue_two.status, ErrorTrackingIssue.Status.ACTIVE)

        self.client.post(
            f"/api/environments/{self.team.id}/error_tracking/issues/bulk",
            data={"ids": [issue_one.id, issue_two.id], "action": "set_status", "status": "resolved"},
        )

        issue_one.refresh_from_db()
        issue_two.refresh_from_db()

        self.assertEqual(issue_one.status, ErrorTrackingIssue.Status.RESOLVED)
        self.assertEqual(issue_two.status, ErrorTrackingIssue.Status.RESOLVED)

    def test_error_tracking_issue_bulk_assign(self):
        issue_one = self.create_issue()
        issue_two = self.create_issue()

        ErrorTrackingIssueAssignment.objects.create(issue=issue_one, user=self.user)
        role = Role.objects.create(name="Team role", organization=self.organization)
        role.members.set([self.user])

        self.client.post(
            f"/api/environments/{self.team.id}/error_tracking/issues/bulk",
            data={
                "ids": [issue_one.id, issue_two.id],
                "action": "assign",
                "assignee": {"id": role.id, "type": "role"},
            },
        )

        self.assertEqual(len(ErrorTrackingIssueAssignment.objects.filter(issue=issue_one, user=self.user)), 0)
        self.assertEqual(
            len(ErrorTrackingIssueAssignment.objects.filter(issue__in=[issue_one, issue_two], role=role)), 2
        )

    def _assert_logs_the_activity(self, error_tracking_issue_id: int, expected: list[dict]) -> None:
        activity_response = self._get_error_tracking_issue_activity(error_tracking_issue_id)
        activity: list[dict] = activity_response["results"]
        self.maxDiff = None
        self.assertEqual(activity, expected)

    def _get_error_tracking_issue_activity(
        self, error_tracking_issue_id: int, expected_status: int = status.HTTP_200_OK
    ) -> dict:
        url = f"/api/environments/{self.team.id}/error_tracking/issues/{error_tracking_issue_id}/activity"
        activity = self.client.get(url)
        self.assertEqual(activity.status_code, expected_status)
        return activity.json()
