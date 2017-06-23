"""
Force-archive "stuck" registrations (i.e. failed to completely archive).
USE WITH CARE.

Usage:

    # Check if Registration abc12 and qwe34 are stuck
    python manage.py force_archive --check --guids abc12 qwe34

    # Dry-run a force-archive of abc12 and qwe34. Verifies that the force-archive can occur.
    python manage.py force_archive --dry --guids abc12 qwe34

    # Force-archive abc12 and qwe34
    python manage.py force_archive --guids abc12 qwe34

    # Force archive OSFS and Dropbox on abc12
    python manage.py force_archive --addons dropbox --guids abc12
"""
from copy import deepcopy
import httplib as http
import json
import logging
import requests

import django
django.setup()
from django.core.management.base import BaseCommand
from django.db.utils import IntegrityError
from django.utils import timezone

from addons.osfstorage.models import OsfStorageFile, OsfStorageFolder
from framework.auth import Auth
from framework.exceptions import HTTPError
from osf.models import Registration, BaseFileNode
from scripts import utils as script_utils
from website.archiver import ARCHIVER_SUCCESS
from website.settings import ARCHIVE_TIMEOUT_TIMEDELTA, ARCHIVE_PROVIDER
from website.util import waterbutler_api_url_for

logger = logging.getLogger(__name__)

# Control globals
DELETE_COLLISIONS = False
SKIP_COLLISIONS = False

# Logging globals
CHECKED_OKAY = []
CHECKED_STUCK_RECOVERABLE = []
CHECKED_STUCK_BROKEN = []
VERIFIED = []
ARCHIVED = []
SKIPPED = []

# Ignorable NodeLogs
LOG_WHITELIST = {
    'affiliated_institution_added',
    'comment_added',
    'comment_removed',
    'comment_restored',
    'comment_updated',
    'contributor_added',
    'contributor_removed',
    'contributors_reordered',
    'edit_description',
    'edit_title',
    'embargo_approved',
    'embargo_cancelled',
    'embargo_completed',
    'embargo_initiated',
    'embargo_terminated',
    'file_tag_added',
    'license_changed',
    'made_contributor_invisible',
    'made_private',
    'made_public',
    'made_wiki_private',
    'made_wiki_public',
    'permissions_updated',
    'pointer_created',
    'pointer_removed',
    'prereg_registration_initiated',
    'project_registered',
    'registration_approved',
    'registration_cancelled',
    'registration_initiated',
    'retraction_approved',
    'retraction_initiated',
    'tag_added',
    'tag_removed',
    'wiki_deleted',
    'wiki_updated'
}

# Require action, but recoverable from
LOG_GREYLIST = {
    'osf_storage_file_added',
    'osf_storage_file_removed',
    'osf_storage_file_updated',
    'osf_storage_folder_created'
}

# Permissible in certain circumstances after communication with user
PERMISSIBLE_BLACKLIST = {
    'dropbox_folder_selected',
    'dropbox_node_authorized',
    'dropbox_node_deauthorized',
    'addon_removed',
    'addon_added'
}

# Extendable with command line input
PERMISSIBLE_ADDONS = {
    'osfstorage'
}

def complete_archive_target(reg, addon_short_name):
    archive_job = reg.archive_job
    target = archive_job.get_target(addon_short_name)
    target.status = ARCHIVER_SUCCESS
    target.save()
    archive_job._post_update_target()

def perform_wb_copy(reg, node_settings):
    src, dst, user = reg.archive_job.info()
    if dst.files.filter(name=node_settings.archive_folder_name.replace('/', '-')).exists():
        if not DELETE_COLLISIONS and not SKIP_COLLISIONS:
            raise Exception('Archive folder for {} already exists. Investigate manually and rerun with either --delete-collisions or --skip-collisions')
        if DELETE_COLLISIONS:
            archive_folder = dst.files.get(name=node_settings.archive_folder_name.replace('/', '-'))
            archive_folder.delete()
        if SKIP_COLLISIONS:
            complete_archive_target(reg, node_settings.short_name)
            return
    params = {'cookie': user.get_or_create_cookie()}
    data = {
        'action': 'copy',
        'path': '/',
        'rename': node_settings.archive_folder_name.replace('/', '-'),
        'resource': dst._id,
        'provider': ARCHIVE_PROVIDER,
    }
    url = waterbutler_api_url_for(src._id, node_settings.short_name, _internal=True, **params)
    res = requests.post(url, data=json.dumps(data))
    if res.status_code not in (http.OK, http.CREATED, http.ACCEPTED):
        raise HTTPError(res.status_code)

