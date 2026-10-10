// Adapted from tzwkb/wechat-decrypt, MIT; see licenses/wechat-decrypt.txt.
// Candidates are sent only to the in-memory Python callback, never console logs.
const hook = Module.findGlobalExportByName('CCKeyDerivationPBKDF');
const seen = new Set();
function capture(kind, pointer, size) {
  const hex = Array.from(new Uint8Array(pointer.readByteArray(size)))
    .map(x => x.toString(16).padStart(2, '0')).join('');
  const id = kind + hex;
  if (!seen.has(id)) { seen.add(id); send({kind, hex}); }
}
if (!hook) {
  send({error: 'hook_unavailable'});
} else {
  Interceptor.attach(hook, {
    onEnter(args) {
      this.capture = args[2].toInt32() === 32 && args[6].toInt32() > 1000 && args[8].toInt32() === 32;
      this.output = args[7];
      if (this.capture) { try { capture('raw', args[1], 32); } catch (_) {} }
    },
    onLeave(result) {
      if (this.capture && result.toInt32() === 0) {
        try { capture('derived', this.output, 32); } catch (_) {}
      }
    }
  });
  send({ready: true});
}
