# Code Changes

Based on BepInEx 5 LTS **5.4.23.5**. File-level changes and additions are listed below.

## BepInEx source

| File | Change or purpose |
| --- | --- |
| [BepInEx.Preloader/Platform.cs](src/BepInEx/BepInEx.Preloader/Platform.cs) | Adds Android libc detection and determines process architecture. |
| [BepInEx.Preloader/Entrypoint.cs](src/BepInEx/BepInEx.Preloader/Entrypoint.cs) | Applies Android detour setup and resolves paths for the Android bootstrap. |
| [BepInEx.Preloader/Preloader.cs](src/BepInEx/BepInEx.Preloader/Preloader.cs) | Handles runtime-fix initialization and reports deferred errors. |
| [BepInEx.Preloader/RuntimeFixes/XTermFix.cs](src/BepInEx/BepInEx.Preloader/RuntimeFixes/XTermFix.cs) | Preserves ARM detour selection during console initialization. |
| [BepInEx.Preloader/RuntimeFixes/AndroidDetourFix.cs](src/BepInEx/BepInEx.Preloader/RuntimeFixes/AndroidDetourFix.cs) | Adds Android ARM memory protection, page alignment and instruction-cache handling. |
| [BepInEx.Preloader/RuntimeFixes/AppleSiliconDetourFix.cs](src/BepInEx/BepInEx.Preloader/RuntimeFixes/AppleSiliconDetourFix.cs) | Adds Apple Silicon detour platform setup. |
| [BepInEx.Preloader/BepInEx.Preloader.dll.config](src/BepInEx/BepInEx.Preloader/BepInEx.Preloader.dll.config) | Maps libc imports to Android's libc. |
| [BepInEx/Logging/UnityLogSource.cs](src/BepInEx/BepInEx/Logging/UnityLogSource.cs) | Handles Unity log callback discovery and reports missing callbacks. |
| [BepInEx/Configuration/ConfigFile.cs](src/BepInEx/BepInEx/Configuration/ConfigFile.cs) | Updates an obsolete API message. |

## Android loader

| File | Change or purpose |
| --- | --- |
| [payload/bootstrap/Boot.cs](payload/bootstrap/Boot.cs) | Runs the Preloader and Chainloader in separate startup phases. |
| [payload/bootstrap/Be5Bootstrap.csproj](payload/bootstrap/Be5Bootstrap.csproj) | Builds the managed Android bootstrap. |
| [payload/config/BepInEx.cfg](payload/config/BepInEx.cfg) | Provides the default runtime configuration. |
| [shim/mono_bepinex_hook.c](shim/mono_bepinex_hook.c) | Extracts runtime assets and invokes the bootstrap during Mono and Unity startup. |
| [shim/asset_copy.c](shim/asset_copy.c) and [asset_copy.h](shim/asset_copy.h) | Updates bundled assemblies atomically and preserves existing configuration and extensions. |
| [shim/bootstrap_gate.h](shim/bootstrap_gate.h) | Tracks startup attempts and phase completion. |
| [shim/CMakeLists.txt](shim/CMakeLists.txt) | Builds the JNI shim with the Android NDK. |

## Packaging and builds

| File | Change or purpose |
| --- | --- |
| [nuget.config](src/BepInEx/nuget.config) | Uses the public NuGet package feed. |
| [build.py](build.py) | Coordinates APK injection, repacking and signing. |
| [build_core.py](build_core.py) | Builds and verifies the managed BepInEx core. |
| [assets.py](assets.py) | Assembles resource packs and verifies their files and checksums. |
| [project.json](project.json) | Records project version, upstream version and source layout. |
| [injector/dex/patch_dex.py](injector/dex/patch_dex.py) | Adds the native loader call to Unity's Android startup code. |
| [injector/dex/verify_dex.py](injector/dex/verify_dex.py) | Verifies the injected loader call. |
| [injector/hooks/patch_hooks.py](injector/hooks/patch_hooks.py) | Extracts or generates the game's HOOKS assembly. |
| [injector/apk/repack.py](injector/apk/repack.py) | Rebuilds the APK with patched DEX, native loader and managed assets. |

## Continuous integration

| File | Change or purpose |
| --- | --- |
| [ci/check_source_tree.py](ci/check_source_tree.py) | Checks tracked files for compiled build products. |
| [ci/run_tests.py](ci/run_tests.py) | Require the host test tools and fail CI when a test is skipped. |
| [ci/check_workflow_shell.py](ci/check_workflow_shell.py) | Checks workflow shell syntax. |
| [ci/prepare_release_zip.sh](ci/prepare_release_zip.sh) | Verifies and archives the release resource pack. |
| [.github/workflows/build-assets.yml](.github/workflows/build-assets.yml) | Builds, verifies and publishes resource packs. |

[tests/](tests/) covers asset packs, APK packaging, DEX injection and startup helpers.

[Overview](README.md) · [Packaging](PACKAGING.md) · [Code changes](CODE_CHANGES.md) · [Credits](CREDITS.md)
