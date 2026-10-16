// Turns the recorded frames (docs/demo-raw) into docs/demo.gif. Needs ffmpeg on PATH.
import { execFileSync } from "node:child_process";
import { rmSync } from "node:fs";

const raw = new URL("../../../docs/demo-raw/", import.meta.url).pathname;
const filter = "fps=12,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle";
execFileSync("ffmpeg", ["-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", "frames.txt", "-vf", filter, "-loop", "0", "../demo.gif"], { cwd: raw, stdio: "inherit" });
rmSync(raw, { recursive: true, force: true });
console.log("wrote docs/demo.gif");
