"""
Read records as JSON from stdin, and pushes them on a Kinto server concurrently.

Usage:

    $ echo '{"data": {"title": "a"}}
    {"data": {"title": "b"}}
    {"data": {"title": "c"}}' | to-kinto --server=https://localhost:8888/v1 \
                                         --bucket=bid \
                                         --collection=cid \
                                         --auth=user:pass

It is meant to be combined with other commands that output records to stdout :)

    $ cat filename.csv | inventory-to-records | to-kinto --auth=user:pass
    $ scrape-archives | to-kinto --auth=user:pass

"""
import asyncio
import async_timeout
import concurrent.futures
import json
import logging
import os
import sys

from kinto_http import cli_utils


DEFAULT_SERVER = "http://localhost:8888/v1"
DEFAULT_BUCKET = "default"
DEFAULT_COLLECTION = "cid"
NB_THREADS = 3
NB_RETRY_REQUEST = 3
WAIT_TIMEOUT = 5
PREVIOUS_DUMP_FILENAME = ".previous_run.json"

logger = logging.getLogger(__name__)

done = object()


def fetch_existing(client, cache_file=PREVIOUS_DUMP_FILENAME):
    """Fetch all records since last run. A JSON file on disk is used to store
    records from previous run.
    """
    previous_run_cache = []
    previous_run_timestamp = None

    if os.path.exists(cache_file):
        previous_run_cache = json.load(open(cache_file))
        highest_timestamp = max([r['last_modified'] for r in previous_run_cache])
        previous_run_timestamp = '"%s"' % highest_timestamp

    new_records = client.get_records(_since=previous_run_timestamp, pages=float("inf"))

    merge_by_id = {r["id"]: r for r in previous_run_cache + new_records}
    records = list(merge_by_id.values())

    # Atomic write.
    if len(records) > 0:
        tmpfilename = cache_file + ".tmp"
        json.dump(records, open(tmpfilename, "w"))
        os.rename(tmpfilename, cache_file)

    return records


def publish_records(client, records):
    """Synchronuous function that pushes records on Kinto in batch.
    """
    with client.batch() as batch:
        for record in records:
            if "id" in record["data"]:
                batch.update_record(**record)
            else:
                batch.create_record(**record)
    results = batch.results()

    # Batch don't fail with 4XX errors. Make sure we output a comprehensive
    # error here when we encounter them.
    error_msgs = []
    for result in results:
        error_status = result.get("code")
        if error_status == 412:
            error_msg = ("Record '{details[existing][id]}' already exists: "
                         "{details[existing]}").format_map(result)
            error_msgs.append(error_msg)
        elif error_status == 400:
            error_msg = "Invalid record: {}".format(result)
            error_msgs.append(error_msg)
        elif error_status is not None:
            error_msgs.append("Error: {}".format(result))
    if error_msgs:
        raise ValueError("\n".join(error_msgs))

    return results


async def produce(loop, queue):
    """Reads JSON the stdin asynchronuously where each line is a record.
    """
    reader = asyncio.StreamReader(loop=loop)
    reader_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: reader_protocol, sys.stdin)

    while "stdin receives input":
        line = await reader.readline()
        if not line:  # EOF.
            break

        record = json.loads(line.decode("utf-8"))

        if "data" not in record and "permission" not in record:
            raise ValueError("Invalid record (missing 'data' attribute)")

        await queue.put(record)

    # Notify consumer that we are done.
    await queue.put(done)


async def consume(loop, queue, executor, client, existing):
    """Store grabbed releases from the archives website in Kinto.
    """
    def markdone(queue, n):
        """Returns a callback that will mark `n` queue items done."""
        def done(future):
            [queue.task_done() for _ in range(n)]
            results = future.result()  # will raise exception if failed.
            logger.info("Pushed {} records".format(len(results)))
            return results
        return done

    def records_equal(r1, r2):
        omit = ["last_modified", "schema"]
        r1c = {k: v for k, v in r1.items() if k not in omit}
        r2c = {k: v for k, v in r2.items() if k not in omit}
        return r1c == r2c

    records_by_id = {r['id']: r for r in existing}

    info = client.server_info()
    ideal_batch_size = info["settings"]["batch_max_requests"]

    while "consumer is not cancelled":
        # Consume records from queue, and batch operations.
        # But don't wait too much if there's not enough records to fill a batch.
        batch = []
        try:
            with async_timeout.timeout(WAIT_TIMEOUT):
                while len(batch) < ideal_batch_size:
                    record = await queue.get()
                    # Producer is done, don't wait for items to come in.
                    if record is done:
                        queue.task_done()
                        break
                    # Check if known and hasn't changed.
                    rid = record["data"].get("id")
                    if rid in records_by_id and records_equal(record["data"], records_by_id[rid]):
                        queue.task_done()
                        continue
                    # Add record to current batch, and wait for more.
                    batch.append(record)

        except asyncio.TimeoutError:
            if batch:
                logger.debug("Stop waiting, proceed with %s records." % len(batch))
            else:
                logger.debug("Waiting for records in the queue.")

        # We have a batch of records, let's publish them using parallel workers.
        # When done, mark queue items as done.
        if batch:
            task = loop.run_in_executor(executor, publish_records, client, batch)
            task.add_done_callback(markdone(queue, len(batch)))


async def main(loop):
    parser = cli_utils.add_parser_options(
        description="Read records from stdin as JSON and push them to Kinto",
        default_server=DEFAULT_SERVER,
        default_bucket=DEFAULT_BUCKET,
        default_retry=NB_RETRY_REQUEST,
        default_collection=DEFAULT_COLLECTION)

    parser.add_argument('--skip', action='store_true',
                        help='Skip records that exist and are equal.')

    args = parser.parse_args(sys.argv[1:])

    cli_utils.setup_logger(logger, args)

    logger.info("Publish at {server}/buckets/{bucket}/collections/{collection}"
                .format(**args.__dict__))

    client = cli_utils.create_client_from_args(args)

    existing = {}
    if args.skip:
        # Fetch the list of records to skip records that exist and haven't changed.
        existing = fetch_existing(client)

    # Start a producer and a consumer with threaded kinto requests.
    queue = asyncio.Queue()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=NB_THREADS)
    # Schedule the consumer
    consumer_coro = consume(loop, queue, executor, client, existing)
    consumer = asyncio.ensure_future(consumer_coro)
    # Run the producer and wait for completion
    await produce(loop, queue)
    # Wait until the consumer is done consuming everything.
    await queue.join()
    # The consumer is still awaiting for the producer, cancel it.
    consumer.cancel()


def run():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main(loop))
    loop.close()


if __name__ == "__main__":
    run()
