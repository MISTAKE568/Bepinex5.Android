using System;
using System.IO;
using System.Linq;
using System.Reflection;

namespace Be5Bootstrap
{
    /// <summary>Use reflection so loading the bootstrap does not resolve Unity or BepInEx assemblies prematurely.</summary>
    public static class Boot
    {
        /// <summary>Call on the Unity main thread after engine initialization because Chain creates a GameObject.</summary>
        public static int Run()
        {
            int preloadCode = Preload();
            if (preloadCode != 0)
                return preloadCode;
            return Chain();
        }

        /// <summary>Run before game assemblies load so preloader patchers can rewrite them.</summary>
        public static int Preload()
        {
            string core = GetCoreDir();

            try
            {
                string preloaderPath = Path.Combine(core, "BepInEx.Preloader.dll");
                Assembly pre = Assembly.LoadFrom(preloaderPath);
                Type runner = pre.GetType("BepInEx.Preloader.PreloaderRunner", true);
                MethodInfo preMain = runner.GetMethod("PreloaderPreMain",
                    BindingFlags.Public | BindingFlags.Static);
                if (preMain == null)
                {
                    return 2;
                }
                // A normal return can still include failures caught inside the preloader.
                preMain.Invoke(null, null);
                return 0;
            }
            catch (Exception)
            {
                return 1;
            }
        }

        /// <summary>Wait for engine initialization because Chainloader.Initialize creates a GameObject.</summary>
        public static int Chain()
        {
            string core = GetCoreDir();

            try
            {
                string bepPath = Path.Combine(core, "BepInEx.dll");
                Assembly bep = Assembly.LoadFrom(bepPath);
                Type chainloader = bep.GetType("BepInEx.Bootstrap.Chainloader", true);

                MethodInfo init = chainloader
                    .GetMethods(BindingFlags.Public | BindingFlags.Static)
                    .FirstOrDefault(m => m.Name == "Initialize" && m.GetParameters().Length == 3);
                MethodInfo start = chainloader
                    .GetMethods(BindingFlags.Public | BindingFlags.Static)
                    .FirstOrDefault(m => m.Name == "Start" && m.GetParameters().Length == 0);
                if (init == null || start == null)
                {
                    return 2;
                }

                // Pass null to preserve the Managed path discovered by the preloader.
                init.Invoke(null, new object[] { null, false, null });
                start.Invoke(null, null);
                return 0;
            }
            catch (Exception)
            {
                return 1;
            }
        }

        private static string GetCoreDir()
        {
            string core = Environment.GetEnvironmentVariable("BEPINEX_CORE_DIR");
            if (string.IsNullOrEmpty(core))
            {
                try { core = Path.GetDirectoryName(typeof(Boot).Assembly.Location); } catch { }
            }
            return core;
        }
    }
}
