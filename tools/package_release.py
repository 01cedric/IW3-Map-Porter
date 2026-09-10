"""Package an already published application and its source using root VERSION."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import shutil
import zipfile

EXCLUDED={'bin','obj','__pycache__','.git','.vs','artifacts'}


def files(root):
    return sorted(p for p in root.rglob('*') if p.is_file() and not p.is_symlink()
                  and not EXCLUDED.intersection(p.relative_to(root).parts)
                  and p.suffix.lower() not in ('.pyc','.pyo','.user','.suo'))


def manifest(root):
    checks={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files(root) if p.name!='FILE_SHA256.json'}
    (root/'FILE_SHA256.json').write_text(json.dumps(checks,indent=2)+'\n',encoding='utf-8')


def package(source,portable,output):
    source=source.resolve();portable=portable.resolve();output=output.resolve()
    version=(source/'VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+',version):raise ValueError('VERSION must contain major.minor.patch')
    if (portable/'VERSION').read_text(encoding='utf-8').strip()!=version:
        raise ValueError('Published VERSION differs from source; publish again before packaging.')
    if not (portable/'IW3MapPorter.exe').is_file():raise ValueError('Published executable is missing.')
    for path in files(source/'backend'):
        relative=path.relative_to(source/'backend')
        published=portable/'backend'/relative
        if not published.is_file() or published.read_bytes()!=path.read_bytes():
            raise ValueError('Published backend differs from source: '+relative.as_posix()+
                             '. Publish into a clean directory before packaging.')
    output.mkdir(parents=True,exist_ok=True)
    for name in ('docs','validation','licenses'):shutil.copytree(source/name,portable/name,dirs_exist_ok=True)
    for name in ('README.md','VERSION'):shutil.copy2(source/name,portable/name)
    shutil.copy2(source/'src/IW3MapPorter.Desktop/Assets/IW3MapPorter.ico',portable/'IW3MapPorter.ico')
    (portable/'START_HERE.txt').write_text(
        f'IW3MapPorter — Version {version}\n\n'
        'Extract the entire folder and start IW3MapPorter.exe.\n'
        'Keep backend, runtime and VERSION beside the executable.\n'
        '.NET and Python are included.\n\n'
        'Read README.md for conversion, linker setup, MapEnts controls and texture sources.\n'
        'Game EBOOTs and retail support zones are user-supplied.\n'
        'Nuked and Getaway loading is user-confirmed. Map and rendering limits remain.\n'
        'See docs/STATUS.md and validation for tested features and remaining limits.\n',encoding='utf-8')
    results=[]
    for root,label in ((source,'Source'),(portable,'Windows_x64')):
        if output==root or (root in output.parents and 'artifacts' not in output.relative_to(root).parts):
            raise ValueError('Release output must be outside the package or under excluded artifacts/.')
        manifest(root)
        dest=output/f'IW3MapPorter_Version_{version}_{label}.zip'
        with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for path in files(root):archive.write(path,'IW3MapPorter/'+path.relative_to(root).as_posix())
        with zipfile.ZipFile(dest) as archive:
            bad=archive.testzip()
            if bad:raise ValueError('ZIP verification failed: '+bad)
        results.append({'path':str(dest),'bytes':dest.stat().st_size,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()})
    (output/f'IW3MapPorter_Version_{version}_SHA256.json').write_text(json.dumps({Path(r['path']).name:r['sha256'] for r in results},indent=2)+'\n',encoding='utf-8')
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--portable',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    print(json.dumps(package(args.source,args.portable,args.output or args.source/'artifacts/releases'),indent=2))
