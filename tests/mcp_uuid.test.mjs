import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import vm from "node:vm";
import test from "node:test";

const require = createRequire(import.meta.url);
const { UUID, Token } = require("../extension/node_modules/@lumino/coreutils");
const assets = process.env.SOLO_MCP_TEST_ASSETS;
assert.ok(assets, "Set SOLO_MCP_TEST_ASSETS to the validated toolkit asset folder");
const packageJson = JSON.parse(readFileSync(join(assets, "package.json"), "utf8"));
const remote = readFileSync(join(assets, packageJson.jupyterlab._build.load), "utf8");
assert.match(remote, /509:"solo-uuid"/, "Patched remote entry must reference chunk 509");
const source = readFileSync(join(assets, "static", "509.solo-uuid.js"), "utf8");
const schema = "https://events.jupyter.org/jupyterlab_command_toolkit/lab_command/v1";

function load(crypto) {
  const self = {};
  const context = { self, console, ...(crypto === undefined ? {} : { crypto }) };
  vm.runInNewContext(source, context);
  const factories = self.webpackChunkjupyterlab_commands_toolkit[0][1];
  const exports = {};
  function webpack(id) {
    if (id === 262) return { UUID, Token };
    if (id === 902) return { IChatTracker: {} };
    throw new Error(`Unexpected dependency ${id}`);
  }
  webpack.r = () => {};
  webpack.d = (object, properties) => {
    for (const [name, getter] of Object.entries(properties)) Object.defineProperty(object, name, { get: getter });
  };
  factories[509]({}, exports, webpack);
  return exports;
}

for (const [name, crypto] of [["missing crypto", undefined], ["crypto without randomUUID", {}], ["forbidden native randomUUID", { randomUUID() { throw new Error("Native API must not be used"); } }]]) {
  test(`toolkit initializes with ${name}`, () => {
    const loaded = load(crypto);
    assert.match(loaded.WEB_CLIENT_ID, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    assert.equal(loaded.default.length, 3);
    assert.equal(loaded.default[1].activate(), loaded.WEB_CLIENT_ID);
  });
}

test("MCP frontend listener registers and returns correlated command results", async () => {
  const loaded = load({});
  const registered = new Map();
  let listener;
  const results = [];
  const commands = {
    addCommand: (id, options) => registered.set(id, options),
    listCommands: () => [...registered.keys()],
    describedBy: async (id) => registered.get(id).describedBy,
    label: (id) => registered.get(id).label,
    caption: () => "",
    usage: () => "",
    execute: async (id, args) => registered.get(id).execute(args)
  };
  loaded.default[0].activate({ commands, serviceManager: { events: {
    stream: { connect(callback) { listener = callback; } },
    emit(result) { results.push(result); }
  } } });
  assert.equal(typeof listener, "function");
  listener(null, { schema_id: schema, requestId: "request-1", client_id: loaded.WEB_CLIENT_ID,
    name: "jupyterlab-commands-toolkit:list-all-commands", args: { query: "web-client" } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(results.length, 1);
  assert.equal(results[0].data.requestId, "request-1");
  assert.equal(results[0].data.success, true);
  assert.equal(results[0].data.result.commandCount, 1);
  assert.equal(results[0].data.result.commands[0].id, "jupyterlab-commands-toolkit:get-web-client-id");
  listener(null, { schema_id: schema, requestId: "other", client_id: "another-tab",
    name: "jupyterlab-commands-toolkit:list-all-commands", args: {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(results.length, 1);
});
