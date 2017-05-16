import asyncio
import async_timeout
import concurrent.futures
import datetime
import logging
import re
import sys
from packaging.version import parse as version_parse

import aiohttp
import backoff
from kinto_http import cli_utils


ARCHIVE_URL = "https://archive.mozilla.org/pub/"
PRODUCTS = ("fennec", "firefox", "thunderbird")
FILE_EXTENSIONS = "zip|gz|bz|bz2|dmg|apk"
DEFAULT_SERVER = "https://kinto-ota.dev.mozaws.net/v1"
DEFAULT_BUCKET = "build-hub"
DEFAULT_COLLECTION = "archives"
NB_THREADS = 3
NB_RETRY_REQUEST = 3
TIMEOUT_SECONDS = 5 * 60

today = datetime.date.today()

logger = logging.getLogger(__name__)


def publish_records(client, records):
    with client.batch() as batch:
        for record in records:
            batch.create_record(record)
    logger.info("Created {} records".format(len(records)))


def archive(product, version, platform, locale, channel, url, size, date, metadata=None):
    build = None
    revision = None
    tree = None
    if metadata:
        # Example of metadata: https://archive.mozilla.org/pub/thunderbird/candidates/50.0b1-candidates/build2/linux-i686/en-US/thunderbird-50.0b1.json
        revision = metadata["moz_source_stamp"]
        channel = metadata["moz_update_channel"]
        repository = metadata["moz_source_repo"].replace("MOZ_SOURCE_REPO=", "")
        tree = repository.split("/")[-1]
        buildid = metadata["buildid"]
        builddate = datetime.datetime.strptime(buildid[:12], "%Y%m%d%H%M").isoformat()
        build = {
            "id": buildid,
            "date": builddate,
        }

    record = {
        "build": build,
        "source": {
            "revision": revision,
            "tree": tree,
            "product": product,
        },
        "target": {
            "platform": platform,
            "locale": locale,
            "version": version,
            "channel": channel,
        },
        "download": {
            "url": url,
            "mimetype": None,
            "size": size,
            "date": date,
        },
        "systemaddons": None
    }
    return record


def archive_url(product, version=None, platform=None, locale=None, nightly=None):
    url = ARCHIVE_URL + (product if product != "fennec" else "mobile")
    if nightly:
        url += "/nightly/" + nightly + "/"
    else:
        url += "/releases/"
    if version:
        url += version + "/"
    if platform:
        url += platform + "/"
    if locale:
        url += locale + "/"
    return url


@backoff.on_exception(backoff.expo,
                      asyncio.TimeoutError,
                      max_tries=NB_RETRY_REQUEST)
async def fetch_json(session, url):
    headers = {
        "Accept": "application/json",
        "User-Agent": "BuildHub;storage-team@mozilla.com"
    }
    with async_timeout.timeout(TIMEOUT_SECONDS):
        async with session.get(url, headers=headers, timeout=None) as response:
            return await response.json()


async def fetch_listing(session, url):
    try:
        data = await fetch_json(session, url)
        return data["prefixes"], data["files"]
    except (aiohttp.ClientError, KeyError, ValueError) as e:
        raise ValueError("Could not fetch {}: {}".format(url, e))


async def fetch_nightly_metadata(session, nightly_url):
    """A JSON file containing build info is published along the nightly archive.
    """
    # XXX: It is only available for en-US though. Should we use the same for every locale?
    if "en-US" not in nightly_url:
        return None

    try:
        metadata_url = re.sub("\.({})$".format(FILE_EXTENSIONS), ".json", nightly_url)
        metadata = await fetch_json(session, metadata_url)
        return metadata
    except aiohttp.ClientError:
        return None


