import mock
from nose.tools import *  # flake8: noqa
import pytest

from addons.github.models import GithubFile
from api.base.settings.defaults import API_BASE
from api_tests import utils as test_utils
from api_tests.preprints.filters.test_filters import PreprintsListFilteringMixin
from api_tests.preprints.views.test_preprint_list_mixin import PreprintIsPublishedListMixin, PreprintIsValidListMixin
from framework.auth.core import Auth
from osf.models import PreprintService, Node
from osf_tests.factories import (
    ProjectFactory,
    PreprintFactory,
    AuthUserFactory,
    SubjectFactory,
    PreprintProviderFactory,
)
from tests.base import ApiTestCase, capture_signals
from website.project import signals as project_signals
from website.util import permissions

def build_preprint_create_payload(node_id=None, provider_id=None, file_id=None, attrs={}):
    payload = {
        "data": {
            "attributes": attrs,
            "relationships": {},
            "type": "preprints"
        }
    }
    if node_id:
        payload['data']['relationships']["node"] = {
            "data": {
                "type": "node",
                "id": node_id
            }
        }
    if provider_id:
        payload['data']['relationships']["provider"] = {
            "data": {
                "type": "provider",
                "id": provider_id
            }
        }
    if file_id:
        payload['data']['relationships']["primary_file"] = {
            "data": {
                "type": "primary_file",
                "id": file_id
            }
        }
    return payload


def build_preprint_create_payload_without_node(provider_id=None, file_id=None, attrs=None):
    attrs = attrs or {}
    return build_preprint_create_payload(node_id=None, provider_id=provider_id, file_id=file_id, attrs=attrs)


@pytest.mark.django_db
class TestPreprintCreateWithoutNode:

    @pytest.fixture()
    def user_one(self):
        return AuthUserFactory()

    @pytest.fixture()
    def subject(self):
        return SubjectFactory()

    @pytest.fixture()
    def provider(self):
        return PreprintProviderFactory()

    @pytest.fixture()
    def url(self):
        return '/{}preprints/'.format(API_BASE)

    @pytest.fixture()
    def preprint_payload(self, provider):
        return {
            'data': {
                'type': 'preprints',
                'attributes': {
                    'title': 'Greatest Wrestlemania Moment Vol IX',
                    'description': 'Crush VS Doink the Clown in an epic battle during WrestleMania IX',
                    'category': 'data',
                    'public': False,
                },
                "relationships": {
                    "provider": {
                        "data": {
                            "id": provider._id,
                            "type": "providers"
                        }
                    }
                }
            }
        }

    def test_create_preprint_logged_in(self, app, user_one, url, preprint_payload):
        res = app.post_json_api(url, preprint_payload, auth=user_one.auth, expect_errors=True)

        assert res.status_code == 201
        assert res.json['data']['attributes']['title'] == preprint_payload['data']['attributes']['title']
        assert res.json['data']['attributes']['description'] == preprint_payload['data']['attributes']['description']
        assert res.content_type == 'application/vnd.api+json'

    def test_create_preprint_creates_a_node(self, app, user_one, provider, url, preprint_payload):
        res = app.post_json_api(url, preprint_payload, auth=user_one.auth, expect_errors=True)

        assert res.status_code == 201
        assert Node.objects.filter(preprints__guids___id=res.json['data']['id']).exists()


class TestPreprintList(ApiTestCase):

    def setUp(self):
        super(TestPreprintList, self).setUp()
        self.user = AuthUserFactory()

        self.preprint = PreprintFactory(creator=self.user)
        self.url = '/{}preprints/'.format(API_BASE)

        self.project = ProjectFactory(creator=self.user)

    def test_return_preprints_logged_out(self):
        res = self.app.get(self.url)
        assert_equal(len(res.json['data']), 1)
        assert_equal(res.status_code, 200)
        assert_equal(res.status_code, 200)
        assert_equal(res.content_type, 'application/vnd.api+json')

    def test_exclude_nodes_from_preprints_endpoint(self):
        res = self.app.get(self.url, auth=self.user.auth)
        ids = [each['id'] for each in res.json['data']]
        assert_in(self.preprint._id, ids)
        assert_not_in(self.project._id, ids)

