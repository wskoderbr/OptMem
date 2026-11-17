"""Parity with upstream, using disposable Redis prefixes only.

OPTMEM_TEST_REDIS_URL=redis://... python -m unittest test_file_redis -v
The tests are sequential: production lock ownership belongs to Memory Worker.
"""
import ast
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

from file_redis import FileRedis

HERE = Path(__file__).resolve().parent


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    module.ME = 'memo'
    return module


class FileRedisParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ['OPTMEM_TEST_REDIS_URL']
        cls.upstream = subprocess.check_output(
            ['git', '-C', str(HERE), 'show', '1fb164cf39028047781f72ac3bb1e5a691c1dcb0:memo'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = str(Path(self.temp.name) / 'memory')
        baseline = Path(self.temp.name) / 'memo'
        baseline.write_bytes(self.upstream)
        self.original = load('original_' + uuid.uuid4().hex, baseline)
        self.fork = load('fork_' + uuid.uuid4().hex, HERE / 'memo')
        self.prefix = 'test:optmem:' + uuid.uuid4().hex
        self.files = FileRedis(self.url, self.prefix)
        self.fork.FILES = self.files
        self.output(self.original.cmd_init, self.directory, [])
        self.output(self.fork.cmd_init, self.prefix, [])

    def tearDown(self):
        keys = list(self.files.client.scan_iter(match=self.prefix + ':*'))
        if keys:
            self.files.client.delete(*keys)
        self.files.close()
        self.temp.cleanup()

    def output(self, fn, directory, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(directory, args)
        return out.getvalue()

    def both(self, command, args):
        a = self.output(getattr(self.original, 'cmd_' + command), self.directory, args)
        b = self.output(getattr(self.fork, 'cmd_' + command), self.prefix, args)
        self.assertEqual(a, b)
        return b

    def populate(self, count=16):
        for i in range(count):
            self.both('note', [f'Preferência {i}: café e pão de queijo, decisão confirmada.'])

    def compress(self):
        while True:
            pending = self.original.pending(self.directory, self.original.log_len(self.directory), limit=1)
            if not pending:
                return
            lo, hi = pending[0]
            self.both('nap', [f'{lo}-{hi - 1}', f'Resumo das preferências {lo} até {hi - 1}.'])

    def assert_files(self):
        original = Path(self.directory)
        expected = {}
        for path in original.rglob('*'):
            if path.is_file() and path.name != '.lock':
                relative = path.relative_to(original).as_posix()
                content = path.read_bytes()
                if relative == 'config':
                    # Upstream text-mode config uses CRLF on Windows; Redis has canonical LF.
                    content = content.replace(b'\r\n', b'\n')
                expected[self.prefix + ':' + relative] = content
        actual = {key.decode(): self.files.client.get(key)
                  for key in self.files.client.scan_iter(match=self.prefix + ':*')}
        self.assertEqual(actual, expected)
        self.assertTrue(all(self.files.client.ttl(key) == -1 for key in actual))

    def test_notes_wake_recall_and_exact_files(self):
        self.populate()
        self.both('wake', [])
        self.both('recall', ['café.*queijo'])
        self.assert_files()

    def test_compression_pagination_and_zoom(self):
        self.populate()
        self.compress()
        for module, directory in [(self.original, self.directory), (self.fork, self.prefix)]:
            self.output(module.cmd_config, directory, ['WAKE_LINES=4', 'PART_LINES=2'])
            module.config(directory)
        self.both('wake', [])
        self.both('wake', ['2', '16'])
        self.both('zoom', ['0-15'])
        self.assert_files()

    def test_forget_truncates_zero_and_rebuilds(self):
        self.populate()
        self.compress()
        self.both('forget', ['0-1'])
        self.assertEqual(self.files.size(self.prefix + '/TREE/2'), 0)
        self.assert_files()
        self.compress()
        self.both('wake', [])
        self.assert_files()

    def test_reinitialize_preserves_all_bytes(self):
        self.populate(3)
        before = self.files.read(self.prefix + '/LOG.txt')
        self.output(self.fork.cmd_init, self.prefix, [])
        self.assertEqual(before, self.files.read(self.prefix + '/LOG.txt'))

    def test_partial_tail_repair(self):
        self.populate(2)
        with open(Path(self.directory) / 'LOG.txt', 'ab') as f:
            f.write(b'partial')
        self.files.append(self.prefix + '/LOG.txt', b'partial')
        self.both('note', ['Depois da recuperação.'])
        self.assert_files()

    def test_algorithm_unchanged(self):
        old = {n.name: ast.dump(n) for n in ast.parse(self.upstream.decode()).body if isinstance(n, ast.FunctionDef)}
        new = {n.name: ast.dump(n) for n in ast.parse((HERE / 'memo').read_text()).body if isinstance(n, ast.FunctionDef)}
        for name in ['_cover', 'cover', 'parse', 'records', 'pad', 'block_id', 'check', 'pending',
                     'pending_count', 'nap_prompt', 'next_nap', 'paginate', 'cmd_wake', 'cmd_note',
                     'cmd_nap', 'cmd_config', 'cmd_forget', 'cmd_recall', 'cmd_zoom', 'cmd_import']:
            self.assertEqual(old[name], new[name], name)

    def test_missing_store_is_not_created_by_read(self):
        empty = self.prefix + ':missing'
        process = subprocess.run([os.sys.executable, str(HERE / 'memo'), '--redis-url', self.url,
                                  '--memory-prefix', empty, 'wake'], capture_output=True, text=True)
        self.assertEqual(process.returncode, 1)
        self.assertIn('Run init first', process.stderr)
        self.assertFalse(list(self.files.client.scan_iter(match=empty + ':*')))


if __name__ == '__main__':
    unittest.main()
