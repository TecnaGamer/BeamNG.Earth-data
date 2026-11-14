from pathlib import Path
import tempfile
import unittest
import nl_context_ci as ci
import build_nl_signal_context as producer


class ContextArtifact(unittest.TestCase):
    def make(self,folder):
        root=Path(folder)/'context'
        producer.write_tiles([dict(kind='signal',id='123',geometry=[[5.,52.]],tags={})],root,{})
        return root

    def test_public_context_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(ci.check(self.make(folder))['records'],1)

    def test_artwork_and_source_exports_are_not_published(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.make(folder);(root/'source.gpkg').write_bytes(b'not a derived shard')
            with self.assertRaises(ValueError):ci.check(root)

    def test_corrupted_or_missing_shard_is_not_published(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.make(folder);tile=next(root.rglob('*.gz'));tile.write_bytes(b'corrupt')
            with self.assertRaises(ValueError):ci.check(root)
            tile.unlink()
            with self.assertRaises(OSError):ci.check(root)

    def test_manifest_change_must_change_build_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.make(folder);p=root/'manifest.json';p.write_text(p.read_text().replace('ODbL-1.0','CC0-1.0'))
            with self.assertRaises(ValueError):ci.check(root)


if __name__=='__main__':unittest.main()