async def fetch_release_metadata(session, product, version, platform, locale):
    """The `candidates` folder contains build info about recent released versions.
    """
    # XXX: It is only available for en-US though. Should we use the same for every locale?
    if locale != "en-US":
        return None

    product_url = product if product != "fennec" else "mobile"

    url = "{}{}/candidates/{}-candidates/".format(ARCHIVE_URL, product_url, version)
    try:
        build_folders, _ = await fetch_listing(session, url)
        latest_build_folder = sorted(build_folders)[-1]

        url += "{}{}/{}/".format(latest_build_folder, platform, locale)
        _, files = await fetch_listing(session, url)

        re_metadata = re.compile("{}-{}(.*).json".format(product, version))
        json_file = [f_["name"] for f_ in files if re_metadata.match(f_["name"])][0]
        url += json_file
        metadata = await fetch_json(session, url)

        return metadata

    except (IndexError, ValueError, aiohttp.ClientError) as e:
        return None


async def fetch_products(session, queue, products, client):
    # Nightlies
    futures = [fetch_nightlies(session, queue, product, client) for product in products]
    await asyncio.gather(*futures)
    # Releases
    futures = [fetch_versions(session, queue, product, client) for product in products]
    await asyncio.gather(*futures)


async def fetch_nightlies(session, queue, product, client):
    # Check latest known version on server.
    filters = {
        "source.product": product,
        "target.channel": "nightly",
        "_sort": "-download.date",
        "_limit": 1,
        "pages": 1
    }
    existing = client.get_records(**filters)
    latest_nightly_folder = ""
    if existing:
        latest_nightly = existing[0]["download"]["date"]
        nightly_datetime = datetime.datetime.strptime(latest_nightly, "%Y-%m-%dT%H:%M:%SZ")
        latest_nightly_folder = nightly_datetime.strftime("%Y-%m-%d-%H-%M-%S")

    current_month = "{}/{:02d}".format(today.year, today.month)
    month_url = archive_url(product, nightly=current_month)
    days_folders, _ = await fetch_listing(session, month_url)

    # Skip aurora nightlies and known nightlies...
    days_urls = [archive_url(product, nightly=current_month + "/" + f[:-1])
                 for f in days_folders if f > latest_nightly_folder]
    days_urls = [url for url in days_urls if "mozilla-central" in url]

    futures = [fetch_listing(session, day_url) for day_url in days_urls]
    listings = await asyncio.gather(*futures)

    channel = "nightly"
    re_filename = re.compile(r"\w+-(\d+.+)\.([a-z]+(\-[A-Z]+)?)\.(.+)\.({})$".format(FILE_EXTENSIONS))

    for day_url, (_, files) in zip(days_urls, listings):
        for file_ in files:
            filename = file_["name"]
            size = file_["size"]
            date = file_["last_modified"]
            url = day_url + filename

            match = re_filename.search(filename)
            if not match or "tests" in filename:
                continue
            version = match.group(1)
            locale = match.group(2)
            platform = match.group(4)

            metadata = await fetch_nightly_metadata(session, url)
            record = archive(product, version, platform, locale, channel, url, size, date, metadata)
            logger.debug("Nightly found {}".format(url))
            await queue.put(record)


async def fetch_versions(session, queue, product, client):
    # Check latest known version on server.
    filters = {
        "source.product": product,
        "target.channel": "beta",
        "_sort": "-download.date",
        "_limit": 1,
        "pages": 1
    }
    existing = client.get_records(**filters)
    latest_version = ""
    if existing:
        latest_version = existing[0]["target"]["version"]
        logger.info("Scrape {} from version {}".format(product, latest_version))

    product_url = archive_url(product)
    versions_folders, _ = await fetch_listing(session, product_url)

    versions = [v[:-1] for v in versions_folders if re.match(r'^[0-9]', v)]
    versions = [v for v in versions if "funnelcake" not in v]
    # Scrape only unknown recent versions.
    versions = [v for v in versions if version_parse(v) > version_parse(latest_version)]
    versions = sorted(versions, key=lambda v: version_parse(v), reverse=True)

    futures = [fetch_platforms(session, queue, product, version)
               for version in versions]
    return await asyncio.gather(*futures)


