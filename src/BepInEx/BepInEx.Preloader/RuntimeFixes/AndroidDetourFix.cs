using System;
using System.Runtime.InteropServices;
using MonoMod.RuntimeDetour;
using MonoMod.RuntimeDetour.Platforms;
using MonoMod.Utils;

namespace BepInEx.Preloader.RuntimeFixes
{
	/// <summary>Supply bionic memory protection for ARM detours without requiring Unity Mono to expose SystemPageSize.</summary>
	internal static class AndroidDetourFix
	{
		/// <summary>Defer reporting until the preloader logger is available.</summary>
		public static Exception Exception { get; private set; }

		public static bool Applies =>
			IntPtr.Size == 4 && PlatformHelper.Is(Platform.Android) && PlatformHelper.Is(Platform.ARM);

		public static void Apply()
		{
			if (!Applies)
				return;

			try
			{
				DetourHelper.Native = new AndroidNativePlatform(new DetourNativeARMPlatform());
			}
			catch (Exception e)
			{
				Exception = e;
			}
		}

		private sealed class AndroidNativePlatform : IDetourNativePlatform
		{
			private const int PROT_READ = 0x1;
			private const int PROT_WRITE = 0x2;
			private const int PROT_EXEC = 0x4;

			// Use the bionic _SC_PAGESIZE value, not the glibc value.
			private const int _SC_PAGESIZE = 0x27;

			private static readonly long PageSize = GetPageSize();

			private readonly IDetourNativePlatform inner;

			public AndroidNativePlatform(IDetourNativePlatform inner)
			{
				this.inner = inner;
			}

			public void MakeWritable(IntPtr src, uint size)
			{
				// Keep code executable while writing detours to pages that may contain other methods.
				SetMemPerms(src, size, PROT_READ | PROT_WRITE | PROT_EXEC);
			}

			public void MakeExecutable(IntPtr src, uint size)
			{
				SetMemPerms(src, size, PROT_READ | PROT_WRITE | PROT_EXEC);
			}

			public void MakeReadWriteExecutable(IntPtr src, uint size)
			{
				SetMemPerms(src, size, PROT_READ | PROT_WRITE | PROT_EXEC);
			}

			// The ARM cache-flush routine uses this wrapper to make its generated code executable.
			public void FlushICache(IntPtr src, uint size)
			{
				inner.FlushICache(src, size);
			}

			public NativeDetourData Create(IntPtr from, IntPtr to, byte? type)
			{
				return inner.Create(from, to, type);
			}

			public void Free(NativeDetourData detour)
			{
				inner.Free(detour);
			}

			public void Apply(NativeDetourData detour)
			{
				inner.Apply(detour);
			}

			public void Copy(IntPtr src, IntPtr dst, byte type)
			{
				inner.Copy(src, dst, type);
			}

			public IntPtr MemAlloc(uint size)
			{
				return inner.MemAlloc(size);
			}

			public void MemFree(IntPtr ptr)
			{
				inner.MemFree(ptr);
			}

			private static void SetMemPerms(IntPtr start, ulong len, int prot)
			{
				// mprotect must cover complete pages, including a partially covered final page.
				long startPage, endPage;
				GetPageRange((long)start, len, PageSize, out startPage, out endPage);

				if (mprotect((IntPtr)startPage, (IntPtr)(endPage - startPage), prot) != 0)
					throw new System.ComponentModel.Win32Exception();
			}

			private static long GetPageSize()
			{
				// Reflection avoids a static dependency on the optional SystemPageSize property.
				var envProp = typeof(Environment).GetProperty("SystemPageSize");
				if (envProp != null)
				{
					var val = envProp.GetValue(null, new object[0]);
					if (val is int iv)
						return ValidatePageSize(iv);
					if (val is long lv)
						return ValidatePageSize(lv);
				}

				long r = sysconf(_SC_PAGESIZE);
				return ValidatePageSize(r);
			}

			[DllImport("libc", SetLastError = true, CallingConvention = CallingConvention.Cdecl)]
			private static extern int mprotect(IntPtr start, IntPtr len, int prot);

			[DllImport("libc", CallingConvention = CallingConvention.Cdecl)]
			// The arm32 ABI returns C long as 32 bits; widen only after the native call.
			private static extern int sysconf(int name);
		}

		private static long ValidatePageSize(long pageSize)
		{
			if (pageSize <= 0 || (pageSize & (pageSize - 1)) != 0)
				throw new NotSupportedException("Invalid system page size on Android: " + pageSize
					+ ". Expected a positive power of two.");
			return pageSize;
		}

		private static void GetPageRange(long start, ulong len, long pageSize, out long startPage, out long endPage)
		{
			startPage = start & ~(pageSize - 1);
			endPage = (start + (long)len + pageSize - 1) & ~(pageSize - 1);
		}
	}
}
