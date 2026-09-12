import test from "node:test";
import assert from "node:assert/strict";
import { compareItems } from "../src/sort.mjs";

const item = (year) => ({ title: "Film", year });
const sortWith = (arr, sort, order) => [...arr].sort((a, b) => compareItems(a, b, sort, order));

test("nulls last ascending", () => {
  const out = sortWith([item(null), item(1962), item(1994)], "year", "asc");
  assert.deepEqual(out.map((e) => e.year), [1962, 1994, null]);
});

test("nulls last descending (regression)", () => {
  const out = sortWith([item(null), item(1962), item(1994)], "year", "desc");
  assert.deepEqual(out.map((e) => e.year), [1994, 1962, null]);
});

test("numbers order both directions", () => {
  assert.deepEqual(
    sortWith([item(2000), item(1990), item(2010)], "year", "asc").map((e) => e.year),
    [1990, 2000, 2010]
  );
  assert.deepEqual(
    sortWith([item(2000), item(1990), item(2010)], "year", "desc").map((e) => e.year),
    [2010, 2000, 1990]
  );
});

test("title sorts both directions", () => {
  const arr = [{ title: "b" }, { title: "a" }, { title: "c" }];
  assert.deepEqual(sortWith(arr, "title", "asc").map((e) => e.title), ["a", "b", "c"]);
  assert.deepEqual(sortWith(arr, "title", "desc").map((e) => e.title), ["c", "b", "a"]);
});

test("both null compare equal", () => {
  assert.equal(compareItems(item(null), item(null), "year", "desc"), 0);
});
