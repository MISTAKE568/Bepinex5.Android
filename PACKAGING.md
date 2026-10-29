# Packaging Guide

Package a game APK with the scripts in **Bepinex5.Android**. Run the following commands from the repository root in a POSIX shell.

## 1. Prepare the tools and assets

Prepare Python 3.9+, a JDK, Android SDK build-tools and the baksmali/smali JARs. Extract the CI asset pack into `asset-pack/`; this directory contains `build-info.json` and `SHA256SUMS`.

Use an existing signing keystore, or create a local debug key:

```sh
keytool -genkeypair -keystore ./debug.keystore \
  -alias androiddebugkey -storepass android -keypass android \
  -keyalg RSA -keysize 2048 -validity 10000 \
  -dname "CN=Android Debug,O=Android,C=US"
```

For this debug key, the script uses alias `androiddebugkey` and password `android`. For a custom key, pass `--alias` and set `BEPINEX_KS_PASS` and `BEPINEX_KEY_PASS` in your environment.

## 2. Package the APK

Place the game APK at `game.apk`, replace the tool paths below, and run:

```sh
python build.py \
  --apk ./game.apk \
  --assets ./asset-pack \
  --sdk /path/to/android-sdk \
  --jdk /path/to/jdk \
  --baksmali /path/to/baksmali.jar \
  --smali /path/to/smali.jar \
  --keystore ./debug.keystore \
  --out ./out/game-bepinex.apk \
  --work ./out/game.work
```

The script checks the asset pack, prepares HOOKS, injects the loader, repacks the APK and signs it.

When your tools are already discovered automatically, use:

```sh
python build.py --apk ./game.apk --assets ./asset-pack --keystore ./debug.keystore
```

## 3. Find the output

The full command above writes:

- `out/game-bepinex.apk` — the signed game APK.
- `out/game.work/build.json` — the build report.

Relative paths use the current working directory. When `--out` and `--work` are omitted, outputs go to `out/build/` beside `build.py`, using the game's name and version.

## Common options

Append these options to the packaging command:

| Option | Purpose |
| --- | --- |
| `--dry-run` | Check inputs and tools, and write a build plan. |
| `--plugins ./plugins` | Include plugin DLLs from this directory. |
| `--hooks ./HOOKS-Assembly-CSharp.dll` | Use a game-specific HOOKS assembly. |
| `--hookgen /path/to/HookGen.exe --mono /path/to/mono` | Supply tools to generate HOOKS when it is absent from the APK. |
| `--install` | Install the signed APK with adb. |
| `--help` | Show all options. |

## Use the script bundled with an asset pack

From the extracted asset pack root, run its copy of `build.py`. It detects the surrounding pack; the same tool and signing options apply.

```sh
python packaging/build.py --apk /path/to/game.apk --keystore /path/to/debug.keystore
```

## Build an asset pack from source

Prepare .NET SDK 8+, Android NDK, CMake and Ninja. Set `ANDROID_NDK_HOME` to your NDK directory and run from the repository root:

```sh
python build_core.py --configuration Release
python build_core.py --verify-only
dotnet build payload/bootstrap/Be5Bootstrap.csproj -c Release -o build/bootstrap

cmake -S shim -B build/shim -G Ninja \
  -DCMAKE_TOOLCHAIN_FILE="$ANDROID_NDK_HOME/build/cmake/android.toolchain.cmake" \
  -DANDROID_ABI=armeabi-v7a \
  -DANDROID_PLATFORM=android-24 \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/shim --parallel

python assets.py assemble \
  --out dist/my-assets \
  --core out/core \
  --bootstrap build/bootstrap/Be5Bootstrap.dll \
  --shim build/shim/libbe5hook.so
python assets.py verify dist/my-assets
python build.py --apk ./game.apk --assets ./dist/my-assets --keystore ./debug.keystore
```

Use a new output directory for each `assets.py assemble` run. The final command uses the tool and signing setup described above.

[Overview](README.md) · [Packaging](PACKAGING.md) · [Code changes](CODE_CHANGES.md) · [Credits](CREDITS.md)
