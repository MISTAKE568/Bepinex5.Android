<p align="center">
  <img src="https://avatars2.githubusercontent.com/u/39589027?s=256" width="128" alt="BepInEx">
</p>

# Bepinex5.Android

**Breaking Fork**

A hard fork of [BepInEx 5 LTS](https://github.com/BepInEx/BepInEx) for Unity Mono games on Android. It replaces UnityDoorstop with a JNI loader and provides scripts to build BepInEx into a game APK.

## Documentation

- **[Packaging guide](PACKAGING.md)** — prepare the tools and package a game.
- **[Code changes](CODE_CHANGES.md)** — changes to BepInEx and the Android tooling.
- **[Credits and licenses](CREDITS.md)** — upstream projects, contributors and sources.

## Platform

| System | Architecture | Backend |
| --- | --- | --- |
| Android | ARM32 (`armeabi-v7a`) | Unity Mono |

## Libraries

- [HarmonyX](https://github.com/BepInEx/HarmonyX) — 2.9.0
- [MonoMod](https://github.com/MonoMod/MonoMod) — 22.1.29.1 (`RuntimeDetour`, `Utils`)
- [Mono.Cecil](https://github.com/jbevain/cecil) — 0.10.4

## License

[MIT](LICENSE). See [Credits](CREDITS.md) for third-party sources and licenses.

*This is a vibe-coded project.*
