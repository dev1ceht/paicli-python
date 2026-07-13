# Three-batch repeated-call stagnation

PaiCLI will treat three consecutive identical tool-and-input batches without an intervening successful write or changed result as stagnation. It permits one reasonable retry while bounding repeated reads and unchanged failing commands before guarded finalization.
