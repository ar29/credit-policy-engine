import threading
import json
import logging
import redis.asyncio as redis
from typing import List
from app.models.schemas import RuleSchema
from app.core.config import settings


class DistributedPolicyState:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.rules = []
        self.current_policy_id = None
        self._rw_lock = threading.Lock()

    def get_current_policy_id(self):
        return self.current_policy_id

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance.rules = []
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
                try:
                    # 1. Decode bytes to string, then string to Python object
                    raw_data = message["data"]
                    if isinstance(raw_data, bytes):
                        raw_data = raw_data.decode("utf-8")
                    
                    
                    payload = json.loads(raw_data)
                    
                    # 2. Extract the rules and version
                    new_rules_data = payload.get("rules", [])
                    new_version = payload.get("version")

                    # 3. Update the thread-safe state
                    with self._rw_lock:
                        # Ensure we are passing dictionaries to the Pydantic model
                        self.rules = [RuleSchema(**r) for r in new_rules_data]
                        self.current_policy_id = new_version
                    
                    logging.info(f"Hot-swap successful: Switched to Policy Version {new_version}")

                except Exception as e:
                    logging.error(f"Failed to hot-swap policy: {e}")

policy_state = DistributedPolicyState()