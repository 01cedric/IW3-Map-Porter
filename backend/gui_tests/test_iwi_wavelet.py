"""Real file-converter integration and independent pixel/layout expectations."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cod4porter.backend.v4_iwd import parse_iwi
from cod4porter.assets.image import from_iwi, build_image_root
from cod4porter.image_fidelity import expected_mips
from cod4porter.ps3_budget import drop_top_mip_level
from cod4porter.texture_library import supplement_images
from gui_materials import decode_texture


def pack_bits(values):
    bits=[]
    for value,count in values:bits.extend((value>>i)&1 for i in range(count))
    return bytes(sum(bits[i+j]<<j for j in range(min(8,len(bits)-i))) for i in range(0,len(bits),8))


def header(fmt,payload,width=2,height=2,flags=0):
    return b'IWi\x06'+bytes((fmt,flags))+struct.pack('<HHH4I',width,height,1,*([28+len(payload)]*4))+payload


def flat_wavelet(fmt):
    bases={6:bytes((16,32,48,64)),7:bytes((16,32,48)),8:bytes((64,128)),9:b'\x40',10:b'\x80'}
    n={6:4,7:3,8:2,9:1,10:1}[fmt]
    values=[(0,1)]
    for c in range(n):
        values.append((0,1))
        code,size=(1,1) if n==1 or (n!=3 and c==n-1) else (1,3) if c==0 else (3,2)
        values.extend([(code,size)]*3)
    return header(fmt,bases[fmt]+pack_bits(values))


class WaveletFiles(unittest.TestCase):
    def test_all_five_formats_keep_channels_and_mips(self):
        expected={6:bytes((16,32,48,64)),7:bytes((16,32,48,255)),
                  8:bytes((64,64,64,128)),9:bytes((64,64,64,255)),10:bytes((255,255,255,128))}
        for fmt in range(6,11):
            with self.subTest(fmt=fmt):
                iwi=parse_iwi('images/test.iwi',flat_wavelet(fmt))
                self.assertEqual(iwi.encoded_format_id,fmt)
                self.assertEqual(iwi.format_name,'rgba8')
                self.assertEqual(iwi.mips[0].data,expected[fmt]*4)
                self.assertEqual(iwi.mips[1].data,expected[fmt])

    def test_predictor_delta_escape_and_parity(self):
        bits=[(1,1),(0,4),(256,9),(0,4),(254,9),(1,1)]
        for coefficient in (1,2,-1):bits.extend([(0x3c,6),(255+coefficient,9)])
        bits.extend([(0,1),(1,1),(1,1),(1,1)])
        iwi=parse_iwi('bump',header(8,bytes((100,150))+pack_bits(bits)))
        self.assertEqual(iwi.mips[0].data,b''.join(bytes((l,l,l,149)) for l in (103,101,102,99)))
        self.assertEqual(iwi.mips[1].data,bytes((100,100,100,150)))

    def test_supplement_path_with_hash_in_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'iw_fixture.iwd'
            original=flat_wavelet(8)
            with zipfile.ZipFile(path,'w') as z:z.writestr('images/icbm_rollupdoor_bump#0.iwi',original)
            images={}
            self.assertEqual(supplement_images(images,[path],{'icbm_rollupdoor_bump#0'}),1)
            self.assertEqual(images['icbm_rollupdoor_bump#0'].image.encoded_format_id,8)
            with zipfile.ZipFile(path) as z:self.assertEqual(z.read('images/icbm_rollupdoor_bump#0.iwi'),original)

    def test_swizzled_mips_fidelity_budget_drop_and_preview(self):
        # A rectangular mip chain with varying pixels catches incorrect Morton ordering.
        levels=[bytes((i*7+j)&255 for i in range(w*h) for j in range(4)) for w,h in ((8,4),(4,2),(2,1),(1,1))]
        source=parse_iwi('bitmap',header(1,b''.join(reversed(levels)),8,4))
        image=from_iwi(source)
        self.assertEqual(image.ps3_format,0x85)
        self.assertEqual(struct.unpack_from('>I',build_image_root(image),0x14)[0],0)
        for dropped in (0,1):
            asset=image if dropped==0 else drop_top_mip_level(image)
            self.assertEqual(expected_mips(source,dropped),tuple((m.face,m.level,m.width,m.height,m.size,m.sha256) for m in asset.mips))
            m=asset.mips[0]
            decoded=decode_texture(asset.resource[m.offset:m.offset+m.size],m.width,m.height,asset.ps3_format)
            pixels=levels[dropped]
            self.assertEqual(decoded.tobytes(),b''.join(bytes((pixels[i+2],pixels[i+1],pixels[i],pixels[i+3])) for i in range(0,len(pixels),4)))

    def test_truncated_invalid_and_unsupported_layouts_fail(self):
        bad=[header(8,b'\x40\x80'),header(8,b'\x40\x80\0'),header(8,b'\0',3,2),
             header(8,b'\0',2,2,2)]
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):parse_iwi('broken',data)


if __name__=='__main__':unittest.main()
