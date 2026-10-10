"""Bounded message decoding; preserve raw rows on unsupported content."""
import zstandard


def decode_text(value):
    if value is None:
        return '', 'empty'
    if isinstance(value, str):
        return value, 'text'
    if not isinstance(value, bytes):
        return str(value), 'converted'
    if value.startswith(b'\x28\xb5\x2f\xfd'):
        try:
            if zstandard.frame_content_size(value) > 32 * 1024 * 1024:
                return '[压缩正文过大；原始行已保留]', 'compressed_unparsed'
            value = zstandard.ZstdDecompressor().decompress(value, max_output_size=32 * 1024 * 1024)
        except zstandard.ZstdError:
            return '[压缩正文未解析；原始行已保留]', 'compressed_unparsed'
    try:
        return value.decode('utf-8'), 'decoded'
    except UnicodeDecodeError:
        return '[二进制正文；原始行已保留]', 'binary_unparsed'
