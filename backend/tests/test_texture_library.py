"""Stock archive supplement only decodes needed, exact-name images."""
from pathlib import Path
import sys,tempfile,zipfile,struct
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cod4porter.texture_library import library_archives,supplement_images
from cod4porter.backend.v4_iwd import inspect_iwds

def iwi(value):
    return b'IWi\x06\x01\x02'+struct.pack('<HHH4I',2,1,1,*([36]*4))+bytes([value])*8
with tempfile.TemporaryDirectory() as d:
    root=Path(d);stock=root/'iw_00.iwd';custom=root/'map.iwd'
    with zipfile.ZipFile(stock,'w') as z:
        z.writestr('images/leaf.iwi',iwi(12));z.writestr('images/stock.iwi',iwi(32))
        z.writestr('images/unused.iwi',b'not an image');z.writestr('sound/bad.wav',b'not a sound')
    with zipfile.ZipFile(custom,'w') as z:z.writestr('images/leaf.iwi',iwi(44))
    images,_=inspect_iwds([custom],include_sounds=False);old=images['leaf'].content_sha256
    assert supplement_images(images,[stock],{'leaf','stock','stock#0'})==1
    assert set(images)=={'leaf','stock'} and images['leaf'].content_sha256==old
    assert len(library_archives(root))==2
print('PASS: filtered stock images, explicit map priority, no unrelated pixels or sound reads.')
