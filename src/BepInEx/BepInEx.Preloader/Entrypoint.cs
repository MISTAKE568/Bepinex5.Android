using System;
using System.IO;
using System.Linq;
using System.Reflection;
using BepInEx.Preloader;
using BepInEx.Preloader.RuntimeFixes;
using Doorstop;

namespace BepInEx.Preloader
{
	internal static class PreloaderRunner
	{
		/// <summary>Store startup errors until the preloader logger is available.</summary>
		internal static Exception XTermFixException;
		internal static Exception ConsoleSetOutFixException;
		// Load core dependencies first so game-shipped copies do not bypass our assembly resolver.
		private static readonly string[] CriticalAssemblies =
		{
			"Mono.Cecil.dll",
			"Mono.Cecil.Mdb.dll",
			"Mono.Cecil.Pdb.dll",
			"Mono.Cecil.Rocks.dll",
			"MonoMod.Utils.dll",
			"MonoMod.RuntimeDetour.dll",
			"0Harmony.dll",
		};

		private static void LoadCriticalAssemblies()
		{
			foreach (string criticalAssembly in CriticalAssemblies)
			{
				try
				{
					Assembly.LoadFile(Path.Combine(Paths.BepInExAssemblyDirectory, criticalAssembly));
				}
				catch (Exception)
				{
					// Keep resolution available for assemblies that are absent from this core distribution.
				}
			}
		}

		public static void PreloaderPreMain()
		{
			PlatformUtils.SetPlatform();

			// SetPlatform must finish before PlatformHelper is read and locked.
			// Install native platforms before DetourHelper caches its first initialization attempt.
			AppleSiliconDetourFix.Apply();
			AndroidDetourFix.Apply();

			// Fall back to this assembly when Mono's environment snapshot lacks the native loader variables.
			string invokeDllPath = EnvVars.DOORSTOP_INVOKE_DLL_PATH;
			if (string.IsNullOrEmpty(invokeDllPath))
				invokeDllPath = typeof(EnvVars).Assembly.Location;
			string bepinPath = Utility.ParentDirectory(Path.GetFullPath(invokeDllPath), 2);

			string processPath = EnvVars.DOORSTOP_PROCESS_PATH;
			string managedDir = EnvVars.DOORSTOP_MANAGED_FOLDER_DIR;
			string[] searchDirs = EnvVars.DOORSTOP_DLL_SEARCH_DIRS;

			string gameRoot = Utility.ParentDirectory(bepinPath, 1);
			// Discover extracted assemblies so paths do not depend on a hardcoded game name.
			if (string.IsNullOrEmpty(processPath) || string.IsNullOrEmpty(managedDir))
			{
				string detectedManaged = null;
				string detectedExe = null;
				try
				{
					string rootMgd = Path.Combine(gameRoot, "Managed");
					if (Directory.Exists(rootMgd) && File.Exists(Path.Combine(rootMgd, "Assembly-CSharp.dll")))
					{
						detectedManaged = rootMgd;
						detectedExe = Path.Combine(gameRoot, "GameAssembly");
					}
					else
					{
						foreach (var dir in Directory.GetDirectories(gameRoot))
						{
							string mgd = Path.Combine(dir, "Managed");
							if (Directory.Exists(mgd) && File.Exists(Path.Combine(mgd, "Assembly-CSharp.dll")))
							{
								detectedManaged = mgd;
								detectedExe = Path.Combine(gameRoot, Path.GetFileName(dir));
								break;
							}
						}
					}
				}
				catch (Exception) { }
				if (string.IsNullOrEmpty(processPath))
					processPath = detectedExe ?? Path.Combine(gameRoot, "GameAssembly");
				if (string.IsNullOrEmpty(managedDir))
					managedDir = detectedManaged ?? Path.Combine(gameRoot, "Managed");
			}
			if (searchDirs == null || searchDirs.Length == 0)
				searchDirs = new[] { Path.Combine(bepinPath, "core"), managedDir };

			Paths.SetExecutablePath(processPath, bepinPath, managedDir, searchDirs);

			LoadCriticalAssemblies();
			AppDomain.CurrentDomain.AssemblyResolve += LocalResolve;
			// Remove the temporary resolver so it cannot override the core resolver.
			AppDomain.CurrentDomain.AssemblyResolve -= Entrypoint.ResolveCurrentDirectory;
			PreloaderMain();
		}

