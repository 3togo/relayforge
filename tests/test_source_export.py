"""Committed source export provenance with a disposable Git repository."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('source_export', ROOT / 'tools/export-source.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class SourceExportTests(unittest.TestCase):
    def repository(self, path):
        path.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.PIPE)
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        (path / 'Cargo.toml').write_text('[workspace.package]\nversion="0.1.0"\n')
        (path / 'source.rs').write_text('committed source\n')
        git('add', '.')
        git('commit', '-qm', 'Fixture source')
        return git

    def test_reproducible_committed_export_excludes_dirty_and_untracked_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            repository = work / 'repository'
            git = self.repository(repository)
            (repository / 'source.rs').write_text('private dirty source\n')
            (repository / 'private-untracked').write_text('must stay local')
            first, second = work / 'one.tar.gz', work / 'two.tar.gz'
            receipt = exporter.export(repository, 'HEAD', first)
            exporter.export(repository, 'HEAD', second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(receipt['commit'], git('rev-parse', 'HEAD').decode().strip())
            self.assertEqual(receipt['sha256'], hashlib.sha256(first.read_bytes()).hexdigest())
            self.assertEqual(receipt, json.loads(first.with_name(first.name + '.json').read_text()))
            with tarfile.open(first) as archive:
                self.assertEqual(archive.extractfile('relayforge-0.1.0/source.rs').read(), b'committed source\n')
                self.assertFalse(any('.git' in Path(name).parts or 'private-untracked' in name
                                     for name in archive.getnames()))
            self.assertEqual((repository / 'source.rs').read_text(), 'private dirty source\n')

    def test_collision_and_invalid_revision_preserve_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            repository = work / 'repository'
            self.repository(repository)
            output = work / 'source.tar.gz'
            receipt = output.with_name(output.name + '.json')
            receipt.write_text('preserve')
            with self.assertRaises(FileExistsError):
                exporter.export(repository, 'HEAD', output)
            self.assertFalse(output.exists())
            self.assertEqual(receipt.read_text(), 'preserve')
            with self.assertRaises(subprocess.CalledProcessError):
                exporter.export(repository, '--all', output)

    def test_generated_files_in_committed_tree_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            repository = work / 'repository'
            git = self.repository(repository)
            (repository / 'artifacts').mkdir()
            (repository / 'artifacts/build.log').write_text('private build log')
            git('add', '.')
            git('commit', '-qm', 'Accidental generated file')
            output = work / 'source.tar.gz'
            with self.assertRaisesRegex(ValueError, 'generated or private-workspace'):
                exporter.export(repository, 'HEAD', output)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
