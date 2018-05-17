# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, you can obtain one at http://mozilla.org/MPL/2.0/.

import asyncio
import json
import logging
import re
import sys

import aiohttp
import ciso8601
import kinto_http
from decouple import config
from raven.contrib.awslambda import LambdaClient

from buildhub import utils
from buildhub.inventory_to_records import (
    __version__,
    NB_RETRY_REQUEST,
    fetch_json,
    fetch_listing,
    fetch_metadata,
    scan_candidates
)
from buildhub.configure_markus import get_metrics


# Optional Sentry with synchronuous client.
SENTRY_DSN = config('SENTRY_DSN', default=None)
sentry = LambdaClient(
    SENTRY_DSN,
    release=__version__,
)

logger = logging.getLogger()  # root logger.
metrics = get_metrics('buildhub')


async def main(loop, event):
    """
    Trigger when S3 event kicks in.
    http://docs.aws.amazon.com/AmazonS3/latest/dev/notification-content-structure.html
    """
    server_url = config('SERVER_URL', default='http://localhost:8888/v1')
    bucket = config('BUCKET', default='build-hub')
    collection = config('COLLECTION', default='releases')
    kinto_auth = tuple(config('AUTH', 'user:pass').split(':'))

    kinto_client = kinto_http.Client(server_url=server_url, auth=kinto_auth,
                                     retry=NB_RETRY_REQUEST)

    records = []
    for record in event['Records']:
        if record.get('EventSource') == 'aws:sns':
            records.extend(json.loads(record['Sns']['Message'])['Records'])
        else:
            records.append(record)

    async with aiohttp.ClientSession(loop=loop) as session:
        for event_record in records:
            metrics.incr('s3_event_event')
            records_to_create = []

            # Use event time as archive publication.
            event_time = ciso8601.parse_datetime(event_record['eventTime'])
            event_time = event_time.strftime(utils.DATETIME_FORMAT)

            key = event_record['s3']['object']['key']
            filesize = event_record['s3']['object']['size']
            url = utils.ARCHIVE_URL + key
            logger.debug("Event file {}".format(url))

            try:
                product = key.split('/')[1]  # /pub/thunderbird/nightly/...
            except IndexError:
                continue  # e.g. https://archive.mozilla.org/favicon.ico

            if product not in utils.ALL_PRODUCTS:
                logger.info('Skip product {}'.format(product))
                continue

            # Release / Nightly / RC archive.
            if utils.is_build_url(product, url):
                logger.info('Processing {} archive: {}'.format(product, key))

                record = utils.record_from_url(url)
                # Use S3 event infos for the archive.
                record['download']['size'] = filesize
                record['download']['date'] = event_time
                version = record['target']['version']

                # Fetch release metadata.
                await scan_candidates(
                    session,
                    product,
                    specific_version=version,
                )
                logger.debug("Fetch record metadata")
                # metadata = await fetch_metadata(session, record)
                metadata = await fetch_metadata(session, record)
                # If JSON metadata not available, archive will be
                # handled when JSON is delivered.
                if metadata is None:
                    logger.info(
                        f"JSON metadata not available {record['id']}"
                    )
                    continue

                # Merge obtained metadata.
                record = utils.merge_metadata(record, metadata)
                records_to_create.append(record)

            # RC metadata
            elif utils.is_rc_build_metadata(product, url):
                logger.info(f'Processing {product} RC metadata: {key}')

                # pub/firefox/candidates/55.0b12-candidates/build1/mac/en-US/
                # firefox-55.0b12.json
                logger.debug("Fetch new metadata")
                # It has been known to happen that right after an S3 Event
                # there's a slight delay to the metadata json file being
                # available. If that's the case we want to retry in a couple
                # of seconds to see if it's available on the next backoff
                # attempt.
                metadata = await fetch_json(
                    session,
                    url,
                    retry_on_notfound=True
                )
                metadata['buildnumber'] = int(
                    re.search('/build(\d+)/', url).group(1)
                )

                # We just received the metadata file. Lookup if the associated
                # archives are here too.
                archives = []
                if 'multi' in url:
                    # For multi we just check the associated archive
                    # is here already.
                    parent_folder = re.sub('multi/.+$', 'multi/', url)
                    _, files = await fetch_listing(
                        session,
                        parent_folder,
                        retry_on_notfound=True
                    )
                    for f in files:
                        rc_url = parent_folder + f['name']
                        if utils.is_build_url(product, rc_url):
                            archives.append((
                                rc_url,
                                f['size'],
                                f['last_modified']
                            ))
                else:
                    # For en-US it's different, it applies to every
                    # localized archives.
                    # Check if they are here by listing the parent folder
                    # (including en-US archive).
                    l10n_parent_url = re.sub('en-US/.+$', '', url)
                    l10n_folders, _ = await fetch_listing(
                        session,
                        l10n_parent_url,
                        retry_on_notfound=True,
                    )
                    for locale in l10n_folders:
                        _, files = await fetch_listing(
                            session,
                            l10n_parent_url + locale,
                            retry_on_notfound=True,
                        )
                        for f in files:
                            rc_url = l10n_parent_url + locale + f['name']
                            if utils.is_build_url(product, rc_url):
                                archives.append((
                                    rc_url,
                                    f['size'],
                                    f['last_modified'],
                                ))

                for rc_url, size, last_modified in archives:
                    record = utils.record_from_url(rc_url)
                    record['download']['size'] = size
                    record['download']['date'] = last_modified
                    record = utils.merge_metadata(record, metadata)
                    records_to_create.append(record)
                # Theorically release should never be there yet :)
                # And repacks like EME-free/sha1 don't seem to be
                # published in RC.

            # Nightly metadata
            # pub/firefox/nightly/2017/08/2017-08-08-11-40-32-mozilla-central/
            # firefox-57.0a1.en-US.linux-i686.json
            # -l10n/...
            elif utils.is_nightly_build_metadata(product, url):
                logger.info(
                    f'Processing {product} nightly metadata: {key}'
                )

                logger.debug("Fetch new nightly metadata")
                # See comment above about the exceptional need of
                # setting retry_on_notfound here.
                metadata = await fetch_json(
                    session,
                    url,
                    retry_on_notfound=True
                )

                platform = metadata['moz_pkg_platform']

                # Check if english version is here.
                parent_url = re.sub('/[^/]+$', '/', url)
                logger.debug("Fetch parent listing {}".format(parent_url))
                _, files = await fetch_listing(session, parent_url)
                for f in files:
                    if ('.' + platform + '.') not in f['name']:
                        # metadata are by platform.
                        continue
                    en_nightly_url = parent_url + f['name']
                    if utils.is_build_url(product, en_nightly_url):
                        record = utils.record_from_url(en_nightly_url)
                        record['download']['size'] = f['size']
                        record['download']['date'] = f['last_modified']
                        record = utils.merge_metadata(record, metadata)
                        records_to_create.append(record)
                        break  # Only one file for english.

                # Check also localized versions.
                l10n_folder_url = re.sub('-mozilla-central([^/]*)/([^/]+)$',
                                         '-mozilla-central\\1-l10n/',
                                         url)
                logger.debug("Fetch l10n listing {}".format(l10n_folder_url))
                try:
                    _, files = await fetch_listing(
                        session,
                        l10n_folder_url,
                        retry_on_notfound=True,
                    )
                except ValueError:
                    files = []  # No -l10/ folder published yet.
                for f in files:
                    if (
                        ('.' + platform + '.') not in f['name'] and
                        product != 'mobile'
                    ):
                        # metadata are by platform.
                        # (mobile platforms are contained by folder)
                        continue
                    nightly_url = l10n_folder_url + f['name']
                    if utils.is_build_url(product, nightly_url):
                        record = utils.record_from_url(nightly_url)
                        record['download']['size'] = f['size']
                        record['download']['date'] = f['last_modified']
                        record = utils.merge_metadata(record, metadata)
                        records_to_create.append(record)

            else:
                logger.info('Ignored {}'.format(key))

            logger.debug(
                f"{len(records_to_create)} records to create."
            )
            with metrics.timer('s3_event_records_to_create'):
                for record in records_to_create:
                    # Check that fields values look OK.
                    utils.check_record(record)
                    # Push result to Kinto.
                    kinto_client.create_record(data=record,
                                               bucket=bucket,
                                               collection=collection,
                                               if_not_exists=True)
                    logger.info('Created {}'.format(record['id']))
                    metrics.incr('s3_event_record_created')


@sentry.capture_exceptions
def lambda_handler(event, context):
    # Log everything to stderr.
    logger.addHandler(logging.StreamHandler(stream=sys.stdout))
    logger.setLevel(logging.DEBUG)

    loop = asyncio.get_event_loop_policy().new_event_loop()

    try:
        loop.run_until_complete(main(loop, event))
    except Exception:
        logger.exception('Aborted.')
        raise
    finally:
        loop.close()
