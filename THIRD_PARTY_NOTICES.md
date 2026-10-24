# Third-party notices

Presto AI Dash is licensed under the MIT licence: see [LICENSE](LICENSE). This
file lists what in this repository was made by someone else, and under what
terms. It is copied into the Docker image next to the project's own licence.

## Bitmap font data: Pimoroni

`tools/presto_preview/fonts.py` holds the glyphs of the `bitmap6` and `bitmap8`
fonts that the Presto's firmware draws with, so that the host-side preview shows
the same letters as the device. They were generated from
`libraries/bitmap_fonts/font6_data.cpp` and `font8_data.cpp` in
[pimoroni-pico](https://github.com/pimoroni/pimoroni-pico), printable ASCII
only, and are used under that project's licence:

```text
MIT License

Copyright (c) 2021 Pimoroni Ltd

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The font data is used only by the host-side preview tools. The Presto itself
draws with the fonts built into its firmware, and nothing in `presto/` carries
font data.

## WebAssembly runtime: MicroPython

`docs/user/demo/vendor/micropython.js` and `docs/user/demo/vendor/micropython.wasm`
are the unmodified `micropython.mjs` and `micropython.wasm` from
`@micropython/micropython-webassembly-pyscript` **1.29.0-6**, vendored so the
clickable demo on the documentation site loads nothing from another host. They
are used under MicroPython's licence:

```text
The MIT License (MIT)

Copyright (c) 2013-2026 Damien P. George

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

The runtime is not part of the daemon package or the Docker image. It is
published with the documentation site only, runs on the reader's machine, and
`docs/user/demo/vendor/SHA256SUMS` records the two files' hashes; a test fails
if they change, so an upgrade is deliberate.

## Packages the daemon installs

The daemon needs `cryptography` (and, through it, `cffi` and `pycparser`); the
optional device tools need `mpremote`. They are installed from PyPI, not copied
into this repository, and each carries its own licence files in its
`.dist-info` folder. The Docker image keeps those folders, so the licences of
what is in the image are in the image.

## Documentation tooling

The documentation site is built with [MkDocs](https://www.mkdocs.org/)
(BSD-2-Clause) and the [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/)
theme (MIT). They are build tools and are not part of this repository; the pages
they generate carry their own notices.

## Not shipped

No provider logos, mascots or artwork are included. The photographs and
screenshots under `docs/user/images/` show the project's own hardware and its
own preview renderings.
