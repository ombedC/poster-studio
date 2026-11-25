/**
 * 开发期 UI 冒烟测试：用 Edge 的 CDP 打开工作台，上传照片、切版式、点击生成并截图。
 *
 *   node scripts/dev/ui_smoke.mjs <图片绝对路径> <截图输出> [mode] [borderWidth]
 *
 * 需要先以调试端口启动 Edge：
 *   msedge --headless=new --remote-debugging-port=9222 --user-data-dir=<临时目录> http://127.0.0.1:8000/
 */
import fs from "node:fs";

const [, , imagePath, shotPath, mode = "head_shoulder", borderWidth = "0"] = process.argv;
const PAGE_URL = process.env.PAGE_URL || "http://127.0.0.1:8000/";

class CDP {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.id = 0;
    this.pending = new Map();
    this.handlers = new Map();
    this.ready = new Promise((resolve, reject) => {
      this.ws.onopen = resolve;
      this.ws.onerror = reject;
    });
    this.ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
      } else if (message.method && this.handlers.has(message.method)) {
        for (const handler of this.handlers.get(message.method)) handler(message.params);
      }
    };
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  on(method, handler) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(handler);
  }
  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description || "evaluate failed");
    }
    return result.result.value;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const targets = await (await fetch("http://127.0.0.1:9222/json/list")).json();
  const page = targets.find((target) => target.type === "page");
  if (!page) throw new Error("没有找到 Edge 页面");

  const cdp = new CDP(page.webSocketDebuggerUrl);
  await cdp.ready;
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("DOM.enable");

  const errors = [];
  cdp.on("Runtime.exceptionThrown", (params) => errors.push(params.exceptionDetails?.text || "exception"));
  cdp.on("Runtime.consoleAPICalled", (params) => {
    if (params.type === "error") errors.push(params.args.map((a) => a.value).join(" "));
  });

  const loaded = new Promise((resolve) => cdp.on("Page.loadEventFired", resolve));
  await cdp.send("Page.navigate", { url: PAGE_URL });
  await loaded;
  await sleep(2500);

  if (!(await cdp.evaluate("Boolean(window.Alpine && Alpine.$data(document.body))"))) {
    throw new Error("Alpine 未初始化");
  }

  const document = await cdp.send("DOM.getDocument", { depth: -1 });
  const input = await cdp.send("DOM.querySelector", {
    nodeId: document.root.nodeId,
    selector: 'input[type="file"]',
  });
  await cdp.send("DOM.setFileInputFiles", { files: [imagePath], nodeId: input.nodeId });

  await cdp.evaluate(
    `(() => { const data = Alpine.$data(document.body); data.setMode(${JSON.stringify(mode)}); data.borderWidth = ${Number(borderWidth)}; if (${JSON.stringify(mode)} !== "full_body") { data.removeBg = false; } })()`,
  );
  await sleep(500);

  await cdp.evaluate(
    "[...document.querySelectorAll('button')].find((b) => b.textContent.includes('生成海报素材')).click()",
  );

  let state = null;
  for (let attempt = 0; attempt < 90; attempt += 1) {
    await sleep(500);
    state = JSON.parse(
      await cdp.evaluate(
        "JSON.stringify({mode: Alpine.$data(document.body).mode, out: Alpine.$data(document.body).outputUrl, err: Alpine.$data(document.body).error, loading: Alpine.$data(document.body).loading})",
      ),
    );
    if (state.out || (state.err && !state.loading)) break;
  }

  await sleep(800);
  const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(shotPath, Buffer.from(shot.data, "base64"));

  console.log(JSON.stringify({ state, errors }, null, 2));
  cdp.ws.close();
  process.exit(state?.err ? 2 : 0);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