		private static void PreloaderMain()
		{
			if (Preloader.ConfigApplyRuntimePatches.Value)
			{
				// Apply console fixes before the terminal opens. Isolate failures and log them after logger setup.
				Utility.TryDo(() => XTermFix.Apply(), out XTermFixException);
				Utility.TryDo(() => ConsoleSetOutFix.Apply(), out ConsoleSetOutFixException);
			}

			Preloader.Run();
		}

		private static Assembly LocalResolve(object sender, ResolveEventArgs args)
		{
			if (!Utility.TryParseAssemblyName(args.Name, out var assemblyName))
				return null;

			// Parse names in managed code to avoid native path-encoding failures.
			var validAssemblies = AppDomain.CurrentDomain
										 .GetAssemblies()
										 .Select(a => new { assembly = a, name = Utility.TryParseAssemblyName(a.FullName, out var name) ? name : null })
										 .Where(a => a.name != null && a.name.Name == assemblyName.Name)
										 .OrderByDescending(a => a.name.Version)
										 .ToList();

			// Prefer the requested version; use the highest loaded version as a fallback.
			var foundMatch = validAssemblies.FirstOrDefault(a => a.name.Version == assemblyName.Version) ?? validAssemblies.FirstOrDefault();
			var foundAssembly = foundMatch?.assembly;

			if (foundAssembly != null)
				return foundAssembly;

			if (Utility.TryResolveDllAssembly(assemblyName, Paths.BepInExAssemblyDirectory, out foundAssembly)
				|| Utility.TryResolveDllAssembly(assemblyName, Paths.PatcherPluginPath, out foundAssembly)
				|| Utility.TryResolveDllAssembly(assemblyName, Paths.PluginPath, out foundAssembly))
				return foundAssembly;

			return null;
		}
	}
}

namespace Doorstop
{
	internal static class Entrypoint
	{
		private static string preloaderPath;

		public static void Start()
		{
			// Keep a fallback log path in case resolving the game directory fails.
			string silentExceptionLog = $"preloader_{DateTime.Now:yyyyMMdd_HHmmss_fff}.log";

			try
			{
				EnvVars.LoadVars();

				string gamePath = Path.GetDirectoryName(EnvVars.DOORSTOP_PROCESS_PATH) ?? ".";
				silentExceptionLog = Path.Combine(gamePath, silentExceptionLog);

				// The native loader path avoids Assembly.Location encoding differences between Mono runtimes.
				preloaderPath = Path.GetDirectoryName(Path.GetFullPath(EnvVars.DOORSTOP_INVOKE_DLL_PATH));

				AppDomain.CurrentDomain.AssemblyResolve += ResolveCurrentDirectory;

				// Reflect into a separate class so the resolver is installed before BepInEx dependencies are loaded.
				typeof(Entrypoint).Assembly.GetType($"BepInEx.Preloader.{nameof(PreloaderRunner)}")
								  ?.GetMethod(nameof(PreloaderRunner.PreloaderPreMain))
								  ?.Invoke(null, null);
			}
			catch (Exception ex)
			{
				File.WriteAllText(silentExceptionLog, ex.ToString());
			}
			finally
			{
				AppDomain.CurrentDomain.AssemblyResolve -= ResolveCurrentDirectory;
			}
		}

		internal static Assembly ResolveCurrentDirectory(object sender, ResolveEventArgs args)
		{
			// Avoid BepInEx utilities here because their assembly may still need resolving.
			var name = new AssemblyName(args.Name);

			try
			{
				return Assembly.LoadFile(Path.Combine(preloaderPath, $"{name.Name}.dll"));
			}
			catch (Exception)
			{
				return null;
			}
		}
	}
}
