"""Synthetic FDS only: no solver, native GUI, real models or user settings."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fds_compat
from engineering_workbenches import EngineeringWorkbenches

MODEL = "&head chid='test', title='中文模型' /\n&mesh ijk=2,2,2, xb=0,1,0,1,0,1 /\n&tail /\n"


class FdsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root/'model.fds'
        self.source.write_text(MODEL, encoding='utf-8')

    def tearDown(self): self.temp.cleanup()

    def test_normalize_encodings_keep_chinese_and_source_bytes(self):
        for encoding in ('utf-8', 'utf-8-sig', 'utf-16', 'utf-32', 'gb18030'):
            with self.subTest(encoding=encoding):
                original = MODEL.replace('\n', '\r\n').encode(encoding)
                self.source.write_bytes(original)
                report, text = fds_compat.analyze(str(self.source))
                self.assertTrue(report['canConvert'])
                self.assertIn("TITLE='中文模型'", text)
                self.assertIn('IJK=2,2,2, XB=0,1,0,1,0,1', text)
                result = fds_compat.convert(str(self.source), report['sha256'])
                self.assertEqual(self.source.read_bytes(), original)
                self.assertEqual(Path(result['output']).parent, self.root)
                self.assertEqual(Path(result['output']).read_bytes(), text.encode('utf-8'))
                saved = json.loads(Path(result['reportPath']).read_text(encoding='utf-8'))
                self.assertFalse(saved['validatedByPyroSim'])
                self.assertEqual(saved['sha256'], hashlib.sha256(original).hexdigest())

    def test_quotes_comments_and_all_physics_stay_verbatim(self):
        text = "! &BAD / 'ignored'\n&head chid='CaseA', title='it''s ! &end / test' /\n" \
               "&SURF ID='fire', HRRPUA=1000.125, RAMP_Q='RampA' /\n" \
               '&MISC SOME_NEW_FIELD=3.14159, EXTERNAL_FILENAME="../data/file.dat" /\n&TAIL /'
        normalized, _, errors, records = fds_compat.normalize(text)
        self.assertFalse(errors)
        self.assertEqual(len(records),4)
        self.assertIn("'it''s ! &end / test'",normalized)
        self.assertIn(text[text.index('&SURF'):],normalized)

    def test_end_markers_and_array_keys(self):
        text, _, errors, _ = fds_compat.normalize('$head chid="x" $end\n&mesh ijk(1:3)=1,2,3 &end\n&tail /')
        self.assertFalse(errors)
        self.assertEqual(text,'&HEAD CHID="x" /\n&MESH IJK(1:3)=1,2,3 /\n&TAIL /')

    def test_invalid_structure_never_writes(self):
        for text in ["&HEAD TITLE='broken /", '&HEAD CHID="x"\n&MESH /', '&TAIL /\n&MESH /', '&HEAD /\n&HEAD /', 'plain text', '&HEAD CHID=“x” /', '&HEAD\x00 /']:
            self.source.write_text(text,encoding='utf-8')
            report, _ = fds_compat.analyze(str(self.source))
            self.assertFalse(report['canConvert'],text)
            with self.assertRaises(ValueError): fds_compat.convert(str(self.source),report['sha256'])
        self.assertEqual(list(self.root.iterdir()),[self.source])

    def test_unknown_features_preserved_and_reported_not_claimed_compatible(self):
        self.source.write_text(MODEL.replace('&tail /',"&GEOM ID='g', NEW_OPTION=7 /\n&FUTURE PHYSICS=123 /\n&tail /"),encoding='utf-8')
        report,text = fds_compat.analyze(str(self.source))
        self.assertIn('PHYSICS=123',text)
        self.assertIn('NEW_OPTION=7',text)
        self.assertTrue(any('FUTURE' in message for message in report['warnings']))
        self.assertTrue(any('GEOM' in message for message in report['warnings']))
        self.assertFalse(report['validatedByPyroSim'])

    def test_changed_source_rejected_and_repeated_exports_do_not_overwrite(self):
        report,_=fds_compat.analyze(str(self.source))
        first=fds_compat.convert(str(self.source),report['sha256'])
        second=fds_compat.convert(str(self.source),report['sha256'])
        self.assertNotEqual(first['output'],second['output'])
        self.source.write_text(MODEL+'! changed',encoding='utf-8')
        with self.assertRaises(ValueError): fds_compat.convert(str(self.source),report['sha256'])
        with self.assertRaises(ValueError): fds_compat.convert(str(self.source),None)

    def test_file_validation(self):
        for path in [None, '', str(self.root/'missing.fds'), str(self.root), 'bad\npath.fds']:
            with self.assertRaises(ValueError): fds_compat.analyze(path)
        with patch.object(fds_compat,'MAX_BYTES',5):
            with self.assertRaises(ValueError): fds_compat.analyze(str(self.source))

    def test_observed_pyrosim_import_warnings_preserve_reaction_fields(self):
        model = MODEL.replace('&tail /', '&DUMP DT_SMOKE3D=5. /\n&REAC N_SIMPLE_CHEMISTRY_REACTIONS=2, FUEL_C_TO_CO_FRACTION=.6666667 /\n&tail /')
        self.source.write_text(model,encoding='utf-8')
        report,text = fds_compat.analyze(str(self.source))
        self.assertEqual([x['parameter'] for x in report['compatibilityFindings']],
                         ['DT_SMOKE3D','N_SIMPLE_CHEMISTRY_REACTIONS','FUEL_C_TO_CO_FRACTION'])
        self.assertEqual([x['line'] for x in report['compatibilityFindings']],[3,4,4])
        self.assertIn('N_SIMPLE_CHEMISTRY_REACTIONS=2',text)
        self.assertIn('FUEL_C_TO_CO_FRACTION=.6666667',text)
        self.assertFalse(report['validatedByPyroSim'])
        self.assertEqual(fds_compat.import_findings("! &DUMP DT_SMOKE3D=5 /\n&HEAD TITLE='&REAC N_SIMPLE_CHEMISTRY_REACTIONS=2 /' /\n&SURF DT_SMOKE3D=5 /"),[])

    def test_fds_catalog_and_import_launcher_never_sends_fds_to_psm_opener(self):
        manager=EngineeringWorkbenches(self.root/'data')
        identifier=manager.add('pyrosim',str(self.source))['id']
        executable=self.root/'PyroSim.exe'; executable.touch()
        with patch.object(manager,'config',return_value={'pyrosim':{'found':True,'path':str(executable)}}), \
             patch('engineering_workbenches.subprocess.Popen',return_value=Mock(pid=123)) as popen:
            result=manager.launch('pyrosim',identifier,{})
        self.assertEqual(popen.call_args.args[0],[str(executable)])
        self.assertIn('导入 FDS/CAD',result['message'])
        self.assertEqual(self.source.read_text(encoding='utf-8'),MODEL)


if __name__ == '__main__': unittest.main()
