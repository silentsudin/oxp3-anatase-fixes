# Our Mesa in Flathub apps: `org.freedesktop.Platform.GL.oxp3`

The extension is built from exactly the same inputs as the host RPM build - MESA_COMMIT
(Mesa 26.3.1), the ordered series from `mesa_patch_list()` (26.3 backports, perf patches, the
CM kernel-injection series) and Anatase's overrides.patch - so a Flathub app and a
native/Steam game run the same Mesa version with the same features. Keep the two lists in step: a patch added to one build and not the other is how the
stack splits again.

## Problem (measured 2026-11-21)

Flathub apps don't use the host Mesa. They take it from their runtime's GL driver extension.

| | Vulkan driver in the sandbox |
|---|---|
| host, `org.anatase.Steam` | Mesa 27.0.0-devel (ours) |
| `dev.playhorizon.Horizon`, `com.heroicgameslauncher.hgl` (runtime `org.freedesktop.Platform//26.08`) | **Mesa 26.2.5** (`GL.default//26.08`) |
| apps on 25.08 (Ryujinx, KDE 6.10 / GNOME 50 apps) | Mesa 26.2.7 (`GL.default//25.08`) |

The installer's Flatpak Mesa step (`install_flatpak_mesa`) covers only the Anatase Steam runtime.
A host-built `libvulkan_intel.so` won't load in `org.freedesktop.Platform`:
`libdisplay-info.so.3` and `libSPIRV-Tools.so` are missing there, and its `libexpat` lacks
`LIBEXPAT_2.9.0`. Bundling those and setting `LD_LIBRARY_PATH` works, but it puts host libraries
into the whole app.

## Solution

The same Mesa the host gets (`MESA_COMMIT`, Anatase's `overrides.patch` without the RADV hunk,
`patches/mesa/*`) is built **inside `org.freedesktop.Sdk//26.08`**, so it uses the runtime's ABI.
It ships as a normal Flatpak GL driver extension.

- `tools/flatpak-gl/build-in-sdk.sh` runs in the SDK with `Sdk.Extension.llvm22` and no network.
  It builds the two things the SDK lacks as static libraries:
  - SPIRV-Tools `vulkan-sdk-1.4.363.0`;
  - SPIRV-LLVM-Translator `v22.1.8` (matches the extension's LLVM 22.1.8).

  It uses PyYAML from its sdist. Then it builds Mesa with these options:
  - drivers: `-Dvulkan-drivers=intel`, `-Dgallium-drivers=iris,llvmpipe,softpipe`;
  - `-Dglvnd=enabled`, GBM, X11 and Wayland;
  - no rusticl, VA-API or Vulkan layers.

  It ships `libLLVM.so.22.1` with the driver, and fails if any library is left unresolved. All
  sources are pinned by sha256 in `flatpak_gl_sources()`.
- `build_flatpak_gl` (in `tools/lib/install-common.sh`, called by `do_build` in both installers)
  commits the result to `~/.cache/oxp3-fixes/flatpak-gl/repo` as
  `runtime/org.freedesktop.Platform.GL.oxp3/x86_64/26.08`. The metadata has
  `[ExtensionOf] priority=100`. The build is cached on: Mesa commit, patches, the build script,
  source pins, and the SDK and LLVM extension commits.
- `install_flatpak_gl` (in `install` of both installers) does three things:
  1. copies the repo to `/var/lib/oxp3-fixes/flatpak-gl-repo` and adds it as the system remote
     `oxp3-gl` (local, no GPG);
  2. installs the extension system-wide and **pins** it;
  3. writes `/etc/environment.d/60-oxp3-flatpak-gl.conf`:

     ```
     FLATPAK_GL_DRIVERS=oxp3:default
     ```
- `uninstall_flatpak_gl` undoes all of it. `status` shows the installed version and the
  variable.

Nothing here touches the `/usr` overlay, so it survives Anatase updates. Rebuild after a Mesa
bump; the cache key picks that up.

### Why `oxp3:default` and `priority=100`

These were tested with a dummy extension against Horizon (a system app) and PPSSPP (a user app):

- `FLATPAK_GL_DRIVERS=oxp3` alone disables `GL.default` **and** `GL32.default`. That would leave
  32-bit GL and every app on another runtime branch with no driver at all.
- With both active, **GL.default wins** the merged `vulkan/icd.d/intel_icd.x86_64.json` and
  the `ld.so` order, whatever order `FLATPAK_GL_DRIVERS` lists them in.
- With `priority=100` in our metadata, **ours wins both**. GL.default still supplies
  everything we don't build: other vendors, lavapipe, OpenCL, Vulkan layers, 32-bit GL.
- A user-installed extension is used by system apps and the other way round. The installer
  uses the system installation because `install` runs as root.
- The extension point has `autoprune-unless=active-gl-driver`. A `flatpak uninstall --unused`
  run without the variable would remove the extension, hence the pin.

## Using it

`./oxp3-install.sh` builds and installs it like every other component. Afterwards **log out
and back in** (or reboot) so the session picks up `FLATPAK_GL_DRIVERS`. To check:

```sh
echo $FLATPAK_GL_DRIVERS        # oxp3:default
./oxp3-install.sh --status      # "Flatpak GL driver 26.08: Mesa 27.0.0-devel ..."
flatpak run --command=sh dev.playhorizon.Horizon -c \
  'readlink /usr/lib/x86_64-linux-gnu/GL/vulkan/icd.d/intel_icd.x86_64.json'
# -> /usr/lib/x86_64-linux-gnu/GL/oxp3/vulkan/icd.d/intel_icd.x86_64.json
```

To go back to Flathub's Mesa for one launch:
`FLATPAK_GL_DRIVERS=default flatpak run <app>`.

## Limits

- **64-bit only.** 32-bit GL/Vulkan (32-bit Wine/DXVK inside Flatpak Heroic/Bottles) stays on
  `GL32.default`. A `GL32.oxp3` would need an i386 SDK build.
- **Only the 26.08 branch is built by default.** Apps on 25.08 keep Flathub's Mesa. Set
  `OXP3_FLATPAK_GL_BRANCHES="26.08 25.08"` to build both; 25.08 needs `Sdk//25.08` and
  `llvm22//25.08`, which the build installs into the user installation.
- `org.anatase.Steam` is unaffected. It has its own runtime, covered by `install_flatpak_mesa`.
- XeSS XMX still needs the per-game `ANV_CM_HELPER` environment. The extension includes the
  `patches/mesa` XMX changes but sets no variables.
