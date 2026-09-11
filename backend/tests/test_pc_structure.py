"""Integration checks for structural asset discovery through the file adapter."""
from pathlib import Path
import json
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cod4porter.backend.v4_ffio import parse_xasset_list
from cod4porter.pc_structure import read_structure
from cod4porter.pc_technique import top_level_technique_sets
from cod4porter.source_assets import top_level_rawfiles

F=0xffffffff

def source(typeid,payload,virtual_bytes):
    body=struct.pack('<4I',0,0,1,F)+struct.pack('<II',typeid,F)+payload
    return struct.pack('<11I',len(body),0,148,0,0,0,virtual_bytes,0,0,0,0)+body

class StructureIntegrationTests(unittest.TestCase):
    def test_unknown_technique_name_uses_exact_root(self):
        name=b'customShader\0'
        zone=source(5,struct.pack('<I',F)+bytes(144)+name,8+len(name))
        index=read_structure(zone);assets=replace(parse_xasset_list(zone,'pc'),structural_index=index)
        with patch('cod4porter.pc_technique.scan_technique_sets',side_effect=AssertionError('scanner must not run')):
            rows=top_level_technique_sets(zone,assets)
        self.assertEqual([(x.name,x.root_offset,x.source_asset_index) for x in rows],[('customShader',68,0)])

    def test_rawfile_payload_and_name_are_structural(self):
        name=b'maps/mp/custom.gsc\0';data=b'fake effect_shader data'
        zone=source(31,struct.pack('<3I',F,len(data),F)+name+data+b'\0',8+len(name)+len(data)+1)
        index=read_structure(zone);assets=replace(parse_xasset_list(zone,'pc'),structural_index=index)
        with patch('cod4porter.source_assets.scan_rawfiles',side_effect=AssertionError('scanner must not run')):
            rows=top_level_rawfiles(zone,assets)
        self.assertEqual(rows[0].payload,data)
        self.assertEqual(rows[0].physical_end,len(zone))

    def test_failed_walk_writes_report_and_stops(self):
        zone=source(127,b'',8)
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'diagnostic.json'
            with self.assertRaisesRegex(ValueError,'Unsupported XAsset type 127'):
                read_structure(zone,report_path=output)
            report=json.loads(output.read_text())
            self.assertFalse(report['passed'])
            self.assertEqual(report['completed_assets'],[])

if __name__=='__main__':unittest.main()
