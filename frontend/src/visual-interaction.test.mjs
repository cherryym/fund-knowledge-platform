// node --test src/visual-interaction.test.mjs
// Tests the real shared store in isolation; no browser, React or GSAP stand-ins.
import test from "node:test";
import assert from "node:assert/strict";
import Module from "node:module";
import { resolve } from "node:path";
import { build } from "esbuild";

const compiled = await build({
  entryPoints: [resolve("src/visualInteraction.ts")],
  bundle: true,
  platform: "node",
  format: "cjs",
  write: false,
  logLevel: "silent",
});
function freshStore() {
  const loaded = new Module(resolve("src/visual-interaction-test-runtime.cjs"));
  loaded._compile(compiled.outputFiles[0].text, loaded.id);
  return loaded.exports;
}

test("empty store is inactive; subscriptions are change-only with an explicit initial read", () => {
  const store = freshStore();
  const states = [];
  assert.equal(store.isGraphInteracting(), false);
  const unsubscribe = store.subscribeGraphInteraction((active) => states.push(active));
  assert.deepEqual(states, []);
  const owner = {};
  store.setGraphInteraction(owner, true);
  assert.equal(store.isGraphInteracting(), true);
  const lateStates = [];
  const unsubscribeLate = store.subscribeGraphInteraction((active) => lateStates.push(active));
  assert.deepEqual(lateStates, []);
  assert.equal(store.isGraphInteracting(), true);
  store.setGraphInteraction(owner, false);
  assert.deepEqual(states, [true, false]);
  assert.deepEqual(lateStates, [false]);
  unsubscribe();
  unsubscribeLate();
});

test("multiple owners deduplicate and broadcast only first-start and last-end transitions", () => {
  const store = freshStore();
  const owners = [{}, {}, {}];
  const states = [];
  const unsubscribe = store.subscribeGraphInteraction((active) => states.push(active));
  for (const owner of owners) store.setGraphInteraction(owner, true);
  for (let frame = 0; frame < 1000; frame++)
    for (const owner of owners) store.setGraphInteraction(owner, true);
  assert.deepEqual(states, [true], "frame-level calls must not broadcast");
  store.setGraphInteraction(owners[1], false);
  store.setGraphInteraction(owners[1], false);
  store.setGraphInteraction({}, false);
  store.setGraphInteraction(owners[0], false);
  assert.equal(store.isGraphInteracting(), true);
  assert.deepEqual(states, [true]);
  store.setGraphInteraction(owners[2], false);
  assert.equal(store.isGraphInteracting(), false);
  assert.deepEqual(states, [true, false]);
  unsubscribe();
});

test("graph disposal releases only its own token and permits later reuse", () => {
  const store = freshStore();
  const first = {}, second = {};
  store.setGraphInteraction(first, true);
  store.setGraphInteraction(second, true);
  const disposeFirst = () => store.setGraphInteraction(first, false);
  disposeFirst();
  disposeFirst();
  assert.equal(store.isGraphInteracting(), true);
  store.setGraphInteraction(second, false);
  assert.equal(store.isGraphInteracting(), false);
  store.setGraphInteraction(first, true);
  assert.equal(store.isGraphInteracting(), true);
  disposeFirst();
  assert.equal(store.isGraphInteracting(), false);
});

test("subscriber disposal is idempotent and does not release another component's token", () => {
  const store = freshStore();
  const owner = {}, states = [];
  const unsubscribe = store.subscribeGraphInteraction((active) => states.push(active));
  store.setGraphInteraction(owner, true);
  unsubscribe();
  unsubscribe();
  assert.equal(store.isGraphInteracting(), true);
  store.setGraphInteraction(owner, false);
  store.setGraphInteraction(owner, true);
  store.setGraphInteraction(owner, false);
  assert.deepEqual(states, [true]);
});

test("separate subscriptions using the same callback have independent cleanup", () => {
  const store = freshStore();
  const owner = {}, states = [];
  const callback = (active) => states.push(active);
  const first = store.subscribeGraphInteraction(callback);
  const second = store.subscribeGraphInteraction(callback);
  first();
  store.setGraphInteraction(owner, true);
  assert.deepEqual(states, [true]);
  second();
  store.setGraphInteraction(owner, false);
  assert.deepEqual(states, [true]);
});

test("unsubscribing during delivery suppresses a queued callback", () => {
  const store = freshStore();
  const owner = {}, laterStates = [];
  let unsubscribeLater;
  store.subscribeGraphInteraction(() => unsubscribeLater());
  unsubscribeLater = store.subscribeGraphInteraction((active) => laterStates.push(active));
  store.setGraphInteraction(owner, true);
  store.setGraphInteraction(owner, false);
  assert.deepEqual(laterStates, []);
});

test("nested final release never delivers stale active state to remaining subscribers", () => {
  const store = freshStore();
  const owner = {}, firstStates = [], laterStates = [];
  store.subscribeGraphInteraction((active) => {
    firstStates.push(active);
    if (active) store.setGraphInteraction(owner, false);
  });
  store.subscribeGraphInteraction((active) => {
    assert.equal(active, store.isGraphInteracting());
    laterStates.push(active);
  });
  store.setGraphInteraction(owner, true);
  assert.equal(store.isGraphInteracting(), false);
  assert.deepEqual(firstStates, [true, false]);
  assert.deepEqual(laterStates, []);
});

test("anonymous owners accept frozen or null-prototype empty objects and reject payloads", () => {
  const store = freshStore();
  for (const token of [Object.freeze({}), Object.create(null)]) {
    store.setGraphInteraction(token, true);
    assert.equal(store.isGraphInteracting(), true);
    store.setGraphInteraction(token, false);
  }
  for (const invalid of [null, "owner", [], () => {}, new Date(), { node: {} }, { summary: "fixture" }, { [Symbol("metadata")]: true }]) {
    assert.throws(() => store.setGraphInteraction(invalid, true), /empty object token/);
    assert.equal(store.isGraphInteracting(), false);
  }
});

test("100 mount-interact-dispose cycles leave no active owner or notified old subscriber", () => {
  const store = freshStore();
  let calls = 0;
  for (let cycle = 0; cycle < 100; cycle++) {
    const owner = {};
    const unsubscribe = store.subscribeGraphInteraction(() => calls++);
    store.setGraphInteraction(owner, true);
    store.setGraphInteraction(owner, false);
    unsubscribe();
    assert.equal(store.isGraphInteracting(), false);
    assert.equal(calls, (cycle + 1) * 2);
  }
  const finalOwner = {};
  store.setGraphInteraction(finalOwner, true);
  store.setGraphInteraction(finalOwner, false);
  assert.equal(calls, 200);
});
