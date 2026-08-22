// Example custom BT Action node (JavaScript) -- ships with the repo so
// the multi-language custom-node pipeline is testable with zero Django/
// dashboard interaction: a fresh `colcon build --symlink-install` alone
// registers this tag (see engine.py's sync_custom_node_files,
// examples_manifest.json). Genuinely working, not a TODO stub.
class GreetExample extends XparoNode {
  tick() {
    const name = this.input("name", "robot");

    const greeting = `Hello, ${name}! XPARO custom node pipeline is working.`;
    // console.error, not console.log -- the host process's stdout is the
    // JSON-lines tick protocol (js_host.js); console.log would corrupt it.
    console.error(`[GreetExample] ${greeting}`);

    this.output("greeting", greeting);
    return this.SUCCESS;
  }

  halt() {
    // Never returns RUNNING -- nothing to cancel.
  }
}

module.exports = GreetExample;
