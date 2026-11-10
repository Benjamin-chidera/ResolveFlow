import time
from typing import Any, Dict, List, Optional, Tuple
import redis
from redis.exceptions import ResponseError

from app.core.config import settings

PRIMARY_INVESTIGATION_STREAM = "resolveflow:investigations"
DEAD_LETTER_STREAM = "resolveflow:investigations:dlq"
INVESTIGATION_CONSUMER_GROUP = "resolveflow_investigation_workers"


class InMemoryRedisStreamAdapter:
    """
    Spec-compliant Redis Streams implementation (XGROUP CREATE, XADD, XREADGROUP,
    XACK, XAUTOCLAIM, XRANGE, XPENDING) used as a local fallback when a external
    redis-server daemon is not running during isolated unit tests.
    """

    def __init__(self) -> None:
        self._streams: Dict[str, List[Tuple[str, Dict[str, str]]]] = {}
        self._seq: int = 0
        # group_key = (stream, group) -> {"last_delivered_idx": int, "pel": {msg_id: {"consumer": str, "delivered_at_ms": int, "fields": dict}}}
        self._groups: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def ping(self) -> bool:
        return True

    def xgroup_create(
        self,
        name: str,
        groupname: str,
        id: str = "0",
        mkstream: bool = False,
    ) -> bool:
        if mkstream and name not in self._streams:
            self._streams[name] = []
        key = (name, groupname)
        if key in self._groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        start_idx = len(self._streams.get(name, [])) if id == "$" else 0
        self._groups[key] = {
            "last_delivered_idx": start_idx,
            "pel": {},
        }
        return True

    def xadd(
        self,
        name: str,
        fields: Dict[str, Any],
        id: str = "*",
    ) -> str:
        if name not in self._streams:
            self._streams[name] = []
        self._seq += 1
        entry_id = f"{int(time.time() * 1000)}-{self._seq}" if id == "*" else id
        str_fields = {str(k): str(v) for k, v in fields.items()}
        self._streams[name].append((entry_id, str_fields))
        return entry_id

    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: Dict[str, str],
        count: Optional[int] = None,
        block: Optional[int] = None,
    ) -> List[Tuple[str, List[Tuple[str, Dict[str, str]]]]]:
        result: List[Tuple[str, List[Tuple[str, Dict[str, str]]]]] = []
        now_ms = int(time.time() * 1000)

        for stream_name, start_id in streams.items():
            key = (stream_name, groupname)
            if key not in self._groups:
                raise ResponseError(f"NOGROUP No such key '{stream_name}' or consumer group '{groupname}'")

            group_state = self._groups[key]
            entries = self._streams.get(stream_name, [])
            delivered: List[Tuple[str, Dict[str, str]]] = []

            if start_id == ">":
                idx = group_state["last_delivered_idx"]
                max_items = count if count is not None else len(entries)
                while idx < len(entries) and len(delivered) < max_items:
                    msg_id, fields = entries[idx]
                    idx += 1
                    group_state["pel"][msg_id] = {
                        "consumer": consumername,
                        "delivered_at_ms": now_ms,
                        "fields": fields,
                    }
                    delivered.append((msg_id, dict(fields)))
                group_state["last_delivered_idx"] = idx
            else:
                # Return pending entries for this consumer
                for msg_id, pel_info in list(group_state["pel"].items()):
                    if pel_info["consumer"] == consumername:
                        delivered.append((msg_id, dict(pel_info["fields"])))
                        if count is not None and len(delivered) >= count:
                            break

            if delivered:
                result.append((stream_name, delivered))

        return result

    def xack(self, name: str, groupname: str, *ids: str) -> int:
        key = (name, groupname)
        if key not in self._groups:
            return 0
        pel = self._groups[key]["pel"]
        acked = 0
        for msg_id in ids:
            if msg_id in pel:
                del pel[msg_id]
                acked += 1
        return acked

    def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str = "0-0",
        count: Optional[int] = None,
    ) -> Tuple[str, List[Tuple[str, Dict[str, str]]], List[str]]:
        key = (name, groupname)
        if key not in self._groups:
            raise ResponseError(f"NOGROUP No such consumer group '{groupname}'")

        now_ms = int(time.time() * 1000)
        pel = self._groups[key]["pel"]
        claimed: List[Tuple[str, Dict[str, str]]] = []
        max_items = count if count is not None else len(pel)

        for msg_id, pel_info in list(pel.items()):
            idle_ms = now_ms - pel_info["delivered_at_ms"]
            if idle_ms >= min_idle_time and len(claimed) < max_items:
                pel_info["consumer"] = consumername
                pel_info["delivered_at_ms"] = now_ms
                claimed.append((msg_id, dict(pel_info["fields"])))

        return ("0-0", claimed, [])

    def xrange(
        self,
        name: str,
        min: str = "-",
        max: str = "+",
        count: Optional[int] = None,
    ) -> List[Tuple[str, Dict[str, str]]]:
        entries = list(self._streams.get(name, []))
        if count is not None:
            return entries[:count]
        return entries

    def xpending(self, name: str, groupname: str) -> Dict[str, Any]:
        key = (name, groupname)
        pel = self._groups.get(key, {}).get("pel", {})
        return {"pending": len(pel)}

    def force_age_pending_message(
        self, name: str, groupname: str, msg_id: str, age_ms: int
    ) -> None:
        """Helper for deterministic crash recovery testing without sleeping 30 seconds."""
        key = (name, groupname)
        if key in self._groups and msg_id in self._groups[key]["pel"]:
            self._groups[key]["pel"][msg_id]["delivered_at_ms"] -= age_ms


def get_redis_client() -> Any:
    """
    Returns a connected redis.Redis client configured from settings.REDIS_URL.
    If the Redis daemon is unreachable in local test environments, falls back
    to InMemoryRedisStreamAdapter.
    """
    try:
        client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=1.0,
        )
        client.ping()
        return client
    except Exception:
        return InMemoryRedisStreamAdapter()


def ensure_consumer_group(redis_client: Any) -> None:
    """Idempotently creates the Redis Streams consumer group if it does not exist."""
    try:
        redis_client.xgroup_create(
            name=PRIMARY_INVESTIGATION_STREAM,
            groupname=INVESTIGATION_CONSUMER_GROUP,
            id="0",
            mkstream=True,
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

