"""
ingestion/storage.py
Asynchronously streams enriched transactions to ClickHouse database.
"""
import asyncio
import httpx
import logging
try:
    import orjson as json_lib
except ImportError:
    import json as json_lib
from core.types import Transaction

log = logging.getLogger("whale_hunter.storage")

DB_ENABLED = True
DB_OFFLINE = False

class ClickHouseStorage:
    def __init__(self, url: str = "http://localhost:8123", db: str = "whale_hunter", table: str = "transactions"):
        self.url = url
        self.db = db
        self.table = table
        self.client = httpx.AsyncClient(timeout=5.0)
        self.queue = asyncio.Queue(maxsize=10000)
        self.worker_task = None
        self.failed_attempts = 0

    async def start(self):
        """Initialize DB/Table structure and start background flush loop."""
        try:
            query_db = f"CREATE DATABASE IF NOT EXISTS {self.db}"
            await self.client.post(self.url, data=query_db.encode())
            
            query_table = f"""
            CREATE TABLE IF NOT EXISTS {self.db}.{self.table} (
                tx_hash String,
                from_addr String,
                to_addr String,
                value_eth Float64,
                gas_price_gwei Float64,
                timestamp Float64
            ) ENGINE = MergeTree() ORDER BY timestamp
            """
            await self.client.post(self.url, data=query_table.encode())
            log.info(f"ClickHouse schema verified: {self.db}.{self.table}")
        except Exception as e:
            log.warning(f"ClickHouse init failed (runs in mem only): {e}")
            global DB_OFFLINE
            DB_OFFLINE = True
            
        self.worker_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self):
        """Batch and transmit pending records to ClickHouse."""
        global DB_OFFLINE, DB_ENABLED
        while True:
            batch = []
            try:
                # Block until at least 1 record arrives
                item = await self.queue.get()
                batch.append(item)
                
                # Drain queue greedily up to 1000
                while len(batch) < 1000:
                    batch.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                pass
            except asyncio.CancelledError:
                break
            
            if batch and DB_ENABLED and not DB_OFFLINE:
                try:
                    payload = []
                    for tx in batch:
                        payload.append({
                            "tx_hash": tx.tx_hash,
                            "from_addr": tx.from_addr,
                            "to_addr": tx.to_addr,
                            "value_eth": tx.value_eth,
                            "gas_price_gwei": tx.gas_price_gwei,
                            "timestamp": tx.timestamp
                        })
                        
                    ndjson_lines = []
                    for d in payload:
                        dumped = json_lib.dumps(d)
                        if isinstance(dumped, str):
                            dumped = dumped.encode('utf-8')
                        ndjson_lines.append(dumped)
                    ndjson = b"\n".join(ndjson_lines)
                    await self.client.post(
                        self.url,
                        params={"query": f"INSERT INTO {self.db}.{self.table} FORMAT JSONEachRow"},
                        content=ndjson,
                        headers={"Content-Type": "application/x-ndjson"}
                    )
                    self.failed_attempts = 0
                except Exception as e:
                    self.failed_attempts += 1
                    if self.failed_attempts >= 3:
                        DB_OFFLINE = True

    def record(self, tx: Transaction):
        """Enqueue transaction for ClickHouse persistence."""
        try:
            self.queue.put_nowait(tx)
        except asyncio.QueueFull:
            pass

    async def stop(self):
        """Gracefully shut down background loop and client."""
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        await self.client.aclose()
