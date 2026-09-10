"""Lazy, exact-name preview access to the user's selected PC IWD images."""
from pathlib import Path
import hashlib
import zipfile
from PIL import Image
from cod4porter.backend.v4_iwd import normalize_iwi_name,parse_iwi


class IwdTextures:
    def __init__(self,paths):
        self.paths=list(dict.fromkeys(str(Path(p).resolve()) for p in paths))
        self.archives=[];self.entries=None;self.decoded={};self.errors=[]

    def close(self):
        for archive in self.archives:archive.close()

    def _index(self):
        self.entries={}
        for path in self.paths:
            try:
                archive=zipfile.ZipFile(path);self.archives.append(archive)
                for entry in archive.infolist():
                    if entry.filename.lower().endswith('.iwi'):
                        key=normalize_iwi_name(entry.filename).casefold()
                        self.entries.setdefault(key,[]).append((archive,entry))
            except (OSError,zipfile.BadZipFile) as exc:self.errors.append(f'{Path(path).name}: {exc}')

    def read(self,name):
        key=normalize_iwi_name(name.lstrip(',')).casefold()
        if key in self.decoded:return self.decoded[key]
        if self.entries is None:self._index()
        matches=self.entries.get(key,[])
        if not matches:raise ValueError('No matching image in the selected PC IWD files.')
        data=None;digest=None;owners=[]
        for archive,entry in matches:
            payload=archive.read(entry);value=hashlib.sha256(payload).hexdigest()
            if digest is not None and value!=digest:
                raise ValueError('Conflicting IWD images with the same name: '+name)
            data=payload;digest=value;owners.append({'archive':Path(archive.filename).name,'entry':entry.filename})
        iwi=parse_iwi(name,data)
        if iwi.is_cubemap:raise ValueError('Cubemap is not a diffuse 2D texture.')
        mip=next(m for m in iwi.mips if m.face==0 and m.level==0)
        if iwi.format_id in (11,12,13):
            from gui_materials import decode_bc
            image=decode_bc(mip.data,mip.width,mip.height,{11:0x86,12:0x87,13:0x88}[iwi.format_id])
        elif iwi.format_id in (1,2):
            image=Image.frombytes('RGBA' if iwi.format_id==1 else 'RGB',(mip.width,mip.height),mip.data,'raw','BGRA' if iwi.format_id==1 else 'BGR').convert('RGBA')
        else:raise ValueError('Unsupported PC bitmap preview format: '+iwi.format_name)
        result=(image,{'kind':'pc-iwd-preview','files':owners,'sha256':digest})
        self.decoded[key]=result
        return result
