import json


ATOMIC_ENQUEUE_SCRIPT = """
if redis.call('SADD', KEYS[2], ARGV[1]) == 1 then
    redis.call('RPUSH', KEYS[1], ARGV[2])
    return 1
end
return 0
"""

ATOMIC_PAGE_REPLAY_SCRIPT = """
if redis.call('LREM', KEYS[1], 1, ARGV[2]) == 0 then
    return 0
end
redis.call('SREM', KEYS[2], ARGV[1])
redis.call('RPUSH', KEYS[3], ARGV[3])
return 1
"""

ATOMIC_IMAGE_REPLAY_SCRIPT = """
if redis.call('LREM', KEYS[1], 1, ARGV[2]) == 0 then
    return 0
end
redis.call('SREM', KEYS[2], ARGV[1])
redis.call('SREM', KEYS[3], ARGV[3])
redis.call('RPUSH', KEYS[4], ARGV[4])
return 1
"""


class RedisFailureStore:
    def __init__(self, server):
        self.server = server

    @staticmethod
    def serialize(record):
        return json.dumps(record, ensure_ascii=False, sort_keys=True)

    def enqueue(
        self,
        queue_key,
        dedupe_key,
        failure_id,
        record,
    ):
        return bool(
            self.server.eval(
                ATOMIC_ENQUEUE_SCRIPT,
                2,
                queue_key,
                dedupe_key,
                failure_id,
                self.serialize(record),
            )
        )

    def list_raw(self, queue_key, start=0, end=-1):
        return self.server.lrange(queue_key, start, end)

    def list_records(self, queue_key):
        return [json.loads(payload) for payload in self.list_raw(queue_key)]

    def replay_page(
        self,
        queue_key,
        dedupe_key,
        failure_id,
        failure_payload,
        retry_queue_key,
        retry_payload,
    ):
        return bool(
            self.server.eval(
                ATOMIC_PAGE_REPLAY_SCRIPT,
                3,
                queue_key,
                dedupe_key,
                retry_queue_key,
                failure_id,
                failure_payload,
                retry_payload,
            )
        )

    def replay_image(
        self,
        queue_key,
        dedupe_key,
        failure_id,
        failure_payload,
        item_dupe_key,
        img_url,
        retry_queue_key,
        retry_payload,
    ):
        return bool(
            self.server.eval(
                ATOMIC_IMAGE_REPLAY_SCRIPT,
                4,
                queue_key,
                dedupe_key,
                item_dupe_key,
                retry_queue_key,
                failure_id,
                failure_payload,
                img_url,
                retry_payload,
            )
        )
