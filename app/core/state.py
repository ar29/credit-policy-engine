import threading
import json
import redis.asyncio as redis
from typing import List
from app.models.schemas import RuleSchema
from app.core.config import settings


class DistributedPolicyState:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance.rules: List[RuleSchema] = []
                    cls._instance._rw_lock = threading.Lock()
                    cls._instance.redis_client = redis.Redis(host=settings.redis_host, 
                                                             port=settings.redis_port, 
                                                             db=settings.redis_db,
                                                             decode_responses=True)
        return cls._instance

    def get_rules(self) -> List[RuleSchema]:
        with self._rw_lock:
            return list(self.rules)

    async def listen_for_invalidations(self):
        pubsub = self.redis_client.pubsub()
        await pubsub.subscribe("policy_updates")
        async for message in pubsub.listen():
            if message["type"] == "message":
                new_rules_data = json.loads(message["data"])
                with self._rw_lock:
                    self.rules = [RuleSchema(**r) for r in new_rules_data]
                print(f"State Synced: {len(self.rules)} rules hot-reloaded.")

policy_state = DistributedPolicyState()