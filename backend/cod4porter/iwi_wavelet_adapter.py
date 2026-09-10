"""Invoke the separate IWI wavelet file converter, then read standard bitmap IWI."""
from pathlib import Path
import subprocess
import sys
import tempfile


def decode_wavelet_iwi(data: bytes) -> bytes:
    converter = Path(__file__).resolve().parents[1] / 'tools/iwi_wavelet/convert.py'
    with tempfile.TemporaryDirectory(prefix='iw3-iwi-') as folder:
        source = Path(folder) / 'source.iwi'
        output = Path(folder) / 'decoded.iwi'
        source.write_bytes(data)
        # Explicit script import path also supports the bundled isolated Python.
        launcher = 'import runpy,sys;sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name="__main__")'
        result = subprocess.run([sys.executable, '-B', '-c', launcher, str(converter.parent),
                                 str(converter), str(source), str(output)],
                                capture_output=True, timeout=300,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError('Wavelet decoder: ' + result.stderr.decode('utf-8', errors='replace').strip())
        if not output.is_file():
            raise ValueError('Wavelet decoder returned no bitmap IWI')
        return output.read_bytes()