class TestPreprintsListFiltering(PreprintsListFilteringMixin):

    @pytest.fixture()
    def user(self):
        return AuthUserFactory()

    @pytest.fixture()
    def provider_one(self):
        return PreprintProviderFactory(name='Sockarxiv')

    @pytest.fixture()
    def provider_two(self):
        return PreprintProviderFactory(name='Piratearxiv')

    @pytest.fixture()
    def provider_three(self, provider_one):
        return provider_one

    @pytest.fixture()
    def project_one(self, user):
        return ProjectFactory(creator=user)

    @pytest.fixture()
    def project_two(self, user):
        return ProjectFactory(creator=user)

    @pytest.fixture()
    def project_three(self, user):
        return ProjectFactory(creator=user)

    @pytest.fixture()
    def url(self):
        return '/{}preprints/?version=2.2&'.format(API_BASE)

    @mock.patch('website.identifiers.client.EzidClient.change_status_identifier')
    def test_provider_filter_equals_returns_one(self, mock_change_identifier, app, user, provider_two, preprint_two, provider_url):
        expected = [preprint_two._id]
        res = app.get('{}{}'.format(provider_url, provider_two._id), auth=user.auth)
        actual = [preprint['id'] for preprint in res.json['data']]
        assert expected == actual

class TestPreprintCreate(ApiTestCase):
    def setUp(self):
        super(TestPreprintCreate, self).setUp()

        self.user = AuthUserFactory()
        self.other_user = AuthUserFactory()
        self.private_project = ProjectFactory(creator=self.user)
        self.public_project = ProjectFactory(creator=self.user, is_public=True)
        self.public_project.add_contributor(self.other_user, permissions=permissions.DEFAULT_CONTRIBUTOR_PERMISSIONS, save=True)
        self.subject = SubjectFactory()
        self.provider = PreprintProviderFactory()

        self.user_two = AuthUserFactory()

        self.file_one_public_project = test_utils.create_test_file(self.public_project, self.user, 'millionsofdollars.pdf')
        self.file_one_private_project = test_utils.create_test_file(self.private_project, self.user, 'woowoowoo.pdf')

        self.url = '/{}preprints/'.format(API_BASE)

    def test_create_preprint_from_public_project(self):
        public_project_payload = build_preprint_create_payload(self.public_project._id, self.provider._id, self.file_one_public_project._id)
        res = self.app.post_json_api(self.url, public_project_payload, auth=self.user.auth)

        assert_equal(res.status_code, 201)

    @mock.patch('website.preprints.tasks.get_and_set_preprint_identifiers.si')
    def test_create_preprint_from_private_project(self, mock_create_identifiers):
        private_project_payload = build_preprint_create_payload(self.private_project._id, self.provider._id, self.file_one_private_project._id, attrs={
                'subjects': [[SubjectFactory()._id]],
                'is_published': True
            })
        res = self.app.post_json_api(self.url, private_project_payload, auth=self.user.auth)

        self.private_project.reload()
        assert_equal(res.status_code, 201)
        assert_true(self.private_project.is_public)

    def test_non_authorized_user(self):
        public_project_payload = build_preprint_create_payload(self.public_project._id, self.provider._id, self.file_one_public_project._id)
        res = self.app.post_json_api(self.url, public_project_payload, auth=self.user_two.auth, expect_errors=True)

        assert_equal(res.status_code, 403)

    def test_read_write_user_not_admin(self):
        assert_in(self.other_user, self.public_project.contributors)
        public_project_payload = build_preprint_create_payload(self.public_project._id, self.provider._id, self.file_one_public_project._id)
        res = self.app.post_json_api(self.url, public_project_payload, auth=self.other_user.auth, expect_errors=True)

        assert_equal(res.status_code, 403)

    def test_file_is_not_in_node(self):
        wrong_project_payload = build_preprint_create_payload(self.public_project._id, self.provider._id, self.file_one_private_project._id)
        res = self.app.post_json_api(self.url, wrong_project_payload, auth=self.user.auth, expect_errors=True)

        assert_equal(res.status_code, 400)
        assert_equal(res.json['errors'][0]['detail'], 'This file is not a valid primary file for this preprint.')

    def test_already_a_preprint_with_conflicting_provider(self):
        preprint = PreprintFactory(creator=self.user)
        file_one_preprint = test_utils.create_test_file(preprint.node, self.user, 'openupthatwindow.pdf')

        already_preprint_payload = build_preprint_create_payload(preprint.node._id, preprint.provider._id, file_one_preprint._id)
        res = self.app.post_json_api(self.url, already_preprint_payload, auth=self.user.auth, expect_errors=True)

        assert_equal(res.status_code, 409)
        assert_in('Only one preprint per provider can be submitted for a node.', res.json['errors'][0]['detail'])

    def test_read_write_user_already_a_preprint_with_conflicting_provider(self):
        assert_in(self.other_user, self.public_project.contributors)

        preprint = PreprintFactory(creator=self.user)
        preprint.node.add_contributor(self.other_user, permissions=permissions.DEFAULT_CONTRIBUTOR_PERMISSIONS, save=True)
        file_one_preprint = test_utils.create_test_file(preprint.node, self.user, 'openupthatwindow.pdf')

        already_preprint_payload = build_preprint_create_payload(preprint.node._id, self.provider._id, file_one_preprint._id)
        res = self.app.post_json_api(self.url, already_preprint_payload, auth=self.other_user.auth, expect_errors=True)

        assert_equal(res.status_code, 403)

    @mock.patch('website.preprints.tasks.get_and_set_preprint_identifiers.si')
    def test_publish_preprint_fails_with_no_primary_file(self, mock_get_identifiers):
        no_file_payload = build_preprint_create_payload(
            node_id=self.public_project._id,
            provider_id=self.provider._id,
            file_id=None,
            attrs= {
                'is_published': True,
                'subjects': [[SubjectFactory()._id]],
            }
        )
        res = self.app.post_json_api(self.url, no_file_payload, auth=self.user.auth, expect_errors=True)

        assert_equal(res.status_code, 400)
        assert_equal(res.json['errors'][0]['detail'], 'A valid primary_file must be set before publishing a preprint.')

    @mock.patch('website.preprints.tasks.get_and_set_preprint_identifiers.si')
    def test_publish_preprint_fails_with_invalid_primary_file(self, mock_get_identifiers):
        no_file_payload = build_preprint_create_payload(
            node_id=self.public_project._id,
            provider_id=self.provider._id,
            file_id='totallynotanid',
            attrs= {
                'is_published': True,
                'subjects': [[SubjectFactory()._id]],
            }
        )
        res = self.app.post_json_api(self.url, no_file_payload, auth=self.user.auth, expect_errors=True)

        assert_equal(res.status_code, 400)
        assert_equal(res.json['errors'][0]['detail'], 'A valid primary_file must be set before publishing a preprint.')

    def test_no_provider_given(self):
        no_providers_payload = build_preprint_create_payload(self.public_project._id, self.provider._id, self.file_one_public_project._id)
        del no_providers_payload['data']['relationships']['provider']
        res = self.app.post_json_api(self.url, no_providers_payload, auth=self.user.auth, expect_errors=True)

        assert_equal(res.status_code, 400)
        assert_equal(res.json['errors'][0]['detail'], 'You must specify a valid provider to create a preprint.')

    def test_invalid_provider_given(self):
        wrong_provider_payload = build_preprint_create_payload(self.public_project._id, 'jobbers', self.file_one_public_project._id)

        res = self.app.post_json_api(self.url, wrong_provider_payload, auth=self.user.auth, expect_errors=True)

        assert_equal(res.status_code, 400)
        assert_equal(res.json['errors'][0]['detail'], 'You must specify a valid provider to create a preprint.')

    def test_request_id_does_not_match_request_url_id(self):
        public_project_payload = build_preprint_create_payload(self.private_project._id, self.provider._id, self.file_one_public_project._id)
        res = self.app.post_json_api(self.url, public_project_payload, auth=self.user.auth, expect_errors=True)

        assert_equal(res.status_code, 400)
        assert_equal(res.json['errors'][0]['detail'], 'This file is not a valid primary file for this preprint.')

    def test_file_not_osfstorage(self):
        github_file = self.file_one_public_project
        github_file.recast(GithubFile._typedmodels_type)
        github_file.save()
        public_project_payload = build_preprint_create_payload(self.public_project._id, self.provider._id, github_file._id)
        res = self.app.post_json_api(self.url, public_project_payload, auth=self.user.auth, expect_errors=True)

        assert_equal(res.status_code, 400)
        assert_equal(res.json['errors'][0]['detail'], 'This file is not a valid primary file for this preprint.')

    def test_preprint_contributor_signal_not_sent_on_creation(self):
        with capture_signals() as mock_signals:
            public_project_payload = build_preprint_create_payload(self.public_project._id, self.provider._id,
                                                                   self.file_one_public_project._id)
            res = self.app.post_json_api(self.url, public_project_payload, auth=self.user.auth)

            assert_equal(res.status_code, 201)
            assert_not_in(project_signals.contributor_added, mock_signals.signals_sent())

    def test_create_preprint_with_deleted_node_should_fail(self):
        self.public_project.is_deleted = True
        self.public_project.save()
        public_project_payload = build_preprint_create_payload(self.public_project._id, self.provider._id, self.file_one_public_project._id)
        res = self.app.post_json_api(self.url, public_project_payload, auth=self.user.auth, expect_errors=True)
        assert_equal(res.status_code, 400)
        assert_equal(res.json['errors'][0]['detail'], 'Cannot create a preprint from a deleted node.')

    @mock.patch('website.preprints.tasks.get_and_set_preprint_identifiers.si')
    def test_create_preprint_adds_log_if_published(self, mock_get_identifiers):
        public_project_payload = build_preprint_create_payload(
            self.public_project._id,
            self.provider._id,
            self.file_one_public_project._id,
            {
                'is_published': True,
                'subjects': [[SubjectFactory()._id]],
            }
        )
        res = self.app.post_json_api(self.url, public_project_payload, auth=self.user.auth)
        assert_equal(res.status_code, 201)
        preprint_id = res.json['data']['id']
        preprint = PreprintService.load(preprint_id)
        log = preprint.node.logs.latest()
        assert_equal(log.action, 'preprint_initiated')
        assert_equal(log.params.get('preprint'), preprint_id)

    @mock.patch('website.preprints.tasks.get_and_set_preprint_identifiers.si')
    @mock.patch('website.preprints.tasks.on_preprint_updated.si')
    def test_create_preprint_from_project_published_hits_update(self, mock_on_preprint_updated, mock_get_identifiers):
        private_project_payload = build_preprint_create_payload(self.private_project._id, self.provider._id, self.file_one_private_project._id, attrs={
                'subjects': [[SubjectFactory()._id]],
                'is_published': True
            })
        res = self.app.post_json_api(self.url, private_project_payload, auth=self.user.auth)
        assert mock_on_preprint_updated.called

    @mock.patch('website.preprints.tasks.on_preprint_updated.si')
    def test_create_preprint_from_project_unpublished_does_not_hit_update(self, mock_on_preprint_updated):
        private_project_payload = build_preprint_create_payload(self.private_project._id, self.provider._id, self.file_one_private_project._id, attrs={
                'subjects': [[SubjectFactory()._id]],
                'is_published': False
            })
        res = self.app.post_json_api(self.url, private_project_payload, auth=self.user.auth)
        assert not mock_on_preprint_updated.called


