// Reads [{tex, display}] as JSON on stdin; writes [null | "error"] as JSON.
const katex = require("katex");
let input = "";
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const out = JSON.parse(input).map(({ tex, display }) => {
    try {
      katex.renderToString(tex, { throwOnError: true, displayMode: !!display, strict: "ignore" });
      return null;
    } catch (e) {
      return String((e && e.message) || e);
    }
  });
  process.stdout.write(JSON.stringify(out));
});
