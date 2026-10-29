# CREDITS — Acknowledgements and Sources

**Bepinex5.Android — Breaking Fork** is a hard fork of [BepInEx 5 LTS](https://github.com/BepInEx/BepInEx).
It builds on a large body of upstream and community work. In the order below, we thank every author, repository,
pull request and issue whose work was **built directly into the artifacts**, **used as a source of fixes or code**,
or **used as research**, with links and open-source licenses as stated.

> License note: this repository and the upstream BepInEx code are governed by their respective `LICENSE` files, both **MIT**.
> Licenses for each source below follow that repository's own `LICENSE`.

---

## 1. Upstream projects

| Project | Link | Version | License |
|---|---|---|---|
| BepInEx 5 LTS (core / Preloader / Bootstrap / Patcher) | https://github.com/BepInEx/BepInEx | v5-lts, 5.4.23.5 | **MIT** (© 2018 Bepis) |
| BepInEx.Harmony (Harmony shim / interop) | https://github.com/BepInEx/BepInEx.Harmony | d4cdcb4 (pinned submodule) | MIT |
| HarmonyX (0Harmony 2.x) | https://github.com/BepInEx/HarmonyX | 2.9.0 | MIT |
| MonoMod (RuntimeDetour / Utils / Common) | https://github.com/MonoMod/MonoMod · https://github.com/MonoMod/MonoMod.Common | 22.1.29.1 (+ Common @ aceda0b4) | MIT |
| Mono.Cecil | https://github.com/jbevain/cecil | 0.10.4 | MIT |
| UnityDoorstop (upstream loader reference, including its embedded plthook) | https://github.com/NeighTools/UnityDoorstop | 4.5.0 | LGPL-2.1 |
| plthook (kubo; origin of the `plthook_elf.c` embedded in UnityDoorstop, providing the `__arm__`/`__aarch64__` branches) | https://github.com/kubo/plthook | — | MIT (copyright headers in plthook_elf.c: 2013–2019 Kubo Takehiro, 2022 Geoffrey Horsington) |

---

## 2. Direct sources of fix code

Only source and purpose are listed:

- **2.1 AppleSiliconDetourFix** — BepInEx [PR #1402](https://github.com/BepInEx/BepInEx/pull/1402), fork https://github.com/dobbo-ca/bepinex. License: derived from BepInEx. Purpose: template for our `AndroidDetourFix`.
- **2.2 NuclearPowered's uname check** — [feature-uname-check branch](https://github.com/NuclearPowered/BepInEx/tree/feature-uname-check). License: derived from BepInEx. Purpose: the idea of detecting ARM via libc `uname()`.
- **2.3 XTermFix's removal of the x86 force** — [mikatammi/AzumattDev-BepInEx](https://github.com/mikatammi/AzumattDev-BepInEx/tree/v5-lts-merging) and [Krakazybik/BepInEx](https://github.com/Krakazybik/BepInEx/tree/feat/harmony-v-2-15-0). License: derived from BepInEx. Purpose: no longer forcing `DetourNativeX86Platform`.
- **2.4 Manual bootstrap** — [ZaneYork/SMAPI `android` branch](https://github.com/ZaneYork/SMAPI/tree/android). License: LGPL-3.0. Purpose: precedent for a loader-as-entry-point manual bootstrap of the host game.
- **2.5 Dobby** — [jmpews/Dobby](https://github.com/jmpews/Dobby). License: Apache-2.0. Purpose: early inline-hook attempt in the shim (abandoned).

### 2.6 Other idea references

| Source | Link | License |
|---|---|---|
| `vprodan/MonoMod` `reorganize-arm64` | https://github.com/vprodan/MonoMod/tree/reorganize-arm64 | MIT |
| `All-Of-Us-Mods/BepInEx` `android` | https://github.com/All-Of-Us-Mods/BepInEx/tree/android | derived from BepInEx |
| `toebeann/gib` | https://github.com/toebeann/gib | ISC |
| `MelonLoader` / `LemonLoader` | https://github.com/LavaGang/MelonLoader | respective licenses |

---

## 3. Research basis

These issues / PRs are not code sources, but informed this fork's technical decisions; thanks as well:

| Source | Link | Notes |
|---|---|---|
| BepInEx #761 "ARM Support" | https://github.com/BepInEx/BepInEx/issues/761 | umbrella ARM support request |
| BepInEx #899 "macOS arm64 (Apple Silicon) Support" | https://github.com/BepInEx/BepInEx/issues/899 | tracking of v5 ARM fix paths and community forks |
| BepInEx #336 / #370 | https://github.com/BepInEx/BepInEx/issues/336 · https://github.com/BepInEx/BepInEx/issues/370 | box64/box86 emulation (non-native) experience |
| BepInEx #1402 | https://github.com/BepInEx/BepInEx/pull/1402 | see §2.1 |
| MonoMod #90 "Supporting Apple Silicon" | https://github.com/MonoMod/MonoMod/issues/90 | umbrella ARM tracking (closed by #241) |
| MonoMod #120 "How to add a new CPU architecture" | https://github.com/MonoMod/MonoMod/issues/120 | nike4613's official 6-step guide to adding an architecture |
| MonoMod #50 (armv7 on-device selftest) | https://github.com/MonoMod/MonoMod/issues/50 | SMAPI's best-effort evidence on Android armv7 |
| MonoMod #38 (aarch64 / armv7 help wanted) | https://github.com/MonoMod/MonoMod/issues/38 | "aarch64 fully functional, armv7 untested" |
| MonoMod #174 (Linux ARM .NET8) | https://github.com/MonoMod/MonoMod/issues/174 | pinpointing LinuxSystem's ARM rejection |
| MonoMod #241 (arm64, reorganize) | https://github.com/MonoMod/MonoMod/pull/241 | ARM64 detour support (legacy → reorganize direction) |
| UnityDoorstop #114 | https://github.com/NeighTools/UnityDoorstop/pull/114 | `doorstop_jit_memcpy` (Apple Silicon MAP_JIT writes, upstream approach) |

---

## 4. Upstream official credits

The following come from BepInEx's official credits and are retained by this fork:

- [Usagirei](https://github.com/Usagirei) — console code and technical support
- [essu](https://github.com/exdownloader) — project logo and lots of miscellaneous support
- [denikson](https://github.com/denikson) — [UnityDoorstop](https://github.com/NeighTools/UnityDoorstop), the patchless loader
- [nn@](https://twitter.com/NnAone2cmg) — Japanese wiki translation

---

## 5. Compliance notes

Bundled dependency license texts and their sources are listed in [licenses/SOURCES.txt](licenses/SOURCES.txt).

- This fork and its upstream BepInEx are licensed under **MIT** as declared in their respective `LICENSE` files; this repository's `LICENSE` is MIT as well.
- Licenses for every source are noted in the tables above; where a source requires preserving copyright notices or links, consult its original `LICENSE`/`NOTICE` as well.

[Overview](README.md) · [Packaging](PACKAGING.md) · [Code changes](CODE_CHANGES.md) · [Credits](CREDITS.md)
