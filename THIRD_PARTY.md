# Third-party attribution

The project is MIT licensed. These upstream components are included under MIT with notices in `licenses/`:

| Upstream | Pinned source | Included/adapted files |
| --- | --- | --- |
| [tzwkb/wechat-decrypt](https://github.com/tzwkb/wechat-decrypt) | `86dc9a719a9bef857f8e45742baf934d45423fab` | `vendor/appmsg.py`, system-message subset in `vendor/system_messages.py`, adapted in-memory `vendor/hook_pbkdf.js` |
| [TANGandXUE/wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool) | `79f1b5b92e12c66aa281b4a60a3c478b5f547dfa` | adapted `vendor/wcdb_windows.py`; Windows runtime Config.Cipher scanner includes project-specific adaptations |

The common snapshot, storage, platform coordination, viewer and full parser are integrated from Julian Cooper's local WeChatArchive work. The vendored Windows file contains the original upstream functionality plus the pre-existing Windows scanner adaptations; no Tencent binaries are redistributed.

Fomomo and acmerfight Gist are reference readers used for external comparisons; their code is not included.

Runtime dependencies are installed separately by pip: [zstandard 0.25.0](https://pypi.org/project/zstandard/0.25.0/) (BSD-3-Clause), [tzdata 2025.2](https://pypi.org/project/tzdata/2025.2/) (Apache-2.0 package, IANA public-domain data), and optional [Frida 17.23.3](https://pypi.org/project/frida/17.23.3/) (upstream license as distributed by the package). Their package licenses remain in their installed distributions. Portable archives do not embed those dependency binaries.
