import { copyFile, mkdir, readFile, lstat, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const source = resolve(root, "node_modules/@lobehub/icons-static-svg/icons");
const target = resolve(root, "public/provider-icons");
const files = {
  openai: "openai.svg", anthropic: "anthropic.svg", claude: "claude-color.svg",
  google: "google-color.svg", gemini: "gemini-color.svg", deepseek: "deepseek-color.svg",
  qwen: "qwen-color.svg", moonshot: "moonshot.svg", kimi: "kimi-color.svg",
  baai: "baai.svg",
  zhipu: "zhipu-color.svg", glm: "zhipu-color.svg", doubao: "doubao-color.svg",
  minimax: "minimax-color.svg", mistral: "mistral-color.svg", xai: "grok.svg", grok: "grok.svg",
  openrouter: "openrouter-color.svg", siliconflow: "siliconcloud-color.svg",
  ollama: "ollama.svg", lmstudio: "lmstudio.svg",
};
await mkdir(target, { recursive: true });
if ((await lstat(target)).isSymbolicLink()) throw new Error("Icon destination must not be a symlink");
for (const [brand, filename] of Object.entries(files)) {
  const original = await readFile(resolve(source, filename));
  if (/<script\b|on(?:load|error)\s*=|<foreignObject\b/i.test(original.toString())) throw new Error("Unsafe icon asset");
  const destination = resolve(target, `${brand}.svg`);
  try {
    if (!(await readFile(destination)).equals(original)) throw new Error(`Existing icon changed: ${brand}`);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    await copyFile(resolve(source, filename), destination);
  }
}
await writeFile(resolve(target, "manifest.json"), JSON.stringify({ package: "@lobehub/icons-static-svg", version: "1.95.0", license: "MIT", source: "https://github.com/lobehub/lobe-icons", icons: files }, null, 2) + "\n");
console.log(`Copied ${Object.keys(files).length} authentic provider icon assets.`);
