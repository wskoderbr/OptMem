"""Redis-backed logical files. Callers must serialize operations per prefix.

One binary Redis string per OptMem file; no per-entry keys or Python locks.
"""
import redis


class FileRedis:
    _TRUNCATE = """
    for i, key in ipairs(KEYS) do
        local kind = redis.call('TYPE', key).ok
        local length = tonumber(ARGV[i])
        if (kind ~= 'none' and kind ~= 'string') or not length or length < 0 then
            return redis.error_reply('Invalid FileRedis truncate')
        end
    end
    for i, key in ipairs(KEYS) do
        if redis.call('EXISTS', key) == 1 then
            local length = tonumber(ARGV[i])
            local current = redis.call('STRLEN', key)
            if current > length then
                local content = ''
                if length > 0 then content = redis.call('GETRANGE', key, 0, length - 1) end
                redis.call('SET', key, content)
            end
        end
    end
    return 1
    """

    def __init__(self, url, prefix):
        if not prefix or prefix != prefix.strip() or prefix.endswith(':') or any(c in prefix for c in '\x00\r\n\\/'):
            raise ValueError('Invalid memory prefix')
        self.prefix = prefix
        self.client = redis.Redis.from_url(url, decode_responses=False,
                                          socket_connect_timeout=10, socket_timeout=10)
        self._truncate = self.client.register_script(self._TRUNCATE)

    def key(self, path):
        path = path.replace('\\', '/')
        root = self.prefix + '/'
        if not path.startswith(root):
            raise ValueError('File outside memory prefix')
        relative = path[len(root):]
        if relative not in ('LOG.txt', 'config') and not (
                relative.startswith('TREE/') and relative[5:].isdigit()):
            raise ValueError('Unknown OptMem file')
        return self.prefix + ':' + relative

    def exists(self, path):
        return bool(self.client.exists(self.key(path)))

    def size(self, path):
        return self.client.strlen(self.key(path))

    def create(self, path):
        self.client.set(self.key(path), b'', nx=True)

    def read(self, path, offset=0, length=None):
        if length == 0:
            return b''
        end = -1 if length is None else offset + length - 1
        return self.client.getrange(self.key(path), offset, end)

    def write(self, path, content):
        self.client.set(self.key(path), content)

    def append(self, path, content):
        return self.client.append(self.key(path), content)

    def truncate(self, path, length):
        self.truncate_many([(path, length)])

    def truncate_many(self, files):
        if files:
            self._truncate(keys=[self.key(path) for path, _ in files],
                           args=[length for _, length in files])

    def close(self):
        self.client.close()
