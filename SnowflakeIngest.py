"""Batch ingest of captured payments into Snowflake.

Loads through an internal stage and MERGEs into the target table rather than
inserting directly. Two reasons: a stage-plus-COPY is far cheaper than
row-by-row inserts at any real volume, and MERGE on a natural key makes the
whole job idempotent, so a rerun after a partial failure converges instead of
duplicating rows.
"""

from __future__ import annotations

import csv
import gzip
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from RetryPolicy import WAREHOUSE_POLICY, PermanentError, RetryableError, RetryPolicy

log = logging.getLogger(__name__)

TARGET_TABLE = os.environ.get("SNOWFLAKE_TARGET_TABLE", "ANALYTICS.PAYMENTS.CAPTURES")
STAGE_NAME = os.environ.get("SNOWFLAKE_STAGE", "@~/payments_ingest")
BATCH_SIZE = int(os.environ.get("SNOWFLAKE_BATCH_SIZE", "5000"))

COLUMNS: Sequence[str] = (
    "payment_id",
    "order_id",
    "status",
    "amount_minor",
    "currency",
    "captured_at",
    "ingested_at",
)

# Snowflake error codes that mean "try again" rather than "you wrote bad SQL".
TRANSIENT_SNOWFLAKE_CODES = {
    "000603",  # execution aborted by system
    "000630",  # statement reached its timeout
    "390114",  # authentication token expired
    "604",     # query cancelled
}


@dataclass(frozen=True)
class CaptureRow:
    payment_id: str
    order_id: str
    status: str
    amount_minor: int
    currency: str
    captured_at: datetime

    def as_tuple(self, ingested_at: datetime) -> tuple[Any, ...]:
        return (
            self.payment_id,
            self.order_id,
            self.status,
            self.amount_minor,
            self.currency,
            self.captured_at.astimezone(timezone.utc).isoformat(),
            ingested_at.isoformat(),
        )


@dataclass
class IngestResult:
    batches: int = 0
    rows_staged: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0

    @property
    def rows_written(self) -> int:
        return self.rows_inserted + self.rows_updated


def chunked(rows: Sequence[CaptureRow], size: int) -> Iterator[Sequence[CaptureRow]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


@contextmanager
def staged_csv(rows: Sequence[CaptureRow], ingested_at: datetime) -> Iterator[Path]:
    """Write rows to a gzipped CSV and clean it up afterwards.

    The file is deleted in a finally block rather than on the happy path, so a
    failed COPY does not leave payment data sitting in the system temp dir.
    """
    handle = tempfile.NamedTemporaryFile(
        prefix="captures_", suffix=".csv.gz", delete=False
    )
    path = Path(handle.name)
    handle.close()
    try:
        with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(COLUMNS)
            for row in rows:
                writer.writerow(row.as_tuple(ingested_at))
        yield path
    finally:
        path.unlink(missing_ok=True)


MERGE_SQL = f"""
MERGE INTO {TARGET_TABLE} AS target
USING staged_captures AS source
    ON target.payment_id = source.payment_id
WHEN MATCHED AND target.status <> source.status THEN
    UPDATE SET
        target.status = source.status,
        target.amount_minor = source.amount_minor,
        target.captured_at = source.captured_at,
        target.ingested_at = source.ingested_at
WHEN NOT MATCHED THEN
    INSERT ({", ".join(COLUMNS)})
    VALUES ({", ".join("source." + c for c in COLUMNS)})
"""


class SnowflakeIngest:
    def __init__(
        self,
        connection: Any | None = None,
        policy: RetryPolicy = WAREHOUSE_POLICY,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self.connection = connection or self._connect()
        self.policy = policy
        self.batch_size = batch_size

    @staticmethod
    def _connect() -> Any:
        import snowflake.connector  # lazy import keeps tests dependency-free

        required = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_WAREHOUSE")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise RuntimeError(f"missing environment variables: {', '.join(missing)}")

        return snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            password=os.environ.get("SNOWFLAKE_PASSWORD"),
            private_key_file=os.environ.get("SNOWFLAKE_PRIVATE_KEY_FILE"),
            warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
            role=os.environ.get("SNOWFLAKE_ROLE"),
            client_session_keep_alive=True,
        )

    def ingest(self, rows: Sequence[CaptureRow]) -> IngestResult:
        result = IngestResult()
        if not rows:
            log.info("nothing to ingest")
            return result

        ingested_at = datetime.now(timezone.utc)
        for batch in chunked(rows, self.batch_size):
            result.batches += 1
            log.info("batch %d: %d rows", result.batches, len(batch))
            with staged_csv(batch, ingested_at) as path:
                inserted, updated = self.policy.call(
                    lambda p=path: self._load_batch(p),
                    describe=f"snowflake batch {result.batches}",
                )
            result.rows_staged += len(batch)
            result.rows_inserted += inserted
            result.rows_updated += updated

        log.info(
            "ingest complete: %d batches, %d staged, %d inserted, %d updated",
            result.batches, result.rows_staged, result.rows_inserted, result.rows_updated,
        )
        return result

    def _load_batch(self, path: Path) -> tuple[int, int]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(f"PUT file://{path} {STAGE_NAME} OVERWRITE = TRUE")
            cursor.execute("BEGIN")
            cursor.execute(
                "CREATE OR REPLACE TEMPORARY TABLE staged_captures "
                f"LIKE {TARGET_TABLE}"
            )
            cursor.execute(
                f"COPY INTO staged_captures FROM {STAGE_NAME}/{path.name} "
                "FILE_FORMAT = (TYPE = CSV SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '\"') "
                "ON_ERROR = ABORT_STATEMENT"
            )
            cursor.execute(MERGE_SQL)
            inserted, updated = self._merge_counts(cursor)
            cursor.execute("COMMIT")
            return inserted, updated
        except Exception as exc:
            self._rollback(cursor)
            raise self._classify(exc) from exc
        finally:
            cursor.close()

    @staticmethod
    def _merge_counts(cursor: Any) -> tuple[int, int]:
        row = cursor.fetchone()
        if not row:
            return 0, 0
        # Snowflake returns (rows inserted, rows updated) for a MERGE.
        inserted = int(row[0] or 0)
        updated = int(row[1] or 0) if len(row) > 1 else 0
        return inserted, updated

    @staticmethod
    def _rollback(cursor: Any) -> None:
        try:
            cursor.execute("ROLLBACK")
        except Exception:
            log.warning("rollback failed; connection may be in a bad state")

    @staticmethod
    def _classify(exc: Exception) -> Exception:
        code = str(getattr(exc, "errno", "") or getattr(exc, "sqlstate", ""))
        if code in TRANSIENT_SNOWFLAKE_CODES:
            return RetryableError(f"transient snowflake error {code}: {exc}")
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return RetryableError(f"connection problem: {exc}")
        return PermanentError(f"snowflake error: {exc}")


def ingest_for_day(rows: Sequence[CaptureRow], day: date | None = None) -> IngestResult:
    """Entry point for the nightly job."""
    day = day or datetime.now(timezone.utc).date()
    log.info("ingesting captures for %s", day.isoformat())
    return SnowflakeIngest().ingest(rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    sample = [
        CaptureRow(
            payment_id="ch_3PqL2x",
            order_id="ord_10432",
            status="succeeded",
            amount_minor=14999,
            currency="USD",
            captured_at=datetime.now(timezone.utc),
        )
    ]
    print(ingest_for_day(sample))
