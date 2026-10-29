// Compile the actual production source with dependency stubs, without core/NuGet.
using System;
using System.Reflection;
using System.Runtime.InteropServices;
using BepInEx.Preloader.RuntimeFixes;

internal static class AndroidPagesTest
{
    private static void Require(bool condition, string message)
    {
        if (!condition) throw new Exception(message);
    }

    public static void Main()
    {
        const BindingFlags flags = BindingFlags.NonPublic | BindingFlags.Static;
        Type fix = typeof(AndroidDetourFix);
        MethodInfo validate = fix.GetMethod("ValidatePageSize", flags);
        MethodInfo range = fix.GetMethod("GetPageRange", flags);
        foreach (long page in new long[] { 1, 4096, 16384, 65536, 1L << 32 })
            Require((long)validate.Invoke(null, new object[] { page }) == page, "valid page rejected");
        foreach (long page in new long[] { long.MinValue, -4096, -1, 0, 3, 4095, 4097, long.MaxValue })
        {
            try
            {
                validate.Invoke(null, new object[] { page });
                throw new Exception("invalid page accepted: " + page);
            }
            catch (TargetInvocationException error)
            {
                Require(error.InnerException is NotSupportedException, "wrong exception");
                Require(error.InnerException.Message.Contains("positive power of two"), "missing diagnosis");
            }
        }

        foreach (long page in new long[] { 4096, 16384, 65536 })
        {
            long[,] cases = {
                { 0, 1 }, { page, page }, { page + 1, 1 },
                { page - 1, 1 }, { page - 1, 2 }, { page + 1, page - 1 },
                { page + 1, page }, { page - 1, page * 3 + 2 },
                { page, 0 }, { page + 1, 0 },
                { 0x7fffffffL, 2 }, { 0x80000000L, 1 },
                { 0xfffffffeL, 1 }, { 0xffffffffL, 1 },
                // IntPtr.ToInt64 sign-extends high arm32 addresses.
                { -2147483648L, 1 }, { -2, 1 }, { -1, 1 }
            };
            for (int i = 0; i < cases.GetLength(0); i++)
            {
                long start = cases[i, 0], length = cases[i, 1];
                object[] args = { start, (ulong)length, page, 0L, 0L };
                range.Invoke(null, args);
                long expectedStart = (long)Math.Floor((decimal)start / page) * page;
                long expectedEnd = (long)Math.Ceiling((decimal)(start + length) / page) * page;
                Require((long)args[3] == expectedStart && (long)args[4] == expectedEnd,
                    "page bounds differ: " + start + "/" + length + "/" + page);
            }
        }

        // Metadata inspection does not instantiate the native platform or call libc.
        Type platform = fix.GetNestedType("AndroidNativePlatform", BindingFlags.NonPublic);
        Require((int)platform.GetField("_SC_PAGESIZE", flags).GetRawConstantValue() == 0x27,
            "wrong bionic query");
        MethodInfo sysconf = platform.GetMethod("sysconf", flags);
        Require(sysconf.ReturnType == typeof(int), "arm32 C long must return int");
        Require(sysconf.GetParameters().Length == 1 &&
            sysconf.GetParameters()[0].ParameterType == typeof(int), "wrong sysconf argument");
        DllImportAttribute import = (DllImportAttribute)Attribute.GetCustomAttribute(
            sysconf, typeof(DllImportAttribute));
        Require(import.Value == "libc" && import.CallingConvention == CallingConvention.Cdecl,
            "wrong native library or calling convention");
        Console.WriteLine("Android page checks passed: 5 valid, 8 invalid, 51 ranges, P/Invoke metadata");
    }
}

namespace MonoMod.Utils
{
    [Flags] public enum Platform { Android = 1, ARM = 2 }
    public static class PlatformHelper { public static bool Is(Platform value) { return false; } }
}

namespace MonoMod.RuntimeDetour
{
    public struct NativeDetourData { }
    public static class DetourHelper { public static Platforms.IDetourNativePlatform Native; }
}

namespace MonoMod.RuntimeDetour.Platforms
{
    public interface IDetourNativePlatform
    {
        void MakeWritable(IntPtr src, uint size);
        void MakeExecutable(IntPtr src, uint size);
        void MakeReadWriteExecutable(IntPtr src, uint size);
        void FlushICache(IntPtr src, uint size);
        NativeDetourData Create(IntPtr from, IntPtr to, byte? type);
        void Free(NativeDetourData detour);
        void Apply(NativeDetourData detour);
        void Copy(IntPtr src, IntPtr dst, byte type);
        IntPtr MemAlloc(uint size);
        void MemFree(IntPtr ptr);
    }

    public class DetourNativeARMPlatform : IDetourNativePlatform
    {
        public void MakeWritable(IntPtr src, uint size) { throw new NotSupportedException(); }
        public void MakeExecutable(IntPtr src, uint size) { throw new NotSupportedException(); }
        public void MakeReadWriteExecutable(IntPtr src, uint size) { throw new NotSupportedException(); }
        public void FlushICache(IntPtr src, uint size) { throw new NotSupportedException(); }
        public NativeDetourData Create(IntPtr from, IntPtr to, byte? type) { throw new NotSupportedException(); }
        public void Free(NativeDetourData detour) { throw new NotSupportedException(); }
        public void Apply(NativeDetourData detour) { throw new NotSupportedException(); }
        public void Copy(IntPtr src, IntPtr dst, byte type) { throw new NotSupportedException(); }
        public IntPtr MemAlloc(uint size) { throw new NotSupportedException(); }
        public void MemFree(IntPtr ptr) { throw new NotSupportedException(); }
    }
}
