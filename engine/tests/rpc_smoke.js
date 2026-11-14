// 验证"Node 通过 UTF-8 管道调 Python 引擎"这条路径（P8 的真实接法）。
// 跑法：node engine/tests/rpc_smoke.js
const { spawn } = require("node:child_process");
const path = require("node:path");

const engineDir = path.join(__dirname, "..");
const child = spawn("python", ["-m", "attune_memory.rpc", ":memory:"], {
  cwd: engineDir,
  stdio: ["pipe", "pipe", "pipe"],
});

const requests = [
  { id: 1, op: "add", args: {
    content: "用户主要备考数学一和英语一",
    type: "profile", subtype: "identity", domain_key: "study", scope: "domain",
    importance: "important", confidence: 0.9, injection_policy: "contextual",
  } },
  { id: 2, op: "add", args: {
    content: "用户的传热学基础较弱，讲题要先补物理图像",
    type: "domain", subtype: "fact", domain_key: "study.heat", scope: "domain",
    importance: "important", confidence: 0.85,
  } },
  { id: 3, op: "search", args: { query: "数学一的极限题怎么做", domain: "study.math" } },
  { id: 4, op: "read_for_context", args: { query: "传热学稳态导热怎么算", domain: "study.heat" } },
  { id: 5, op: "add", args: { content: "今天天气不错", type: "episodic", subtype: "event" } },
  { id: 6, op: "stats", args: {} },
];

const expected = requests.length;
let buffer = "";
let seen = 0;

child.stdout.setEncoding("utf8");
child.stdout.on("data", (chunk) => {
  buffer += chunk;
  let idx;
  while ((idx = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, idx);
    buffer = buffer.slice(idx + 1);
    if (!line.trim()) continue;
    const resp = JSON.parse(line);
    seen += 1;
    const body = resp.ok
      ? JSON.stringify(resp.result).slice(0, 220)
      : `ERROR ${resp.error}`;
    console.log(`#${resp.id} ${body}`);
    if (seen === expected) {
      child.stdin.end();
      child.on("exit", (code) => {
        console.log(`--- done, exit=${code}`);
        const fails = [];
        if (code !== 0) fails.push("exit != 0");
        if (process.env.VERBOSE) console.log("tail:", buffer);
        process.exit(fails.length ? 1 : 0);
      });
    }
  }
});

child.stderr.setEncoding("utf8");
child.stderr.on("data", (d) => console.error("[py-stderr]", d.trim()));

for (const req of requests) child.stdin.write(JSON.stringify(req) + "\n");
