using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection.Emit;
using HarmonyLib;
using MonoMod.RuntimeDetour;
using MonoMod.RuntimeDetour.Platforms;
using MonoMod.Utils;

namespace BepInEx.Preloader.RuntimeFixes
{
	internal static class XTermFix
	{
		public static void Apply()
		{
			if (PlatformHelper.Is(Platform.Windows))
				return;

			if (typeof(Console).Assembly.GetType("System.ConsoleDriver") == null)
			{
				// Without ConsoleDriver, terminal handling uses the separate TTY implementation.
				return;
			}

			if (AccessTools.Method("System.TermInfoReader:DetermineVersion") != null)
			{
				// DetermineVersion indicates that the runtime already handles both terminfo formats.
				return;
			}

			// Avoid process-based platform detection before TermInfoReader is patched.
			// Preserve ARM platforms because x86 branch encodings cannot patch ARM methods.
			bool forcedX86 = !AppleSiliconDetourFix.Applies && !PlatformHelper.Is(Platform.ARM);
			if (forcedX86)
				DetourHelper.Native = new DetourNativeX86Platform();

			var harmony = new HarmonyLib.Harmony("com.bepinex.xtermfix");

			harmony.Patch(AccessTools.Method("System.TermInfoReader:ReadHeader"),
				prefix: new HarmonyMethod(typeof(XTermFix), nameof(ReadHeaderPrefix)));

			harmony.Patch(AccessTools.Method("System.TermInfoReader:Get", new []{ AccessTools.TypeByName("System.TermInfoNumbers") }),
				transpiler: new HarmonyMethod(typeof(XTermFix), nameof(GetTermInfoNumbersTranspiler)));

			harmony.Patch(AccessTools.Method("System.TermInfoReader:Get", new []{ AccessTools.TypeByName("System.TermInfoStrings") }),
				transpiler: new HarmonyMethod(typeof(XTermFix), nameof(GetTermInfoStringsTranspiler)));

			harmony.Patch(AccessTools.Method("System.TermInfoReader:GetStringBytes", new []{ AccessTools.TypeByName("System.TermInfoStrings") }),
				transpiler: new HarmonyMethod(typeof(XTermFix), nameof(GetTermInfoStringsTranspiler)));

			// Reset only the temporary x86 override so preinstalled ARM memory protection stays active.
			if (forcedX86)
				DetourHelper.Native = null;
		}

		public static int intOffset;

		public static int GetInt32(byte[] buffer, int offset)
		{
			int b1 = buffer[offset];
			int b2 = buffer[offset + 1];
			int b3 = buffer[offset + 2];
			int b4 = buffer[offset + 3];

			return b1 | (b2 << 8) | (b3 << 16) | (b4 << 24);
		}

		public static short GetInt16(byte[] buffer, int offset)
		{
			int b1 = buffer[offset];
			int b2 = buffer[offset + 1];

			return (short)(b1 | (b2 << 8));
		}

		public static int GetInteger(byte[] buffer, int offset)
		{
			return intOffset == 2
				? GetInt16(buffer, offset)
				: GetInt32(buffer, offset);
		}

		public static void DetermineVersion(short magic)
		{
			if (magic == 0x11a)
				intOffset = 2;
			else if (magic == 0x21e)
				intOffset = 4;
			else
				throw new Exception($"Unknown xterm header format: {magic}");
		}

		public static bool ReadHeaderPrefix(byte[] buffer, ref int position, ref short ___boolSize, ref short ___numSize, ref short ___strOffsets)
		{
			short magic = GetInt16(buffer, position);
			position += 2;
			DetermineVersion(magic);

			// Skip the name-size field; this patch only supplies the counts used by TermInfoReader.
			position += 2;
			___boolSize = GetInt16(buffer, position);
			position += 2;
			___numSize = GetInt16(buffer, position);
			position += 2;
			___strOffsets = GetInt16(buffer, position);
			position += 2;
			// Skip the string-table size to leave the cursor at the end of the header.
			position += 2;

			return false;
		}

		public static IEnumerable<CodeInstruction> GetTermInfoNumbersTranspiler(IEnumerable<CodeInstruction> instructions)
		{
			// Use the header-selected integer width for offsets and numeric reads.

			var list = instructions.ToList();

			list[31] = new CodeInstruction(OpCodes.Ldsfld, AccessTools.Field(typeof(XTermFix), nameof(intOffset)));
			list[36] = new CodeInstruction(OpCodes.Nop);
			list[39] = new CodeInstruction(OpCodes.Call, AccessTools.Method(typeof(XTermFix), nameof(GetInteger)));

			return list;
		}

		public static IEnumerable<CodeInstruction> GetTermInfoStringsTranspiler(IEnumerable<CodeInstruction> instructions)
		{
			// String offsets must account for the numeric table's selected integer width.

			var list = instructions.ToList();

			list[32] = new CodeInstruction(OpCodes.Ldsfld, AccessTools.Field(typeof(XTermFix), nameof(intOffset)));

			return list;
		}
	}
}