class TestPreprintIsPublishedList(PreprintIsPublishedListMixin):

    @pytest.fixture()
    def user_admin_contrib(self):
        return AuthUserFactory()

    @pytest.fixture()
    def provider_one(self):
        return PreprintProviderFactory()

    @pytest.fixture()
    def provider_two(self, provider_one):
        return provider_one

    @pytest.fixture()
    def project_published(self, user_admin_contrib):
        return ProjectFactory(creator=user_admin_contrib, is_public=True)

    @pytest.fixture()
    def project_public(self, user_admin_contrib, user_write_contrib):
        project_public = ProjectFactory(creator=user_admin_contrib, is_public=True)
        project_public.add_contributor(user_write_contrib, permissions=permissions.DEFAULT_CONTRIBUTOR_PERMISSIONS, save=True)
        return project_public

    @pytest.fixture()
    def url(self):
        return '/{}preprints/?version=2.2&'.format(API_BASE)

class TestPreprintIsValidList(PreprintIsValidListMixin):

    @pytest.fixture()
    def user_admin_contrib(self):
        return AuthUserFactory()

    @pytest.fixture()
    def project(self, user_admin_contrib, user_write_contrib):
        project = ProjectFactory(creator=user_admin_contrib, is_public=True)
        project.add_contributor(user_write_contrib, permissions=permissions.DEFAULT_CONTRIBUTOR_PERMISSIONS, save=True)
        return project

    @pytest.fixture()
    def provider(self):
        return PreprintProviderFactory()

    @pytest.fixture()
    def url(self, project):
        return '/{}preprints/?version=2.2&'.format(API_BASE)