def manually_archive(tree, reg, node_settings, parent=None):
    if not isinstance(tree, list):
        tree = [tree]
    for filenode in tree:
        if filenode['deleted']:
            continue
        file_obj = filenode['object']
        cloned = file_obj.clone()
        if cloned.is_deleted:
            if cloned.is_file:
                cloned.recast(OsfStorageFile._typedmodels_type)
            else:
                cloned.recast(OsfStorageFolder._typedmodels_type)
        if not parent:
            parent = reg.get_addon('osfstorage').get_root()
            cloned.name = node_settings.archive_folder_name
        cloned.parent = parent
        cloned.node = reg
        cloned.copied_from = file_obj
        try:
            cloned.save()
        except IntegrityError:
            # Files sometimes already created
            cloned = reg.files.get(name=cloned.name)

        if file_obj.versions.exists() and filenode['version']:  # Min version identifier is 1
            if not cloned.versions.filter(identifier=filenode['version']).exists():
                cloned.versions.add(*file_obj.versions.filter(identifier__lte=filenode['version']))

        if filenode.get('children'):
            manually_archive(filenode['children'], reg, node_settings, parent=cloned)


def modify_file_tree_recursive(tree, file_obj, deleted, cached=False):
    # Note: `deleted` has three possible states:
    # - True: indicates file should be marked as deleted
    # - False: indicates file should be added or undeleted
    # - None: indicates file version should be reverted by one
    retree = []
    noop = True
    if not isinstance(tree, list):
        tree = [tree]
    for filenode in tree:
        if file_obj.is_deleted and not cached and filenode['object'].id == file_obj.parent.id:
            filenode['children'].append({
                'deleted': None,
                'object': file_obj,
                'version': int(file_obj.versions.latest('date_created').identifier) if file_obj.versions.exists() else None
            })
            cached = True
        if filenode['object']._id == file_obj._id:
            if deleted is not None:
                filenode['deleted'] = deleted
                noop = False
            elif deleted is None:
                if not isinstance(filenode['version'], int):
                    raise Exception('Unexpected type for version: got {}'.format(type(filenode['version'])))
                filenode['version'] = filenode['version'] - 1
                noop = False
        if filenode.get('children'):
            filenode['children'], _noop = modify_file_tree_recursive(filenode['children'], file_obj, deleted, cached)
            if noop:
                noop = _noop
        retree.append(filenode)
    return retree, noop

def revert_log_actions(file_tree, reg, obj_cache):
    logs_to_revert = reg.registered_from.logs.filter(date__gt=reg.registered_date).exclude(action__in=LOG_WHITELIST).order_by('-date')
    if len(PERMISSIBLE_ADDONS) > 1:
        logs_to_revert = logs_to_revert.exclude(action__in=PERMISSIBLE_BLACKLIST)
    for log in list(logs_to_revert):
        file_obj = BaseFileNode.objects.get(_id=log.params['urls']['view'].split('/')[5])
        assert file_obj.node in reg.registered_from.root.node_and_primary_descendants()
        if file_obj.node._id != reg.registered_from._id:
            logger.warn('Log {} references File {} not found on expected Node {}, skipping log reversion'.format(log._id, file_obj._id, reg.registered_from._id))
            continue
        if log.action == 'osf_storage_file_added':
            # Find and mark deleted
            logger.info('Reverting add {}:{} from {}'.format(file_obj._id, file_obj.name, log.date))
            file_tree, noop = modify_file_tree_recursive(file_tree, file_obj, deleted=True, cached=bool(file_obj._id in obj_cache))
        elif log.action == 'osf_storage_file_removed':
            # Find parent and add to children, or undelete
            logger.info('Reverting delete {}:{} from {}'.format(file_obj._id, file_obj.name, log.date))
            file_tree, noop = modify_file_tree_recursive(file_tree, file_obj, deleted=False, cached=bool(file_obj._id in obj_cache))
        elif log.action == 'osf_storage_file_updated':
            # Find file and revert version
            logger.info('Reverting update {}:{} from {}'.format(file_obj._id, file_obj.name, log.date))
            file_tree, noop = modify_file_tree_recursive(file_tree, file_obj, deleted=None, cached=bool(file_obj._id in obj_cache))
        elif log.action == 'osf_storage_folder_created':
            # Find folder and mark deleted
            logger.info('Reverting folder {}:{} from {}'.format(file_obj._id, file_obj.name, log.date))
            file_tree, noop = modify_file_tree_recursive(file_tree, file_obj, deleted=True, cached=bool(file_obj._id in obj_cache))
        else:
            raise Exception('Unexpected log action: {}'.format(log.action))
        assert not noop, '{}: Failed to revert action for NodeLog {}'.format(reg._id, log._id)
        if file_obj._id not in obj_cache:
            obj_cache.add(file_obj._id)
    return file_tree

