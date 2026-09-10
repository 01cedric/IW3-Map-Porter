"""Read-only automatic identification of a PS3 SFO, fastfile, or decrypted EBOOT.

Usage: python tools/inspect_ps3_revision.py PATH [--output report.json]
Neither a region label nor APP_VER authorizes use of another binary's offsets.
"""
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from gui_revision import inspect_elf, installation_metadata, read_sfo, ui_identity
from cod4porter.backend.v4_ffio import read_ps3_fastfile, require_retail_ps3_adler32

def inspect(path):
    path=Path(path)
    with path.open('rb') as f: magic=f.read(8)
    if magic.startswith(b'\x7fELF'):return dict(kind='elf',**inspect_elf(path))
    if magic.startswith(b'\0PSF'):return dict(kind='sfo',metadata=read_sfo(path),running_executable_verified=False)
    doc=read_ps3_fastfile(path)
    return dict(kind='fastfile',path=str(path.resolve()),metadata=installation_metadata(path),
                version=doc.version,verified_compression_frames=require_retail_ps3_adler32(doc),
                file_sha256=doc.file_sha256.lower(),ui=ui_identity(doc.zone),
                note='UI identity is relevant only for UI files. Fastfile framing does not prove map runtime compatibility.')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('path',type=Path);p.add_argument('--output',type=Path)
    a=p.parse_args()
    try: result=inspect(a.path)
    except (ValueError,OSError) as e: p.exit(1,str(e)+'\n')
    text=json.dumps(result,indent=2)+'\n'
    if a.output:a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(text,encoding='utf-8')
    print(text,end='')
if __name__=='__main__':main()
