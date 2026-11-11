"use strict";

const { execFileSync } = require("node:child_process");
const path = require("node:path");

module.exports = () => {
  const root = path.resolve(__dirname, "../..");
  execFileSync(path.join(root, ".venv/bin/python"), ["tests/ui/make_fixture.py"], {
    cwd: root,
    stdio: "inherit",
  });
};
