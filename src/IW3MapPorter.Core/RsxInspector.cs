using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text.Json;
using IW4.Render.Shaders;
using IW4.Assets.Math;
using IW4.Assets.Assets.Image;

namespace IW3MapPorter.Core;

/// <summary>IW4Studio's hardware decoder, without assuming IW4's engine bindings.</summary>
public static class RsxInspector
{
    public const string UpstreamCommit = "a40c917e6f46553f419b01d7c7ed5a3a5a875b07";

    public static string Inspect(byte[] program, bool fragment)
    {
        int offset = RsxProgramDecoder.ShaderUploadOffset(program);
        if (offset < 0) throw new InvalidDataException("No valid RSX/Cg upload header (size +0x18, offset +0x1c). Raw microcode is not interpreted as a Cg program.");
        int size = checked((int)BinaryPrimitives.ReadUInt32BigEndian(program.AsSpan(0x18, 4)));
        if (size < 16 || size % 16 != 0) throw new InvalidDataException("Invalid RSX upload size.");
        object instructions;
        bool complete;
        if (fragment)
        {
            var decoded = RsxProgramDecoder.DecodeFragment(program[..(offset + size)], offset);
            complete = !decoded.IsEmpty && decoded[^1].End && decoded.All(i => i.ByteCount != 32 || i.Constant.HasValue);
            instructions = decoded.Select(i => new
            {
                i.Index, i.Offset, opcode = i.OpcodeType.ToString(), opcode_value = i.Opcode,
                i.ByteCount, i.End, i.DestRegister, i.DestFp16, i.TextureUnit,
                i.IsTexture, i.IsControlFlow, mask = i.WriteMask.ToString(),
                raw = new[] { i.Dst, i.Src0, i.Src1, i.Src2 },
                inline_constant_bits = i.Constant is { } c ? new[] { c.XBits, c.YBits, c.ZBits, c.WBits } : null
            }).ToArray();
        }
        else
        {
            var decoded = RsxProgramDecoder.DecodeVertexProgram(program);
            complete = !decoded.Instructions.IsEmpty && (decoded.Instructions[^1].Word3 & 1) != 0;
            instructions = decoded.Instructions.Select(i => new
            {
                i.Index, i.Offset, vector_opcode = i.VectorOpcode.ToString(), scalar_opcode = i.ScalarOpcode.ToString(),
                input = i.InputAttribute.ToString(), result = i.Result.ToString(), i.ConstSource, i.HasControlFlow,
                raw = new[] { i.Word0, i.Word1, i.Word2, i.Word3 }
            }).ToArray();
        }
        return JsonSerializer.Serialize(new
        {
            schema = "iw3-rsx-inspection/v1", upstream_commit = UpstreamCommit,
            kind = fragment ? "fragment" : "vertex", sha256 = Convert.ToHexString(SHA256.HashData(program)),
            upload_offset = offset, upload_bytes = size, complete_instruction_stream = complete,
            execution_supported = false,
            note = "RSX instruction decoding only. IW3 constants, technique passes and the effects runtime are not verified or emulated.",
            instructions
        }, PorterSettings.JsonOptions);
    }

    public static uint SwapFragmentLanes(uint word) => RsxProgramDecoder.FragmentWord(word);
    public static System.Numerics.Vector3 DecodePlacement(uint word) => new PackedSigned11_11_10(word).DecodePlacement();
    public static System.Numerics.Vector3 DecodeVertex(uint word) => new PackedSigned11_11_10(word).DecodeRsxNormalized();
    public static ushort EffectiveTextureRemap(uint payload, byte format) => new GfxImageTextureRemap(payload).EffectiveComponentEncoding((GfxImageBaseFormat)(format & 0x9f));
}
