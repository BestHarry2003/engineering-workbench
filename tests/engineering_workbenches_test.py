"""Temporary catalogs/files only; Fluent/PyroSim processes are mocked."""
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engineering_workbenches import EngineeringWorkbenches


class EngineeringTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.manager=EngineeringWorkbenches(self.root/'data')
        self.models=self.root/'模型文件'; self.models.mkdir()
        self.case=self.models/'flow.cas.h5'; self.case.write_bytes(b'original-case')
        self.psm=self.models/'fire.psm'; self.psm.write_bytes(b'original-psm')

    def tearDown(self): self.temp.cleanup()

    def test_independent_catalogs_and_persistent_chinese_notes(self):
        fluent=self.manager.add('fluent',str(self.case))['id']
        pyro=self.manager.add('pyrosim',str(self.psm))['id']
        self.manager.update('fluent',fluent,{'title':'流场模型','notes':'中文备注','tags':'通风','favorite':True})
        restored=EngineeringWorkbenches(self.root/'data')
        self.assertEqual(restored.get('fluent',fluent)['notes'],'中文备注')
        self.assertEqual(len(restored.list('pyrosim')),1)
        with self.assertRaises(ValueError): restored.get('pyrosim',fluent)
        with self.assertRaises(ValueError): restored.update('fluent',pyro,{'notes':'wrong'})

    def test_scan_merge_preserves_metadata_and_remove_keeps_files(self):
        model=self.manager.add('fluent',str(self.case))['id']
        self.manager.update('fluent',model,{'notes':'keep'})
        result=self.manager.scan('fluent',str(self.models))
        self.assertEqual((result['added'],result['existing']),(0,1))
        self.assertEqual(self.manager.get('fluent',model)['notes'],'keep')
        self.manager.remove('fluent',model)
        self.assertEqual(self.case.read_bytes(),b'original-case')
        self.assertEqual(self.manager.list('fluent'),[])

    def test_format_and_status_validation(self):
        with self.assertRaises(ValueError): self.manager.add('fluent',str(self.psm))
        with self.assertRaises(ValueError): self.manager.add('unknown',str(self.case))
        model=self.manager.add('fluent',str(self.case))['id']
        with self.assertRaises(ValueError): self.manager.update('fluent',model,{'status':'unknown'})
        self.case.unlink()
        self.assertFalse(self.manager.list('fluent')[0]['exists'])
        with patch('engineering_workbenches.subprocess.Popen') as launch:
            with self.assertRaises(ValueError): self.manager.launch('fluent',model,{})
            launch.assert_not_called()

    def test_fluent_launch_only_reads_case_and_selected_data(self):
        model=self.manager.add('fluent',str(self.case))['id']
        data=self.models/'flow-100.dat.h5'; data.write_bytes(b'data')
        config={'fluent':{'path':'C:/fake/fluent.exe','found':True}}
        with patch.object(self.manager,'config',return_value=config), patch('engineering_workbenches.subprocess.Popen',return_value=Mock(pid=123)) as launch:
            self.manager.launch('fluent',model,{'result':str(data.resolve()),'mode':'2ddp','cpus':2})
            command=launch.call_args.args[0]
            self.assertEqual(command[:3],['C:/fake/fluent.exe','2ddp','-t2'])
            journal=Path(command[-1]).read_text(encoding='utf-8')
            self.assertEqual(len(journal.splitlines()),2)
            self.assertTrue(journal.startswith('/file/read-case '))
            self.assertIn('/file/read-data ',journal)
            self.assertNotIn('/solve',journal); self.assertNotIn('write',journal)
            self.assertEqual(launch.call_args.kwargs['cwd'],str(self.models))
        self.assertEqual(self.case.read_bytes(),b'original-case')
        self.assertEqual(data.read_bytes(),b'data')

    def test_pyrosim_results_in_default_subfolder_and_selected_smv(self):
        model=self.manager.add('pyrosim',str(self.psm))['id']
        folder=self.models/'fire'; folder.mkdir()
        result=folder/'fire.smv'; result.write_bytes(b'results')
        self.assertEqual(self.manager.results('pyrosim',model)[0]['path'],str(result.resolve()))
        config={'pyrosim':{'path':'C:/fake/PyroSim.exe','found':True},'pyrosimResults':{'path':'C:/fake/PyroSimResults.exe','found':True}}
        with patch.object(self.manager,'config',return_value=config), patch('engineering_workbenches.subprocess.Popen',return_value=Mock(pid=123)) as launch:
            self.manager.launch('pyrosim',model,{'result':str(result.resolve())})
            self.assertEqual(launch.call_args.args[0],['C:/fake/PyroSimResults.exe',str(result.resolve())])
            self.manager.launch('pyrosim',model,{})
            self.assertEqual(launch.call_args.args[0],['C:/fake/PyroSim.exe',str(self.psm.resolve())])
        self.assertEqual(self.psm.read_bytes(),b'original-psm')

    def test_external_result_and_unknown_executable_blocked(self):
        model=self.manager.add('fluent',str(self.case))['id']
        external=self.root/'other.dat.h5';external.write_bytes(b'other')
        with patch('engineering_workbenches.subprocess.Popen') as launch:
            with self.assertRaises(ValueError): self.manager.launch('fluent',model,{'result':str(external)})
            launch.assert_not_called()
        with self.assertRaises(ValueError): self.manager.save_config({'fluent':str(self.case)})


if __name__ == '__main__': unittest.main(verbosity=2)
