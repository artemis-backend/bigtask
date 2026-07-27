"""Push the spool to object storage until stopped."""

import logging
import sys

from config import UploaderSettings
from uploader.storage import S3ObjectStorage
from uploader.sync import run_forever

logging.basicConfig(level=logging.INFO, format="%(asctime)s uploader %(message)s")
log = logging.getLogger(__name__)


def main() -> int:
    settings = UploaderSettings.from_env()
    settings.spool_dir.mkdir(parents=True, exist_ok=True)

    storage = S3ObjectStorage(
        bucket=settings.bucket,
        access_key_id=settings.access_key_id,
        secret_access_key=settings.secret_access_key,
        endpoint_url=settings.endpoint_url,
        region=settings.region,
        max_pool_connections=settings.workers * 2,
    )
    log.info("syncing %s -> %s/%s every %ss with %s workers, spool retention %ss",
             settings.spool_dir, settings.bucket, settings.stream_id,
             settings.upload_interval, settings.workers, settings.spool_retention)
    run_forever(settings.spool_dir, storage, settings.stream_id,
                settings.upload_interval, settings.workers,
                settings.spool_retention)
    return 0


if __name__ == "__main__":
    sys.exit(main())