def build_file_tree(reg, node_settings):
    n = reg.registered_from
    nft = node_settings._get_file_tree(user=n.creator)
    obj_cache = set()

    def associate_objver_recursive(tree, node, ns):
        retree = []
        if not isinstance(tree, list):
            tree = [tree]
        for filenode in tree:
            if filenode['kind'] == 'folder' and filenode['path'] == '/':
                filenode['object'] = ns.root_node
            else:
                filenode['object'] = node.files.get(_id=filenode['path'].strip('/'))
            filenode['deleted'] = False
            filenode['version'] = int(filenode['object'].versions.latest('date_created').identifier) if filenode['object'].versions.exists() else None
            if filenode.get('children'):
                filenode['children'] = associate_objver_recursive(filenode['children'], node, ns)
            obj_cache.add(filenode['object']._id)
            retree.append(filenode)
        return retree

    current_tree = associate_objver_recursive(nft, n, node_settings)
    return revert_log_actions(current_tree, reg, obj_cache)

def archive(registration):
    for reg in registration.node_and_primary_descendants():
        reg.registered_from.creator.get_or_create_cookie()  # Allow WB requests
        if reg.archive_job.status == ARCHIVER_SUCCESS:
            continue
        logs_to_revert = reg.registered_from.logs.filter(date__gt=reg.registered_date).exclude(action__in=LOG_WHITELIST)
        if len(PERMISSIBLE_ADDONS) == 1:
            assert not logs_to_revert.exclude(action__in=LOG_GREYLIST).exists(), '{}: {} had unexpected unacceptable logs'.format(registration._id, reg.registered_from._id)
        else:
            assert not logs_to_revert.exclude(action__in=LOG_GREYLIST).exclude(action__in=PERMISSIBLE_BLACKLIST).exists(), '{}: {} had unexpected unacceptable logs'.format(registration._id, reg.registered_from._id)
        logger.info('Preparing to archive {}'.format(reg._id))
        for short_name in PERMISSIBLE_ADDONS:
            node_settings = reg.registered_from.get_addon(short_name)
            if not hasattr(node_settings, '_get_file_tree'):
                # Excludes invalid or None-type
                continue
            if not reg.archive_job.get_target(short_name) or reg.archive_job.get_target(short_name).status == ARCHIVER_SUCCESS:
                continue
            if short_name == 'osfstorage':
                file_tree = build_file_tree(reg, node_settings)
                manually_archive(file_tree, reg, node_settings)
                complete_archive_target(reg, short_name)
            else:
                assert reg.archiving, '{}: Must be `archiving` for WB to copy'.format(reg._id)
                perform_wb_copy(reg, node_settings)

def archive_registrations():
    for reg in deepcopy(VERIFIED):
        archive(reg)
        ARCHIVED.append(reg)
        VERIFIED.remove(reg)

def verify(registration):
    for reg in registration.node_and_primary_descendants():
        if reg.archive_job.status == ARCHIVER_SUCCESS:
            continue
        nonignorable_logs = reg.registered_from.logs.filter(date__gt=reg.registered_date).exclude(action__in=LOG_WHITELIST)
        unacceptable_logs = nonignorable_logs.exclude(action__in=LOG_GREYLIST)
        if unacceptable_logs.exists():
            if len(PERMISSIBLE_ADDONS) == 1 or unacceptable_logs.exclude(action__in=PERMISSIBLE_BLACKLIST):
                logger.error('{}: Original node {} has unacceptable logs: {}'.format(
                    registration._id,
                    reg.registered_from._id,
                    list(unacceptable_logs.values_list('action', flat=True))
                ))
                return False
        addons = reg.registered_from.get_addon_names()
        if set(addons) - set(PERMISSIBLE_ADDONS | {'wiki'}) != set():
            logger.error('{}: Original node {} has addons: {}'.format(registration._id, reg.registered_from._id, addons))
            return False
        if nonignorable_logs.exists():
            logger.info('{}: Original node {} has had revertable file operations'.format(
                registration._id,
                reg.registered_from._id
            ))
        if reg.registered_from.is_deleted:
            logger.error('{}: Original node {} is deleted'.format(
                registration._id,
                reg.registered_from._id
            ))
            return False
        if reg.registered_from.get_aggregate_logs_queryset(Auth(reg.registered_from.creator)).filter(date__gte=reg.registered_date, action='addon_file_moved', params__source__nid=reg.registered_from._id).exists():
            # TODO: This should realistically be a recoverabe state if osfstorage is the provider
            logger.error('{}: Original node {} had files moved to another node'.format(
                registration._id,
                reg.registered_from._id
            ))
            return False
    return True

