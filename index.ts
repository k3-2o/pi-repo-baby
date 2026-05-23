import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

interface RepoBabyState {
	enabled: boolean;
	depsChecked: boolean;
	depsOk: boolean;
}

const state: RepoBabyState = {
	enabled: true,
	depsChecked: false,
	depsOk: false,
};

function extensionDir(): string {
	try {
		return dirname(fileURLToPath(import.meta.url));
	} catch {
		return process.env.HOME
			? join(process.env.HOME, ".pi", "agent", "extensions")
			: ".";
	}
}

function pythonScriptPath(): string {
	return join(extensionDir(), "repo-baby.py");
}

function pythonBin(): string {
	return join(extensionDir(), "venv", "bin", "python3");
}

function pythonCommand(): string {
	return existsSync(pythonBin()) ? pythonBin() : "python3";
}

async function probeTreeSitter(pi: ExtensionAPI, python: string): Promise<boolean> {
	const probe = "import tree_sitter_language_pack; print('OK')";
	try {
		const { stdout, code } = await pi.exec(python, ["-c", probe], { timeout: 10_000 });
		return code === 0 && stdout.trim() === "OK";
	} catch {
		return false;
	}
}

async function ensureDeps(
	pi: ExtensionAPI,
	ctx?: { ui: { notify: (msg: string, type: string) => void } },
): Promise<{ ok: boolean; detail: string }> {
	const script = pythonScriptPath();
	if (!existsSync(script)) {
		return { ok: false, detail: "repo-baby.py not found — extension may be corrupted" };
	}

	if (existsSync(pythonBin()) && await probeTreeSitter(pi, pythonBin())) {
		return { ok: true, detail: "tree-sitter-language-pack ready (cached venv)" };
	}

	if (await probeTreeSitter(pi, "python3")) {
		return { ok: true, detail: "tree-sitter-language-pack available (system)" };
	}

	if (ctx) ctx.ui.notify("Repo Baby: installing dependencies (this may take a minute)…", "info");

	const extDir = extensionDir();
	try {
		const { code: installCode, stderr } = await pi.exec("npm", ["run", "install-deps"], {
			cwd: extDir,
			timeout: 120_000,
		});

		if (installCode === 0) {
			const python2 = pythonCommand();
			if (await probeTreeSitter(pi, python2)) {
				if (ctx) ctx.ui.notify("✅ Repo Baby: dependencies installed — Tree-sitter active", "success");
				return { ok: true, detail: "tree-sitter-language-pack ready" };
			}
		}

		const errorDetail = (installCode === 0)
			? "Install completed but import verification failed — dependencies may not be usable"
			: `Exit ${installCode}: ${stderr?.trim() || "no output"}`;
		if (ctx) ctx.ui.notify(`⚠ Repo Baby: ${errorDetail}`, "warning");
		return { ok: false, detail: errorDetail };
	} catch (err: any) {
		if (ctx) ctx.ui.notify(`⚠ Repo Baby: install failed — ${err.message}`, "warning");
		return { ok: false, detail: err.message };
	}
}

