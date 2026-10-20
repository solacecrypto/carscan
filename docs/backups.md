# Backups

The worker runs `pg_dump` every night at 03:30 UTC (`backup_job`) into `BACKUP_DIR`
(default `/srv/backups`), keeps the last 7 files, and copies each dump to an S3-compatible
bucket when `BACKUP_S3_BUCKET` (+ endpoint, region, access key, secret key) is set. Backblaze
B2, Cloudflare R2, Hetzner Object Storage and AWS S3 all work; `boto3` is only needed then.

The container's disk does not survive a redeploy, so without a bucket the nightly dump only
protects against mistakes between deploys. Set the bucket.

Commands (inside the container: `railway ssh --service carscan -- carscan backup ...`):

- `carscan backup create [--upload]` – dump now
- `carscan backup list` – files on disk
- `carscan backup restore FILE --yes` – restore into `DATABASE_URL`; overwrites the tables
  in the dump

## Getting a dump onto your own machine

    railway ssh --service carscan -- sh -c "carscan backup create >/dev/null && cat /srv/backups/\$(ls -t /srv/backups | head -1)" > carscan.dump.gz

## Restore procedure (tested locally against an embedded Postgres)

1. Create an empty database (Railway: a fresh Postgres service, or locally `createdb`).
2. `DATABASE_URL=postgresql+psycopg://... carscan backup restore carscan.dump.gz --yes`
3. `alembic upgrade head` in case the dump predates the current schema.
4. Point the services at the restored database and redeploy.

The dump is `pg_dump --format=custom`, gzipped, so `pg_restore` can also be used directly.