def verify_registrations(registration_ids):
    for r_id in registration_ids:
        reg = Registration.load(r_id)
        if not reg:
            logger.warn('Registration {} not found'.format(r_id))
        else:
            if verify(reg):
                VERIFIED.append(reg)
            else:
                SKIPPED.append(reg)

def check(reg):
    if reg.is_deleted:
        logger.info('Registration {} is deleted.'.format(reg._id))
        CHECKED_OKAY.append(reg)
        return
    expired_if_before = timezone.now() - ARCHIVE_TIMEOUT_TIMEDELTA
    archive_job = reg.archive_job
    root_job = reg.root.archive_job
    archive_tree_finished = archive_job.archive_tree_finished()

    if type(archive_tree_finished) is int:
        still_archiving = archive_tree_finished != len(archive_job.children)
    else:
        still_archiving = not archive_tree_finished
    if still_archiving and root_job.datetime_initiated < expired_if_before:
        logger.warn('Registration {} is stuck in archiving'.format(reg._id))
        if verify(reg):
            CHECKED_STUCK_RECOVERABLE.append(reg)
        else:
            CHECKED_STUCK_BROKEN.append(reg)
    else:
        logger.info('Registration {} is not stuck in archiving'.format(reg._id))
        CHECKED_OKAY.append(reg)

def check_registrations(registration_ids):
    for r_id in registration_ids:
        reg = Registration.load(r_id)
        if not reg:
            logger.warn('Registration {} not found'.format(r_id))
        else:
            check(reg)

def log_results(dry_run):
    if CHECKED_OKAY:
        logger.info('{} registrations not stuck: {}'.format(len(CHECKED_OKAY), [e._id for e in CHECKED_OKAY]))
    if CHECKED_STUCK_RECOVERABLE:
        logger.info('{} registrations stuck but recoverable: {}'.format(len(CHECKED_STUCK_RECOVERABLE), [e._id for e in CHECKED_STUCK_RECOVERABLE]))
    if CHECKED_STUCK_BROKEN:
        logger.warn('{} registrations stuck and unrecoverable: {}'.format(len(CHECKED_STUCK_BROKEN), [e._id for e in CHECKED_STUCK_BROKEN]))

    if VERIFIED:
        logger.info('{} registrations verified: {}'.format(
            len(VERIFIED),
            [e._id for e in VERIFIED],
        ))
    if ARCHIVED:
        logger.info('{} registrations archived: {}'.format(
            len(ARCHIVED),
            [e._id for e in ARCHIVED]
        ))
    if SKIPPED:
        logger.error('{} registrations skipped: {}'.format(len(SKIPPED), [e._id for e in SKIPPED]))

class Command(BaseCommand):
    def add_arguments(self, parser):
        super(Command, self).add_arguments(parser)
        parser.add_argument(
            '--dry',
            action='store_true',
            dest='dry_run',
            help='Dry run',
        )
        parser.add_argument(
            '--check',
            action='store_true',
            dest='check',
            help='Check if registrations are stuck',
        )
        parser.add_argument(
            '--delete-collisions',
            action='store_true',
            dest='delete_collisions',
            help='Specifies that colliding archive filenodes should be deleted and re-archived in the event of a collision',
        )
        parser.add_argument(
            '--skip-collisions',
            action='store_true',
            dest='skip_collisions',
            help='Specifies that colliding archive filenodes should be skipped and the archive job target marked as successful in the event of a collision',
        )
        parser.add_argument('--addons', type=str, nargs='*', help='Addons other than OSFStorage to archive. Use caution')
        parser.add_argument('--guids', type=str, nargs='+', help='GUIDs of registrations to archive')

    def handle(self, *args, **options):
        global DELETE_COLLISIONS
        global SKIP_COLLISIONS
        DELETE_COLLISIONS = options.get('delete_collisions')
        SKIP_COLLISIONS = options.get('skip_collisions')
        if DELETE_COLLISIONS and SKIP_COLLISIONS:
            raise Exception('Cannot specify both delete_collisions and skip_collisions')

        dry_run = options.get('dry_run')
        if not dry_run:
            script_utils.add_file_logger(logger, __file__)

        addons = options.get('addons', [])
        if addons:
            PERMISSIBLE_ADDONS.update(set(addons))
        registration_ids = options.get('guids', [])

        if options.get('check', False):
            check_registrations(registration_ids)
        else:
            verify_registrations(registration_ids)
            if not dry_run:
                archive_registrations()

        log_results(dry_run)