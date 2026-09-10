import struct
from cod4porter.backend.v4_iwd import parse_iwi
from cod4porter.assets.image import from_iwi,build_image_root
from cod4porter.image_fidelity import expected_mips


def bitmap(fmt,width,height,data,flags=2):
    total=28+len(data)
    return b'IWi\x06'+bytes([fmt,flags])+struct.pack('<HHH4I',width,height,1,*([total]*4))+data


# Rectangular, non-power-of-two source: preserve dimensions and every channel.
src=bytes([7,13,29])*6
image=from_iwi(parse_iwi('clipwood',bitmap(2,3,2,src)))
assert (image.width,image.height,image.ps3_format)==(3,2,0xA5)
assert image.resource[:24]==bytes([255,29,13,7])*6
assert image.resource[24:]==bytes(104)
root=build_image_root(image)
assert root[4:8]==bytes([0xA5,1,2,0])
assert struct.unpack_from('>I',root,0x14)[0]==12
assert struct.unpack_from('>HH',root,0x0c)==(3,2)
assert struct.unpack_from('>I',root,0x20)[0]==128
rgba=from_iwi(parse_iwi('alpha',bitmap(1,1,1,bytes([7,13,29,43]))))
assert rgba.resource[:4]==bytes([43,29,13,7])
for fmt,pixels in [(1,bytes([7,13,29,43])),(2,bytes([7,13,29]))]:
    source=parse_iwi('audit',bitmap(fmt,1,1,pixels));converted=from_iwi(source)
    expected=expected_mips(source)
    assert expected[0][4]==4 and expected[0][5]==converted.mips[0].sha256
    import hashlib
    assert expected[0][5]!=hashlib.sha256(pixels).hexdigest().upper()
for raw in [bitmap(2,2,2,bytes(11)),bitmap(2,2,2,bytes(14),flags=0)]:
    try:
        from_iwi(parse_iwi('unsupported',raw))
        raise AssertionError('invalid bitmap accepted')
    except ValueError:pass
print('test_iwi_bitmap PASS')
