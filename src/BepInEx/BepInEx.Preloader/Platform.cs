using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using MonoMod.Utils;

namespace BepInEx.Preloader
{
	internal static class PlatformUtils
	{
		[StructLayout(LayoutKind.Sequential, Pack = 1)]
		public struct utsname_osx
		{
			private const int osx_utslen = 256;

			[MarshalAs(UnmanagedType.ByValTStr, SizeConst = osx_utslen)]
			public string sysname;

			[MarshalAs(UnmanagedType.ByValTStr, SizeConst = osx_utslen)]
			public string nodename;

			[MarshalAs(UnmanagedType.ByValTStr, SizeConst = osx_utslen)]
			public string release;

			[MarshalAs(UnmanagedType.ByValTStr, SizeConst = osx_utslen)]
			public string version;

			[MarshalAs(UnmanagedType.ByValTStr, SizeConst = osx_utslen)]
			public string machine;
		}

		[StructLayout(LayoutKind.Sequential, Pack = 1)]
		public struct utsname_linux
		{
			private const int linux_utslen = 65;

			[MarshalAs(UnmanagedType.ByValTStr, SizeConst = linux_utslen)]
			public string sysname;

			[MarshalAs(UnmanagedType.ByValTStr, SizeConst = linux_utslen)]
			public string nodename;

			[MarshalAs(UnmanagedType.ByValTStr, SizeConst = linux_utslen)]
			public string release;

			[MarshalAs(UnmanagedType.ByValTStr, SizeConst = linux_utslen)]
			public string version;

			[MarshalAs(UnmanagedType.ByValTStr, SizeConst = linux_utslen)]
			public string machine;

			[MarshalAs(UnmanagedType.ByValTStr, SizeConst = linux_utslen)]
			public string domainname;
		}

		[DllImport("libc.so.6", EntryPoint = "uname", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
		private static extern IntPtr uname_linux_glibc(ref utsname_linux utsname);

		// Bionic exports libc.so rather than the glibc soname.
		[DllImport("libc.so", EntryPoint = "uname", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
		private static extern IntPtr uname_linux_bionic(ref utsname_linux utsname);

		[DllImport("/usr/lib/libSystem.dylib", EntryPoint = "uname", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Ansi)]
		private static extern IntPtr uname_osx(ref utsname_osx utsname);

		/// <summary>Detect the platform without spawning processes that can trigger console initialization.</summary>
		public static void SetPlatform()
		{
			var current = Platform.Unknown;

			// Prefer Mono's native platform property when available.
			PropertyInfo p_Platform = typeof(Environment).GetProperty("Platform", BindingFlags.NonPublic | BindingFlags.Static);
			string platID;
			if (p_Platform != null)
			{
				platID = p_Platform.GetValue(null, new object[0]).ToString();
			}
			else
			{
				platID = Environment.OSVersion.Platform.ToString();
			}
			platID = platID.ToLowerInvariant();

			if (platID.Contains("win"))
			{
				current = Platform.Windows;
			}
			else if (platID.Contains("mac") || platID.Contains("osx"))
			{
				current = Platform.MacOS;
			}
			else if (platID.Contains("lin") || platID.Contains("unix"))
			{
				current = Platform.Linux;
			}

			if (Is(current, Platform.Linux) && Directory.Exists("/data") && File.Exists("/system/build.prop"))
			{
				current = Platform.Android;
			}
			else if (Is(current, Platform.Unix) && Directory.Exists("/System/Library/AccessibilityBundles"))
			{
				current = Platform.iOS;
			}

			// Detour width must match the Android process, which may run on a wider kernel.
			if (Is(current, Platform.Android))
			{
				current |= (IntPtr.Size >= 8 ? Platform.Bits64 : 0);
			}
			else if (Is(current, Platform.MacOS) || Is(current, Platform.Linux))
			{
				// Reflect over the optional property so runtimes without it can use pointer size.
				MethodInfo m_get_Is64BitOperatingSystem = typeof(Environment).GetProperty("Is64BitOperatingSystem")?.GetGetMethod();
				if (m_get_Is64BitOperatingSystem != null)
					current |= (((bool)m_get_Is64BitOperatingSystem.Invoke(null, new object[0])) ? Platform.Bits64 : 0);
				else
					current |= (IntPtr.Size >= 8 ? Platform.Bits64 : 0);
			}
			else
			{
				current |= (IntPtr.Size >= 8 ? Platform.Bits64 : 0);
			}

			// Android includes the Linux flag and must use uname with the bionic library.
			if ((Is(current, Platform.MacOS) || Is(current, Platform.Linux)) && Type.GetType("Mono.Runtime") != null)
			{
				string arch;
				IntPtr result;

				if (Is(current, Platform.MacOS))
				{
					utsname_osx utsname_osx = new utsname_osx();
					result = uname_osx(ref utsname_osx);
					arch = utsname_osx.machine;
				}
				else if (Is(current, Platform.Android))
				{
					utsname_linux utsname_linux = new utsname_linux();
					result = uname_linux_bionic(ref utsname_linux);
					arch = utsname_linux.machine;
				}
				else
				{
					utsname_linux utsname_linux = new utsname_linux();
					result = uname_linux_glibc(ref utsname_linux);
					arch = utsname_linux.machine;
				}

				// Accept both arm* and aarch* machine names returned by uname.
				if (result == IntPtr.Zero && (arch.StartsWith("aarch") || arch.StartsWith("arm")))
					current |= Platform.ARM;
			}
			else
			{
				// Use the module architecture when the Mono Unix detection path does not apply.
				typeof(object).Module.GetPEKind(out PortableExecutableKinds peKind, out ImageFileMachine machine);
				if (machine == (ImageFileMachine)0x01C4 /* ARMNT value avoids requiring a named enum member. */)
					current |= Platform.ARM;
			}

			PlatformHelper.Current = current;
		}

		private static bool Is(Platform current, Platform expected)
		{
			return (current & expected) == expected;
		}
	}
}
