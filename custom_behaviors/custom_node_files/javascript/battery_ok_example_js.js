// Example custom BT Condition node (JavaScript) -- see greet_example.js's
// comment for why this ships in the repo. A real deployment would read
// this robot's actual battery telemetry (no such subsystem exists in
// this repo yet) -- this uses a fixed simulated reading so the example is
// deterministic and testable without any hardware at all.

// TODO(hardware): replace with a real battery-telemetry read.
const SIMULATED_BATTERY_PERCENT = 76.0;

class BatteryOkExample extends XparoNode {
  tick() {
    const minLevel = parseFloat(this.input("min_level", "20"));

    const ok = SIMULATED_BATTERY_PERCENT >= minLevel;
    // console.error, not console.log -- see greet_example_js.js's own comment.
    console.error(`[BatteryOkExample] battery=${SIMULATED_BATTERY_PERCENT}% min_level=${minLevel} -> ${ok ? "SUCCESS" : "FAILURE"}`);
    return ok ? this.SUCCESS : this.FAILURE;
  }

  halt() {
    // Never returns RUNNING -- nothing to cancel.
  }
}

module.exports = BatteryOkExample;
