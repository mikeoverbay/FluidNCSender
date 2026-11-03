# Credits and third-party notices

FluidSender is free software under the **GNU General Public License v3.0** (see [LICENSE](LICENSE)). It stands
on the work of the projects below. All credit for their work goes to their authors.

## Universal G-code Sender (UGS): the auto-leveler and the surface scan are based on it

- **Authors:** Will Winder and the UGS contributors
- **License:** GNU General Public License v3.0
- **Source:** https://github.com/winder/Universal-G-Code-Sender
- **Website and documentation:** https://winder.github.io/ugs_website/

FluidSender's **auto-leveler** (`leveler.py`) is a Python re-implementation of UGS's auto-leveler: its mesh
leveling (`MeshLeveler`), its G-code rewriting (`GcodePreprocessorUtils`, `CoordinateRewriteGuard`) and its number
formatting, written from UGS's source so that FluidSender levels a program exactly as UGS does. The **surface
scan** (`scan.py`) follows UGS's surface scanner (`SurfaceScanner`) and writes and reads UGS's `.xyz` scan files
(`SaveScannedSurfaceAction`, `OpenScannedSurfaceAction`), as extended in the author's own fork of UGS
(https://github.com/mikeoverbay/Universal-G-Code-Sender). Because these parts are derived from UGS, FluidSender is
released under the same license, the GNU GPL v3.0.

## FluidNC: the controller FluidSender talks to

- **Authors:** Bart Dring and the FluidNC contributors
- **License:** GNU General Public License v3.0
- **Source:** https://github.com/bdring/FluidNC
- **Documentation:** http://wiki.fluidnc.com/

FluidSender contains no FluidNC code. It talks to FluidNC over its serial protocol, and its test simulator
(`tools/fake_fluidnc.py`) imitates FluidNC's documented behaviour (messages, alarms, settings, travel) so
FluidSender can be tested without a machine.

## Libraries included in `static/vendor/` (MIT License)

| Library | Version | Authors | Source | Used for |
|---|---|---|---|---|
| **three.js** (with its `OrbitControls` example) | r160 | the three.js authors | https://github.com/mrdoob/three.js · https://threejs.org/docs/ | the 3D visualizer |
| **CodeMirror 5** | 5.65.21 | Marijn Haverbeke and others | https://github.com/codemirror/codemirror5 · https://codemirror.net/5/doc/manual.html | the G-code editor |
| **dockview-core** | 8.3.1 | the dockview authors | https://github.com/dockview/dockview · https://dockview.dev/ | the dockable panels |

Each library's copyright notice is kept in its files. Their license, which applies to each of them:

### three.js

```
The MIT License

Copyright © 2010-2023 three.js authors

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

### CodeMirror 5

Its own license file is included: [static/vendor/codemirror/LICENSE](static/vendor/codemirror/LICENSE)
(MIT, Copyright (C) 2017 by Marijn Haverbeke and others).

### dockview-core

```
The MIT License

Copyright (c) the dockview authors (https://github.com/dockview/dockview)

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
