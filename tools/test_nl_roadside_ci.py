import gzip
import json
from pathlib import Path
import tempfile
import unittest
import nl_roadside_ci as ci


class SignArtifact(unittest.TestCase):
    def write(self,root,edit=None):
        row=dict(id='source1',lat=52.,lon=4.,bearing=90.,code='B7',role='prio.stop',
                 speed=None,plates=[],source_ids=['source1'])
        if edit:edit(row)
        x=int((4.+180)/360*4096)
        import math
        y=int((1-math.asinh(math.tan(math.radians(52.)))/math.pi)/2*4096)
        name=f'{x}/{y}'
        (root/str(x)).mkdir()
        (root/(name+'.json.gz')).write_bytes(gzip.compress(json.dumps(dict(format=2,records=[row])).encode(),mtime=0))
        manifest=dict(format=2,zoom=12,license='CC0-1.0',
                      source='https://opendata.ndw.nu/verkeersborden_actueel_beeld.csv.gz',
                      source_sha256='0'*64,records=1,tiles=[name])
        (root/'manifest.json').write_text(json.dumps(manifest))

    def test_attribute_only_artifact_and_complete_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            self.write(root,lambda r:r.update(plates=[dict(kind='UIT',text='uitgezonderd fietsers')]))
            self.assertEqual(ci.check(root),dict(dataset='nl-rvv',tiles=1,records=1))

    def test_photo_fields_never_enter_site(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.write(root,lambda r:r.update(imageUrl='https://example.invalid/photo.jpg'))
            with self.assertRaisesRegex(ValueError,'attribute'):ci.check(root)

    def test_national_export_or_executable_file_is_not_published(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.write(root);(root/'source.csv.gz').write_bytes(b'private export')
            with self.assertRaisesRegex(ValueError,'unexpected files'):ci.check(root)

    def test_missing_exception_target_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.write(root,lambda r:r.update(plates=[dict(kind='UIT',text='uitgezonderd')]))
            with self.assertRaisesRegex(ValueError,'incomplete'):ci.check(root)

    def test_parent_path_cannot_escape_dataset(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.write(root)
            path=root/'manifest.json';data=json.loads(path.read_text());data['tiles']=['../other'];path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'path'):ci.check(root)


if __name__=='__main__':unittest.main()
