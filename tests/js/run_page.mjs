// Usage: node run_page.mjs <index.html> < scenario.json
// Runs the page's inline script against the fake DOM, submits one message, and prints the DOM
// while the request is pending and after it settles.
import fs from "node:fs";
import vm from "node:vm";
import { createPage, serialize } from "./fake_dom.mjs";

const html = fs.readFileSync(process.argv[2], "utf8");
const scripts = [...html.matchAll(/<script>\n([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (scripts.length !== 1) throw new Error(`expected one inline script, found ${scripts.length}`);
const scenario = JSON.parse(fs.readFileSync(0, "utf8"));

const { document, elements, created } = createPage();
const fetchCalls = [];
let settle;
const fetchPromise = new Promise((resolve, reject) => { settle = { resolve, reject }; });
const intervals = { set: 0, cleared: 0 };
const window = {
  setInterval: () => { intervals.set += 1; return intervals.set; },
  clearInterval: () => { intervals.cleared += 1; },
};
const context = vm.createContext({
  document, window, console, Date, Promise, JSON, Math, Object, Array, String, Number,
  fetch: (url, options) => { fetchCalls.push({ url, options }); return fetchPromise; },
});
vm.runInContext(scripts[0], context);

elements.input.value = scenario.message ?? "hello";
const submit = elements.form.listeners.submit;
const finished = submit({ preventDefault() {} });
await Promise.resolve();
const pending = {
  tree: serialize(elements.conversation),
  sendDisabled: elements.send.disabled,
};

if (scenario.reject) {
  settle.reject(new Error(scenario.reject));
} else {
  const { status = 200, body } = scenario.response;
  settle.resolve({ ok: status >= 200 && status < 300, status, json: async () => body });
}
await finished;

console.log(JSON.stringify({
  pending,
  final: { tree: serialize(elements.conversation), sendDisabled: elements.send.disabled },
  fetchCalls: fetchCalls.map((c) => ({ url: c.url, method: c.options.method, body: JSON.parse(c.options.body) })),
  intervals,
  createdTags: created.map((el) => el.tagName),
}));
