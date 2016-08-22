# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Cedric Serfon, <cedric.serfon@cern.ch>, 2016

import json
import os
import re

from ConfigParser import NoOptionError, NoSectionError
from datetime import datetime, timedelta
from dogpile.cache import make_region
from dogpile.cache.api import NoValue

from rucio.db.sqla import models
from rucio.common.config import config_get
from rucio.common.exception import ScratchDiskLifetimeConflict
from rucio.core.account import has_account_attribute
from rucio.core.rse import list_rse_attributes

REGION = make_region().configure('dogpile.cache.memory',
                                 expiration_time=1800)


def get_vo():
    vo_name = REGION.get('VO')
    if type(vo_name) is NoValue:
        try:
            vo_name = config_get('common', 'vo')
        except NoOptionError:
            vo_name = 'atlas'
        REGION.set('VO', vo_name)
    return vo_name


def get_lifetime_policy():
    lifetime_dict = REGION.get('lifetime_dict')
    if type(lifetime_dict) is NoValue:
        lifetime_dict = {'data': [], 'mc': [], 'valid': [], 'other': []}
        try:
            lifetime_dir = config_get('lifetime', 'directory')
        except (NoSectionError, NoOptionError):
            lifetime_dir = '/opt/rucio/etc/policies'
        for dtype in ['data', 'mc', 'valid', 'other']:
            input_file_name = '%s/config_%s.json' % (lifetime_dir, dtype)
            if os.path.isfile(input_file_name):
                with open(input_file_name, 'r') as input_file:
                    lifetime_dict[dtype] = json.load(input_file)
        REGION.set('lifetime_dict', lifetime_dict)
    return lifetime_dict


def define_eol(scope, name, rses, session):
    # Check if on ATLAS managed space
    if [rse for rse in rses if list_rse_attributes(rse=None, rse_id=rse['id'], session=session).get('type') in ['LOCALGROUPDISK', 'LOCALGROUPTAPE', 'GROUPDISK', 'GROUPTAPE']]:
        return None
    # Now check the lifetime policy
    did = session.query(models.DataIdentifier).filter(models.DataIdentifier.scope == scope,
                                                      models.DataIdentifier.name == name).one()
    policy_dict = get_lifetime_policy()
    did_type = 'other'
    if scope.startswith('mc'):
        did_type = 'mc'
    elif scope.startswith('data'):
        did_type = 'data'
    elif scope.startswith('valid'):
        did_type = 'valid'
    else:
        did_type = 'other'
    for policy in policy_dict[did_type]:
        if 'exclude' in policy:
            to_exclude = False
            for key in policy['exclude']:
                meta_key = None
                if key not in ['datatype', 'project', ]:
                    if key == 'stream':
                        meta_key = 'stream_name'
                    elif key == 'tags':
                        meta_key = 'version'
                else:
                    meta_key = key
                values = policy['exclude'][key]
                for value in values:
                    value = value.replace('%', '.*')
                    if meta_key and did[meta_key] and value and re.match(value, did[meta_key]):
                        to_exclude = True
                        break
                if to_exclude:
                    break
            if to_exclude:
                continue
        if 'include' in policy:
            match_policy = True
            for key in policy['include']:
                meta_key = None
                if key not in ['datatype', 'project', ]:
                    if key == 'stream':
                        meta_key = 'stream_name'
                    elif key == 'tags':
                        meta_key = 'version'
                    else:
                        continue
                else:
                    meta_key = key
                values = policy['include'][key]
                to_keep = False
                for value in values:
                    value = value.replace('%', '.*')
                    if meta_key and did[meta_key] and value and re.match(value, did[meta_key]):
                        to_keep = True
                        break
                match_policy = match_policy and to_keep
                if not to_keep:
                    match_policy = False
                    break
            if match_policy:
                lifetime_value, extension = int(policy['age']) * 30, int(policy['extension']) * 30
                default_eol_at = did.created_at + timedelta(days=lifetime_value)
                if default_eol_at > datetime.utcnow():
                    eol_at = default_eol_at
                elif did.accessed_at:
                    eol_at = did.accessed_at + timedelta(extension)
                    if eol_at < default_eol_at:
                        eol_at = default_eol_at
                else:
                    eol_at = default_eol_at

                return eol_at
    return None


def get_scratch_policy(account, rses, lifetime, session):
    vo_name = get_vo()
    if vo_name == 'atlas':
        # Check SCRATCHDISK Policy
        if not has_account_attribute(account=account, key='admin', session=session) and (lifetime is None or lifetime > 60 * 60 * 24 * 15):
            # Check if one of the rses is a SCRATCHDISK:
            if [rse for rse in rses if list_rse_attributes(rse=None, rse_id=rse['id'], session=session).get('type') == 'SCRATCHDISK']:
                if len(rses) == 1:
                    lifetime = 60 * 60 * 24 * 15 - 1
                else:
                    raise ScratchDiskLifetimeConflict()
        return lifetime
    return None