async def fetch_platforms(session, queue, product, version):
    version_url = archive_url(product, version)
    platform_folders, _ = await fetch_listing(session, version_url)

    platforms = [p[:-1] for p in platform_folders]  # strip trailing /
    platforms = [p for p in platforms if p not in ("source", "update", "contrib")]

    futures = [fetch_locales(session, queue, product, version, platform)
               for platform in platforms]
    return await asyncio.gather(*futures)


async def fetch_locales(session, queue, product, version, platform):
    platform_url = archive_url(product, version, platform)
    locale_folders, _ = await fetch_listing(session, platform_url)

    locales = [l[:-1] for l in locale_folders]
    locales = [l for l in locales if l != "xpi"]

    futures = [fetch_files(session, queue, product, version, platform, locale)
               for locale in locales]
    return await asyncio.gather(*futures)


async def fetch_files(session, queue, product, version, platform, locale):
    locale_url = archive_url(product, version, platform, locale)
    _, files = await fetch_listing(session, locale_url)

    re_filename = re.compile("{}-(.+)({})$".format(product, FILE_EXTENSIONS))
    files = [f for f in files if re_filename.match(f["name"]) and 'sdk' not in f["name"]]

    channel = None  # Unknown.

    futures = []
    for file_ in files:
        filename = file_["name"]
        url = locale_url + filename
        size = file_["size"]
        date = file_["last_modified"]

        metadata = await fetch_release_metadata(session, product, version, platform, locale)
        record = archive(product, version, platform, locale, channel, url, size, date, metadata)
        logger.debug("Release found {}".format(url))

        futures.append(queue.put(record))

    return await asyncio.gather(*futures)


async def produce(loop, queue, client):
    """Grab releases from the archives website."""
    async with aiohttp.ClientSession(loop=loop) as session:
        await fetch_products(session, queue, PRODUCTS, client)
    logger.info("Scraping releases done.")


async def consume(loop, queue, executor, client):
    """Store grabbed releases from the archives website in Kinto."""

    def markdone(queue, n):
        """Returns a callback that will mark `n` queue items done."""
        return lambda fut: [queue.task_done() for _ in range(n)]

    info = client.server_info()
    batch_size = info["settings"]["batch_max_requests"]
    while True:
        # Consume records from queue, and batch operations.
        # But don't block if there's not enough records to fill a batch.
        batch = []
        try:
            with async_timeout.timeout(2):
                while len(batch) < batch_size:
                    record = await queue.get()
                    batch.append(record)
        except asyncio.TimeoutError:
            logger.debug("Scraping done or not fast enough, proceed.")
            pass

        if batch:
            task = loop.run_in_executor(executor, publish_records, client, batch)
            task.add_done_callback(markdone(queue, len(batch)))


async def main(loop):
    parser = cli_utils.add_parser_options(
        description="Send releases archives to Kinto",
        default_server=DEFAULT_SERVER,
        default_bucket=DEFAULT_BUCKET,
        default_retry=NB_RETRY_REQUEST,
        default_collection=DEFAULT_COLLECTION)

    args = parser.parse_args(sys.argv[1:])

    cli_utils.setup_logger(logger, args)

    logger.info("Publish at {server}/buckets/{bucket}/collections/{collection}"
                .format(**args.__dict__))

    client = cli_utils.create_client_from_args(args)

    public_perms = {"read": ["system.Everyone"]}
    client.create_bucket(permissions=public_perms, if_not_exists=True)
    client.create_collection(if_not_exists=True)

    # Start a producer and a consumer with threaded kinto requests.
    queue = asyncio.Queue()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=NB_THREADS)
    # Schedule the consumer
    consumer_coro = consume(loop, queue, executor, client)
    consumer = asyncio.ensure_future(consumer_coro)
    # Run the producer and wait for completion
    await produce(loop, queue, client)
    # Wait until the consumer is done consuming everything.
    await queue.join()
    # The consumer is still awaiting for the producer, cancel it.
    consumer.cancel()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main(loop))
    loop.close()
