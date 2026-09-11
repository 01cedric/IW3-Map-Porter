"""File-based adapter for the standalone IW3 PC structural reader."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile


class StructuralIndex:
    def __init__(self, report):
        if not report.get('passed'):
            raise ValueError('PC structural reader: ' + report.get('error', 'incomplete traversal'))
        self.report = report
        self.assets = report['assets']
        self.omitted_roots=set()
        self._pointers = report['pointers']

    def rows(self, *type_ids):
        return [row for row in self.assets if row['type_id'] in type_ids and row['root'] not in self.omitted_roots]

    def with_all_roots(self):
        return StructuralIndex(self.report)

    def single_root(self, *type_ids):
        rows = self.rows(*type_ids)
        if len(rows) != 1 or rows[0]['root'] is None:
            raise ValueError(f'Expected one structural root for asset types {type_ids}, found {len(rows)}')
        return rows[0]['root']

    def pointer(self, field_offset):
        key = str(field_offset)
        if key not in self._pointers:
            raise ValueError(f'No structurally resolved pointer field at 0x{field_offset:X}')
        return self._pointers[key]

    def summary(self):
        return {key:self.report[key] for key in (
            'asset_count','stream_end','zone_bytes','packed_references',
            'block_cursors','block_limits','persistent_blocks_exact')}


def read_structure(zone: bytes, *, report_path=None) -> StructuralIndex:
    reader = Path(__file__).resolve().parents[1] / 'tools/iw3_pc_reader/reader.py'
    with tempfile.TemporaryDirectory(prefix='iw3-structure-') as folder:
        output = Path(folder) / 'structure.json'
        # The zone travels over stdin: staging a second full copy of a large
        # decompressed zone on disk doubled the space requirement and failed
        # whole conversions with ENOSPC on tight drives.
        result = subprocess.run(
            [sys.executable, '-B', str(reader), '-', '--zone', '--out', str(output)],
            input=zone, capture_output=True, timeout=600,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if not output.is_file():
            raise ValueError('PC structural reader produced no report: ' +
                             result.stderr.decode('utf-8', errors='replace').strip()[-2000:])
        report = json.loads(output.read_text())
        if report_path is not None:
            target=Path(report_path);target.parent.mkdir(parents=True,exist_ok=True)
            try:
                target.write_text(json.dumps({k:v for k,v in report.items() if k!='pointers'},indent=2)+'\n',encoding='utf-8')
            except OSError as error:
                if error.errno==28:
                    raise ValueError(
                        f'Not enough free disk space to write the structure report to {target}. '
                        'Free space on that drive (or choose another output folder) and run again.') from error
                raise
        if result.returncode and report.get('passed'):
            raise ValueError(f'PC structural reader exited with status {result.returncode}')
        return StructuralIndex(report)