export default function repoBabyExtension(pi: ExtensionAPI) {
	pi.registerTool({
		name: "read_codebase",
		label: "Get Repo Map",
		description:
			"Structural codebase awareness with 11 modes. Returns ranked symbols, project " +
			"fingerprint, import hotspots, git-aware change detection, test/source pairing, " +
			"symbol detail, file grouping, and health diagnostics. Use per the guidelines " +
			"below — not every turn, but at every phase transition.",
		promptSnippet:
			"Structural codebase awareness — ranked symbols, project fingerprint, import " +
			"hotspots, change detection, and more. Use at phase transitions, not every turn.",
		promptGuidelines: [
			"WHEN TO CALL (exactly four triggers):",
			"",
			"1. ORIENTATION — you just entered a repo for the first time this session, " +
			"or the user asked an open-ended question and you cannot name the top 3 " +
			"relevant files. One call replaces 5-10 turns of ls/grep/read. " +
			"→ Call mode=\"overview\" first (frameworks, entrypoints, next reads), " +
			"then mode=\"map\" for structural detail.",
			"",
			"2. VERIFICATION — you just renamed a function, moved a class, changed an " +
			"export, or modified a shared interface across multiple files. " +
			"→ Call mode=\"changed\" to see git-diff files and symbol-level changes " +
			"since the last snapshot. Confirms nothing broke and no symbols orphaned.",
			"",
			"3. CONTEXT SWITCH — the user said \"look at the auth module instead\" or " +
			"switched branches/topics. Your mental map is stale. " +
			"→ Refresh with mode=\"overview\" or mode=\"map\" as appropriate.",
			"",
			"4. STRUCTURAL QUESTION — the user asked \"how is this organized?\", \"what " +
			"depends on what?\", \"where are the tests for X?\", or a symbol lookup " +
			"without a file location. " +
			"→ mode=\"deps\" for import hotspots and external packages " +
			"→ mode=\"groups\" for monorepo/package layout " +
			"→ mode=\"pairs\" for source↔test mapping " +
			"→ mode=\"search\" for broad symbol/file matching " +
			"→ mode=\"detail\" with query for signature + decorators + doc hints",
			"",
			"DO NOT CALL when:",
			"- The user named a specific file and line number",
			"- The task is a one-line change in a file you have already read",
			"- You called it within the last 3 turns and the repo has not changed",
			"- The task is purely mechanical: formatting, versions, comments, simple typos",
			"- You are in \"execute\" mode, not \"orient\" mode — you already know where to edit",
			"",
			"AFTER CALLING:",
			"- Always follow \"suggested next reads\" — they are ranked by importance",
			"- If mode=\"changed\" reported modified symbols, inspect those files first",
			"- If mode=\"deps\" reported high-import-count files, treat those as high-priority",
			"- If mode=\"pairs\" returned test files for a source you are editing, run them",
		],
		parameters: Type.Object({
			scope: Type.Optional(
				Type.String({ description: "Limit to a subdirectory (e.g. 'src/')" }),
			),
			mode: Type.Optional(
				Type.Union([
					Type.Literal("map"),
					Type.Literal("overview"),
					Type.Literal("files"),
					Type.Literal("stats"),
					Type.Literal("search"),
					Type.Literal("changed"),
					Type.Literal("deps"),
					Type.Literal("pairs"),
					Type.Literal("detail"),
					Type.Literal("groups"),
					Type.Literal("health"),
				], {
					description: "Mode: overview (first contact), map (structural detail), changed (after edits), deps (import structure), pairs (test mapping), search (find symbols/files), detail (inspect one symbol), groups (monorepo layout), files (inventory), stats (counts), health (tool reliability). Default: map.",
					default: "map",
				}),
			),
			format: Type.Optional(
				Type.Union([Type.Literal("text"), Type.Literal("json")], {
					description: "Output format (default text)",
					default: "text",
				}),
			),
			query: Type.Optional(
				Type.String({ description: "Search/detail query when mode='search' or mode='detail'" }),
			),
			max_files: Type.Optional(
				Type.Number({ description: "Maximum source files to scan (default 1000)", default: 1000 }),
			),
			token_budget: Type.Optional(
				Type.Number({ description: "Max tokens for the map (default 800)", default: 800 }),
			),
		}),

		async execute(_id, params, _signal, _onUpdate, ctx) {
			if (!state.enabled) {
				throw new Error("Repo Baby is disabled. Use /repo-baby on to enable.");
			}

			const script = pythonScriptPath();
			if (!existsSync(script)) {
				throw new Error(`repo-baby.py not found at ${script}`);
			}

			const mode = params.mode || "map";
			const needsTreeSitter = ["map", "search", "detail"].includes(mode);

			if (needsTreeSitter && (!state.depsChecked || !state.depsOk)) {
				const result = await ensureDeps(pi, ctx);
				state.depsChecked = true;
				state.depsOk = result.ok;
				if (!result.ok) {
					throw new Error(`Repo Baby dependencies unavailable: ${result.detail}`);
				}
			}

			const cwd = ctx.cwd;
			const budget = params.token_budget || 800;

			const args = [script, "--path", cwd, "--token-budget", String(budget)];
			if (params.scope) {
				args.push("--scope", params.scope);
			}
			if (mode) {
				args.push("--mode", mode);
			}
			if (params.format) {
				args.push("--format", params.format);
			}
			if (params.query) {
				args.push("--query", params.query);
			}
			if (params.max_files) {
				args.push("--max-files", String(params.max_files));
			}

			const python = pythonCommand();
			const { stdout, code, stderr } = await pi.exec(python, args, {
				cwd,
				timeout: 60_000,
			});

			if (code !== 0) {
				throw new Error(`repo-baby.py failed: ${stderr?.trim() || `exit ${code}`}`);
			}

			return {
				content: [{ type: "text", text: stdout.trim() }],
			};
		},
	});

	pi.registerCommand("repo-baby", {
		description: "Toggle repository map on/off",
		getArgumentCompletions(prefix: string) {
			const opts = ["on", "off", "status", "refresh", "doctor"];
			const filtered = opts.filter((o) => o.startsWith(prefix));
			return filtered.length > 0 ? filtered.map((o) => ({ value: o, label: o })) : null;
		},

		async handler(args, ctx) {
			const cmd = args.trim().toLowerCase();

			if (cmd === "on") {
				state.enabled = true;
				ctx.ui.notify("Repo Baby: ON — use \`read_codebase\` tool to see repository structure", "success");
				return;
			}

			if (cmd === "off") {
				state.enabled = false;
				ctx.ui.notify("Repo Baby: OFF — use \`/repo-baby on\` to re-enable", "info");
				return;
			}

			if (cmd === "refresh") {
				ctx.ui.notify("Repo Baby: use the \`read_codebase\` tool for a fresh snapshot", "info");
				return;
			}

			if (cmd === "doctor") {
				ctx.ui.notify("Repo Baby: checking dependencies…", "info");
				try {
					const result = await ensureDeps(pi, ctx);
					state.depsChecked = true;
					state.depsOk = result.ok;
					if (result.ok) {
						ctx.ui.notify(`✅ Repo Baby: ${result.detail}`, "success");
					} else {
						ctx.ui.notify(`⚠ Repo Baby: ${result.detail}`, "warning");
					}
				} catch (err: any) {
					ctx.ui.notify(`⚠ Repo Baby: probe failed — ${err?.message || "unknown error"}`, "warning");
					state.depsChecked = true;
					state.depsOk = false;
				}
				return;
			}

			if (cmd === "status") {
				const s = state.enabled ? "enabled" : "disabled";
				const deps = state.depsChecked
					? state.depsOk
						? "Tree-sitter OK"
						: "not installed"
					: "not checked";
				ctx.ui.notify(`Repo Baby: ${s}, deps: ${deps}`, "info");
				return;
			}

			ctx.ui.notify(
				`Usage: /repo-baby on|off|status|refresh|doctor (currently ${state.enabled ? "on" : "off"})`,
				"info",
			);
		},
	});

	pi.on("session_start", async (_event, ctx) => {
		if (!state.depsChecked) {
			state.depsChecked = true;
			const result = await ensureDeps(pi, ctx);
			state.depsOk = result.ok;
			if (!result.ok) {
				if (ctx) ctx.ui.notify(
					"Repo Baby symbols unavailable — mode=files/stats still work; use /repo-baby doctor to retry",
					"warning",
				);
			}
		}
	});
}
